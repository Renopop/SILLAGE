# registry.py
"""
Registre SQLite - Indexation incrémentale & accès concurrent (§8)

L'ajout régulier de données ne doit jamais imposer de tout ré-encoder. Le
registre identifie chaque fichier par son **contenu** (hash), et non par son
nom ou sa date (§8.1). Il relie chaque vecteur FAISS à son fichier d'origine,
ce qui autorise la suppression ciblée lors d'une modification/suppression
(§8.3).

Deux modes d'ouverture :
  - écrivain (`Registry`)        : une seule machine indexe (§2)
  - lecteur (`ReadOnlyRegistry`) : mode ro + immutable, multi-clients sûrs
    sur partage réseau (§8.5)

Schéma 2 (§8.2 ; audit perf 2.1, I-7 — constat A-9 de l'audit V2) :
    fichiers(id INTEGER PK, hash UNIQUE, chemin, taille, mtime, famille, ocr_pending)
    chunks(faiss_id INTEGER PK, fichier_id, page) + idx_chunks_fichier
    alias(chemin PK, hash, taille, mtime)   — doublons de contenu (audit V2, A-4)
    meta(cle PK, valeur)                    — next_faiss_id, schema

Schéma 1 (générations publiées jusqu'en 2.4, toujours LU par les lecteurs) :
    fichiers(hash PK, chemin, taille, mtime, famille, ocr_pending)
    chunks(id PK, hash_fichier, page, faiss_id UNIQUE) + idx_chunks_hash

Pourquoi le schéma 2 : au schéma 1, chaque fragment portait le hash de son
fichier (64 caractères) dans la table ET dans l'index `idx_chunks_hash`, soit
≈ 230 octets par fragment — 1,1 Go de registre recopié sur le partage à
chaque run et à chaque point de reprise pour 5 M de fragments. Un entier de
fichier (rowid) indexé ramène le fragment à quelques dizaines d'octets (÷3
mesuré sur le registre entier). Le hash reste la clé d'identification des
fichiers (§8.1) : l'API publique (`FileRecord.hash`, `add_chunks(hash, …)`,
`delete_file(hash)`…) ne change pas, la résolution hash -> id est interne.
Les lecteurs servent les DEUX schémas (ils rencontrent des générations
anciennes) ; l'écrivain migre un registre semé au schéma 1 (voir
`migrer_schema_2`) sauf si RIVAGE_REGISTRY_SCHEMA=1 (parc de lecteurs non
mis à jour : un lecteur 2.4 ne lit pas le schéma 2).
"""

import os
import re
import sqlite3
import hashlib
import logging
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple, Iterable

import numpy as np

logger = logging.getLogger(__name__)

_SEP_RE = re.compile(r"[\\/]+")

# Schéma écrit par défaut (audit perf 2.1, I-7). RIVAGE_REGISTRY_SCHEMA=1
# conserve le schéma 1 à l'écriture (registre neuf au schéma 1, registre semé
# au schéma 1 laissé tel quel) tant que des lecteurs 2.4 (ou antérieurs)
# consultent la base ; un registre DÉJÀ au schéma 2 n'est jamais rétrogradé.
SCHEMA_COURANT = 2
_ENV_SCHEMA = "RIVAGE_REGISTRY_SCHEMA"

# Identifiant de fichier d'un fragment ORPHELIN au schéma 2 (fragment
# enregistré sous un hash absent de `fichiers`) : les rowid commencent à 1,
# 0 ne joint jamais — même effet que la jointure du schéma 1 qui ignorait ces
# fragments (ils restent comptés par `live_faiss_ids`, comme avant).
_ORPHELIN = 0

# Jointure fragments -> fichiers, par schéma.
_JOIN = {
    1: "chunks c JOIN fichiers f ON c.hash_fichier = f.hash",
    2: "chunks c JOIN fichiers f ON c.fichier_id = f.id",
}


def schema_cible() -> int:
    """Schéma que l'écrivain produit : `SCHEMA_COURANT`, ou 1 si
    RIVAGE_REGISTRY_SCHEMA=1 (lecteurs non mis à jour)."""
    v = (os.environ.get(_ENV_SCHEMA) or "").strip()
    return 1 if v == "1" else SCHEMA_COURANT


def schema_de_connexion(conn) -> Optional[int]:
    """Schéma du registre ouvert : 2 si `chunks` porte `fichier_id`, 1 si elle
    porte `hash_fichier`, None si la table n'existe pas (base vierge). La
    structure fait foi (pas `meta.schema`, absent des registres anciens et
    sans valeur si la table ne la suit pas)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    if not cols:
        return None
    return 2 if "fichier_id" in cols else 1


def schema_de_fichier(db_path: str) -> Optional[int]:
    """Schéma d'un registre sur disque, lu en mode ro + immutable (un
    registre PUBLIÉ n'est jamais ouvert autrement)."""
    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        return schema_de_connexion(conn)
    finally:
        conn.close()


