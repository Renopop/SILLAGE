# store.py
"""
Publication atomique par génération - §5.1, §8 (robustesse « 1 écrivain / N lecteurs »)

Corrige la faille structurelle du pipeline : la publication n'était atomique
que *par fichier*, jamais pour l'ensemble cohérent {faiss, bm25, registre}, et
le registre SQLite était muté en place sur le partage réseau pendant que les
lecteurs l'ouvraient en `immutable=1`.

Principe : chaque run d'indexation écrit un jeu d'artefacts **estampillé d'un
numéro de génération** (`corpus.<gen>.faiss`, `bm25.<gen>.pkl`,
`registre.<gen>.db`), puis bascule un unique fichier pointeur `manifest.json`
par `os.replace` (atomique). Conséquences :

  - Les lecteurs ne découvrent une génération qu'après la bascule du manifest ;
    ils ne voient donc jamais un fichier en cours d'écriture (résout B1).
  - Les trois artefacts deviennent visibles ensemble, ou pas du tout : un crash
    avant la bascule laisse la génération précédente intacte (résout M1/M6).
  - Le registre d'une génération publiée n'est plus jamais muté : `immutable=1`
    redevient une promesse vraie.

Les générations anciennes sont nettoyées au mieux (on conserve la courante et
la précédente pour les lecteurs déjà en cours).
"""

import os
import json
import time
import shutil
import logging
import tempfile
from typing import Optional, Dict, Any, Callable

logger = logging.getLogger(__name__)

MANIFEST = "manifest.json"
# Générations conservées : plus large qu'avant (C1) pour ne pas supprimer une
# génération sous un lecteur lent qui la charge encore.
_KEEP_GENERATIONS = 4
# Âge minimal avant qu'une génération devienne éligible à la suppression (C1) :
# un lecteur ayant lu le manifest a le temps d'ouvrir ses fichiers. Relevé à
# 30 min : avec les points de reprise, les générations défilent plus vite et
# _KEEP_GENERATIONS seul ne protège plus un lecteur lent (audit).
_MIN_GENERATION_AGE_S = 1800  # 30 minutes


def _manifest_path(index_dir: str) -> str:
    return os.path.join(index_dir, MANIFEST)


# Suffixes des résidus d'écriture (audit V2, A-10) : `.tmp` d'une publication
# atomique interrompue (FAISS, BM25), `-journal` SQLite d'un run tué.
_RESIDUE_SUFFIXES = (".tmp", "-journal")


def _base_name(name: str) -> str:
    """Nom de l'artefact dont `name` est un résidu (`corpus.3.faiss.tmp` ->
    `corpus.3.faiss`), ou `name` lui-même s'il n'en est pas un."""
    for suf in _RESIDUE_SUFFIXES:
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


# Points de reprise EN DELTA (audit perf 2.1, I-4) : une génération
# intermédiaire ne réécrit plus le FAISS cumulé (des gigaoctets sur le partage
# à chaque point) mais publie les seuls vecteurs ajoutés depuis le dernier
# point dans `corpus.<gen>.delta.faiss`. Le manifest référence le FAISS de
# BASE (`faiss`, inchangé) et la liste ORDONNÉE des deltas (`faiss_deltas`) ;
# un lecteur charge la base puis y ajoute chaque delta ; la publication
# FINALE fusionne tout en un seul FAISS (liste vide). Même principe que les
# segments de contenu. Les lecteurs antérieurs à cette version ignorent
# `faiss_deltas` : mettre à jour les lecteurs AVANT l'indexeur, ou publier
# avec RIVAGE_CHECKPOINT_DELTA=0 (réécriture complète à chaque point).
DELTA_TAG = "delta"


def _generation_of(name: str) -> Optional[int]:
    """Numéro de génération porté par un nom d'artefact (`bm25.12.pkl` -> 12,
    `corpus.12.delta.faiss` -> 12), ou None si le nom n'a pas cette forme."""
    parts = name.split(".")
    if len(parts) == 4 and parts[2] == DELTA_TAG:
        parts = [parts[0], parts[1], parts[3]]
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    return int(parts[1])


