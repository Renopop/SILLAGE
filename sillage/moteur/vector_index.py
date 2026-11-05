# vector_index.py
"""
Index vectoriel FAISS - §5.1

Les vecteurs sont stockés dans un index FAISS, qui gère sans difficulté
plusieurs centaines de milliers à quelques millions de chunks et répond en une
fraction de seconde.

On utilise `IndexIDMap(IndexFlatIP)` (et non `IndexFlatIP` seul) pour :
  - le produit scalaire sur vecteurs normalisés = cosinus (§5.1) ;
  - pouvoir **supprimer** des vecteurs par identifiant via `remove_ids()`,
    indispensable à l'incrémental (§8.3).

Les identifiants stables proviennent du registre SQLite (§8).

Publication atomique (§5.1) : on écrit un fichier temporaire puis on le renomme
avec `os.replace` — opération atomique. Les lecteurs voient toujours soit
l'ancien fichier complet, soit le nouveau complet, jamais un état intermédiaire.
C'est ce mécanisme qui remplace le rôle d'un serveur.
"""

import os
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    logger.warning("[vector_index] faiss non installé — pip install faiss-cpu")


def _split_ids(ids: List[int], blocks) -> List[List[int]]:
    """Redécoupe la liste plate d'ids du tampon selon les tailles des blocs."""
    out, pos = [], 0
    for b in blocks:
        out.append(ids[pos:pos + len(b)])
        pos += len(b)
    return out


# --- Ensemble autorisé (filtre de portée) -------------------------------------
#
# `search(allowed_ids=…)` accepte trois formes (audit perf 2.1, L-1 / L-11) :
#   - un tableau numpy BOOLÉEN indexé PAR IDENTIFIANT (bitmap : bits[id]
#     vrai si autorisé, taille max_id + 1) ;
#   - un tableau numpy d'ENTIERS (identifiants autorisés, de préférence
#     triés) — ce que produit la portée précalculée (`registry.Portee.ids`) ;
#   - un `set` ou tout itérable d'entiers (API historique, tests, base de
#     recherche) — converti une fois à l'entrée.
# Au-delà de _BITMAP_RATIO de l'index (portée large), le sélecteur natif est
# un `IDSelectorBitmap` (test d'appartenance en O(1) par vecteur : 33 à
# 54 ms contre 66 à 86 pour `IDSelectorBatch`, dont la table de hachage
# devenait plus lente que la recherche sans filtre — régression C-7 relevée
# par l'audit) ; en deçà, `IDSelectorBatch` (0,8 ms sur 500 ids, le bitmap
# coûterait sa construction sur tout l'espace d'identifiants).
_BITMAP_RATIO = 0.1


def _portee_normalisee(allowed_ids):
    """(ids: int64 ou None, bitmap: bool ou None, nombre d'autorisés)."""
    if isinstance(allowed_ids, np.ndarray):
        if allowed_ids.dtype == np.bool_:
            return None, allowed_ids, int(np.count_nonzero(allowed_ids))
        arr = np.ascontiguousarray(allowed_ids.ravel(), dtype="int64")
        return arr, None, int(arr.size)
    try:
        n = len(allowed_ids)
    except TypeError:
        allowed_ids = list(allowed_ids)
        n = len(allowed_ids)
    arr = np.ascontiguousarray(np.fromiter(allowed_ids, dtype="int64", count=n))
    return arr, None, n


def _masque_membres(ids0: np.ndarray, arr, bits) -> np.ndarray:
    """Masque des positions de `ids0` dont l'identifiant est autorisé (repli
    numpy ; -1 = case vide de FAISS, jamais autorisée)."""
    if bits is not None:
        ok = (ids0 >= 0) & (ids0 < bits.shape[0])
        m = np.zeros(ids0.shape[0], dtype=bool)
        m[ok] = bits[ids0[ok]]
        return m
    return (ids0 != -1) & np.isin(ids0, arr)


# Vecteurs accumulés avant l'entraînement du quantizer 8 bits : ses plages
# par dimension sont calibrées sur ce premier échantillon (les vecteurs hors
# plage ultérieurs sont bornés — validé : auto-appariement intact).
#
# Sous ce seuil, l'échantillon est trop pauvre (audit V2, C-6 : recall@10
# mesuré 0,87 sur 50 vecteurs contre 0,98 sur 4096). Règles :
#   - `search` n'entraîne JAMAIS : tant que le quantizer n'est pas entraîné,
#     la recherche est un scan exact (float32) du tampon ;
#   - `save` doit publier un index lisible et interrogeable par N'IMPORTE QUEL
#     lecteur : un fichier annexe (tampon à part) serait ignoré du miroir local
#     et de la rétention du magasin (store.py ne connaît que `corpus.*.faiss`),
#     donc perdu ou collecté à contretemps. Sous le seuil, `save` entraîne
#     quand même — en l'annonçant — et `load` RÉ-ARME le tampon d'un index
#     8 bits de moins de _QT_TRAIN_N vecteurs (vecteurs reconstruits) : la
#     calibration provisoire d'un point de reprise précoce est refaite, sur un
#     échantillon complet, dès que le seuil est atteint. Le fichier publié
#     reste un index FAISS standard.
_QT_TRAIN_N = 4096


def _is_8bit_quantizer(inner) -> bool:
    """Vrai si l'index de base est un IndexScalarQuantizer 8 bits."""
    try:
        dc = faiss.downcast_index(inner)
        sq = getattr(dc, "sq", None)
        return sq is not None and int(sq.qtype) == int(faiss.ScalarQuantizer.QT_8bit)
    except Exception:
        return False