def migrer_schema_2(conn, log=None, vacuum: bool = True) -> Dict[str, Any]:
    """
    Migre EN PLACE un registre du schéma 1 au schéma 2 (audit perf 2.1, I-7).

    Jamais appelé sur une génération publiée : seulement sur la COPIE semée
    pour la génération en préparation (store.seed_registry), ou sur un
    registre ouvert en écriture hors génération (tests, outils). Une seule
    transaction : nouvelles tables, `chunks.fichier_id` rempli par jointure
    sur le hash (un fragment dont le hash n'est dans aucun fichier reçoit
    `_ORPHELIN`), anciennes tables supprimées (leurs index avec), puis
    `VACUUM` pour rendre les pages libérées (sans lui, le fichier garde la
    taille du schéma 1). Retourne {fichiers, fragments, duree_s}.
    """
    log = log or logger
    t0 = time.perf_counter()
    if schema_de_connexion(conn) != 1:
        raise ValueError("migrer_schema_2 : le registre n'est pas au schéma 1")
    # Registre antérieur au rattrapage OCR : colonne ajoutée avant la copie.
    try:
        conn.execute(
            "ALTER TABLE fichiers ADD COLUMN ocr_pending INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE fichiers_v2 (
            id          INTEGER PRIMARY KEY,
            hash        TEXT NOT NULL UNIQUE,
            chemin      TEXT,
            taille      INTEGER,
            mtime       REAL,
            famille     TEXT,
            ocr_pending INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO fichiers_v2(hash, chemin, taille, mtime, famille, ocr_pending)
            SELECT hash, chemin, taille, mtime, famille, COALESCE(ocr_pending, 0)
            FROM fichiers WHERE hash IS NOT NULL ORDER BY rowid;
        CREATE TABLE chunks_v2 (
            faiss_id   INTEGER PRIMARY KEY,
            fichier_id INTEGER NOT NULL,
            page       INTEGER
        );
        INSERT INTO chunks_v2(faiss_id, fichier_id, page)
            SELECT c.faiss_id, COALESCE(f.id, 0), c.page
            FROM chunks c LEFT JOIN fichiers_v2 f ON f.hash = c.hash_fichier
            WHERE c.faiss_id IS NOT NULL ORDER BY c.faiss_id;
        DROP TABLE chunks;
        DROP TABLE fichiers;
        ALTER TABLE fichiers_v2 RENAME TO fichiers;
        ALTER TABLE chunks_v2 RENAME TO chunks;
        CREATE INDEX idx_chunks_fichier ON chunks(fichier_id);
        CREATE INDEX idx_fichiers_chemin ON fichiers(chemin);
        CREATE INDEX idx_fichiers_famille ON fichiers(famille);
        CREATE TABLE IF NOT EXISTS meta (cle TEXT PRIMARY KEY, valeur TEXT);
        INSERT OR REPLACE INTO meta(cle, valeur) VALUES('schema', '2');
        COMMIT;
        """
    )
    n_fichiers = conn.execute("SELECT COUNT(*) FROM fichiers").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if vacuum:
        # Hors transaction (exigence SQLite) : compacte le fichier.
        conn.execute("VACUUM")
    return {"fichiers": int(n_fichiers), "fragments": int(n_chunks),
            "duree_s": time.perf_counter() - t0}


def migrer_fichier_schema_2(db_path: str, log=None) -> Dict[str, Any]:
    """`migrer_schema_2` sur un fichier (ouvert puis refermé ici) ; ajoute
    les tailles avant/après en octets au compte rendu. Le journal SQLite est
    coupé pendant la migration : le fichier n'est pas encore publié et serait
    recréé à neuf en cas d'échec (même raisonnement que les segments de
    contenu, audit V2, A-6)."""
    avant = os.path.getsize(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.isolation_level = None            # scripts BEGIN/COMMIT explicites
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        res = migrer_schema_2(conn, log=log)
    finally:
        conn.close()
    res["avant_octets"] = avant
    res["apres_octets"] = os.path.getsize(db_path)
    return res


def _message_migration(res: Dict[str, Any], detail: str = "") -> str:
    avant = res.get("avant_octets")
    apres = res.get("apres_octets")
    taille = (f", {avant / 2 ** 20:.1f} -> {apres / 2 ** 20:.1f} Mo"
              if avant is not None and apres is not None else "")
    return (f"Registre migré au schéma 2 ({res['fragments']} fragments, "
            f"{res['fichiers']} fichiers{taille}, {res['duree_s']:.1f} s"
            f"{', ' + detail if detail else ''}).")


def norm_path(p) -> str:
    """Chemin normalisé pour comparaison de portée : séparateurs unifiés en `/`,
    slash final retiré, casse abaissée (chemins insensibles à la casse sous
    Windows / partages réseau). Utilisé par le filtre par préfixe de répertoire
    et par la construction de l'arbre côté lecteur (même normalisation des deux
    côtés = correspondance fiable)."""
    return _SEP_RE.sub("/", str(p or "")).rstrip("/").lower()


def _norm_paths(chemins: List[str]) -> List[str]:
    """`norm_path` appliqué à une LISTE en trois opérations sur une seule
    chaîne (jointure, substitution, abaissement de casse) au lieu d'une
    expression régulière par chemin — même résultat, élément par élément
    (audit perf 2.1, L-1 : 200 000 chemins en 0,1 s au lieu de 0,5). Repli
    sur l'appel unitaire si un chemin contient le séparateur de jointure."""
    textes = [str(c or "") for c in chemins]
    if any("\n" in t for t in textes):
        return [norm_path(t) for t in textes]
    if not textes:
        return []
    # `[\\/]+` -> `/` sans expression régulière (une substitution par
    # séparateur coûtait 1 s par 200 000 chemins) : backslashes unifiés, puis
    # les suites de `/` réduites par remplacements successifs (chaque passe
    # divise par deux la longueur des suites restantes).
    s = "\n".join(textes).replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    return [p.rstrip("/") for p in s.lower().split("\n")]


# =============================================================================
#  EMPREINTE DE CONTENU (§8.1)
# =============================================================================

