"""Lecture des documents en paragraphes (docx, txt, md, pdf, tableurs).

Sert au référentiel (exigences dans un document) et à la campagne lexicale
des affaiblissements. Pour la recherche par le sens, c'est RIVAGE qui lit.
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


def paragraphes_pdf(p: Path) -> List[Para]:
    import pdfplumber
    out = []
    with pdfplumber.open(str(p)) as pdf:
        for n, page in enumerate(pdf.pages, start=1):
            texte = page.extract_text() or ""
            for bloc in re.split(r"\n\s*\n", texte):
                lignes = [x.strip() for x in bloc.splitlines() if x.strip()]
                if lignes:
                    out.append(Para(" ".join(lignes), f"page {n}"))
    return out


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


def paragraphes(chemin) -> List[Para]:
    p = Path(chemin)
    lecteur = LECTEURS.get(p.suffix.lower())
    if lecteur is None:
        raise ValueError(f"format non pris en charge : {p.suffix or '(sans extension)'}")
    return lecteur(p)
