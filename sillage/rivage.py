"""Pont vers le moteur de RIVAGE, embarqué dans SILLAGE (`sillage/moteur/`).

Tout ce qui lit du texte par le sens (vérification des « conformes »,
traçabilité vers la norme, fiches de revue) passe par un index du moteur, sa
portée par chemins, sa chaîne hybride et son reranking. SILLAGE n'en réécrit
rien et n'exige aucune installation de RIVAGE : la copie embarquée suffit.

Pour le développement, `SILLAGE_RIVAGE_DIR` désigne un dépôt RIVAGE à utiliser
à la place de la copie embarquée (essai d'une version plus récente avant de la
synchroniser avec `outils/synchroniser_rivage.py`).
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
_ERREUR: str = ""


def localiser() -> Optional[str]:
    """Rend importables les modules du moteur ; renvoie leur dossier, ou None
    (et `erreur()` dit pourquoi : dépendance manquante, par exemple faiss)."""
    global _LOCALISE, _ERREUR
    if _LOCALISE:
        return _LOCALISE
    externe = os.environ.get("SILLAGE_RIVAGE_DIR")
    if externe and (Path(externe) / "retrieval.py").exists():
        if externe not in sys.path:
            sys.path.insert(0, externe)
        dossier = externe
    else:
        from .moteur import activer
        dossier = str(activer())
    try:
        import retrieval  # noqa: F401  (contrôle d'importabilité)
        import indexer  # noqa: F401
    except Exception as exc:   # ImportError, mais aussi une DLL faiss qui refuse de se charger
        _ERREUR = f"{type(exc).__name__}: {exc}"
        return None
    _LOCALISE = dossier
    return _LOCALISE


def disponible() -> bool:
    return localiser() is not None


def erreur() -> str:
    return _ERREUR


def message_absent() -> str:
    return ("moteur embarqué inutilisable" + (f" ({_ERREUR})" if _ERREUR else "") +
            " : vérifiez l'installation des dépendances (requirements.txt : numpy, faiss-cpu, httpx…)")


MESSAGE_ABSENT = "moteur embarqué inutilisable : vérifiez l'installation des dépendances (requirements.txt)"


def indexer(corpus: str, index_dir: str, embedder=None, use_ocr: bool = True, progress_cb=None, log=None):
    """Indexe (ou met à jour, incrémentalement) les documents d'un fournisseur
    avec l'indexeur de RIVAGE, dans un index propre à SILLAGE. Même pipeline
    que RIVAGE (extraction, découpage, déduplication, embedding via la
    passerelle, FAISS + BM25, publication atomique) : SILLAGE n'en réécrit rien."""
    if not disponible():
        raise ImportError(message_absent())
    import indexer as _indexer
    Path(index_dir).mkdir(parents=True, exist_ok=True)
    return _indexer.index_corpus(str(corpus), str(index_dir), embedder=embedder, use_ocr=use_ocr,
                                 progress_cb=progress_cb, log=log or logger)


def version_rivage() -> str:
    """Version du moteur (copie embarquée, ou dépôt désigné par SILLAGE_RIVAGE_DIR)."""
    if not disponible():
        from .moteur import version_rivage as embarquee
        return embarquee()
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
    """Une session de recherche sur un index du moteur : un retriever ouvert pour
    toute une campagne (le cache lecteur de RIVAGE garde FAISS, BM25 et la
    portée en mémoire ; registre et magasin sont empruntés au pool)."""

    def __init__(self, index_dir: str, embedder=None, reranker=None, log=None):
        if not disponible():
            raise ImportError(message_absent())
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
