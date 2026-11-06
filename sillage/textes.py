"""Lecture des documents en paragraphes (docx, txt, md, pdf, tableurs).

Sert au référentiel (exigences dans un document) et à la campagne lexicale
des affaiblissements. Pour la recherche par le sens, c'est le moteur embarqué qui lit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class Para:
    texte: str
    reference: str = ""     # paragraphe n, page n, table k ligne r, feuille x ligne r
    titre: bool = False
    fragment: bool = False  # ligne d'un paragraphe (PDF) : se recolle à la précédente par un espace


# Titre de section : « 5.1.2 Control thrust », « 3. Alimentation », « Chapter 5 », « Annex A ».
_TITRE = re.compile(r"^(?:\d{1,2}(?:\.\d{1,3}){1,6}\.?\s+[^\W\d]|\d{1,2}\.\s+[^\W\d]"
                    r"|(?:chapter|chapitre|section|annex|annexe|appendix)\s+[A-Z0-9])", re.IGNORECASE)
# Bruit de mise en page : marques « ** », filets, numéros de page.
_BRUIT = re.compile(r"^(?:\*+|[-–—_=·]+|\d{1,4}|page\s*\d+(?:\s*(?:/|of|sur|de)\s*\d+)?|\d{1,4}\s*/\s*\d{1,4})$",
                    re.IGNORECASE)


def est_titre(ligne: str) -> bool:
    return bool(_TITRE.match(ligne.strip()))


def paragraphes_docx(p: Path) -> List[Para]:
    import docx
    doc = docx.Document(str(p))
    out = []
    for i, para in enumerate(doc.paragraphs, start=1):
        txt = para.text.strip()
        if not txt:
            continue
        style = (para.style.name if para.style is not None else "") or ""
        titre = style.lower().startswith(("heading", "titre", "title"))
        out.append(Para(txt, f"paragraphe {i}", titre))
    for k, table in enumerate(doc.tables, start=1):
        for r, row in enumerate(table.rows, start=1):
            cellules, prev = [], None
            for cell in row.cells:
                if prev is not None and cell._tc is prev:
                    continue
                prev = cell._tc
                cellules.append(cell.text.strip())
            txt = " — ".join(c for c in cellules if c)
            if txt:
                out.append(Para(txt, f"table {k}, ligne {r}"))
    return out


def paragraphes_texte(p: Path) -> List[Para]:
    data = p.read_text(encoding="utf-8-sig", errors="replace")
    out = []
    for i, bloc in enumerate(re.split(r"\n\s*\n", data), start=1):
        txt = " ".join(x.strip() for x in bloc.splitlines() if x.strip())
        if txt:
            out.append(Para(txt, f"bloc {i}", titre=txt.startswith("#")))
    return out


def paragraphes_pdf(p: Path, ocr: bool = False) -> List[Para]:
    """Une `Para` par ligne de texte (fragment), les titres de section repérés
    par leur numérotation. Les lignes répétées sur au moins trois pages
    (en-tête et pied courants) et le bruit de mise en page (« ** », numéros
    de page) sont ignorés. Un PDF sans couche texte ne donne rien : c'est un
    scan ; avec `ocr`, ses pages passent par l'OCR de secours (appels payants)."""
    import pdfplumber
    from collections import Counter
    out = []
    with pdfplumber.open(str(p)) as pdf:
        pages = [(page.extract_text() or "") for page in pdf.pages]
    if ocr:
        scannees = [n for n, texte in enumerate(pages, start=1) if not texte.strip()]
        if scannees:
            from .ocr import pages_ocr
            for n, texte in pages_ocr(p, scannees).items():
                pages[n - 1] = texte
    compte = Counter()
    for texte in pages:
        compte.update({x.strip() for x in texte.splitlines() if x.strip()})
    courantes = {l for l, n in compte.items() if n >= 3} if len(pages) >= 3 else set()
    for n, texte in enumerate(pages, start=1):
        for ligne in texte.splitlines():
            l = ligne.strip()
            if not l or l in courantes or _BRUIT.fullmatch(l):
                continue
            out.append(Para(l, f"page {n}", titre=est_titre(l), fragment=True))
    return out


def pdf_sans_texte(p: Path) -> bool:
    """Vrai si aucune page du PDF n'a de couche texte (document scanné)."""
    import pdfplumber
    with pdfplumber.open(str(p)) as pdf:
        return not any((page.extract_text() or "").strip() for page in pdf.pages)


def paragraphes_tableur(p: Path) -> List[Para]:
    from .tableaux import lire_tableaux
    out = []
    for t in lire_tableaux(p):
        for cellules, num in zip(t.lignes, t.numeros):
            txt = " — ".join(c for c in cellules if c)
            if txt:
                out.append(Para(txt, f"{t.emplacement}, ligne {num}"))
    return out


LECTEURS = {
    ".docx": paragraphes_docx,
    ".txt": paragraphes_texte, ".md": paragraphes_texte,
    ".pdf": paragraphes_pdf,
    ".xlsx": paragraphes_tableur, ".xlsm": paragraphes_tableur, ".csv": paragraphes_tableur,
}


def paragraphes(chemin, ocr: bool = False) -> List[Para]:
    """`ocr` : OCR de secours des pages scannées (PDF seulement)."""
    p = Path(chemin)
    lecteur = LECTEURS.get(p.suffix.lower())
    if lecteur is None:
        raise ValueError(f"format non pris en charge : {p.suffix or '(sans extension)'}")
    if lecteur is paragraphes_pdf:
        return paragraphes_pdf(p, ocr=ocr)
    return lecteur(p)
