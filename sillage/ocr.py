"""OCR de secours pour les PDF scannés (spécification ou NCM sans couche texte).

Même mécanisme que l'indexeur de RIVAGE : les pages sans texte sont rendues
en image et lues par le modèle Vision de la passerelle (module `llm_ocr` du
moteur embarqué, clé `RIVAGE_SYNTH_KEY`). Chaque page est un appel payant :
l'OCR ne se déclenche que si l'utilisateur l'a demandé (`--ocr`, case « OCR
de secours » de l'interface) et seulement sur les pages qui n'ont pas de
texte. Les pages déjà lues sont reprises du cache de RIVAGE (`RIVAGE_OCR_CACHE`)
et d'un mémo de session, pour qu'une relecture ne repaie rien.

Le texte OCR d'une NCM est ramené à des tableaux par ses lignes à barres
verticales (« | »), que le modèle produit pour les tableaux.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .tableaux import Tableau

logger = logging.getLogger("sillage.ocr")

_MEMO: Dict[Tuple[str, int, float], Dict[int, str]] = {}   # (chemin, taille, mtime) -> page -> texte


def disponible() -> Tuple[bool, str]:
    """(utilisable, raison) : le moteur embarqué et la clé du modèle Vision."""
    from . import rivage
    if not rivage.disponible():
        return False, rivage.message_absent()
    try:
        import llm_ocr
    except Exception as exc:   # pragma: no cover - dépend du poste
        return False, f"module OCR du moteur inutilisable ({exc})"
    if not getattr(llm_ocr, "DALLEM_CONFIG_AVAILABLE", False) or not getattr(llm_ocr, "DALLEM_API_KEY", None):
        return False, "clé du modèle Vision absente (RIVAGE_SYNTH_KEY dans RIVAGE_keys.env)"
    return True, ""


def pages_sans_texte(pdf: Path) -> List[int]:
    """Numéros (à partir de 1) des pages sans couche texte."""
    import pdfplumber
    with pdfplumber.open(str(pdf)) as doc:
        return [n for n, page in enumerate(doc.pages, start=1) if not (page.extract_text() or "").strip()]


def pages_ocr(pdf, pages: Optional[List[int]] = None,
              progression: Optional[Callable[[float, str], None]] = None, log=None) -> Dict[int, str]:
    """Texte OCR des pages demandées (numéros à partir de 1 ; toutes par défaut).

    Lève RuntimeError si l'OCR est indisponible ou si une page n'a pas pu
    être lue (passerelle saturée, clé refusée) : un référentiel partiel
    serait pire qu'une erreur franche."""
    p = Path(pdf)
    st = p.stat()
    cle = (str(p.resolve()), st.st_size, st.st_mtime)
    memo = _MEMO.setdefault(cle, {})
    manquantes = [n for n in (pages or _toutes(p)) if n not in memo]
    if manquantes:
        ok, raison = disponible()
        if not ok:
            raise RuntimeError(f"OCR de secours indisponible : {raison}")
        import llm_ocr
        _log = log or logger
        _log.info("OCR de secours : %d page(s) de %s", len(manquantes), p.name)
        res = llm_ocr.ocr_pdf_with_dallem(str(p), pages=[n - 1 for n in manquantes], progress_cb=progression, log=_log)
        echecs = []
        for r in res.page_results:
            if r.error and not r.text:
                echecs.append(f"page {r.page_number} : {r.error}")
            else:
                memo[r.page_number] = r.text or ""
        if echecs:
            raise RuntimeError("OCR de secours incomplet — " + " ; ".join(echecs[:5]) +
                               (" …" if len(echecs) > 5 else "") + " (relancer : les pages lues sont au cache)")
    return {n: memo.get(n, "") for n in (pages or _toutes(p))}


def _toutes(p: Path) -> List[int]:
    import pdfplumber
    with pdfplumber.open(str(p)) as doc:
        return list(range(1, len(doc.pages) + 1))


_SEPARATEUR_MD = re.compile(r"^:?-{2,}:?$")


def tableaux_depuis_texte(texte: str, fichier: str, page: int) -> List[Tableau]:
    """Tableaux d'une page OCR : les lignes consécutives contenant au moins
    deux barres verticales forment un tableau (séparateurs « --- » du
    Markdown ignorés) ; les lignes sont complétées au nombre de colonnes."""
    out: List[Tableau] = []
    courant: Optional[Tableau] = None
    k = 0
    for i, ligne in enumerate(texte.splitlines(), start=1):
        s = ligne.strip()
        if s.count("|") >= 2:
            cellules = [c.strip() for c in s.strip("|").split("|")]
            if all(not c or _SEPARATEUR_MD.fullmatch(c) for c in cellules):
                continue
            if courant is None:
                k += 1
                courant = Tableau(fichier, f"page {page}, tableau {k}", ocr=True)
            courant.ajouter(cellules, i)
        elif courant is not None:
            out.append(courant)
            courant = None
    if courant is not None:
        out.append(courant)
    for t in out:
        nb = t.nb_colonnes
        t.lignes = [l + [""] * (nb - len(l)) for l in t.lignes]
    return [t for t in out if len(t.lignes) >= 2]


def oublier() -> None:
    """Vide le mémo de session (tests)."""
    _MEMO.clear()


__all__ = ["disponible", "pages_sans_texte", "pages_ocr", "tableaux_depuis_texte", "oublier"]
