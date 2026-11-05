# content_store.py
"""
Magasin de contenu segmenté - §8 (scalabilité S1)

Le texte des chunks ne vit plus dans le registre `registre.<gen>.db` (qui était
recopié intégralement à chaque run — coût O(corpus)). Il est stocké dans des
**segments de contenu immuables** `content.<gen>.db`, écrits une seule fois par
run (le delta de ce run) et jamais modifiés ensuite.

Conséquences :
  - le registre ne contient plus que des métadonnées (petit) → copie par
    génération quasi gratuite ;
  - un segment n'est jamais réécrit → un lecteur peut l'ouvrir en `ro`
    `immutable=1` sans risque, et il est simplement référencé (pas copié)
    d'une génération à l'autre ;
  - la publication d'un run n'écrit que le contenu **nouveau** (O(delta)).

Un `faiss_id` est immuable et n'est jamais réutilisé (§8), donc le couple
`faiss_id -> texte` est stable : les segments sont sûrs à partager entre
générations. Un segment devient supprimable quand aucun de ses `faiss_id`
n'est plus vivant (référencé par le registre).
"""

import os
import sqlite3
import logging
from typing import Dict, List, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

from registry import _sqlite_ro_uri  # réutilise la construction d'URI portable


def segment_name(gen: int) -> str:
    return f"content.{gen}.db"


# =============================================================================
#  ÉCRITURE (un segment par run)
# =============================================================================

class ContentSegmentWriter:
    """Écrit le segment de contenu d'un run : (faiss_id -> texte)."""

    def __init__(self, path: str):
        self.path = path
        # Nouvelle génération = segment neuf : supprimer un résidu d'un run tué
        # avant publication (mêmes lignes seraient inertes, mais on repart
        # propre — et, sans journal, un résidu peut être inconsistant). Un
        # journal orphelin d'un run antérieur part avec lui.
        for residu in (path, path + "-journal"):
            if os.path.exists(residu):
                try:
                    os.remove(residu)
                except OSError:
                    pass
        self.conn = sqlite3.connect(path)
        # Segment NON PUBLIÉ (audit V2, A-6) : il n'est visible des lecteurs
        # qu'après la bascule du manifest et recréé à neuf (ci-dessus,
        # store.discard) si le run est abandonné. Journal de rollback et fsync
        # ne protègent donc rien ici, et coûtaient un aller-retour réseau
        # complet par commit sur le partage.
        self.conn.execute("PRAGMA journal_mode=OFF")
        self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS contenu ("
            " faiss_id INTEGER PRIMARY KEY, texte TEXT)"
        )
        self._count = 0

    def add(self, faiss_id: int, texte: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO contenu(faiss_id, texte) VALUES(?,?)",
            (faiss_id, texte),
        )
        self._count += 1

    def add_many(self, rows: Iterable[Tuple[int, str]]) -> None:
        """Insertion groupée (faiss_id, texte) — un fichier = un executemany
        (A-6), sans commit : il a lieu à la fermeture du segment."""
        rows = list(rows)
        if not rows:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO contenu(faiss_id, texte) VALUES(?,?)", rows)
        self._count += len(rows)

    @property
    def count(self) -> int:
        return self._count

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None


# =============================================================================
#  LECTURE (sur plusieurs segments actifs)
# =============================================================================

