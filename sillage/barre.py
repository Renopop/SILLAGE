"""Texte barré : supprimé, donc ignoré.

Dans une NCM ou une spécification, un passage barré est une rédaction
remplacée (ancienne formulation de l'exigence, réponse corrigée). Il ne doit
entrer ni dans le texte de l'exigence, ni dans la justification, ni dans la
reconnaissance des identifiants et des statuts. Ce module donne, pour chaque
format, le texte **sans** ses parties barrées :

- Excel : police barrée de la cellule entière, ou portions barrées d'un
  texte enrichi (classeur ouvert avec `rich_text=True`) ;
- Word : runs barrés (simple ou double) ; les suppressions suivies
  (« w:del ») ne font déjà pas partie du texte ;
- PDF : caractères traversés à mi-hauteur par un trait horizontal (ligne ou
  rectangle fin) — un soulignement, lui, passe sous la ligne de base et est
  conservé ;
- OCR : la consigne au modèle Vision lui demande de ne pas recopier le texte
  rayé (voir `ocr.CONSIGNE_SYSTEME`).
"""

from __future__ import annotations

from typing import Any, Iterable, List

_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# --- Excel --------------------------------------------------------------------------
def texte_cellule_xlsx(cell) -> Any:
    """Valeur d'une cellule openpyxl sans ses parties barrées : la cellule
    entière si sa police est barrée (→ ""), les blocs barrés d'un texte
    enrichi sinon. Les autres valeurs (nombres, dates) sont rendues telles quelles."""
    v = cell.value
    if v is None:
        return None
    try:
        from openpyxl.cell.rich_text import CellRichText, TextBlock
    except ImportError:   # pragma: no cover - openpyxl ancien
        CellRichText = TextBlock = ()
    if CellRichText and isinstance(v, CellRichText):
        morceaux = []
        for bloc in v:
            if isinstance(bloc, TextBlock):
                if bloc.font is not None and getattr(bloc.font, "strike", None):
                    continue
                morceaux.append(str(bloc.text))
            else:
                morceaux.append(str(bloc))
        v = "".join(morceaux)
        if cell.font is not None and cell.font.strike:      # police barrée sur la cellule : le texte enrichi hérite
            return ""
        return v
    if isinstance(v, str) and cell.font is not None and cell.font.strike:
        return ""
    return v


# --- Word ---------------------------------------------------------------------------
def _run_barre(r) -> bool:
    rpr = r.find(_NS + "rPr")
    if rpr is None:
        return False
    for balise in ("strike", "dstrike"):
        el = rpr.find(_NS + balise)
        if el is not None and el.get(_NS + "val", "true").lower() not in ("0", "false", "off"):
            return True
    return False


def texte_paragraphe_docx(paragraphe) -> str:
    """Texte d'un paragraphe python-docx sans ses runs barrés (liens
    hypertextes compris, suppressions suivies exclues)."""
    morceaux: List[str] = []
    for r in paragraphe._p.iter(_NS + "r"):
        parent = r.getparent()
        if parent is not None and parent.tag == _NS + "del":
            continue
        if _run_barre(r):
            continue
        for el in r:
            if el.tag == _NS + "t":
                morceaux.append(el.text or "")
            elif el.tag in (_NS + "tab",):
                morceaux.append("\t")
            elif el.tag in (_NS + "br", _NS + "cr"):
                morceaux.append("\n")
    return "".join(morceaux)


def texte_cellule_docx(cellule) -> str:
    return "\n".join(texte_paragraphe_docx(p) for p in cellule.paragraphs)


# --- PDF ----------------------------------------------------------------------------
def _segments_horizontaux(page) -> List[tuple]:
    segs = []
    for l in page.lines:
        if abs(l["top"] - l["bottom"]) <= 2.5 and l["x1"] - l["x0"] >= 3:
            segs.append((l["x0"], l["x1"], (l["top"] + l["bottom"]) / 2))
    for r in page.rects:
        if r["bottom"] - r["top"] <= 2.5 and r["x1"] - r["x0"] >= 3:
            segs.append((r["x0"], r["x1"], (r["top"] + r["bottom"]) / 2))
    return segs


def page_sans_barre(page):
    """Page pdfplumber dont les caractères barrés (traversés à mi-hauteur par
    un trait horizontal) sont retirés ; les autres objets sont conservés."""
    segs = _segments_horizontaux(page)
    if not segs:
        return page

    def garder(obj) -> bool:
        if obj.get("object_type") != "char":
            return True
        xm = (obj["x0"] + obj["x1"]) / 2
        ym = (obj["top"] + obj["bottom"]) / 2
        tol = max(1.0, 0.35 * (obj["bottom"] - obj["top"]))
        return not any(x0 - 1 <= xm <= x1 + 1 and abs(y - ym) <= tol for x0, x1, y in segs)

    return page.filter(garder)


def pages_sans_barre(pages: Iterable) -> List:
    return [page_sans_barre(p) for p in pages]
