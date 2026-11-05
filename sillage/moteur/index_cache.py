# index_cache.py
"""
Cache LECTEUR de la génération publiée : mémoire + miroir disque local.

Problème résolu (réseau lent, base de plusieurs Go) : le lecteur rechargeait
l'index vectoriel ET le BM25 depuis le partage réseau À CHAQUE question.
Désormais :

  1. CACHE MÉMOIRE par FICHIER (audit V2, C-2) — à chaque question, seule la
     lecture du manifest (quelques Ko) vérifie la fraîcheur ; FAISS et BM25
     sont cachés SÉPARÉMENT, clé = nom du fichier de génération. Un point de
     reprise de l'indexeur publie un nouveau FAISS mais réutilise le BM25 :
     seul le FAISS est rechargé (le re-parse BM25, le plus coûteux, est
     évité). La garantie « chaque question voit la dernière publication »
     est inchangée.
  2. LIBÉRATION AVANT CHARGEMENT (audit V2, C-5) — l'ancienne version d'un
     fichier est déréférencée (et le ramasse-miettes forcé) AVANT de charger
     la nouvelle, sous le verrou : plus de pic mémoire ×2 au changement de
     génération (un lecteur à 1 M de fragments passait de 6,5 à 13 Go).
  3. MIROIR LOCAL sur disque — les fichiers FAISS/BM25 de la génération sont
     copiés UNE FOIS dans un cache local du poste ; les chargements suivants
     (y compris après redémarrage de l'application) se font depuis le disque
     local : zéro transfert réseau tant que la génération n'a pas changé.
     Les fichiers de génération sont IMMUABLES par nom (store.py), la
     comparaison nom+taille suffit.
  4. LECTURE OPTIMISÉE du FAISS (audit perf 2.1, L-4 / L-5) — le fichier du
     miroir local est lu en mmap (mémoire de fichier reclaimable, chargement
     quasi instantané) et l'index est découpé en sous-index pour paralléliser
     chaque recherche ; voir `VectorIndex.load_lecteur` (RIVAGE_FAISS_MMAP,
     RIVAGE_FAISS_SHARDS). Un chemin réseau direct reste lu classiquement.
  5. PORTÉE PRÉCALCULÉE (audit perf 2.1, L-1 / L-11) — troisième slot, clé =
     nom du registre de la génération : chemins normalisés triés et ordinal
     de fichier de chaque fragment (`registry.Portee`), construits une fois
     (dans le préchargement du lecteur, qui appelle `get_indexes`), et
     interrogés par question par dichotomie et masque numpy (0,2 ms au lieu
     de 0,9 à 1,9 s à 200 000 fichiers). Libéré au changement de registre
     comme les autres slots (C-5).
  6. CONNEXIONS CONSERVÉES (audit perf 2.1, L-9) — le registre et le magasin
     de contenu (SQLite `mode=ro&immutable=1`, fichiers immuables par nom)
     étaient rouverts à chaque question : 560 lectures de pages et deux
     lectures du manifest par question, cache de pages SQLite vide à chaque
     fois. Ils sont désormais PRÊTÉS par un petit pool, clé = (registre,
     segments) de la génération : `get_lecteur` emprunte une paire
     (registre + magasin) libre — ou en ouvre une s'il n'y en a pas —,
     `rendre_lecteur` la restitue. Une paire n'est jamais utilisée par deux
     threads à la fois (le retriever qui l'emprunte vit dans un seul thread
     et la rend à sa fermeture) ; elle peut en revanche servir dans un
     AUTRE thread à la question suivante (connexions ouvertes avec
     `check_same_thread=False`). Choix argumenté : `threading.local` ne
     servirait à rien au lecteur graphique, qui crée un thread de travail
     PAR question (chaque thread aurait rouvert les siennes) ; une connexion
     unique sous verrou obligerait à sérialiser aussi le préchargement et
     l'arbre, qui tournent en parallèle d'une question. Au changement de
     génération, les paires libres sont fermées aussitôt, les paires
     empruntées à leur restitution (elles désignent l'ancienne génération).

Emplacement du miroir : %LOCALAPPDATA%\\rivage_cache (Windows) ou
~/.rivage/rivage_cache, sous-dossier par répertoire d'index — surchargeable
par RIVAGE_LOCAL_CACHE_DIR ; RIVAGE_LOCAL_CACHE=0 désactive le miroir
(chargement réseau direct, cache mémoire conservé).

Le registre et les segments de contenu ne sont PAS miroités : SQLite les lit
par petites requêtes ciblées (immutable=1), insensibles à la taille totale.
"""

