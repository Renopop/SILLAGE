"""Pont vers RIVAGE : SILLAGE est un client du moteur de RIVAGE, pas une réécriture.

Tout ce qui lit du texte par le sens (vérification des « conformes »,
traçabilité vers la norme, fiches de revue) passe par l'index publié de
RIVAGE, sa portée par chemins, sa chaîne hybride et son reranking.

Localisation des modules de RIVAGE (modules plats : `retrieval`, `query`,
`indexer`…), dans l'ordre :

1. déjà importables (RIVAGE installé dans le même environnement : `pip install
   chemin/vers/RIVAGE`) ;
2. `SILLAGE_RIVAGE_DIR` ou `RIVAGE_DIR` ;
3. un dossier `RIVAGE` (ou `rivage`) voisin du dépôt SILLAGE.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger("sillage.rivage")

_LOCALISE: Optional[str] = None


def localiser() -> Optional[str]:
    """Rend importables les modules de RIVAGE ; renvoie leur dossier, ou None."""
    global _LOCALISE
    if _LOCALISE:
        return _LOCALISE
    try:
        import retrieval  # noqa: F401
        _LOCALISE = str(Path(retrieval.__file__).resolve().parent)
        return _LOCALISE
    except ImportError:
        pass
    racine = Path(__file__).resolve().parents[1]
    candidats = [os.environ.get("SILLAGE_RIVAGE_DIR"), os.environ.get("RIVAGE_DIR"),
                 racine.parent / "RIVAGE", racine.parent / "rivage", racine / "RIVAGE"]
    for c in candidats:
        if not c:
            continue
        p = Path(c)
        if (p / "retrieval.py").exists() and (p / "query.py").exists():
            sys.path.insert(0, str(p))
            _LOCALISE = str(p)
            return _LOCALISE
    return None


def disponible() -> bool:
    return localiser() is not None


MESSAGE_ABSENT = ("RIVAGE introuvable : installez-le dans le même environnement (pip install chemin\\vers\\RIVAGE) "
                  "ou indiquez son dossier par SILLAGE_RIVAGE_DIR")


def indexer(corpus: str, index_dir: str, embedder=None, use_ocr: bool = True, progress_cb=None, log=None):
    """Indexe (ou met à jour, incrémentalement) les documents d'un fournisseur
    avec l'indexeur de RIVAGE, dans un index propre à SILLAGE. Même pipeline
    que RIVAGE (extraction, découpage, déduplication, embedding via la
    passerelle, FAISS + BM25, publication atomique) : SILLAGE n'en réécrit rien."""
    if not disponible():
        raise ImportError(MESSAGE_ABSENT)
    import indexer as _indexer
    Path(index_dir).mkdir(parents=True, exist_ok=True)
    return _indexer.index_corpus(str(corpus), str(index_dir), embedder=embedder, use_ocr=use_ocr,
                                 progress_cb=progress_cb, log=log or logger)


def version_rivage() -> str:
    if not disponible():
        return ""
    try:
        from version import __version__
        return __version__
    except ImportError:  # pragma: no cover
        return "?"


@dataclass
class Passage:
    chemin: str
    page: Optional[int]
    texte: str
    score: Optional[float]      # score de reranking (None sans reranker)
    score_rrf: float
    faiss_id: Optional[int]

    def to_dict(self) -> dict:
        return {"chemin": self.chemin, "page": self.page, "texte": self.texte, "score": self.score,
                "score_rrf": self.score_rrf, "faiss_id": self.faiss_id}

    @classmethod
    def from_dict(cls, d: dict) -> "Passage":
        return cls(d.get("chemin", ""), d.get("page"), d.get("texte", ""), d.get("score"),
                   d.get("score_rrf", 0.0), d.get("faiss_id"))


class Moteur:
    """Une session de recherche sur un index RIVAGE : un retriever ouvert pour
    toute une campagne (le cache lecteur de RIVAGE garde FAISS, BM25 et la
    portée en mémoire ; registre et magasin sont empruntés au pool)."""

    def __init__(self, index_dir: str, embedder=None, reranker=None, log=None):
        if not disponible():
            raise ImportError(MESSAGE_ABSENT)
        from retrieval import HybridRetriever
        self.index_dir = index_dir
        self.log = log or logger
        self.retriever = HybridRetriever.from_index_dir(index_dir, embedder=embedder, reranker=reranker, log=self.log)

    # --- recherche ----------------------------------------------------------------
    def passages(self, question: str, path_prefixes: Optional[Sequence[str]] = None,
                 allowed_paths: Optional[Sequence[str]] = None, retrieve_k: Optional[int] = None,
                 rerank_k: Optional[int] = None) -> Tuple[List[Passage], bool]:
        """Les meilleurs passages pour `question` dans la portée donnée, et si le
        reranking a eu lieu. UN embedding, UNE recherche hybride, UN reranking."""
        vivier = self.retriever.candidats(question, path_prefixes=list(path_prefixes) if path_prefixes else None,
                                          allowed_paths=list(allowed_paths) if allowed_paths else None,
                                          retrieve_k=retrieve_k, log=self.log)
        if not vivier:
            return [], False
        res = self.retriever.reclasser(question, vivier, retrieve_k=retrieve_k, rerank_k=rerank_k, log=self.log)
        out = [Passage(c.chemin, c.page, c.texte or "", c.rerank_score, float(c.score or 0.0), c.faiss_id)
               for c in res.chunks]
        return out, bool(res.reranked)

    def seuil_comparable(self, ref: float) -> float:
        from retrieval import _seuil_comparable
        return _seuil_comparable(ref)

    # --- portée ---------------------------------------------------------------------
    def fichiers(self, path_prefixes: Optional[Sequence[str]] = None) -> List[str]:
        """Chemins des fichiers indexés, restreints aux préfixes donnés (normalisation de RIVAGE)."""
        from registry import norm_path
        tous = [c for c, _f in self.retriever.registry.list_files()]
        if not path_prefixes:
            return tous
        prefs = [norm_path(p).rstrip("/") + "/" for p in path_prefixes]
        return [c for c in tous if any(norm_path(c).startswith(p) or norm_path(c) == p.rstrip("/") for p in prefs)]

    def close(self) -> None:
        try:
            self.retriever.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