def read_manifest(index_dir: str) -> Optional[Dict[str, Any]]:
    """
    Lit le manifest publié. Retourne None UNIQUEMENT si le manifest n'existe
    pas (premier run légitime).

    Un manifest PRÉSENT mais illisible (hoquet SMB, JSON corrompu) ne doit
    JAMAIS être traité comme « aucun index » : l'indexeur repartirait à la
    génération 1 sur un registre vierge et publierait un manifest régressé —
    perte logique de tout l'index (audit, critique). On retente avec backoff,
    puis on lève explicitement.
    """
    path = _manifest_path(index_dir)
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None  # réellement absent : premier run
        except (OSError, ValueError) as e:
            last = e
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(
        f"Manifest présent mais illisible ({path}): {last}. "
        "Refus de continuer (repartir de zéro écraserait l'index publié). "
        "Vérifiez le partage réseau, puis relancez."
    )


def reader_paths(index_dir: str) -> Optional[Dict[str, Any]]:
    """
    Retourne les chemins absolus de la génération publiée
    ({faiss, bm25, registry, generation}), ou None si aucun index publié.
    `faiss` peut être None si le corpus est vide (aucun vecteur, n4) ;
    `bm25` peut être None pendant un run à points de reprise (le BM25 n'est
    reconstruit qu'à la publication finale — recherche vectorielle seule
    entre-temps). `content_segments_info` porte les bornes lo/hi de chaque
    segment pour router les lectures sans sonder tous les segments (audit).
    `faiss_deltas` (audit perf 2.1, I-4) : chemins absolus, ORDONNÉS, des
    deltas publiés par les points de reprise depuis le FAISS de base — à
    charger par-dessus `faiss` (`VectorIndex.load_lecteur(..., deltas=)`) ;
    liste vide hors période de points de reprise.
    """
    m = read_manifest(index_dir)
    if not m:
        return None
    # Chemins ABSOLUS garantis (audit perf 2.1, P-12) : avec un répertoire
    # d'index relatif, ContentStore re-joignait le répertoire aux chemins déjà
    # joints (« idx/idx/content.1.db »), ne trouvait aucun segment et la
    # réponse sortait avec « 0 textes » sans erreur visible.
    index_dir = os.path.abspath(index_dir)
    faiss_name = m.get("faiss")
    bm25_name = m.get("bm25")
    segs = list(m.get("content_segments", []))
    deltas = list(m.get("faiss_deltas", []) or []) if faiss_name else []
    return {
        "generation": m.get("generation"),
        "faiss": os.path.join(index_dir, faiss_name) if faiss_name else None,
        "faiss_deltas": [os.path.join(index_dir, d) for d in deltas],
        "bm25": os.path.join(index_dir, bm25_name) if bm25_name else None,
        "registry": os.path.join(index_dir, m["registry"]),
        "content_segments": [os.path.join(index_dir, s["file"]) for s in segs],
        "content_segments_info": [
            {"file": os.path.join(index_dir, s["file"]),
             "lo": s.get("lo"), "hi": s.get("hi")}
            for s in segs
        ],
    }


def validate_generation(index_dir: str) -> None:
    """
    Vérifie que les fichiers référencés par le manifest existent réellement
    (R1). Lève RuntimeError si le registre, le BM25 ou un FAISS déclaré présent
    est introuvable — pour ne JAMAIS repartir d'un index partiel/vide en
    croyant faire de l'incrémental.
    """
    paths = reader_paths(index_dir)
    if paths is None:
        return  # aucun index publié : premier run légitime
    missing = []
    if not os.path.exists(paths["registry"]):
        missing.append(os.path.basename(paths["registry"]))
    # bm25 peut être None sur une génération de point de reprise (reconstruit
    # à la publication finale seulement).
    if paths["bm25"] is not None and not os.path.exists(paths["bm25"]):
        missing.append(os.path.basename(paths["bm25"]))
    if paths["faiss"] is not None and not os.path.exists(paths["faiss"]):
        missing.append(os.path.basename(paths["faiss"]))
    for delta in paths.get("faiss_deltas", []):
        if not os.path.exists(delta):
            missing.append(os.path.basename(delta))
    for seg in paths.get("content_segments", []):
        if not os.path.exists(seg):
            missing.append(os.path.basename(seg))
    if missing:
        raise RuntimeError(
            f"Génération {paths['generation']} incohérente : fichiers "
            f"référencés introuvables ({', '.join(missing)}). "
            f"Indexation interrompue pour éviter une perte de données."
        )