import gc
import os
import hashlib
import logging
import shutil
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Un `.tmp` plus jeune que ce délai appartient peut-être à un AUTRE lecteur du
# poste en pleine copie (audit V2, C-8) : le GC l'épargne.
_TMP_GRACE_S = 3600.0

_LOCK = threading.Lock()
# Cache par FICHIER : {"dir": répertoire d'index normalisé,
#   "faiss":  {"name": nom de génération, "obj": VectorIndex, "path": local},
#   "bm25":   {"name": ..., "obj": LexicalIndex, "path": local},
#   "portee": {"name": nom du registre, "obj": registry.Portee},
#   "info": dernier résumé rendu}
_CACHE = {"dir": None, "faiss": None, "bm25": None, "portee": None, "info": None}

# Pool de connexions (L-9) : {"cle": (chemin du registre, segments),
#   "libres": [(ReadOnlyRegistry, ContentStore), ...],
#   "pretes": [Lecteur, ...]} — voir en tête (point 6).
_POOL = {"cle": None, "libres": [], "pretes": []}
# Paires conservées libres au plus (au-delà : fermées à la restitution). Le
# lecteur graphique n'en emploie qu'une ou deux (question + préchargement).
_POOL_MAX_LIBRES = 4
# Cache de pages SQLite des connexions conservées (Kio ; défaut SQLite : 2 Mo).
# Mesuré sur 10 questions à 200 000 fragments (strace) : 329 lectures de
# pages du registre par question avec le cache par défaut, 72 avec 32 Mo —
# les pages d'index (faiss_id -> fichier) restent en mémoire d'une question
# à l'autre ; les lectures restantes sont celles des textes nouveaux.
_CACHE_REGISTRE_KIO = 32 * 1024
_CACHE_SEGMENT_KIO = 16 * 1024

# Extensions gérées par le GC du miroir (ne touche à rien d'autre).
_GC_SUFFIXES = (".faiss", ".pkl", ".tmp")


class Lecteur:
    """Les ressources d'une génération prêtées à un retriever : index (du
    cache mémoire), portée précalculée, registre et magasin (du pool de
    connexions). À rendre par `rendre_lecteur` — jamais à fermer soi-même."""

    __slots__ = ("vindex", "lindex", "portee", "registry", "content_store",
                 "info", "_cle", "_rendu")

    def __init__(self, vindex, lindex, portee, registry, content_store, info, cle):
        self.vindex = vindex
        self.lindex = lindex
        self.portee = portee
        self.registry = registry
        self.content_store = content_store
        self.info = info
        self._cle = cle
        self._rendu = False


def mirror_enabled() -> bool:
    return (os.environ.get("RIVAGE_LOCAL_CACHE", "1").strip() != "0")


def local_cache_dir(index_dir: str) -> str:
    """Dossier du miroir local pour CE répertoire d'index (stable, affichable)."""
    base = os.environ.get("RIVAGE_LOCAL_CACHE_DIR")
    if not base:
        root = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), ".rivage")
        base = os.path.join(root, "rivage_cache")
    h = hashlib.sha1(
        os.path.normcase(os.path.abspath(index_dir)).encode("utf-8")
    ).hexdigest()[:12]
    return os.path.join(base, h)