class ContentStore:
    """
    Lecture du texte des chunks à travers les segments actifs d'une génération.

    Les segments sont ouverts en lecture seule immuable (§8.5) et à la demande.
    Un segment est immuable par nom : les connexions restent valables tant que
    le manifest liste les mêmes segments — le cache lecteur (index_cache) les
    conserve d'une question à l'autre (audit perf 2.1, L-9).
    """

    def __init__(self, index_dir: str, segment_files: Iterable[str],
                 ranges: Optional[Iterable[dict]] = None,
                 partage_threads: bool = False,
                 cache_kio: Optional[int] = None):
        """
        Args:
            segment_files: noms/chemins des segments actifs.
            ranges: optionnel, [{file, lo, hi}] (bornes d'ids par segment, du
                manifest). Quand fourni, `get_texts` route chaque faiss_id
                directement vers SON segment au lieu de sonder tous les
                segments un par un sur le partage (audit : latence lecteur
                proportionnelle au nombre de segments sinon).
            partage_threads: les connexions pourront être utilisées par un
                autre thread que celui qui les a ouvertes — jamais deux à la
                fois (prêt par le cache lecteur, audit perf 2.1, L-9).
            cache_kio: cache de pages SQLite par segment (Kio ; None =
                défaut SQLite, 2 Mo) — utile pour des connexions conservées.
        """
        self.index_dir = index_dir
        self._partage_threads = bool(partage_threads)
        self._cache_kio = int(cache_kio) if cache_kio else None
        self.segment_files = [
            f if os.path.isabs(f) else os.path.join(index_dir, f)
            for f in segment_files
        ]
        self._ranges: List[Tuple[int, int, str]] = []
        for r in (ranges or []):
            lo, hi = r.get("lo"), r.get("hi")
            f = r.get("file", "")
            if lo is None or hi is None or not f:
                self._ranges = []  # info incomplète : repli sur le sondage
                break
            path = f if os.path.isabs(f) else os.path.join(index_dir, f)
            self._ranges.append((int(lo), int(hi), path))
        self._ranges.sort()
        self._conns: Dict[str, sqlite3.Connection] = {}

    def _conn(self, path: str) -> Optional[sqlite3.Connection]:
        if path in self._conns:
            return self._conns[path]
        if not os.path.exists(path):
            # Un segment listé au manifest mais absent = incohérence (nettoyage
            # trop agressif, copie partielle) OU hoquet réseau passager : le
            # signaler, ne pas rester muet — et NE PAS mémoriser l'absence
            # (revue 2.5, RB-1) : le magasin vit désormais toute une génération
            # dans le pool du lecteur ; figer `None` ici rendait « 0 extraits »
            # à toutes les questions suivantes, réseau revenu ou non.
            logger.warning(f"[content_store] Segment de contenu introuvable: {path}")
            return None
        conn = sqlite3.connect(_sqlite_ro_uri(path), uri=True,
                               check_same_thread=not self._partage_threads)
        if self._cache_kio:
            conn.execute(f"PRAGMA cache_size=-{self._cache_kio}")
        self._conns[path] = conn
        return conn

    def _segment_for(self, fid: int) -> Optional[str]:
        """Segment dont la plage [lo, hi) contient fid (dichotomie), ou None."""
        import bisect
        i = bisect.bisect_right(self._ranges, (fid, float("inf"), "")) - 1
        if i >= 0:
            lo, hi, path = self._ranges[i]
            if lo <= fid < hi:
                return path
        return None

    def get_texts(self, faiss_ids: Iterable[int]) -> Dict[int, str]:
        """Retourne {faiss_id: texte} pour les ids trouvés (à travers les segments)."""
        ids = list(faiss_ids)
        if not ids:
            return {}
        out: Dict[int, str] = {}

        # Voie routée : chaque id est lu dans SON segment (bornes du manifest).
        if self._ranges:
            by_seg: Dict[str, List[int]] = {}
            for fid in ids:
                path = self._segment_for(fid)
                if path is not None:
                    by_seg.setdefault(path, []).append(fid)
            for path, fids in by_seg.items():
                conn = self._conn(path)
                if conn is None:
                    continue
                for start in range(0, len(fids), 900):
                    chunk = fids[start:start + 900]
                    placeholders = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT faiss_id, texte FROM contenu WHERE faiss_id IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    out.update(rows)
            return out

        # Repli historique : sonder les segments un par un.
        remaining = set(ids)
        for path in self.segment_files:
            if not remaining:
                break
            conn = self._conn(path)
            if conn is None:
                continue
            batch = list(remaining)
            # Requête par paquets pour rester sous la limite de variables SQLite
            for start in range(0, len(batch), 900):
                chunk = batch[start:start + 900]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT faiss_id, texte FROM contenu WHERE faiss_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for fid, texte in rows:
                    out[fid] = texte
                    remaining.discard(fid)
        return out

    def iter_all(self) -> Iterable[Tuple[int, str]]:
        """
        Itère (faiss_id, texte) sur tous les segments (pour reconstruire BM25).

        Sans ensemble Python des ids déjà vus (audit V2, A-11) : les segments
        actifs d'une génération couvrent des plages d'ids DISJOINTES (chaque
        run n'écrit que son delta d'ids monotones, un segment compacté couvre
        [0, run_hi) et remplace tous les précédents). Quand les bornes sont
        connues, chaque segment est en plus borné à SA plage en SQL (clé
        primaire) : une ligne hors plage ne peut pas être livrée deux fois.
        """
        bornes = {path: (lo, hi) for lo, hi, path in self._ranges}
        for path in self.segment_files:
            conn = self._conn(path)
            if conn is None:
                continue
            if path in bornes:
                lo, hi = bornes[path]
                cur = conn.execute(
                    "SELECT faiss_id, texte FROM contenu "
                    "WHERE faiss_id >= ? AND faiss_id < ?", (lo, hi))
            else:
                cur = conn.execute("SELECT faiss_id, texte FROM contenu")
            for fid, texte in cur:
                yield fid, texte

    def close(self) -> None:
        for conn in self._conns.values():
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self._conns.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