# --- Lecture côté LECTEUR (audit perf 2.1, L-4 / L-5) -------------------------
#
# L'indexeur garde l'index PLAT (`load`) : il ajoute et retire des vecteurs.
# Le lecteur (index_cache) passe par `load_lecteur`, qui optimise la lecture :
#
#   L-5, mmap : quand le fichier vient du MIROIR LOCAL, il est lu avec
#     `IO_FLAG_MMAP_IFC` — les vecteurs restent de la mémoire DE FICHIER
#     (reclaimable par le système, partagée entre lecteurs du poste), le
#     chargement est quasi instantané (0,5 ms au lieu de 300 par 400 Mo) et
#     la recherche identique. Jamais sur un chemin réseau direct (un mmap sur
#     SMB se paie à chaque page touchée).
#
#   L-4, sous-index : une recherche FAISS sans filtre n'occupe qu'un cœur
#     (1,5 s par question à 5 M × 1024). L'index est découpé en
#     min(cpu_count, 8) sous-index dans un `IndexShards(threaded=True)` qui
#     parallélise une requête unique : 22 ms au lieu de 64 sur 200 000 × 1024
#     fp16 (4 cœurs), mêmes résultats.
#
#   Les deux SE COMBINENT sans copie : les sous-index sont des VUES
#     (`MaybeOwnedVector.create_view`, faiss ≥ 1.11) sur les codes de l'index
#     chargé — mesuré : découpage 1,6 ms, +0 Mo de mémoire anonyme, l'index
#     mmap reste mmap. Sur un faiss plus ancien, les codes sont RECOPIÉS
#     (+1 fois la taille de l'index, ~250 ms par 400 Mo) ; sur un index d'un
#     type inattendu, l'index plat est conservé.
#
#   Filtre de portée : `IndexShards` + sélecteur d'identifiants donne des
#     résultats FAUX (sélecteur appliqué aux mauvais ids) voire un segfault
#     (faiss 1.15.1) — vérifié. La recherche filtrée interroge donc chaque
#     sous-index AVEC le sélecteur dans un pool de threads (faiss relâche le
#     GIL) et fusionne par score : 8 ms au lieu de 28. Repli numpy inchangé.
#
# Réglages : RIVAGE_FAISS_SHARDS (nombre de sous-index ; 0 = index plat ;
# vide = min(cpu_count, 8), seulement au-delà de _SHARDS_MIN_ND composantes —
# vecteurs × dimensions, ~20 000 × 1024 : en dessous, une recherche dure
# moins d'une milliseconde et le lancement des threads coûterait autant),
# RIVAGE_FAISS_MMAP (1 par défaut, 0 = lecture classique en mémoire).
_SHARDS_MIN_ND = 20_000_000


def _shards_voulus() -> Optional[int]:
    """Nombre de sous-index demandé (RIVAGE_FAISS_SHARDS) ; None = automatique."""
    v = (os.environ.get("RIVAGE_FAISS_SHARDS") or "").strip()
    if not v:
        return None
    try:
        return max(0, int(v))
    except ValueError:
        logger.warning(f"[vector_index] RIVAGE_FAISS_SHARDS={v!r} non entier — automatique.")
        return None


def _shards_auto() -> int:
    return max(1, min(os.cpu_count() or 1, 8))


_SIMD_ABAISSE = False


def _abaisser_niveau_simd() -> Optional[str]:
    """Seconde couche de la sonde de plateforme (V2.7, lot 2) : si
    FAISS_SIMD_LEVEL demande un niveau INFÉRIEUR à celui que faiss a
    choisi (variable posée après l'import, ou faiss qui en changerait le
    nom), l'abaisser par `SIMDConfig.set_level`. Jamais de montée (le
    niveau choisi par faiss est au plus celui que le poste annonce). Une
    fois par processus ; sans effet sur un faiss sans SIMDConfig. Retourne
    le niveau appliqué ou None."""
    global _SIMD_ABAISSE
    if _SIMD_ABAISSE or not FAISS_AVAILABLE:
        return None
    _SIMD_ABAISSE = True
    voulu = (os.environ.get("FAISS_SIMD_LEVEL") or "").strip().upper()
    cfg = getattr(faiss, "SIMDConfig", None)
    if not voulu or cfg is None:
        return None
    try:
        from plateforme import ORDRE_SIMD
        courant = str(cfg.get_level_name()).upper()
        if voulu not in ORDRE_SIMD or courant not in ORDRE_SIMD:
            return None
        if ORDRE_SIMD[voulu] >= ORDRE_SIMD[courant]:
            return None
        niveau = getattr(faiss, f"SIMDLevel_{voulu}", None)
        if niveau is None:
            return None
        cfg.set_level(niveau)
        logger.info(f"[vector_index] niveau SIMD abaissé de {courant} à {voulu} "
                    f"(FAISS_SIMD_LEVEL).")
        return voulu
    except Exception as e:
        logger.info(f"[vector_index] niveau SIMD non abaissé ({e}).")
        return None


def _mmap_voulu() -> bool:
    return (os.environ.get("RIVAGE_FAISS_MMAP", "1").strip() != "0")