def atomic_replace(tmp: str, path: str, retries: int = 5, base_delay: float = 0.5) -> None:
    """
    `os.replace(tmp, path)` robuste face aux `PermissionError` (sharing
    violation SMB quand un lecteur tient le fichier ouvert). Retry avec backoff.
    """
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:  # Windows/SMB : fichier verrouillé par un lecteur
            last = e
            time.sleep(base_delay * (2 ** attempt))
    # Échec : nettoyer le temporaire pour ne pas laisser de résidu
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last if last else RuntimeError(f"os.replace a échoué: {path}")


class Publisher:
    """
    Gère l'écriture d'une nouvelle génération et sa bascule atomique.

    Utilisation (indexer) :
        pub = Publisher(index_dir)
        db_path = pub.seed_registry()          # copie la DB courante (incrémental)
        # ... construire faiss/bm25 dans pub.faiss_path / pub.bm25_path ...
        pub.publish()                          # bascule atomique du manifest
    """

    def __init__(self, index_dir: str, log=None):
        os.makedirs(index_dir, exist_ok=True)
        self.index_dir = index_dir
        self.log = log or logger
        self._prev = read_manifest(index_dir)
        self.prev_gen = int(self._prev["generation"]) if self._prev else 0
        self.gen = self.prev_gen + 1
        # Une fois publish() réussi, cette génération appartient aux lecteurs :
        # discard() devient un no-op (audit : une exception dans la fenêtre
        # publish→suite ne doit JAMAIS détruire des fichiers référencés par le
        # manifest — lecteurs cassés + indexation bloquée par validate_generation).
        self._published = False

        self.faiss_name = f"corpus.{self.gen}.faiss"
        # Delta de point de reprise (I-4) : vecteurs ajoutés depuis le point
        # précédent, par-dessus le FAISS de base référencé par le manifest.
        self.delta_name = f"corpus.{self.gen}.{DELTA_TAG}.faiss"
        self.bm25_name = f"bm25.{self.gen}.pkl"
        self.registry_name = f"registre.{self.gen}.db"
        self.content_name = f"content.{self.gen}.db"
        self.faiss_path = os.path.join(index_dir, self.faiss_name)
        self.delta_path = os.path.join(index_dir, self.delta_name)
        self.bm25_path = os.path.join(index_dir, self.bm25_name)
        self.registry_path = os.path.join(index_dir, self.registry_name)
        self.content_path = os.path.join(index_dir, self.content_name)

    def prev_faiss_path(self) -> Optional[str]:
        """
        Chemin du FAISS de la génération courante, ou None si le corpus publié
        est vide (manifest `faiss` = null, n4). Suppose la génération validée
        au préalable (voir validate_generation) : un FAISS déclaré mais absent
        est une incohérence, pas un corpus vide.
        """
        if self._prev and self._prev.get("faiss"):
            return os.path.join(self.index_dir, self._prev["faiss"])
        return None

    def prev_registry_path(self) -> Optional[str]:
        if self._prev:
            return os.path.join(self.index_dir, self._prev["registry"])
        return None

    def prev_faiss_ref(self) -> Optional[str]:
        """Nom de fichier FAISS publié (peut appartenir à une gén. antérieure)."""
        return self._prev.get("faiss") if self._prev else None

    def prev_faiss_deltas(self) -> list:
        """Noms ORDONNÉS des deltas publiés par-dessus le FAISS de base
        (audit perf 2.1, I-4) ; vide hors période de points de reprise."""
        if not self._prev or not self._prev.get("faiss"):
            return []
        return list(self._prev.get("faiss_deltas", []) or [])

    def prev_faiss_delta_paths(self) -> list:
        return [os.path.join(self.index_dir, d) for d in self.prev_faiss_deltas()]

    def prev_bm25_ref(self) -> Optional[str]:
        return self._prev.get("bm25") if self._prev else None

    def prev_content_segments(self) -> list:
        """Segments de contenu actifs [{file, lo, hi}] de la génération courante."""
        return list(self._prev.get("content_segments", [])) if self._prev else []

    def prev_bm25_stale(self) -> bool:
        """
        True si le BM25 de la génération publiée est EN RETARD (audit v2) : la
        génération est un point de reprise (drapeau explicite du manifest), ou
        le BM25 est absent alors que du contenu existe (run tué pendant la
        reconstruction finale). Le run courant doit alors publier une finale
        réparatrice même sans aucun changement de corpus.
        """
        if not self._prev:
            return False
        if self._prev.get("bm25_stale"):
            return True
        if self._prev.get("bm25") is None:
            return bool(self._prev.get("content_segments"))
        # BM25 publié dans un ANCIEN format (indexeur antérieur à 2.1) : les
        # lecteurs le lisent encore, mais en reconstruisant les statistiques à
        # chaque chargement (minutes sur un gros corpus). Une publication
        # réparatrice — reconstruction du seul BM25 depuis le magasin de
        # contenu, sans ré-extraction ni ré-embedding — l'amène au format
        # courant, même si le corpus n'a pas changé.
        try:
            from lexical_index import FORMAT as _BM25_FORMAT
        except ImportError:
            return False
        return self._prev.get("bm25_format") != _BM25_FORMAT

    def seed_registry(self) -> str:
        """
        Prépare le registre de la nouvelle génération. En incrémental, copie la
        DB de la génération courante (désormais LÉGÈRE : sans le texte des
        chunks, S1) ; sinon crée un chemin vierge. Retourne le chemin de la DB.

        Si la génération publiée est au schéma 1 du registre (indexeur 2.4 ou
        antérieur) et que l'écriture au schéma 2 est voulue (défaut ; voir
        RIVAGE_REGISTRY_SCHEMA), la COPIE est migrée avant d'être posée sur le
        partage — jamais la génération publiée (audit perf 2.1, I-7).
        """
        if self._prev:
            src = os.path.join(self.index_dir, self._prev["registry"])
            if not os.path.exists(src):
                # Ne jamais repartir d'un registre vierge en croyant faire de
                # l'incrémental : ce serait une perte silencieuse (R1).
                raise RuntimeError(
                    f"Registre de la génération précédente introuvable ({src}). "
                    f"Indexation interrompue."
                )
            if not self._seed_migre(src):
                shutil.copy2(src, self.registry_path)
        return self.registry_path

    def _seed_migre(self, src: str) -> bool:
        """
        Migration du registre semé au schéma 2 (audit perf 2.1, I-7), par
        COPIE LOCALE : la génération publiée est copiée dans le répertoire
        temporaire du poste, migrée et compactée là (`VACUUM`), puis le
        résultat est copié à sa place sur le partage. Pourquoi pas en place sur
        le partage : la migration et le VACUUM passent par le cache de pages
        de SQLite (écritures de 4 Kio, journal, verrous — ce qui coûtait 1 à
        3 h de commits réseau en A-6), alors qu'une copie de fichier est un
        flux séquentiel ; en local, le partage ne voit qu'UNE lecture (taille
        au schéma 1) et UNE écriture (taille au schéma 2, ÷3) — mesuré sur
        140 000 fragments : 28,2 Mo lus et 9,1 Mo écrits en 0,6 s, contre
        28,2 Mo copiés puis 64,7 Mo de pages réécrites (journal, tables,
        VACUUM) sur le partage en place.
        Retourne False (copie simple, migration EN PLACE à l'ouverture par
        `Registry`) si rien n'est à migrer ou si la copie locale échoue
        (disque local plein…) : jamais bloquant.
        """
        from registry import (schema_cible, schema_de_fichier,
                              migrer_fichier_schema_2, _message_migration)
        try:
            if schema_cible() != 2 or schema_de_fichier(src) != 1:
                return False
        except Exception as e:                         # registre illisible ?
            self.log.warning(f"[store] Schéma du registre {src} indéterminé "
                             f"({e}) — copie telle quelle.")
            return False
        tmp = None
        t0 = time.monotonic()
        try:
            fd, tmp = tempfile.mkstemp(prefix="registre.", suffix=".db")
            os.close(fd)
            shutil.copy2(src, tmp)
            res = migrer_fichier_schema_2(tmp, log=self.log)
            shutil.copy2(tmp, self.registry_path)
            self.log.info("[store] " + _message_migration(
                res, "copie locale puis renvoi sur le partage, "
                     f"{time.monotonic() - t0:.1f} s en tout"))
            return True
        except Exception as e:
            self.log.warning(f"[store] Migration locale du registre impossible "
                             f"({e}) — copie telle quelle, migration en place à "
                             "l'ouverture.")
            try:
                os.remove(self.registry_path)
            except OSError:
                pass
            return False
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def discard(self) -> None:
        """
        Abandonne la génération en préparation : supprime les fichiers
        estampillés de CETTE génération déjà écrits. La génération précédente
        reste active. No-op si la génération a été publiée (ses fichiers
        appartiennent alors aux lecteurs).

        Défense INDÉPENDANTE de l'instance (audit v2, critique) : le manifest
        est RELU et aucun fichier qu'il référence n'est jamais supprimé. Le
        drapeau _published ne suffit pas — après un checkpoint, un Publisher
        NEUF (recréé) peut, sur une relecture de manifest périmée (cache SMB),
        porter le numéro d'une génération DÉJÀ publiée : son discard()
        détruirait le registre/contenu actifs des lecteurs.
        """
        if self._published:
            return
        try:
            current = read_manifest(self.index_dir)
        except RuntimeError:
            # Manifest illisible : impossible de savoir ce qui est référencé —
            # ne rien supprimer (des résidus valent mieux qu'une perte).
            return
        referenced = self._referenced_names(current) if current else set()
        # Les temporaires (.tmp d'une écriture atomique interrompue) et les
        # journaux SQLite (-journal) de CETTE génération partent avec elle
        # (audit V2, A-10) : rien ne les nettoyait, ils survivaient à toutes
        # les rétentions. Un fichier dont la BASE est référencée est épargné.
        for path in (self.registry_path, self.faiss_path, self.delta_path,
                     self.bm25_path, self.content_path,
                     self.faiss_path + ".tmp", self.delta_path + ".tmp",
                     self.bm25_path + ".tmp",
                     self.registry_path + "-journal",
                     self.content_path + "-journal"):
            if _base_name(os.path.basename(path)) in referenced:
                continue
            try:
                os.remove(path)
            except OSError:
                pass

    def publish(self, faiss_ref: Optional[str], bm25_ref: Optional[str],
                content_segments: list, bm25_stale: bool = False,
                bm25_format: Optional[int] = None,
                faiss_deltas: Optional[list] = None) -> int:
        """
        Bascule atomique du manifest vers la nouvelle génération, puis nettoyage.

        Args:
            faiss_ref: nom du fichier FAISS à référencer (celui de cette gén.
                si réécrit, ou celui d'une gén. antérieure si réutilisé, ou None
                si corpus vide).
            bm25_ref: nom du fichier BM25 à référencer (idem, réutilisable).
            content_segments: liste [{file, lo, hi}] des segments de contenu
                actifs pour cette génération.
            bm25_format: format du fichier BM25 référencé quand il vient
                d'être (ré)écrit ; None = BM25 réutilisé d'une génération
                antérieure, dont le format est alors reporté tel quel.
            faiss_deltas: noms ORDONNÉS des deltas de points de reprise à
                charger par-dessus `faiss_ref` (audit perf 2.1, I-4) ; None
                ou vide = aucun (FAISS complet). Ignoré sans FAISS de base.
        """
        manifest = {
            "generation": self.gen,
            "faiss": faiss_ref,
            "bm25": bm25_ref,
            "registry": self.registry_name,
            "content_segments": content_segments,
            "published_by": "rivage-indexer",
        }
        if faiss_ref and faiss_deltas:
            manifest["faiss_deltas"] = list(faiss_deltas)
        if bm25_ref is not None:
            if bm25_format is None and self._prev and bm25_ref == self._prev.get("bm25"):
                bm25_format = self._prev.get("bm25_format")
            if bm25_format is not None:
                manifest["bm25_format"] = int(bm25_format)
        if bm25_stale:
            # Génération intermédiaire dont le BM25 est en retard : persisté
            # pour que le PROCHAIN run publie une finale réparatrice même si le
            # corpus n'a pas changé (audit v2 — sinon un run tué pendant la
            # reconstruction finale laisserait le BM25 périmé pour toujours).
            manifest["bm25_stale"] = True
        tmp = _manifest_path(self.index_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        atomic_replace(tmp, _manifest_path(self.index_dir))
        self._published = True  # dès la bascule : discard() devient no-op
        self.log.info(f"[store] Génération {self.gen} publiée.")
        self._cleanup(manifest)
        return self.gen

    # --- Rétention par dé-référence -----------------------------------------
    #
    # Un segment de contenu est IMMUABLE (mtime figé) et un FAISS/BM25 peut être
    # RÉUTILISÉ sur plusieurs générations (nom de génération ancien). Se fier au
    # mtime ou au numéro de génération du nom pour décider de la suppression est
    # donc faux : un fichier encore référencé par des manifests récents serait
    # supprimé sous un lecteur à peine en retard (B/M1). On mémorise plutôt, par
    # fichier, la DERNIÈRE génération/heure où il a été référencé, et on ne
    # supprime qu'après `keep` générations ET `min_age_s` depuis la dé-référence.

    def _retention_path(self) -> str:
        return os.path.join(self.index_dir, "retention.json")

    def _load_retention(self) -> dict:
        try:
            with open(self._retention_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_retention(self, ret: dict) -> None:
        tmp = self._retention_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(ret, f)
            atomic_replace(tmp, self._retention_path())
        except OSError as e:
            # Visible (audit) : sinon les artefacts orphelins s'accumulent
            # indéfiniment sans que personne ne sache pourquoi.
            self.log.warning(f"[store] retention.json non écrit : {e} — "
                             "le nettoyage des anciennes générations est suspendu.")

    def _referenced_names(self, manifest: dict) -> set:
        ref = {manifest["registry"], MANIFEST, "retention.json"}
        if manifest.get("bm25"):
            ref.add(manifest["bm25"])
        if manifest.get("faiss"):
            ref.add(manifest["faiss"])
            # Deltas de points de reprise (I-4) : conservés tant qu'ils sont
            # référencés, nettoyés comme le FAISS après la fusion finale.
            ref.update(manifest.get("faiss_deltas", []) or [])
        for seg in manifest.get("content_segments", []):
            ref.add(seg["file"])
        return ref

    def _cleanup(
        self, manifest: dict,
        keep: int = _KEEP_GENERATIONS, min_age_s: float = _MIN_GENERATION_AGE_S
    ) -> None:
        """
        Supprime (au mieux) les artefacts qui ne sont plus référencés depuis
        assez longtemps. Un fichier n'est supprimé que s'il n'est PAS référencé
        par le manifest courant, ET que sa dernière référence remonte à au moins
        `keep` générations ET `min_age_s` secondes (protège un lecteur en retard).
        """
        now = time.time()
        referenced = self._referenced_names(manifest)
        ret = self._load_retention()

        # Marquer tous les fichiers référencés comme « vus » à cette génération.
        for name in referenced:
            ret[name] = {"gen": self.gen, "ts": now}

        specs = (("corpus.", ".faiss"), ("bm25.", ".pkl"),
                 ("registre.", ".db"), ("content.", ".db"))
        try:
            names = os.listdir(self.index_dir)
        except OSError:
            self._save_retention(ret)
            return

        for name in names:
            if name in referenced:
                continue
            # Temporaires et journaux orphelins (audit V2, A-10) : `.tmp`
            # d'une écriture atomique interrompue (FAISS/BM25), `-journal`
            # SQLite d'un run tué. Supprimés SANS garde d'âge s'ils
            # appartiennent à une génération STRICTEMENT antérieure à celle
            # qu'on vient de publier (un seul écrivain : rien n'y écrit plus) ;
            # jamais ceux de la génération en cours, jamais ceux dont la base
            # est référencée par le manifest (journal d'un registre publié).
            base = _base_name(name)
            if base != name:
                if not any(base.startswith(p) and base.endswith(s) for p, s in specs):
                    continue
                gen = _generation_of(base)
                if gen is None or gen >= self.gen or base in referenced:
                    continue
                try:
                    os.remove(os.path.join(self.index_dir, name))
                    self.log.debug(f"[store] Résidu orphelin supprimé: {name}")
                except OSError:
                    pass
                continue
            if not any(name.startswith(p) and name.endswith(s) for p, s in specs):
                continue
            entry = ret.get(name)
            if entry is None:
                # Première fois qu'on voit ce fichier non référencé : période de
                # grâce (il sera éligible aux prochains passages).
                ret[name] = {"gen": self.gen, "ts": now}
                continue
            old_enough = (entry.get("gen", self.gen) <= self.gen - keep
                          and now - entry.get("ts", now) >= min_age_s)
            if not old_enough:
                continue
            full = os.path.join(self.index_dir, name)
            try:
                os.remove(full)
                ret.pop(name, None)
                self.log.debug(f"[store] Artefact non référencé supprimé: {name}")
            except OSError:
                pass  # tenu ouvert par un lecteur : on réessaiera

        self._save_retention(ret)