def _mirror_file(src, cache_dir: str, log) -> str:
    """Copie locale (une fois) d'un fichier de génération immuable.
    Retourne le chemin LOCAL, ou `src` (réseau) si la copie échoue."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        dst = os.path.join(cache_dir, os.path.basename(src))
        size = os.path.getsize(src)
        if os.path.exists(dst) and os.path.getsize(dst) == size:
            return dst                       # déjà miroité (fichier immuable)
        # Nom temporaire PAR PROCESSUS (audit V2, C-8) : deux lecteurs du même
        # poste copiant la même génération écrivaient le même `.tmp` — l'un
        # renommait la copie tronquée de l'autre.
        tmp = f"{dst}.{os.getpid()}.tmp"
        log.info(f"[index_cache] Copie locale de {os.path.basename(src)} "
                 f"({size // (1024 * 1024)} Mo)…")
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return dst
    except Exception as e:
        log.warning(f"[index_cache] Miroir local impossible ({e}) — "
                    f"lecture réseau directe.")
        return src


def _gc_mirror(cache_dir: str, keep: set, log) -> None:
    """Retire du miroir les fichiers de générations dé-référencées. Les
    `.tmp` récents (copie en cours d'un autre lecteur du poste) sont
    épargnés (audit V2, C-8)."""
    try:
        now = time.time()
        for name in os.listdir(cache_dir):
            if name in keep or not name.endswith(_GC_SUFFIXES):
                continue
            full = os.path.join(cache_dir, name)
            if name.endswith(".tmp"):
                try:
                    if now - os.path.getmtime(full) < _TMP_GRACE_S:
                        continue
                except OSError:
                    continue
            try:
                os.remove(full)
                log.debug(f"[index_cache] Miroir : {name} retiré (génération "
                          f"dé-référencée).")
            except OSError:
                pass
    except OSError:
        pass


def _release(slot: str) -> None:
    """Déréférence l'objet caché d'un slot et force le ramasse-miettes (C-5) :
    la mémoire de l'ancienne version est rendue AVANT de charger la nouvelle."""
    entry = _CACHE.get(slot)
    _CACHE[slot] = None
    if entry is not None and entry.get("obj") is not None:
        entry["obj"] = None
        del entry
        gc.collect()


def _load_slot(slot: str, src_path, cache_dir, loader, log, name=None):
    """Charge (ou réutilise) le fichier d'un slot. Retourne
    (objet, chemin local, rechargé: bool). `name` : clé de fraîcheur du slot
    (défaut : nom du fichier ; FAISS de base + deltas pour le vectoriel)."""
    if name is None:
        name = os.path.basename(src_path) if src_path else None
    entry = _CACHE.get(slot)
    if entry is not None and entry.get("name") == name:
        return entry.get("obj"), entry.get("path"), False
    # Fichier changé (ou premier chargement) : libérer l'ancien D'ABORD.
    _release(slot)
    if not src_path or not os.path.exists(src_path):
        _CACHE[slot] = {"name": name, "obj": None, "path": src_path}
        return None, src_path, True
    local = _mirror_file(src_path, cache_dir, log) if cache_dir else src_path
    try:
        obj = loader(local)
    except Exception as e:
        if local == src_path:
            raise
        # Copie locale illisible (tronquée, disque plein, corruption — audit
        # V2, C-8) : sans repli, TOUTES les questions échouaient jusqu'à la
        # génération suivante. On jette la copie et on lit le réseau ; la
        # prochaine question retentera le miroir.
        log.warning(f"[index_cache] Copie locale illisible ({e}) — copie "
                    f"supprimée, chargement depuis le réseau : {src_path}")
        try:
            os.remove(local)
        except OSError:
            pass
        local = src_path
        obj = loader(local)
    _CACHE[slot] = {"name": name, "obj": obj, "path": local}
    return obj, local, True


# --- Pool de connexions (L-9) ------------------------------------------------

def _cle_pool(paths) -> tuple:
    return (paths["registry"], tuple(paths.get("content_segments") or []))


def _fermer_paire(paire) -> None:
    for res in paire:
        try:
            if res is not None:
                res.close()
        except Exception:
            pass


def _fermer_libres() -> None:
    libres, _POOL["libres"] = _POOL["libres"], []
    for paire in libres:
        _fermer_paire(paire)


def _emprunter(index_dir: str, paths, log):
    """Sous _LOCK : une paire (registre, magasin) de la génération `paths`,
    libre si possible, ouverte sinon. Retourne (registre, magasin, clé)."""
    from registry import ReadOnlyRegistry
    from content_store import ContentStore
    cle = _cle_pool(paths)
    if _POOL["cle"] != cle:
        # Autre génération (ou autre répertoire) : les paires libres
        # désignent des fichiers périmés — fermées maintenant ; les paires
        # prêtées le seront à leur restitution (clé différente).
        _fermer_libres()
        _POOL["cle"] = cle
    if _POOL["libres"]:
        registry, store = _POOL["libres"].pop()
    else:
        registry = ReadOnlyRegistry(paths["registry"], partage_threads=True,
                                    cache_kio=_CACHE_REGISTRE_KIO)
        try:
            store = ContentStore(index_dir, paths.get("content_segments", []),
                                 ranges=paths.get("content_segments_info"),
                                 partage_threads=True,
                                 cache_kio=_CACHE_SEGMENT_KIO)
        except Exception:
            registry.close()
            raise
        log.debug(f"[index_cache] Connexions ouvertes : "
                  f"{os.path.basename(paths['registry'])} + "
                  f"{len(paths.get('content_segments') or [])} segment(s) "
                  f"(prêtées entre les questions).")
    return registry, store, cle


def rendre_lecteur(lecteur: Optional[Lecteur], jeter: bool = False) -> None:
    """Restitue au pool les connexions d'un `Lecteur` (idempotent). Fermées
    si la génération a changé entre-temps, si le pool est plein, ou si
    `jeter` est vrai (revue 2.5, RB-1) : une paire sur laquelle une question
    vient d'échouer (erreur SQLite, handle invalidé par un hoquet réseau) ne
    doit pas être resservie à toutes les questions suivantes — elle est
    fermée et la prochaine question en ouvre une neuve."""
    if lecteur is None or lecteur._rendu:
        return
    lecteur._rendu = True
    paire = (lecteur.registry, lecteur.content_store)
    with _LOCK:
        try:
            _POOL["pretes"].remove(lecteur)
        except ValueError:
            pass
        if (not jeter and lecteur._cle == _POOL["cle"]
                and len(_POOL["libres"]) < _POOL_MAX_LIBRES):
            _POOL["libres"].append(paire)
            return
    _fermer_paire(paire)


def connexions_ouvertes() -> int:
    """Nombre de paires (registre, magasin) actuellement ouvertes par le pool
    (libres + prêtées) — visibilité, tests."""
    with _LOCK:
        return len(_POOL["libres"]) + len(_POOL["pretes"])


# --- Chargement d'une génération --------------------------------------------

def get_lecteur(index_dir: str, log=None, paths=None) -> Lecteur:
    """Toutes les ressources LECTEUR de la génération publiée, avec caches :
    index (mémoire), portée précalculée (mémoire), registre et magasin de
    contenu (pool de connexions, EMPRUNTÉS : l'appelant les rend par
    `rendre_lecteur` quand il a fini — `HybridRetriever.close`).

    `paths` : résultat de `store.reader_paths` déjà lu par l'appelant (le
    manifest n'est lu qu'une fois par question) ; None = lu ici. Lève
    FileNotFoundError si aucun index n'est publié.
    """
    _log = log or logger
    if paths is None:
        from store import reader_paths
        paths = reader_paths(index_dir)
    if paths is None:
        raise FileNotFoundError(
            f"Aucun index publié dans {index_dir} (manifest absent).")
    faiss_p, bm25_p = paths["faiss"], paths["bm25"]
    # Deltas de points de reprise (audit perf 2.1, I-4) : fusionnés par-dessus
    # le FAISS de base au chargement ; la clé de fraîcheur du slot les inclut
    # (un nouveau delta recharge le vectoriel, le BM25 reste en cache).
    deltas_p = list(paths.get("faiss_deltas") or [])
    faiss_key = ("+".join(os.path.basename(p) for p in [faiss_p] + deltas_p)
                 if faiss_p else None)
    dir_key = os.path.normcase(os.path.abspath(index_dir))

    with _LOCK:
        if _CACHE["dir"] != dir_key:
            # Autre répertoire d'index : tout libérer avant de charger.
            _release("faiss")
            _release("bm25")
            _release("portee")
            _CACHE.update(dir=dir_key, info=None)

        cache_dir = local_cache_dir(index_dir) if mirror_enabled() else None
        from vector_index import VectorIndex
        from lexical_index import LexicalIndex

        memoire_msg = [None]

        def charger_faiss(chemin):
            # Lecture LECTEUR (audit perf 2.1, L-4 / L-5) : mmap seulement
            # quand le fichier lu est la copie du miroir local (jamais sur le
            # chemin réseau direct — y compris le repli « copie illisible »),
            # puis découpage en sous-index pour la recherche parallèle.
            # L'indexeur, lui, garde `VectorIndex.load` (index plat).
            local_deltas = [(_mirror_file(d, cache_dir, _log) if cache_dir else d)
                            for d in deltas_p]
            # Règle mémoire (V2.7, lot 2) : base plus grande que la mémoire
            # libre du poste → pas de sous-index ni de reranking parallèle
            # (threads et tampons en plus), mmap conservé ; message affiché.
            shards = None
            try:
                import plateforme
                memoire_msg[0] = plateforme.contrainte_memoire(os.path.getsize(chemin))
                if memoire_msg[0]:
                    shards = 0
                    os.environ.setdefault("RIVAGE_RERANK_WORKERS", "1")
                    _log.warning(f"[index_cache] {memoire_msg[0]}")
            except Exception:
                memoire_msg[0] = None
            try:
                return VectorIndex.load_lecteur(chemin, local=(chemin != faiss_p),
                                                deltas=local_deltas, shards=shards)
            except Exception as e:
                # Copie miroitée d'un DELTA illisible (revue 2.5, RB-2) : le
                # repli « copie illisible » de _load_slot ne connaît que le
                # fichier de base — il supprimait la base saine puis
                # re-miroitait le delta corrompu (même nom, même taille), et
                # chaque question échouait jusqu'à la génération suivante. On
                # purge les copies des deltas et on les lit depuis le réseau.
                if not cache_dir or local_deltas == deltas_p:
                    raise
                _log.warning(f"[index_cache] Delta miroité illisible ({e}) — copies "
                             f"des deltas supprimées, deltas lus depuis le réseau.")
                for loc, src in zip(local_deltas, deltas_p):
                    if loc != src:
                        try:
                            os.remove(loc)
                        except OSError:
                            pass
                return VectorIndex.load_lecteur(chemin, local=(chemin != faiss_p),
                                                deltas=deltas_p)

        # Témoin de chargement (V2.7, lot 3) : écrit AVANT un chargement réel
        # du FAISS, effacé par le lecteur après la première recherche réussie
        # (plateforme.marquer_recherche_reussie). Présent au démarrage
        # suivant = le processus est mort entre les deux.
        entree = _CACHE.get("faiss")
        if not (entree and entree.get("name") == faiss_key and entree.get("obj") is not None):
            try:
                import plateforme
                plateforme.ecrire_temoin(index_dir)
            except Exception:
                pass
        vindex, local_faiss, faiss_reloaded = _load_slot(
            "faiss", faiss_p, cache_dir, charger_faiss, _log, name=faiss_key)
        lindex, local_bm25, bm25_reloaded = _load_slot(
            "bm25", bm25_p, cache_dir, LexicalIndex.load, _log)

        # Registre + magasin : empruntés au pool (L-9) — ouverts seulement
        # s'il n'y a pas de paire libre pour cette génération.
        registry, store, cle = _emprunter(index_dir, paths, _log)
        try:
            portee, portee_reloaded = _charger_portee(paths["registry"], registry, _log)
        except Exception:
            _fermer_paire((registry, store))
            raise
        lecteur = Lecteur(vindex, lindex, portee, registry, store, None, cle)
        _POOL["pretes"].append(lecteur)

        reloaded = faiss_reloaded or bm25_reloaded or portee_reloaded
        if not reloaded:
            info = dict(_CACHE["info"] or {})
            info["source"] = "mémoire"
            info["generation"] = paths.get("generation")
            lecteur.info = info
            return lecteur

        source = "réseau"
        if cache_dir and ((local_faiss and local_faiss != faiss_p)
                          or (local_bm25 and local_bm25 != bm25_p)):
            source = "cache local"
        if cache_dir and (faiss_reloaded or bm25_reloaded):
            _gc_mirror(cache_dir,
                       {os.path.basename(p) for p in [local_faiss, local_bm25] + deltas_p
                        if p}, _log)

        info = {
            "generation": paths.get("generation"),
            "vecteurs": vindex.size if vindex else 0,
            "cache_dir": cache_dir,
            "faiss_path": local_faiss,
            "bm25_path": local_bm25,
            "source": source,
            # Mode de lecture du FAISS (L-4 / L-5) : {"mmap": bool, "shards": n}.
            "faiss_lecture": dict(getattr(vindex, "lecture", None) or {}),
            # Portée précalculée (L-1) : taille et empreinte.
            "portee": {"fichiers": portee.n_fichiers, "fragments": portee.n_fragments,
                       "octets": portee.taille_octets()},
            # Règle mémoire (lot 2) : message si la base dépasse la mémoire libre.
            "memoire": memoire_msg[0] if faiss_reloaded else (_CACHE["info"] or {}).get("memoire"),
        }
        _CACHE["info"] = info
        quoi = " et ".join(n for n, r in (("FAISS", faiss_reloaded),
                                         ("BM25", bm25_reloaded),
                                         ("portée", portee_reloaded)) if r)
        _log.info(f"[index_cache] Génération {info['generation']} : {quoi} "
                  f"chargé ({info['vecteurs']} vecteurs, source : {source}"
                  + (f", miroir : {cache_dir}" if cache_dir else "") + ").")
        lecteur.info = dict(info)
        return lecteur


def _charger_portee(registry_path: str, registry, log):
    """Sous _LOCK : la portée précalculée de la génération (slot « portee »,
    clé = nom du registre), construite depuis `registry` si elle n'est pas
    en cache. Retourne (Portee, reconstruite: bool)."""
    name = os.path.basename(registry_path)
    entry = _CACHE.get("portee")
    if entry is not None and entry.get("name") == name and entry.get("obj") is not None:
        return entry["obj"], False
    _release("portee")                       # l'ancienne génération d'abord (C-5)
    t0 = time.perf_counter()
    portee = registry.portee()
    _CACHE["portee"] = {"name": name, "obj": portee}
    log.info(f"[index_cache] Portée précalculée : {portee.n_fichiers} fichiers, "
             f"{portee.n_fragments} fragments, "
             f"{portee.taille_octets() / 2 ** 20:.1f} Mo, "
             f"{(time.perf_counter() - t0) * 1000:.0f} ms.")
    return portee, True


def get_indexes(index_dir: str, log=None, paths=None):
    """(vindex, lindex, info) de la génération publiée, avec caches — et, en
    passant, la portée précalculée et une paire de connexions prêtes pour la
    première question (c'est l'appel du préchargement du lecteur).

    info = {"generation", "vecteurs", "cache_dir", "faiss_path", "bm25_path",
    "source": "mémoire" | "cache local" | "réseau", "faiss_lecture",
    "portee"}. Lève FileNotFoundError si aucun index n'est publié (comme
    l'ancien chemin direct).
    """
    lecteur = get_lecteur(index_dir, log=log, paths=paths)
    rendre_lecteur(lecteur)
    return lecteur.vindex, lecteur.lindex, lecteur.info


def get_portee(index_dir: str, log=None):
    """La portée précalculée (`registry.Portee`) de la génération publiée —
    pour résoudre une liste de chemins ou de préfixes sans ouvrir le
    registre (lecteur graphique, L-1)."""
    lecteur = get_lecteur(index_dir, log=log)
    rendre_lecteur(lecteur)
    return lecteur.portee


def clear_cache() -> None:
    """Vide le cache mémoire (tests / changement de répertoire forcé) et ferme
    TOUTES les connexions du pool — libres et prêtées : un `Lecteur` encore
    emprunté à cet instant appartient à un retriever non fermé (fuite), pas
    à une question en cours."""
    with _LOCK:
        _release("faiss")
        _release("bm25")
        _release("portee")
        _CACHE.update(dir=None, info=None)
        _fermer_libres()
        pretes, _POOL["pretes"] = _POOL["pretes"], []
        _POOL["cle"] = None
    for lecteur in pretes:
        lecteur._rendu = True
        _fermer_paire((lecteur.registry, lecteur.content_store))