def _sous_index_vide(dc):
    """Sous-index VIDE du même type et des mêmes paramètres que `dc`
    (IndexScalarQuantizer ou IndexFlat), prêt à recevoir des codes.

    Le quantizer entraîné est recopié par AFFECTATION de la structure
    (`sub.sq = dc.sq`, copie C++ complète, plages comprises) et non via
    `vector_to_array(dc.sq.trained)` : dès que PyMuPDF est importé dans le
    même processus (indexeur, lecteur avec visionneuse), ses propres proxys
    SWIG de `std::vector<float>` remplacent ceux de faiss et
    `vector_to_array` échoue (audit perf 2.1, lot 9 — constaté sur les
    deltas de points de reprise, qui s'écrivent dans l'indexeur)."""
    if isinstance(dc, faiss.IndexScalarQuantizer):
        sub = faiss.IndexScalarQuantizer(dc.d, int(dc.sq.qtype), dc.metric_type)
        sub.sq = dc.sq
        sub.is_trained = True
        return sub
    if isinstance(dc, faiss.IndexFlat):
        return faiss.IndexFlat(dc.d, dc.metric_type)
    raise TypeError(f"type d'index non découpable : {type(dc).__name__}")


def _quant_mode() -> int:
    """Bits par composante des NOUVEAUX index (RIVAGE_FAISS_QUANT).

    IndexFlatIP en float32 plein (32) : 4 Ko/chunk en 1024 dims — l'index
    finissait par dépasser la mémoire de la machine (std::bad_alloc dans
    add_with_ids, constaté en production). Le quantizer scalaire divise
    mémoire ET fichier :
        16 (défaut) : ÷2 — sans entraînement, rappel quasi identique ;
        8           : ÷4 — option pour très grands corpus ; nécessite un
                      ENTRAÎNEMENT sur les premiers vecteurs (géré par un
                      tampon interne, transparent) ;
        32          : float32 plein historique.
    Même API (add/remove/search). Ne s'applique qu'aux index CRÉÉS après
    coup : un index existant garde son format au chargement. (Compat :
    l'ancienne RIVAGE_FAISS_FP16 0/1 vaut 32/16 si RIVAGE_FAISS_QUANT n'est
    pas posée.)
    """
    v = (os.environ.get("RIVAGE_FAISS_QUANT") or "").strip()
    if v in ("8", "16", "32"):
        return int(v)
    legacy = (os.environ.get("RIVAGE_FAISS_FP16") or "").strip()
    if legacy == "0":
        return 32
    return 16