def hash_file(path: str, blocksize: int = 1 << 20) -> str:
    """
    Hash SHA-256 du contenu d'un fichier (§8.1).
    Deux fichiers identiques donnent le même hash, où qu'ils soient.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloc in iter(lambda: f.read(blocksize), b""):
            h.update(bloc)
    return h.hexdigest()


# =============================================================================
#  MODÈLES
# =============================================================================

@dataclass
class FileRecord:
    hash: str
    chemin: str
    taille: int
    mtime: float
    famille: str
    # 1 si le fichier a été indexé SANS OCR alors qu'il en avait besoin (scan
    # sous le seuil de qualité, PPTX à images…) : un run ultérieur avec l'OCR
    # coché le RETRAITE automatiquement (rattrapage — voir indexer).
    ocr_pending: int = 0


@dataclass
class AliasRecord:
    """Doublon de contenu (audit V2, A-4) : un chemin dont le contenu est
    DÉJÀ indexé sous un autre chemin (même hash). Mémoriser (taille, mtime)
    permet au pré-filtre de le reconnaître sans le re-hacher à chaque run."""
    chemin: str
    hash: str
    taille: int
    mtime: float


@dataclass
class ChunkRecord:
    id: int
    hash_fichier: str
    texte: str
    page: Optional[int]
    faiss_id: int


# =============================================================================
#  ÉCRIVAIN
# =============================================================================

class Registry:
    """
    Registre en écriture — utilisé par la seule machine d'indexation (§2).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # Écritures de MÉTADONNÉES seules depuis l'ouverture (rafraîchissement
        # taille/mtime d'un fichier « touché », alias de doublon) : l'indexeur
        # les compte comme une activité à PUBLIER — sinon un run sans autre
        # changement jetterait la génération et ces mises à jour avec elle,
        # et le fichier serait re-haché à chaque run (audit V2, A-3 / A-4).
        self.meta_changes = 0
        # Schéma de CE registre (1 ou 2) : fixé à l'ouverture, voir _init_schema.
        self.schema = SCHEMA_COURANT
        self._init_schema()

    # Schéma 1 (jusqu'en 2.4) : le hash du fichier répété par fragment.
    _SCHEMA_1_SQL = """
            CREATE TABLE IF NOT EXISTS fichiers (
                hash        TEXT PRIMARY KEY,
                chemin      TEXT,
                taille      INTEGER,
                mtime       REAL,
                famille     TEXT,
                ocr_pending INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id           INTEGER PRIMARY KEY,
                hash_fichier TEXT,
                page         INTEGER,
                faiss_id     INTEGER UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(hash_fichier);
            -- Pas d'index explicite sur chunks(faiss_id) : la contrainte UNIQUE
            -- en crée déjà un (sqlite_autoindex) ; l'ancien idx_chunks_faiss le
            -- doublait et grossissait le registre recopié à chaque génération
            -- (audit V2, A-9). Retiré des registres hérités ci-dessous.
    """
    # Schéma 2 (audit perf 2.1, I-7) : le fichier est désigné par son rowid.
    # `faiss_id` EST le rowid de `chunks` (identifiant stable et unique, §8.3) :
    # pas d'index secondaire pour le retrouver ; le seul index est celui de
    # `fichier_id` (suppression ciblée d'un fichier, filtres de portée).
    _SCHEMA_2_SQL = """
            CREATE TABLE IF NOT EXISTS fichiers (
                id          INTEGER PRIMARY KEY,
                hash        TEXT NOT NULL UNIQUE,
                chemin      TEXT,
                taille      INTEGER,
                mtime       REAL,
                famille     TEXT,
                ocr_pending INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS chunks (
                faiss_id   INTEGER PRIMARY KEY,
                fichier_id INTEGER NOT NULL,
                page       INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_fichier ON chunks(fichier_id);
    """

    def _init_schema(self) -> None:
        # Le texte des chunks n'est PLUS stocké ici (S1) : il vit dans des
        # segments de contenu immuables (content_store.py). Le registre ne
        # garde que des métadonnées, ce qui rend sa copie par génération
        # quasi gratuite.
        existant = schema_de_connexion(self.conn)
        cible = schema_cible()
        if existant == 1 and cible == 2:
            # Registre semé au schéma 1 que store.seed_registry n'a pas migré
            # (copie locale impossible, ou ouverture directe hors génération) :
            # migration EN PLACE, ici. Jamais une génération publiée : un
            # Registry n'ouvre que la copie de la génération en préparation.
            res = migrer_schema_2(self.conn, log=logger)
            logger.info("[registry] " + _message_migration(res, "en place"))
            existant = 2
        elif existant == 2 and cible == 1:
            logger.warning(f"[registry] {_ENV_SCHEMA}=1 ignoré : le registre est "
                           "déjà au schéma 2 (pas de rétrogradation).")
        self.schema = existant or cible
        self.conn.executescript(
            (self._SCHEMA_2_SQL if self.schema == 2 else self._SCHEMA_1_SQL) +
            """
            CREATE INDEX IF NOT EXISTS idx_fichiers_chemin ON fichiers(chemin);
            CREATE INDEX IF NOT EXISTS idx_fichiers_famille ON fichiers(famille);
            CREATE TABLE IF NOT EXISTS meta (
                cle    TEXT PRIMARY KEY,
                valeur TEXT
            );
            -- Doublons de contenu (audit V2, A-4) : chemins dont le contenu est
            -- déjà indexé sous un autre chemin. Table ADDITIVE (aucune
            -- migration destructive) ; disjointe de `fichiers` (un chemin n'est
            -- jamais dans les deux). Le pré-filtre la consulte : (taille, mtime)
            -- identiques => « inchangé » sans re-hachage.
            CREATE TABLE IF NOT EXISTS alias (
                chemin TEXT PRIMARY KEY,
                hash   TEXT,
                taille INTEGER,
                mtime  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_alias_hash ON alias(hash);
            DROP INDEX IF EXISTS idx_chunks_faiss;
            """
        )
        # Migration des registres antérieurs (semés depuis une génération
        # publiée avant l'ajout du rattrapage OCR) : colonne ajoutée en
        # douceur, erreur « duplicate column » ignorée.
        try:
            self.conn.execute(
                "ALTER TABLE fichiers ADD COLUMN ocr_pending INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        # Compteur d'identifiants FAISS stables (jamais réutilisés)
        cur = self.conn.execute("SELECT valeur FROM meta WHERE cle='next_faiss_id'")
        if cur.fetchone() is None:
            self.conn.execute(
                "INSERT INTO meta(cle, valeur) VALUES('next_faiss_id', '0')"
            )
        if self.schema == 2:
            # Informatif (la structure de `chunks` fait foi à la lecture).
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(cle, valeur) VALUES('schema', '2')")
        self.conn.commit()

    def _id_fichier(self, file_hash: str) -> Optional[int]:
        """Rowid du fichier de ce hash (schéma 2), None s'il est inconnu."""
        row = self.conn.execute(
            "SELECT id FROM fichiers WHERE hash=?", (file_hash,)).fetchone()
        return int(row[0]) if row else None

    # --- Compteur d'ids FAISS -------------------------------------------------

    def peek_next_faiss_id(self) -> int:
        """Prochain identifiant FAISS à allouer (sans réserver)."""
        cur = self.conn.execute("SELECT valeur FROM meta WHERE cle='next_faiss_id'")
        return int(cur.fetchone()[0])

    def allocate_faiss_ids(self, n: int) -> List[int]:
        """Réserve n identifiants FAISS stables et monotones."""
        cur = self.conn.execute("SELECT valeur FROM meta WHERE cle='next_faiss_id'")
        start = int(cur.fetchone()[0])
        new_next = start + n
        self.conn.execute(
            "UPDATE meta SET valeur=? WHERE cle='next_faiss_id'", (str(new_next),)
        )
        return list(range(start, new_next))

    # --- Pré-filtre incrémental (§8.1) ---------------------------------------

    def get_file_by_hash(self, file_hash: str) -> Optional[FileRecord]:
        row = self.conn.execute(
            "SELECT * FROM fichiers WHERE hash=?", (file_hash,)
        ).fetchone()
        return _to_file(row) if row else None

    def get_file_by_path(self, chemin: str) -> Optional[FileRecord]:
        row = self.conn.execute(
            "SELECT * FROM fichiers WHERE chemin=?", (chemin,)
        ).fetchone()
        return _to_file(row) if row else None

    def classify(self, path: str) -> Tuple[str, Optional[str]]:
        """
        Décide du sort d'un fichier via le pré-filtre à deux niveaux (§8.1).

        1. Si (chemin, taille, mtime) identique au registre -> 'unchanged'.
        2. Sinon on calcule le hash de contenu :
             - hash déjà connu au même chemin   -> 'unchanged'
             - ce chemin avait un autre contenu -> 'modified' (retirer anciens vecteurs)
             - hash connu à un autre chemin     -> 'moved'    (maj chemin, pas de ré-encodage)
             - hash inconnu, chemin absent      -> 'new'

        L'ordre est important : on teste d'abord si le CONTENU de ce chemin a
        changé (-> 'modified', pour purger ses anciens vecteurs) AVANT de
        conclure à un déplacement. Sinon un fichier édité pour devenir identique
        à un autre serait classé 'moved' et ses anciens vecteurs resteraient
        orphelins dans l'index (M10).

        Returns:
            (statut, hash_ou_None). hash None seulement si 'unchanged' par pré-filtre.
        """
        try:
            stat = os.stat(path)
        except OSError:
            return ("missing", None)

        existing_path = self.get_file_by_path(path)
        if existing_path and existing_path.taille == stat.st_size \
                and abs(existing_path.mtime - stat.st_mtime) < 1e-6:
            return ("unchanged", existing_path.hash)

        # Doublon de contenu connu (audit V2, A-4) : même (taille, mtime) que
        # lors de son enregistrement comme alias -> inchangé, SANS hachage.
        # Le chemin original est vérifié par la purge finale (promotion de
        # l'alias s'il a disparu), pas ici : le pré-filtre doit rester
        # un simple stat.
        if existing_path is None:
            alias = self.get_alias(path)
            if alias and alias.taille == stat.st_size \
                    and abs(alias.mtime - stat.st_mtime) < 1e-6:
                return ("unchanged", alias.hash)

        file_hash = hash_file(path)

        # Ce chemin est déjà connu avec un contenu différent -> modifié.
        if existing_path and existing_path.hash != file_hash:
            return ("modified", file_hash)

        by_hash = self.get_file_by_hash(file_hash)
        if by_hash:
            if by_hash.chemin == path:
                # Fichier « touché » (mtime changé, contenu identique — robocopy
                # sans horodatage, migration de serveur) : RAFRAÎCHIR taille et
                # mtime pour que le pré-filtre réussisse au run suivant, sinon
                # tout le corpus est relu sur le partage à chaque run (audit
                # V2, A-3).
                self.refresh_stat(file_hash, stat.st_size, stat.st_mtime)
                return ("unchanged", file_hash)
            return ("moved", file_hash)

        return ("new", file_hash)

    # --- Écritures ------------------------------------------------------------

    def upsert_file(self, rec: FileRecord) -> None:
        self.conn.execute(
            """INSERT INTO fichiers(hash, chemin, taille, mtime, famille,
                                    ocr_pending)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(hash) DO UPDATE SET
                 chemin=excluded.chemin, taille=excluded.taille,
                 mtime=excluded.mtime, famille=excluded.famille,
                 ocr_pending=excluded.ocr_pending""",
            (rec.hash, rec.chemin, rec.taille, rec.mtime, rec.famille,
             int(rec.ocr_pending or 0)),
        )
        # Un chemin qui entre dans `fichiers` cesse d'être un alias (A-4).
        self.conn.execute("DELETE FROM alias WHERE chemin=?", (rec.chemin,))

    def refresh_stat(self, file_hash: str, taille: int, mtime: float) -> None:
        """Fichier « touché » (contenu identique, mtime changé) : met à jour
        (taille, mtime) pour que le pré-filtre réussisse ensuite (A-3)."""
        self.conn.execute(
            "UPDATE fichiers SET taille=?, mtime=? WHERE hash=?",
            (taille, mtime, file_hash),
        )
        self.meta_changes += 1

    # --- Doublons de contenu (audit V2, A-4) ---------------------------------

    def get_alias(self, chemin: str) -> Optional[AliasRecord]:
        row = self.conn.execute(
            "SELECT chemin, hash, taille, mtime FROM alias WHERE chemin=?",
            (chemin,),
        ).fetchone()
        return AliasRecord(row[0], row[1], row[2], row[3]) if row else None

    def upsert_alias(self, chemin: str, file_hash: str, taille: int,
                     mtime: float) -> None:
        """Enregistre (ou rafraîchit) un doublon de contenu : `chemin` a le
        même contenu que le fichier indexé sous `file_hash`."""
        self.conn.execute(
            """INSERT INTO alias(chemin, hash, taille, mtime) VALUES(?,?,?,?)
               ON CONFLICT(chemin) DO UPDATE SET
                 hash=excluded.hash, taille=excluded.taille,
                 mtime=excluded.mtime""",
            (chemin, file_hash, taille, mtime),
        )
        self.meta_changes += 1

    def aliases_for_hash(self, file_hash: str) -> List[AliasRecord]:
        """Alias (doublons) d'un contenu, par chemin croissant (déterministe)."""
        rows = self.conn.execute(
            "SELECT chemin, hash, taille, mtime FROM alias WHERE hash=? "
            "ORDER BY chemin", (file_hash,),
        ).fetchall()
        return [AliasRecord(r[0], r[1], r[2], r[3]) for r in rows]

    def all_aliases(self) -> Dict[str, str]:
        """{chemin: hash} de tous les alias enregistrés."""
        rows = self.conn.execute("SELECT chemin, hash FROM alias").fetchall()
        return {r[0]: r[1] for r in rows}

    def delete_alias(self, chemin: str) -> None:
        cur = self.conn.execute("DELETE FROM alias WHERE chemin=?", (chemin,))
        if cur.rowcount:
            self.meta_changes += 1

    def purge_orphan_aliases(self) -> int:
        """Retire les alias dont le contenu n'est plus indexé (défense : un
        alias ne doit jamais désigner un hash absent de `fichiers`)."""
        cur = self.conn.execute(
            "DELETE FROM alias WHERE hash NOT IN (SELECT hash FROM fichiers)")
        n = cur.rowcount or 0
        self.meta_changes += n
        return n

    def is_ocr_pending(self, file_hash: str) -> bool:
        """Vrai si le fichier attend un rattrapage OCR (indexé sans OCR alors
        qu'il en avait besoin)."""
        row = self.conn.execute(
            "SELECT ocr_pending FROM fichiers WHERE hash=?", (file_hash,)
        ).fetchone()
        return bool(row and row[0])

    def count_ocr_pending(self) -> int:
        """Nombre de fichiers en attente de rattrapage OCR (visibilité coût)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM fichiers WHERE ocr_pending=1"
        ).fetchone()[0]

    def update_path(self, file_hash: str, new_path: str, mtime: float, famille: str) -> None:
        """Fichier déplacé : maj du chemin sans ré-encodage (§8.1)."""
        self.conn.execute(
            "UPDATE fichiers SET chemin=?, mtime=?, famille=? WHERE hash=?",
            (new_path, mtime, famille, file_hash),
        )
        # Le nouveau chemin n'est plus un alias (A-4 : tables disjointes).
        self.conn.execute("DELETE FROM alias WHERE chemin=?", (new_path,))

    def add_chunks(self, file_hash: str,
                   rows: Iterable[Tuple[Optional[int], int]]) -> None:
        """
        Enregistre les métadonnées des chunks d'un fichier.
        rows : itérable de (page, faiss_id). Le TEXTE va dans le segment de
        contenu (content_store.py), pas ici (S1).
        """
        if self.schema == 2:
            # Le fichier est désigné par son rowid (I-7) ; un hash inconnu
            # (fragment orphelin) reçoit `_ORPHELIN`, jamais joint.
            fid = self._id_fichier(file_hash)
            fid = _ORPHELIN if fid is None else fid
            self.conn.executemany(
                "INSERT INTO chunks(faiss_id, fichier_id, page) VALUES(?,?,?)",
                [(faiss_id, fid, page) for (page, faiss_id) in rows],
            )
            return
        self.conn.executemany(
            "INSERT INTO chunks(hash_fichier, page, faiss_id) VALUES(?,?,?)",
            [(file_hash, page, faiss_id) for (page, faiss_id) in rows],
        )

    def live_faiss_ids(self) -> List[int]:
        """Tous les faiss_id vivants (référencés par un chunk)."""
        return [r[0] for r in self.conn.execute("SELECT faiss_id FROM chunks")]

    def count_faiss_in_range(self, lo: int, hi: int) -> int:
        """Nombre de faiss_id vivants dans [lo, hi) — pour le GC des segments."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE faiss_id >= ? AND faiss_id < ?", (lo, hi)
        ).fetchone()[0]

    def get_faiss_ids_for_hash(self, file_hash: str) -> List[int]:
        if self.schema == 2:
            fid = self._id_fichier(file_hash)
            if fid is None:
                return []
            rows = self.conn.execute(
                "SELECT faiss_id FROM chunks WHERE fichier_id=?", (fid,)
            ).fetchall()
            return [r[0] for r in rows]
        rows = self.conn.execute(
            "SELECT faiss_id FROM chunks WHERE hash_fichier=?", (file_hash,)
        ).fetchall()
        return [r[0] for r in rows]

    def delete_file(self, file_hash: str) -> List[int]:
        """
        Supprime un fichier et ses chunks du registre.
        Retourne les faiss_id à retirer de l'index vectoriel (§8.3).
        """
        faiss_ids = self.get_faiss_ids_for_hash(file_hash)
        if self.schema == 2:
            # Les fragments AVANT le fichier : la jointure passe par son id.
            self.conn.execute(
                "DELETE FROM chunks WHERE fichier_id IN "
                "(SELECT id FROM fichiers WHERE hash=?)", (file_hash,))
        else:
            self.conn.execute("DELETE FROM chunks WHERE hash_fichier=?", (file_hash,))
        self.conn.execute("DELETE FROM fichiers WHERE hash=?", (file_hash,))
        # Ses doublons ne désignent plus rien d'indexé (A-4) : ils seront
        # re-hachés (et réindexés s'ils existent encore) au prochain passage.
        self.conn.execute("DELETE FROM alias WHERE hash=?", (file_hash,))
        return faiss_ids

    # --- Requêtes utilitaires -------------------------------------------------

    def all_hashes(self) -> Dict[str, str]:
        """Retourne {hash: chemin} de tous les fichiers indexés."""
        rows = self.conn.execute("SELECT hash, chemin FROM fichiers").fetchall()
        return {r["hash"]: r["chemin"] for r in rows}

    def all_files(self) -> Dict[str, Tuple[str, str]]:
        """Retourne {hash: (chemin, famille)} de tous les fichiers indexés."""
        rows = self.conn.execute("SELECT hash, chemin, famille FROM fichiers").fetchall()
        return {r["hash"]: (r["chemin"], r["famille"]) for r in rows}

    def list_families(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT famille FROM fichiers WHERE famille IS NOT NULL ORDER BY famille"
        ).fetchall()
        return [r[0] for r in rows]

    def all_chunks(self) -> List[Dict[str, Any]]:
        """
        Métadonnées de tous les chunks (faiss_id, page, chemin, famille) — SANS
        texte (il est dans les segments de contenu). Le texte pour BM25 est
        récupéré séparément via ContentStore.
        """
        rows = self.conn.execute(
            f"""SELECT c.page, c.faiss_id, f.chemin, f.famille
                FROM {_JOIN[self.schema]}
                ORDER BY c.faiss_id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict[str, int]:
        n_files = self.conn.execute("SELECT COUNT(*) FROM fichiers").fetchone()[0]
        n_chunks = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"files": n_files, "chunks": n_chunks}

    def commit(self) -> None:
        if self.conn is not None:
            self.conn.commit()

    def close(self, commit: bool = True) -> None:
        # Idempotent : un double close ne doit pas lever de ProgrammingError
        # (qui masquerait de vraies erreurs de fermeture) — n6.
        # `commit=False` pour la fermeture d'un run en ÉCHEC (audit) : ne pas
        # figer un état partiel dans une base qui sera de toute façon jetée.
        if self.conn is not None:
            if commit:
                self.conn.commit()
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        self.close()


# =============================================================================
#  LECTEUR (mode ro + immutable, §8.5)
# =============================================================================

class ReadOnlyRegistry:
    """
    Registre en lecture seule — plusieurs lecteurs simultanés sur réseau (§8.5).

    Ouvre en mode `ro` avec `immutable=1` : SQLite considère le fichier comme
    immuable pendant la lecture, ce qui supprime toute mécanique de
    verrouillage réseau. Le fichier d'une génération est immuable PAR NOM :
    une connexion reste valable tant que le manifest désigne le même registre
    — le cache lecteur (index_cache) la conserve d'une question à l'autre
    (audit perf 2.1, L-9) ; un appelant direct rouvre à chaque requête pour
    voir la dernière version publiée (§8.5).

    `partage_threads` : la connexion pourra être UTILISÉE par un autre thread
    que celui qui l'a ouverte (jamais par deux à la fois — c'est le contrat
    du prêt d'index_cache : un retriever, un thread, puis restitution). Le
    contrôle de thread de sqlite3 est alors désactivé ; la base, immuable,
    n'a rien à perdre.
    """

    def __init__(self, db_path: str, partage_threads: bool = False,
                 cache_kio: Optional[int] = None):
        # Construire une URI file:// valide et portable (chemins locaux,
        # Windows `C:\...`, et UNC `\\serveur\partage\...`). Une simple
        # interpolation `file:{db_path}` casse sur les backslashes et les
        # chemins UNC (B2) ; pathlib.as_uri() les encode correctement.
        uri = _sqlite_ro_uri(db_path)
        self.db_path = db_path
        self.conn = sqlite3.connect(uri, uri=True,
                                    check_same_thread=not partage_threads)
        self.conn.row_factory = sqlite3.Row
        if cache_kio:
            # Cache de pages SQLite élargi (défaut 2 Mo) pour une connexion
            # CONSERVÉE entre les questions (audit perf 2.1, L-9) : les pages
            # d'index du registre (faiss_id -> fichier) restent en mémoire
            # d'une question à l'autre au lieu d'être relues sur le partage.
            self.conn.execute(f"PRAGMA cache_size=-{int(cache_kio)}")
        # Les lecteurs rencontrent des générations publiées au schéma 1
        # (jusqu'en 2.4) comme au schéma 2 (audit perf 2.1, I-7) : le schéma
        # est détecté une fois, chaque requête prend sa forme.
        self.schema = schema_de_connexion(self.conn) or SCHEMA_COURANT

    def portee(self) -> "Portee":
        """Structure de portée précalculée de ce registre (audit perf 2.1,
        L-1) : voir `Portee`. À construire une fois par génération."""
        return Portee.depuis_connexion(self.conn)

    def get_chunk_by_faiss_id(self, faiss_id: int) -> Optional[Dict[str, Any]]:
        """Métadonnées d'un chunk (SANS texte : le texte vient du ContentStore)."""
        row = self.conn.execute(
            f"""SELECT c.page, c.faiss_id, f.chemin, f.famille
                FROM {_JOIN[self.schema]}
                WHERE c.faiss_id=?""",
            (faiss_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_chunks_by_faiss_ids(self, faiss_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Métadonnées par faiss_id (SANS texte : hydrater via ContentStore)."""
        ids = list(faiss_ids)
        out: Dict[int, Dict[str, Any]] = {}
        # Découper par paquets pour rester sous la limite de variables SQLite (999).
        for start in range(0, len(ids), 900):
            batch = ids[start:start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"""SELECT c.page, c.faiss_id, f.chemin, f.famille
                    FROM {_JOIN[self.schema]}
                    WHERE c.faiss_id IN ({placeholders})""",
                batch,
            ).fetchall()
            for r in rows:
                out[r["faiss_id"]] = dict(r)
        return out

    def faiss_ids_for_families(self, families: List[str]) -> List[int]:
        """
        Ids FAISS des chunks appartenant aux familles données — support du
        filtre par thème appliqué avant le retrieval (§10.2).
        """
        if not families:
            return []
        out: List[int] = []
        fam = list(families)
        for start in range(0, len(fam), 900):
            batch = fam[start:start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"""SELECT c.faiss_id FROM {_JOIN[self.schema]}
                    WHERE f.famille IN ({placeholders})""",
                batch,
            ).fetchall()
            out.extend(r[0] for r in rows)
        return out

    def faiss_ids_for_path_prefixes(self, prefixes) -> List[int]:
        """
        Ids FAISS des chunks dont le fichier est SOUS l'un des répertoires donnés
        — filtre de portée hiérarchique (recherche ciblée dans un sous-arbre).

        Généralise le filtre par thème : un « thème » n'est que le préfixe de 1er
        niveau. Aucune réindexation : le registre stocke déjà `chemin`. La
        comparaison est normalisée (séparateurs `/`/`\\`, casse) pour être robuste
        entre Windows et partages réseau.
        """
        pfxs = [norm_path(p) + "/" for p in (prefixes or []) if p and str(p).strip()]
        if not pfxs:
            return []
        # Audit V2, A-7 : filtrer d'abord la table `fichiers` (50 fois moins
        # de lignes que `chunks`, une normalisation par FICHIER et non par
        # fragment), puis les ids des fragments par l'index de `chunks` (hash
        # au schéma 1, identifiant de fichier au schéma 2 — I-7).
        # Mesuré : 500 000 fragments, 2,3 s -> quelques dizaines de ms.
        cle = "id" if self.schema == 2 else "hash"
        cles = [c for c, chemin in self.conn.execute(f"SELECT {cle}, chemin FROM fichiers")
                if any(norm_path(chemin).startswith(p) for p in pfxs)]
        if self.schema == 2:
            return self._faiss_ids_for_ids(cles)
        return self._faiss_ids_for_hashes(cles)

    def _hashes_par_chemin(self, paths) -> Dict[str, List[str]]:
        """
        {chemin normalisé: [hash, …]} des fichiers donnés présents dans l'index.

        Audit perf 2.1, L-3 : l'ancien chemin normalisait TOUS les chemins de
        la table à chaque appel (0,8 s par question « dans ma base » à
        200 000 fichiers) alors que l'index `idx_fichiers_chemin` existe.
        Désormais :
          1. requête EXACTE `WHERE chemin IN (…)` sur le chemin tel
             qu'enregistré (paquets de 900, limite de variables SQLite) —
             le cas courant, une base de recherche mémorise les chemins du
             registre tels quels : quelques dizaines de µs ;
          2. scan normalisé (casse, séparateurs) UNIQUEMENT pour les chemins
             non trouvés, interrompu dès que tout est trouvé.
        Résultat identique à la comparaison normalisée historique.
        """
        bruts: Dict[str, str] = {}             # chemin normalisé -> forme brute
        for p in paths or []:
            if p and str(p).strip():
                bruts.setdefault(norm_path(p), str(p))
        out: Dict[str, List[str]] = {}
        if not bruts:
            return out
        exacts = list(dict.fromkeys(bruts.values()))
        for start in range(0, len(exacts), 900):
            batch = exacts[start:start + 900]
            placeholders = ",".join("?" * len(batch))
            for h, chemin in self.conn.execute(
                    f"SELECT hash, chemin FROM fichiers WHERE chemin IN ({placeholders})",
                    batch):
                out.setdefault(norm_path(chemin), []).append(h)
        restants = set(bruts) - set(out)
        if restants:
            for h, chemin in self.conn.execute("SELECT hash, chemin FROM fichiers"):
                cn = norm_path(chemin)
                if cn in restants:
                    out.setdefault(cn, []).append(h)
            # Pas d'arrêt anticipé ici : un même chemin normalisé peut porter
            # plusieurs enregistrements (casse distincte sur un système
            # sensible à la casse) — tous sont voulus, comme auparavant.
        return out

    def faiss_ids_for_paths(self, paths) -> List[int]:
        """
        Ids FAISS de TOUS les chunks des fichiers donnés (chemins exacts,
        comparaison normalisée) — le filtre d'une recherche restreinte aux
        documents d'une base de recherche (mode auto-RAG, §6.7) : grain
        document, tous les fragments d'un document repéré sont interrogeables.
        Requête par l'index des chemins puis `chunks` par hash (audit perf
        2.1, L-3 ; même stratégie `fichiers` d'abord qu'en A-7).
        """
        hashes = [h for hs in self._hashes_par_chemin(paths).values() for h in hs]
        if not hashes:
            return []
        return self._faiss_ids_for_hashes(hashes)

    def paths_present(self, paths) -> set:
        """
        Sous-ensemble (normalisé, voir `norm_path`) des chemins donnés qui
        figurent dans l'index — vérification d'une base de recherche sans
        matérialiser la table `fichiers` entière (audit V2, A-7 / E-5 ;
        requête par l'index des chemins, audit perf 2.1, L-3).
        """
        return set(self._hashes_par_chemin(paths))

    def _faiss_ids_for_hashes(self, hashes: List[str]) -> List[int]:
        """Ids FAISS des chunks des fichiers donnés (par hash, paquets de 900
        pour rester sous la limite de variables SQLite). Schéma 1 : index
        idx_chunks_hash ; schéma 2 (I-7) : hash -> rowid par l'index unique
        de `fichiers`, puis idx_chunks_fichier — même résultat."""
        out: List[int] = []
        if self.schema == 2:
            sql = ("SELECT faiss_id FROM chunks WHERE fichier_id IN "
                   "(SELECT id FROM fichiers WHERE hash IN ({}))")
        else:
            sql = "SELECT faiss_id FROM chunks WHERE hash_fichier IN ({})"
        for start in range(0, len(hashes), 900):
            batch = hashes[start:start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(sql.format(placeholders), batch).fetchall()
            out.extend(r[0] for r in rows)
        return out

    def _faiss_ids_for_ids(self, ids: List[int]) -> List[int]:
        """Ids FAISS des chunks des fichiers donnés par rowid (schéma 2,
        index idx_chunks_fichier ; paquets de 900)."""
        out: List[int] = []
        for start in range(0, len(ids), 900):
            batch = ids[start:start + 900]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT faiss_id FROM chunks WHERE fichier_id IN ({placeholders})",
                batch,
            ).fetchall()
            out.extend(r[0] for r in rows)
        return out

    def list_files(self) -> List[Tuple[str, str]]:
        """(chemin, famille) de tous les fichiers indexés — pour bâtir l'arbre
        des répertoires côté lecteur (sans accès au corpus)."""
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT chemin, famille FROM fichiers ORDER BY chemin"
        )]

    def rechercher_chemins(self, motif: str, limite: int = 200) -> Tuple[List[Tuple[str, str]], bool]:
        """Fichiers de l'index dont le chemin contient TOUS les mots de
        `motif` (séparés par des espaces ; casse ignorée, séparateurs `\\`
        et `/` confondus — même normalisation que `norm_path`) : la
        recherche par nom de « Ajouter un document… » à la base de recherche
        (2.6). Retourne ([(chemin tel qu'enregistré, famille), …] triés par
        chemin, au plus `limite`, et un booléen « liste tronquée »). Un motif
        vide ne retourne rien (un index entier n'est pas une liste de choix).
        Parcours de la table `fichiers` en Python (comparaison Unicode exacte,
        contrairement à `lower()` de SQLite, ASCII seulement) : une action
        explicite de l'utilisateur, pas une opération par question."""
        mots = [norm_path(m) for m in (motif or "").split() if m.strip()]
        mots = [m for m in mots if m]
        if not mots:
            return [], False
        limite = max(1, int(limite))
        out: List[Tuple[str, str]] = []
        tronque = False
        cur = self.conn.execute("SELECT chemin, famille FROM fichiers ORDER BY chemin")
        for chemin, famille in cur:
            n = norm_path(chemin)
            if all(m in n for m in mots):
                if len(out) >= limite:
                    tronque = True
                    break
                out.append((chemin, famille))
        return out, tronque

    def all_chunks(self) -> List[Dict[str, Any]]:
        """Métadonnées de tous les chunks (SANS texte : voir ContentStore)."""
        rows = self.conn.execute(
            f"""SELECT c.page, c.faiss_id, f.chemin, f.famille
                FROM {_JOIN[self.schema]}
                ORDER BY c.faiss_id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def list_families(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT famille FROM fichiers WHERE famille IS NOT NULL ORDER BY famille"
        ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


# =============================================================================
#  PORTÉE PRÉCALCULÉE (audit perf 2.1, L-1 / L-11)
# =============================================================================

class Portee:
    """
    Portée d'une génération, précalculée UNE FOIS puis interrogée par masque.

    Pourquoi : `faiss_ids_for_path_prefixes` normalisait TOUS les chemins de
    la table `fichiers` à chaque question (0,9 à 1,9 s à 200 000 fichiers,
    2,5 à 5 s extrapolés à 500 000), puis matérialisait l'ensemble autorisé
    en `set` Python reconverti deux fois en tableau (L-11, 0,3 s sur une
    portée large). Ici, tout ce qui ne dépend pas de la question est fait au
    chargement de la génération (index_cache, dans le préchargement) :

      - `chemins` : les chemins normalisés (`norm_path`) de la table
        `fichiers`, TRIÉS — l'ordinal d'un fichier est sa position ;
      - `_ord_fichier` : pour chaque fragment, l'ordinal de son fichier
        (int32 ; les fragments orphelins — hash absent de `fichiers` —
        reçoivent l'ordinal sentinelle `n_fichiers`, jamais sélectionné,
        comme la jointure historique les ignorait) ;
      - `faiss_ids` : les identifiants des fragments, triés, alignés sur
        `_ord_fichier` (int32 quand ils tiennent, sinon int64) ;
      - `_fam_fichier` : le code de famille de chaque fichier (ordinal).

    Par question, un préfixe de répertoire devient un intervalle d'ordinaux
    par dichotomie (`bisect`) sur les chemins triés, un masque de fichiers
    puis un masque de fragments par indexation numpy : 0,2 ms au lieu de
    1 700 à 200 000 fichiers, résultats identiques (mêmes règles : préfixe
    suivi de `/`, casse ignorée, séparateurs unifiés, UNC). Le masque
    alimente directement FAISS (sélecteur d'identifiants) et BM25 (masque
    aligné sur ses `faiss_ids`), sans `set` ni `np.isin`.

    Mémoire : la liste des chemins normalisés (quelques dizaines d'octets par
    fichier au-delà de la chaîne elle-même) + 8 octets par fragment.
    """

    def __init__(self, chemins: List[str], fam_fichier: np.ndarray,
                 familles: List[str], faiss_ids: np.ndarray,
                 ord_fichier: np.ndarray):
        self.chemins = chemins                      # normalisés, triés
        self.n_fichiers = len(chemins)
        self.familles = familles                    # code -> nom
        self._fam_index = {f: i for i, f in enumerate(familles)}
        self._fam_fichier = fam_fichier             # par ordinal, -1 = NULL
        self.faiss_ids = faiss_ids                  # triés
        self._ord_fichier = ord_fichier             # par fragment (ordre de faiss_ids)
        self.n_fragments = int(faiss_ids.shape[0])
        # Alignement mémorisé sur les `faiss_ids` d'un AUTRE tableau (le BM25
        # de la génération : il peut dater d'une génération antérieure après
        # un point de reprise, donc différer du registre) : {id(tableau):
        # (tableau, positions)}. Un seul (le BM25 courant).
        self._alignements: Dict[int, Tuple[Any, Optional[np.ndarray]]] = {}

    # --- construction ---------------------------------------------------------

    @classmethod
    def depuis_connexion(cls, conn) -> "Portee":
        """Construit la portée depuis une connexion au registre : deux
        lectures (`fichiers`, puis `chunks`), le reste en numpy — pas de
        boucle Python par fragment. Schéma 1 : la correspondance hash de
        fichier -> ordinal se fait par `searchsorted` sur les hash triés ;
        schéma 2 (audit perf 2.1, I-7) : l'ordinal vient directement de
        `fichier_id` par une table de correspondance rowid -> ordinal (deux
        colonnes entières, plus de chaînes à comparer)."""
        schema = schema_de_connexion(conn) or SCHEMA_COURANT
        cur = conn.cursor()
        cur.row_factory = None                      # tuples nus (pas de sqlite3.Row)
        cle = "id" if schema == 2 else "hash"
        fichiers = cur.execute(f"SELECT {cle}, chemin, famille FROM fichiers").fetchall()
        n = len(fichiers)
        cles = [r[0] for r in fichiers]
        normes = _norm_paths([r[1] for r in fichiers])
        ordre = sorted(range(n), key=normes.__getitem__)
        chemins = [normes[i] for i in ordre]
        # ordinal (position triée) de chaque LIGNE de `fichiers`
        ord_ligne = np.empty(n, dtype=np.int64)
        ord_ligne[np.asarray(ordre, dtype=np.int64)] = np.arange(n, dtype=np.int64)
        familles: List[str] = []
        fam_index: Dict[str, int] = {}
        fam_ligne = np.full(n, -1, dtype=np.int32)
        for i, r in enumerate(fichiers):
            f = r[2]
            if f is None:
                continue
            code = fam_index.get(f)
            if code is None:
                code = fam_index[f] = len(familles)
                familles.append(f)
            fam_ligne[i] = code
        fam_fichier = np.empty(n, dtype=np.int32)
        fam_fichier[ord_ligne] = fam_ligne
        del fichiers, normes

        if schema == 2:
            # Deux colonnes entières : tableau int64 direct, pas d'objets.
            chunks = cur.execute("SELECT faiss_id, fichier_id FROM chunks").fetchall()
            m = len(chunks)
            if m:
                brut = np.array(chunks, dtype=np.int64).reshape(m, 2)
                fids = brut[:, 0]
                ord_frag = cls._ordinaux_par_id(cles, ord_ligne, brut[:, 1], n)
                del brut, chunks
            else:
                fids = np.zeros(0, dtype=np.int64)
                ord_frag = np.zeros(0, dtype=np.int32)
        else:
            chunks = cur.execute("SELECT faiss_id, hash_fichier FROM chunks").fetchall()
            m = len(chunks)
            if m:
                brut = np.array(chunks, dtype=object)
                fids = brut[:, 0].astype(np.int64)
                ord_frag = cls._ordinaux_par_hash(cles, ord_ligne, brut[:, 1], n)
                del brut, chunks
            else:
                fids = np.zeros(0, dtype=np.int64)
                ord_frag = np.zeros(0, dtype=np.int32)
        if m:
            o = np.argsort(fids, kind="stable")
            fids, ord_frag = fids[o], ord_frag[o]
        if fids.size and int(fids.max()) < 2 ** 31 - 1 and int(fids.min()) >= 0:
            fids = fids.astype(np.int32)
        return cls(chemins, fam_fichier, familles, fids,
                   np.ascontiguousarray(ord_frag, dtype=np.int32))

    @staticmethod
    def _ordinaux_par_id(ids: List[int], ord_ligne: np.ndarray,
                         id_frag: np.ndarray, sentinelle: int) -> np.ndarray:
        """Ordinal du fichier de chaque fragment depuis son `fichier_id`
        (schéma 2). Table rowid -> ordinal quand les rowid sont denses (le
        cas normal : ils suivent l'insertion, les trous viennent des
        suppressions) ; dichotomie vectorisée sur les rowid triés sinon.
        Un identifiant inconnu (fragment orphelin, `_ORPHELIN` = 0) reçoit
        la sentinelle, jamais sélectionnée."""
        if not ids:
            return np.full(len(id_frag), sentinelle, dtype=np.int64)
        arr = np.asarray(ids, dtype=np.int64)
        maxi = int(arr.max())
        if 0 <= int(arr.min()) and maxi < 2 * len(ids) + 1024:
            table = np.full(maxi + 1, sentinelle, dtype=np.int64)
            table[arr] = ord_ligne
            dedans = (id_frag >= 0) & (id_frag <= maxi)
            return np.where(dedans, table[np.clip(id_frag, 0, maxi)], sentinelle)
        tri = np.argsort(arr, kind="stable")
        tries = arr[tri]
        pos = np.searchsorted(tries, id_frag)
        pos_b = np.minimum(pos, len(ids) - 1)
        ok = tries[pos_b] == id_frag
        return np.where(ok, ord_ligne[tri[pos_b]], sentinelle)

    @staticmethod
    def _ordinaux_par_hash(hashes: List[str], ord_ligne: np.ndarray,
                           hash_frag: np.ndarray, sentinelle: int) -> np.ndarray:
        """Ordinal du fichier de chaque fragment, par recherche dichotomique
        vectorisée des hash de fragments dans les hash de fichiers triés.
        Les hash sont des chaînes ASCII (SHA-256 hexadécimal) ; dans le cas
        contraire, correspondance par dictionnaire (même résultat)."""
        try:
            hf = np.array(hashes if hashes else [""], dtype="S")
            hc = hash_frag.astype("S")
            largeur = max(hf.dtype.itemsize, hc.dtype.itemsize)
            hf = hf.astype(f"S{largeur}")
            hc = hc.astype(f"S{largeur}")
        except (UnicodeEncodeError, TypeError, ValueError):
            index = {h: int(o) for h, o in zip(hashes, ord_ligne)}
            return np.fromiter((index.get(h, sentinelle) for h in hash_frag),
                               dtype=np.int64, count=len(hash_frag))
        if not hashes:
            return np.full(len(hash_frag), sentinelle, dtype=np.int64)
        tri = np.argsort(hf, kind="stable")
        hf_tries = hf[tri]
        pos = np.searchsorted(hf_tries, hc)
        pos_b = np.minimum(pos, len(hashes) - 1)
        ok = hf_tries[pos_b] == hc
        return np.where(ok, ord_ligne[tri[pos_b]], sentinelle)

    # --- masques de fichiers -> masques de fragments -------------------------

    def _masque_fichiers(self) -> np.ndarray:
        # Une case de plus : la sentinelle des fragments orphelins, toujours
        # fausse.
        return np.zeros(self.n_fichiers + 1, dtype=bool)

    def _vers_fragments(self, masque_fichiers: np.ndarray) -> np.ndarray:
        if self.n_fragments == 0:
            return np.zeros(0, dtype=bool)
        return masque_fichiers[self._ord_fichier]

    def masque_prefixes(self, prefixes) -> np.ndarray:
        """Masque (aligné sur `faiss_ids`) des fragments dont le fichier est
        SOUS l'un des répertoires donnés — sémantique exacte de
        `ReadOnlyRegistry.faiss_ids_for_path_prefixes` : préfixe normalisé
        suivi de `/` (`docs` ne retient pas `docs2`), entrées vides ignorées.
        Tous les chemins commençant par `p/` forment l'intervalle
        [`p/`, `p0`) des chemins triés (`0` suit `/` en code point)."""
        fm = self._masque_fichiers()
        for p in prefixes or []:
            if not p or not str(p).strip():
                continue
            p = norm_path(p) + "/"
            lo = bisect_left(self.chemins, p)
            hi = bisect_left(self.chemins, p[:-1] + "0")
            if hi > lo:
                fm[lo:hi] = True
        return self._vers_fragments(fm)

    def masque_chemins(self, chemins) -> np.ndarray:
        """Masque des fragments de TOUS les fichiers dont le chemin est l'un
        des chemins donnés (grain document : la base de recherche, §6.7) —
        même résultat que `faiss_ids_for_paths` : comparaison normalisée
        (un chemin enregistré tel quel est trouvé tel quel, un chemin à la
        casse ou aux séparateurs différents aussi), entrées vides ignorées,
        chemins inconnus sans effet. Dichotomie par chemin : un chemin
        normalisé présent sous plusieurs enregistrements les retient tous."""
        fm = self._masque_fichiers()
        for p in chemins or []:
            if not p or not str(p).strip():
                continue
            c = norm_path(p)
            lo = bisect_left(self.chemins, c)
            hi = bisect_right(self.chemins, c)
            if hi > lo:
                fm[lo:hi] = True
        return self._vers_fragments(fm)

    def masque_familles(self, familles) -> np.ndarray:
        """Masque des fragments des familles données (égalité stricte sur la
        famille enregistrée, comme `faiss_ids_for_families`)."""
        fm = self._masque_fichiers()
        codes = [self._fam_index[f] for f in (familles or []) if f in self._fam_index]
        if codes and self.n_fichiers:
            fm[:self.n_fichiers] = np.isin(self._fam_fichier,
                                           np.asarray(codes, dtype=np.int32))
        return self._vers_fragments(fm)

    def masque_ids(self, ids) -> np.ndarray:
        """Masque des fragments dont l'identifiant figure dans `ids` (set,
        liste, tableau ; un masque booléen déjà aligné est rendu tel quel) —
        la compatibilité des appels `allowed_ids=` historiques."""
        if isinstance(ids, np.ndarray) and ids.dtype == np.bool_:
            if ids.shape[0] != self.n_fragments:
                raise ValueError("masque de portée de longueur inattendue")
            return ids
        if isinstance(ids, np.ndarray):
            arr = np.asarray(ids, dtype=np.int64).ravel()
        else:
            try:
                n = len(ids)
            except TypeError:
                ids = list(ids)
                n = len(ids)
            arr = np.fromiter(ids, dtype=np.int64, count=n)
        m = np.zeros(self.n_fragments, dtype=bool)
        if arr.size and self.n_fragments:
            pos = np.searchsorted(self.faiss_ids, arr)
            pos = np.minimum(pos, self.n_fragments - 1)
            ok = self.faiss_ids[pos] == arr
            m[pos[ok]] = True
        return m

    # --- consommation d'un masque --------------------------------------------

    def ids(self, masque: np.ndarray) -> np.ndarray:
        """Identifiants (int64, triés) des fragments d'un masque — l'entrée du
        sélecteur FAISS."""
        return np.ascontiguousarray(self.faiss_ids[masque], dtype=np.int64)

    def nombre(self, masque: np.ndarray) -> int:
        return int(np.count_nonzero(masque))

    def _aligner(self, autres) -> Optional[np.ndarray]:
        """Positions, dans `faiss_ids`, des identifiants du tableau `autres`
        (None si c'est le même tableau d'identifiants ; `n_fragments` pour un
        identifiant inconnu). Mémorisé pour le tableau courant."""
        ent = self._alignements.get(id(autres))
        if ent is not None and ent[0] is autres:
            return ent[1]
        arr = np.asarray(autres)
        if arr.shape == self.faiss_ids.shape and np.array_equal(arr, self.faiss_ids):
            res = None
        elif self.n_fragments == 0 or arr.size == 0:
            res = np.full(arr.shape[0], self.n_fragments, dtype=np.int64)
        else:
            pos = np.searchsorted(self.faiss_ids, arr)
            pos = np.minimum(pos, self.n_fragments - 1)
            ok = self.faiss_ids[pos] == arr
            res = np.where(ok, pos, self.n_fragments)
        self._alignements = {id(autres): (autres, res)}
        return res

    def masque_pour(self, autres, masque: np.ndarray) -> np.ndarray:
        """Le masque transposé sur un autre tableau d'identifiants (ceux du
        BM25) : masque aligné sur `autres`, faux pour un identifiant inconnu
        du registre. Sans copie quand les identifiants sont les mêmes."""
        al = self._aligner(autres)
        if al is None:
            return masque
        return np.append(masque, False)[al]

    def taille_octets(self) -> int:
        """Empreinte approximative (tableaux + chaînes des chemins)."""
        return int(self.faiss_ids.nbytes + self._ord_fichier.nbytes
                   + self._fam_fichier.nbytes
                   + sum(len(c) + 49 for c in self.chemins) + 8 * self.n_fichiers)


# =============================================================================
#  HELPERS
# =============================================================================

def _to_file(row: sqlite3.Row) -> FileRecord:
    # Défensif : un registre publié AVANT l'ajout du rattrapage OCR n'a pas la
    # colonne (les lecteurs l'ouvrent en immutable, sans migration possible).
    keys = row.keys()
    return FileRecord(
        hash=row["hash"], chemin=row["chemin"], taille=row["taille"],
        mtime=row["mtime"], famille=row["famille"],
        ocr_pending=int(row["ocr_pending"] or 0) if "ocr_pending" in keys else 0,
    )


def _sqlite_ro_uri(db_path: str, params: str = "mode=ro&immutable=1") -> str:
    """
    Construit une URI SQLite `file:` valide et portable, en lecture seule.

    Gère les trois formes de chemins absolus, y compris les cas où une simple
    interpolation échoue (B2) :
      - POSIX     /home/x/db          -> file:///home/x/db
      - Windows   C:\\x\\db            -> file:///C:/x/db
      - UNC       \\\\serveur\\part\\db -> file:////serveur/part/db

    SQLite refuse une autorité non vide dans une URI file: ; les chemins UNC
    doivent donc utiliser la forme à autorité vide (`file:////serveur/...`).
    """
    from urllib.parse import quote

    p = os.path.abspath(db_path).replace("\\", "/")
    if p.startswith("//"):        # UNC : //serveur/partage/...
        encoded = quote(p, safe="/:")
        uri = "file://" + encoded          # -> file:////serveur/...
    elif p.startswith("/"):       # POSIX absolu : /home/...
        encoded = quote(p, safe="/:")
        uri = "file://" + encoded          # -> file:///home/...
    else:                         # Windows avec lettre de lecteur : C:/...
        encoded = quote("/" + p, safe="/:")
        uri = "file://" + encoded          # -> file:///C:/...
    return uri + "?" + params