class VectorIndex:
    """
    Enveloppe autour d'un IndexIDMap FAISS (fp16 par défaut — voir
    _quant_mode). En mode 8 bits (opt-in), les premiers vecteurs sont TAMPONNÉS
    jusqu'à l'entraînement du quantizer (_QT_TRAIN_N), puis versés d'un coup ;
    search/save vident le tampon d'eux-mêmes — invisible pour l'appelant.
    """

    def __init__(self, dim: int):
        if not FAISS_AVAILABLE:
            raise RuntimeError("faiss non installé. Installez faiss-cpu.")
        self.dim = dim
        mode = _quant_mode()
        if mode == 8:
            base = faiss.IndexScalarQuantizer(
                dim, faiss.ScalarQuantizer.QT_8bit, faiss.METRIC_INNER_PRODUCT)
            logger.info(f"[vector_index] Nouvel index 8 bits ({dim} dims — "
                        f"mémoire/fichier divisés par 4 vs float32).")
        elif mode == 16:
            base = faiss.IndexScalarQuantizer(
                dim, faiss.ScalarQuantizer.QT_fp16, faiss.METRIC_INNER_PRODUCT)
            logger.info(f"[vector_index] Nouvel index fp16 ({dim} dims — "
                        f"mémoire/fichier divisés par 2 vs float32).")
        else:
            base = faiss.IndexFlatIP(dim)
        self.index = faiss.IndexIDMap(base)
        # Tampon d'entraînement (mode 8 bits uniquement) : [vecs...], [ids...]
        self._pending = ([], []) if not base.is_trained else None
        self._init_lecture()

    def _init_lecture(self) -> None:
        """État de lecture optimisée (L-4 / L-5) : index plat par défaut."""
        self._shards: Optional[list] = None      # sous-index (IndexIDMap) ou None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._pool_lock = threading.Lock()
        self._monolithe = None                   # index plat gardé vivant (vues)
        self.lecture = {"mmap": False, "shards": 0}
        # Plus grand identifiant de l'index (taille du bitmap de portée) :
        # None = à calculer (une lecture de l'id_map, mémorisée) ; -1 =
        # indéterminable (le bitmap n'est alors jamais employé).
        self._id_max: Optional[int] = None

    # --- Tampon d'entraînement (quantizer 8 bits) -----------------------------

    def _flush_pending(self) -> None:
        """Entraîne le quantizer sur le tampon puis y verse les vecteurs."""
        if self._pending is None:
            return
        bufs, ids = self._pending
        self._pending = None
        if not bufs:
            return                        # index vide : entraînement différé
        x = np.vstack(bufs)
        self.index.index.train(x)
        self.index.add_with_ids(x, np.asarray(ids, dtype="int64"))

    def _pending_count(self) -> int:
        return len(self._pending[1]) if self._pending is not None else 0

    def _scan_pending(self, q: np.ndarray, k: int,
                      allowed_ids) -> List[Tuple[int, float]]:
        """Recherche EXACTE (float32) dans le tampon non entraîné (C-6) : on
        n'entraîne pas le quantizer sur un échantillon trop petit pour
        répondre à une question. Même contrat que `search`."""
        bufs, ids = self._pending
        x = np.vstack(bufs)
        id_arr = np.asarray(ids, dtype="int64")
        scores = x @ q.reshape(-1)
        if allowed_ids is not None:
            arr, bits, _n = _portee_normalisee(allowed_ids)
            mask = _masque_membres(id_arr, arr, bits)
            id_arr, scores = id_arr[mask], scores[mask]
        if id_arr.size == 0:
            return []
        kk = min(k, id_arr.size)
        top = np.argpartition(-scores, kk - 1)[:kk]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [(int(id_arr[i]), float(scores[i])) for i in top]

    # --- Construction / mise à jour ------------------------------------------

    def add(self, vecs: np.ndarray, ids: List[int]) -> None:
        """Ajoute des vecteurs avec leurs identifiants stables (§5.1)."""
        if len(vecs) == 0:
            return
        vecs = np.ascontiguousarray(vecs, dtype="float32")
        id_arr = np.asarray(ids, dtype="int64")
        if vecs.shape[0] != id_arr.shape[0]:
            raise ValueError("Nombre de vecteurs != nombre d'identifiants")
        if vecs.shape[1] != self.dim:
            raise ValueError(f"Dimension {vecs.shape[1]} != index {self.dim}")
        self._id_max = None                      # à recalculer (bitmap de portée)
        if self._pending is not None:
            self._pending[0].append(vecs)
            self._pending[1].extend(int(i) for i in id_arr)
            if sum(len(b) for b in self._pending[0]) >= _QT_TRAIN_N:
                self._flush_pending()
            return
        self.index.add_with_ids(vecs, id_arr)

    def remove(self, ids: List[int]) -> int:
        """Retire des vecteurs par identifiant (fichier modifié/supprimé, §8.3)."""
        if not ids:
            return 0
        removed = 0
        if self._pending is not None and self._pending[1]:
            # Vecteurs encore au tampon (pas dans l'index natif) : filtrage.
            drop = set(int(i) for i in ids)
            bufs, bids = self._pending
            keep_v, keep_i = [], []
            for block, block_ids in zip(bufs, _split_ids(bids, bufs)):
                mask = np.asarray([i not in drop for i in block_ids])
                removed += int((~mask).sum())
                if mask.any():
                    keep_v.append(block[mask])
                    keep_i.extend(i for i, m in zip(block_ids, mask) if m)
            self._pending = (keep_v, keep_i)
        id_arr = np.asarray(ids, dtype="int64")
        removed += int(self.index.remove_ids(id_arr))
        self._id_max = None
        return removed

    # --- Recherche ------------------------------------------------------------

    def _id_maximal(self) -> Optional[int]:
        """Plus grand identifiant présent dans l'index (lu une fois dans
        l'id_map, mémorisé), ou None s'il est indéterminable."""
        if self._id_max is None:
            try:
                idmaps = ([sh.id_map for sh in self._shards] if self._shards
                          else [self.index.id_map])
                maxis = [int(faiss.vector_to_array(m).max()) for m in idmaps
                         if int(m.size()) > 0]
                self._id_max = max(maxis) if maxis else -1
            except Exception:
                self._id_max = -1
        return self._id_max if self._id_max >= 0 else None

    def _selecteur(self, arr, bits, n_allowed: int, ntotal: int):
        """Sélecteur d'identifiants natif pour l'ensemble autorisé :
        `IDSelectorBitmap` (portée large) ou `IDSelectorBatch` (portée
        étroite) — voir _BITMAP_RATIO. Retourne (sélecteur, objet à garder
        vivant pendant la recherche)."""
        if n_allowed > ntotal * _BITMAP_RATIO:
            id_max = self._id_maximal()
            if id_max is not None:
                taille = id_max + 1
                if bits is None:
                    bitmap = np.zeros(taille, dtype=bool)
                    dedans = arr[(arr >= 0) & (arr < taille)]
                    bitmap[dedans] = True
                elif bits.shape[0] < taille:
                    bitmap = np.concatenate(
                        [bits, np.zeros(taille - bits.shape[0], dtype=bool)])
                else:
                    bitmap = bits
                # Les ids au-delà du bitmap sont « non membres » (contrôle de
                # borne du sélecteur, vérifié) : un bitmap plus court que
                # l'espace d'identifiants reste correct.
                paquet = np.packbits(bitmap, bitorder="little")
                return faiss.IDSelectorBitmap(paquet), paquet
        if arr is None:
            arr = np.flatnonzero(bits).astype("int64")
        return faiss.IDSelectorBatch(arr), arr

    def search(self, query: np.ndarray, k: int,
               allowed_ids=None) -> List[Tuple[int, float]]:
        """Recherche les k plus proches voisins — dans la base ET, pendant
        un run à points de reprise, dans l'index SÉPARÉ des deltas (V2.7,
        lot 6 : la base reste en mmap, les deltas, petits, en mémoire vive ;
        avant, la base était rechargée entière en mémoire pour y fusionner
        les deltas, impossible sur un poste à 8 Go avec une grosse base).
        Fusion par score décroissant, même format que `_search_base`."""
        res = self._search_base(query, k, allowed_ids)
        extra = getattr(self, "_extra", None)
        if extra is None or extra.size == 0:
            return res
        res2 = extra._search_base(query, k, allowed_ids)
        if not res2:
            return res
        return sorted(res + res2, key=lambda t: (-t[1], t[0]))[:k]

    def _search_base(self, query: np.ndarray, k: int,
                     allowed_ids=None) -> List[Tuple[int, float]]:
        """
        Recherche les k plus proches voisins (cet index seul).

        `allowed_ids` (filtre de portée) : tableau numpy d'identifiants,
        bitmap booléen indexé par identifiant, ou `set`/itérable (voir
        _portee_normalisee). Voie native par sélecteur FAISS (bitmap sur une
        portée large, lot d'identifiants sinon) ; en son absence, scan
        exhaustif (l'index plat calcule de toute façon toutes les distances)
        filtré et sélectionné **en numpy** — sans matérialiser des millions
        de tuples Python (P1). Sans filtre, on ne demande que k voisins
        (chemin rapide habituel).

        Retourne une liste de (faiss_id, score) triée par score décroissant.
        """
        q = np.ascontiguousarray(query.reshape(1, -1), dtype="float32")
        # Contrôle EXPLICITE (audit V2, C-4) : FAISS lève une AssertionError
        # sans message sur une dimension incohérente, que le lecteur avalait en
        # « repli BM25 seul » avec un avertissement vide — le vectoriel était
        # mort sans que personne ne le voie.
        if q.shape[1] != self.dim:
            raise ValueError(
                f"Dimension de la requête ({q.shape[1]}) différente de celle de "
                f"l'index ({self.dim}) : le modèle d'embedding a-t-il changé "
                f"depuis l'indexation ? L'index doit alors être reconstruit.")
        # 8 bits non entraîné (audit V2, C-6) : scan exact du tampon, SANS
        # entraîner le quantizer sur un échantillon trop petit. Tant que le
        # quantizer n'est pas entraîné, l'index natif est vide : le tampon est
        # la totalité des vecteurs.
        if self._pending is not None:
            if not self._pending[1]:
                return []
            return self._scan_pending(q, k, allowed_ids)
        ntotal = self.index.ntotal
        if ntotal == 0:
            return []

        if allowed_ids is None:
            kk = min(k, ntotal)
            scores, ids = self.index.search(q, kk)
            return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i != -1]

        arr, bits, n_allowed = _portee_normalisee(allowed_ids)
        if n_allowed == 0:
            return []

        # Filtre de portée, voie NATIVE (audit V2, C-7 ; audit perf 2.1,
        # L-1) : sélecteur d'ids FAISS appliqué aux identifiants EXTERNES de
        # l'IndexIDMap — `IDSelectorBatch` mesuré 7 fois plus rapide que le
        # scan complet suivi d'un `np.isin` (118 ms -> 16 ms sur 200 000 ×
        # 1024), `IDSelectorBitmap` sur une portée large (voir
        # _BITMAP_RATIO). Disponible à partir de faiss 1.7.3 ; en son absence
        # (ou si l'index ne le gère pas), repli sur le chemin historique.
        kk = min(k, ntotal)
        try:
            sel, _vivant = self._selecteur(arr, bits, n_allowed, ntotal)
            if self._shards:
                scores, ids = self._search_shards_filtre(q, kk, sel)
            else:
                params = faiss.SearchParameters(sel=sel)
                scores, ids = self.index.search(q, kk, params=params)
        except (AttributeError, TypeError, RuntimeError):
            scores = ids = None
        if ids is not None:
            return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i != -1]

        # Repli : scan complet, sélection top-k vectorisée (P1).
        scores, ids = self.index.search(q, ntotal)
        ids0, sc0 = ids[0], scores[0]
        mask = _masque_membres(ids0, arr, bits)
        f_ids = ids0[mask][:k]
        f_sc = sc0[mask][:k]
        return [(int(i), float(s)) for i, s in zip(f_ids, f_sc)]

    def _search_shards_filtre(self, q: np.ndarray, kk: int, sel):
        """Recherche filtrée sur les sous-index EN PARALLÈLE (audit perf 2.1,
        L-4) : chaque sous-index (IndexIDMap) applique lui-même le sélecteur
        d'identifiants externes — exact, contrairement à `IndexShards.search`
        avec paramètres — dans un pool de threads (faiss relâche le GIL), puis
        fusion par score décroissant (stable). Même format de retour que
        `index.search` : (scores[1, kk], ids[1, kk], -1 pour les manquants).
        Le sélecteur (lecture seule) est partagé ; les `SearchParameters`
        sont créés PAR THREAD — un même objet partagé entre threads fait
        planter faiss (vérifié)."""
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(
                        max_workers=len(self._shards),
                        thread_name_prefix="rivage-faiss")
        parts = list(self._pool.map(
            lambda sh: sh.search(q, min(kk, int(sh.ntotal)),
                                 params=faiss.SearchParameters(sel=sel)),
            self._shards))
        sc = np.concatenate([p[0][0] for p in parts])
        ids = np.concatenate([p[1][0] for p in parts])
        keep = ids != -1
        sc, ids = sc[keep], ids[keep]
        order = np.argsort(-sc, kind="stable")[:kk]
        out_sc = np.full((1, kk), -np.inf, dtype="float32")
        out_ids = np.full((1, kk), -1, dtype="int64")
        out_sc[0, :order.size] = sc[order]
        out_ids[0, :order.size] = ids[order]
        return out_sc, out_ids

    @property
    def size(self) -> int:
        pending = len(self._pending[1]) if self._pending is not None else 0
        extra = getattr(self, "_extra", None)
        return int(self.index.ntotal) + pending + (extra.size if extra is not None else 0)

    # --- Points de reprise en delta (audit perf 2.1, I-4) --------------------
    #
    # Un point de reprise réécrivait le FAISS CUMULÉ (des gigaoctets sur le
    # partage à chaque point, ~12 To pour un run de 200 h à petite cadence).
    # L'indexeur n'écrit plus que les vecteurs ajoutés depuis le dernier point
    # (`extraire_delta`), dans un index du même type ; le lecteur — et
    # l'indexeur à la reprise — rechargent la base puis y FUSIONNENT chaque
    # delta (`load_avec_deltas` / `fusionner`), codes copiés tels quels (aucune
    # requantification). Les suppressions ne sont pas portées par les deltas :
    # elles sont dans le registre (un fragment retiré n'y figure plus) ; à la
    # reprise, l'indexeur retire de l'index les identifiants absents du
    # registre (`retirer_absents`), et la publication finale fusionne tout en
    # un seul FAISS exact.

    def ids(self) -> np.ndarray:
        """Tous les identifiants présents (index natif puis tampon), int64."""
        parts = [faiss.vector_to_array(self.index.id_map).astype("int64")]
        if self._pending is not None and self._pending[1]:
            parts.append(np.asarray(self._pending[1], dtype="int64"))
        extra = getattr(self, "_extra", None)
        if extra is not None and extra.size:
            parts.append(extra.ids())
        return np.concatenate(parts) if len(parts) > 1 else parts[0]

    def extraire_delta(self, lo_id: int) -> Optional["VectorIndex"]:
        """Nouvel index (même type, mêmes paramètres de quantification) ne
        contenant que les vecteurs d'identifiant >= `lo_id` — ceux ajoutés
        depuis le dernier point de reprise (identifiants monotones, §8).
        Codes copiés sans décodage : le delta est bit à bit identique à la
        portion correspondante de l'index. None si l'index n'est pas
        découpable (quantizer 8 bits pas encore entraîné, type inattendu) :
        l'appelant réécrit alors le FAISS complet."""
        if self._pending is not None or self._shards:
            return None
        try:
            index = self.index
            inner = faiss.downcast_index(index.index)
            n, cs = int(inner.ntotal), int(inner.code_size)
            ids = faiss.vector_to_array(index.id_map).astype("int64")
            if ids.shape[0] != n:
                raise ValueError("id_map et codes de tailles différentes")
            mask = ids >= int(lo_id)
            k = int(mask.sum())
            sub = _sous_index_vide(inner)
            m = faiss.IndexIDMap(sub)          # AVANT le remplissage (index vide exigé)
            if k:
                codes = faiss.rev_swig_ptr(inner.codes.data(), n * cs)   # vue numpy
                sel = np.ascontiguousarray(codes.reshape(n, cs)[mask]).reshape(-1)
                faiss.copy_array_to_vector(sel, sub.codes)
                sub.ntotal = k
                faiss.copy_array_to_vector(np.ascontiguousarray(ids[mask]), m.id_map)
                m.ntotal = k
            return self._depuis_index(m)
        except Exception as e:
            logger.info(f"[vector_index] Delta non extractible "
                        f"({type(e).__name__}: {e}) : réécriture complète de "
                        f"l'index.")
            return None

    def _vecteurs(self) -> Tuple[np.ndarray, np.ndarray]:
        """(vecteurs float32, identifiants) de tout l'index — reconstruits
        depuis les codes (exact en fp16/float32, au pas de quantification
        près en 8 bits) ; sert de repli à la fusion."""
        if self._pending is not None:
            bufs, ids = self._pending
            if not bufs:
                return (np.zeros((0, self.dim), dtype="float32"),
                        np.zeros(0, dtype="int64"))
            return np.vstack(bufs), np.asarray(ids, dtype="int64")
        inner = faiss.downcast_index(self.index.index)
        n = int(inner.ntotal)
        vecs = np.ascontiguousarray(inner.reconstruct_n(0, n), dtype="float32")
        return vecs, faiss.vector_to_array(self.index.id_map).astype("int64")

    def fusionner(self, autre: "VectorIndex") -> None:
        """Ajoute à cet index tous les vecteurs d'`autre` (un delta). Fusion
        NATIVE des codes (`merge_from`, exacte) quand les deux index sont du
        même type et entraînés ; sinon reconstruction puis `add`. Jamais sur
        un index mmap (la fusion y est impossible) : les chargements avec
        deltas se font en mémoire."""
        if autre.size == 0:
            return
        if (self._pending is None and autre._pending is None
                and not self._shards and not self.lecture.get("mmap")):
            try:
                self.index.merge_from(autre.index, 0)
                return
            except Exception as e:                  # types incompatibles…
                logger.debug(f"[vector_index] merge_from impossible ({e}) : "
                             f"fusion par reconstruction.")
        vecs, ids = autre._vecteurs()
        self.add(vecs, [int(i) for i in ids])

    def retirer_absents(self, ids_vivants) -> int:
        """Retire les vecteurs dont l'identifiant n'est pas dans
        `ids_vivants` (le registre fait foi : fichiers supprimés ou modifiés
        entre le FAISS de base et un point de reprise en delta, jamais
        persistés dans les deltas). Retourne le nombre retiré."""
        presents = self.ids()
        if presents.size == 0:
            return 0
        vivants = np.asarray(list(ids_vivants), dtype="int64")
        morts = np.setdiff1d(presents, vivants)
        if morts.size:
            self.remove([int(i) for i in morts])
        return int(morts.size)

    @classmethod
    def load_avec_deltas(cls, path: str, deltas) -> "VectorIndex":
        """Charge le FAISS de base PUIS y fusionne chaque delta, dans l'ordre
        (indexeur à la reprise après un point de reprise en delta, I-4).
        Index plat, en mémoire."""
        obj = cls.load(path)
        for d in deltas or []:
            obj.fusionner(cls.load(d))
        return obj

    # --- Persistance atomique (§5.1) -----------------------------------------

    def save(self, path: str) -> None:
        """
        Écrit l'index sur disque de façon **atomique** (§5.1) :
        fichier .tmp puis os.replace. Aucun lecteur ne voit un fichier partiel.
        """
        # 8 bits : entraîner/verser avant d'écrire. Sous le seuil (audit V2,
        # C-6), on entraîne QUAND MÊME — un index publié doit rester lisible
        # par tout lecteur, sans fichier annexe — mais on le dit : la
        # calibration est provisoire, `load` ré-armera le tampon et
        # l'entraînement sera refait dès _QT_TRAIN_N vecteurs (voir en tête).
        n_pending = self._pending_count()
        if 0 < n_pending < _QT_TRAIN_N:
            logger.warning(
                f"[vector_index] Quantizer 8 bits entraîné sur {n_pending} "
                f"vecteurs seulement (< {_QT_TRAIN_N} : point de reprise précoce "
                f"ou petit corpus) — calibration provisoire, refaite "
                f"automatiquement dès {_QT_TRAIN_N} vecteurs.")
        self._flush_pending()
        tmp = path + ".tmp"
        faiss.write_index(self.index, tmp)
        # atomic_replace (audit v3) : retries face aux sharing violations SMB
        # (un lecteur tenant l'ancien fichier ouvert), comme le manifest.
        from store import atomic_replace
        atomic_replace(tmp, path)
        logger.info(f"[vector_index] Index publié ({self.size} vecteurs) -> {path}")

    @classmethod
    def load(cls, path: str) -> "VectorIndex":
        """Charge un index publié, PLAT et en mémoire (indexeur : il doit
        pouvoir y ajouter et en retirer des vecteurs ; lecteur : voir
        `load_lecteur`)."""
        if not FAISS_AVAILABLE:
            raise RuntimeError("faiss non installé. Installez faiss-cpu.")
        return cls._depuis_index(faiss.read_index(path))

    @classmethod
    def load_lecteur(cls, path: str, local: bool = False,
                     mmap: Optional[bool] = None,
                     shards: Optional[int] = None,
                     deltas=None) -> "VectorIndex":
        """Charge un index publié pour le LECTEUR (audit perf 2.1, L-4 / L-5 ;
        appelé par index_cache uniquement) : lecture mmap si le fichier est
        une copie LOCALE (`local`, miroir du poste) et que RIVAGE_FAISS_MMAP
        l'autorise, puis découpage en sous-index pour paralléliser chaque
        recherche (RIVAGE_FAISS_SHARDS). `mmap` / `shards` explicites priment
        sur l'environnement (tests). L'objet rendu est en LECTURE SEULE.

        `deltas` (audit perf 2.1, I-4) : chemins des deltas de points de
        reprise à fusionner par-dessus la base (`store.reader_paths(...)
        ["faiss_deltas"]`). Avec des deltas, la base est lue EN MÉMOIRE (pas
        de mmap : on ne peut pas y ajouter) — seulement pendant un run à
        points de reprise, jusqu'à la fusion de la publication finale."""
        if not FAISS_AVAILABLE:
            raise RuntimeError("faiss non installé. Installez faiss-cpu.")
        _abaisser_niveau_simd()               # seconde couche de la sonde (lot 2)
        deltas = list(deltas or [])
        if mmap is None:
            mmap = bool(local) and _mmap_voulu()
        # (lot 6) Les deltas ne forcent plus le chargement de la base en
        # mémoire : ils vont dans un index séparé (voir plus bas).
        obj = None
        if mmap and hasattr(faiss, "IO_FLAG_MMAP_IFC"):
            try:
                obj = cls._depuis_index(faiss.read_index(path, faiss.IO_FLAG_MMAP_IFC))
                obj.lecture["mmap"] = True
            except Exception as e:              # faiss sans mmap sur ce type : repli
                logger.warning(f"[vector_index] Lecture mmap impossible ({e}) — "
                               f"chargement classique en mémoire.")
                obj = None
        if obj is None:
            obj = cls.load(path)
        if deltas:
            # Deltas SÉPARÉS (V2.7, lot 6) : fusionnés entre eux en mémoire
            # vive (petits : les vecteurs ajoutés depuis le dernier FAISS
            # complet), jamais dans la base — qui reste en mmap. `search`,
            # `size` et `ids` couvrent les deux. Résultats identiques à la
            # fusion complète (même métrique, même quantification).
            extra = cls.load(deltas[0])
            for d in deltas[1:]:
                extra.fusionner(cls.load(d))
            obj._extra = extra
            obj.lecture["deltas"] = len(deltas)
            obj.lecture["deltas_separes"] = True
        if shards is None:
            shards = _shards_voulus()
            if shards is None:                  # automatique : au-delà d'un seuil
                shards = _shards_auto() if obj.size * obj.dim >= _SHARDS_MIN_ND else 0
        if shards and shards > 1:
            obj._fragmenter(int(shards))
        try:
            from diagnostic import niveau_simd_faiss, version_faiss
            simd = niveau_simd_faiss()
            logger.info(f"[vector_index] faiss {version_faiss() or '?'} — niveau SIMD "
                        f"{simd.get('niveau') or '?'}"
                        f"{' (aiguillage dynamique)' if simd.get('dynamique') else ''}, "
                        f"lecture {'mmap' if obj.lecture.get('mmap') else 'mémoire'}, "
                        f"{obj.lecture.get('shards') or 0} sous-index.")
        except Exception:
            pass
        return obj

    def _fragmenter(self, n_shards: int) -> None:
        """Découpe l'index plat en `n_shards` sous-index dans un IndexShards
        threadé (L-4) — par VUES sur les codes de l'index chargé (aucune
        copie, l'index mmap reste mmap) ou, à défaut, par copie des codes.
        Tout échec laisse l'index plat intact."""
        if self._pending is not None or self._shards or int(self.index.ntotal) == 0:
            return
        index = self.index
        try:
            inner = faiss.downcast_index(index.index)
            n, cs = int(inner.ntotal), int(inner.code_size)
            n_shards = max(1, min(n_shards, n))
            if n_shards < 2:
                return
            ids = faiss.vector_to_array(index.id_map)
            if ids.shape[0] != n:
                raise ValueError("id_map et codes de tailles différentes")
            codes = faiss.rev_swig_ptr(inner.codes.data(), n * cs)   # vue numpy
            vue_possible = hasattr(type(inner.codes), "create_view")
            bornes = np.linspace(0, n, n_shards + 1).astype(np.int64)
            shards = faiss.IndexShards(int(index.d), True, False)
            sous_index = []
            for a, b in zip(bornes[:-1], bornes[1:]):
                a, b = int(a), int(b)
                if b <= a:
                    continue
                sub = _sous_index_vide(inner)
                m = faiss.IndexIDMap(sub)          # AVANT le remplissage (index vide exigé)
                if vue_possible:
                    sub.codes = type(inner.codes).create_view(
                        faiss.swig_ptr(codes[a * cs:]), (b - a) * cs, inner.codes.owner)
                else:
                    faiss.copy_array_to_vector(
                        np.ascontiguousarray(codes[a * cs:b * cs]), sub.codes)
                sub.ntotal = b - a
                faiss.copy_array_to_vector(np.ascontiguousarray(ids[a:b]), m.id_map)
                m.ntotal = b - a
                shards.add_shard(m)
                sous_index.append(m)
            if int(shards.ntotal) != n:
                raise ValueError(f"IndexShards : {shards.ntotal} vecteurs au lieu de {n}")
        except Exception as e:
            logger.info(f"[vector_index] Index non découpé en sous-index ({e}) : "
                        f"index plat conservé.")
            return
        self._monolithe = index          # garde les codes vivants (vues sans copie)
        self.index = shards
        self._shards = sous_index
        self.lecture["shards"] = len(sous_index)
        logger.info(f"[vector_index] Index découpé en {len(sous_index)} sous-index "
                    f"({'vues' if vue_possible else 'copies'}, recherche parallèle).")

    @classmethod
    def _depuis_index(cls, index) -> "VectorIndex":
        """Enveloppe un index FAISS lu sur disque (plat, IndexIDMap)."""
        obj = cls.__new__(cls)
        obj.index = index
        obj.dim = index.d
        obj._init_lecture()
        # Index 8 bits sauvé VIDE avant entraînement (corpus vide) : ré-armer
        # le tampon pour que des ajouts ultérieurs entraînent le quantizer.
        inner = getattr(index, "index", None)
        obj._pending = ([], []) if (inner is not None
                                    and not inner.is_trained) else None
        # Index 8 bits entraîné sur un échantillon SOUS le seuil (audit V2,
        # C-6 : point de reprise précoce) : les vecteurs sont reconstruits
        # (erreur ≤ un pas de quantification, du même ordre que le stockage
        # 8 bits lui-même) et remis au tampon d'un quantizer NEUF — la
        # calibration sera refaite sur un échantillon complet au prochain
        # seuil ; en attendant, recherche exacte. Le format du fichier (8 bits)
        # est conservé quel que soit RIVAGE_FAISS_QUANT.
        if (obj._pending is None and inner is not None
                and 0 < int(index.ntotal) < _QT_TRAIN_N and _is_8bit_quantizer(inner)):
            try:
                dc = faiss.downcast_index(inner)
                vecs = np.ascontiguousarray(dc.reconstruct_n(0, int(dc.ntotal)),
                                            dtype="float32")
                ids = [int(i) for i in faiss.vector_to_array(index.id_map)]
                if len(ids) == vecs.shape[0]:
                    base = faiss.IndexScalarQuantizer(
                        obj.dim, faiss.ScalarQuantizer.QT_8bit,
                        faiss.METRIC_INNER_PRODUCT)
                    obj.index = faiss.IndexIDMap(base)
                    obj._pending = ([vecs], ids)
                    logger.info(f"[vector_index] Index 8 bits de {len(ids)} vecteurs "
                                f"(< {_QT_TRAIN_N}) : tampon ré-armé, quantizer à "
                                f"ré-entraîner au seuil (C-6).")
            except Exception as e:           # index tel quel : toujours lisible
                logger.debug(f"[vector_index] Ré-armement 8 bits impossible : {e}")
        return obj
