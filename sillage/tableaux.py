"""Lecture des tableaux bruts : xlsx / xlsm, docx, pdf, csv.

Un `Tableau` est une liste de lignes de cellules texte, avec son origine
(fichier, feuille ou page) et le numéro de sa première ligne dans la source,
pour que chaque ligne pivot puisse être retrouvée.

- classeur : une feuille = un tableau ; les cellules fusionnées sont dépliées
  (valeur recopiée dans toute la zone) ; les lignes entièrement vides sont
  ignorées mais la numérotation d'origine est conservée ;
- Word : chaque table du document ;
- PDF : chaque tableau détecté par pdfplumber sur chaque page ; les tableaux
  coupés sur plusieurs pages sont recollés quand le nombre de colonnes est le
  même (l'en-tête répété est retiré) ;
- csv : délimiteur détecté.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Tableau:
    fichier: str
    emplacement: str
    lignes: List[List[str]] = field(default_factory=list)
    numeros: List[int] = field(default_factory=list)   # numéro de ligne d'origine pour chaque ligne

    @property
    def nb_colonnes(self) -> int:
        return max((len(l) for l in self.lignes), default=0)

    def ajouter(self, cellules: List[str], numero: int) -> None:
        if any(c.strip() for c in cellules):
            self.lignes.append([c.strip() for c in cellules])
            self.numeros.append(numero)


def _texte(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).replace("\r", "").strip()


# --- xlsx ---------------------------------------------------------------------
def lire_xlsx(chemin: Path) -> List[Tableau]:
    import openpyxl
    wb = openpyxl.load_workbook(str(chemin), data_only=True, read_only=False)
    out = []
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        maxr, maxc = ws.max_row or 0, ws.max_column or 0
        if maxr == 0 or maxc == 0:
            continue
        grille = [[_texte(ws.cell(r, c).value) for c in range(1, maxc + 1)] for r in range(1, maxr + 1)]
        for rng in ws.merged_cells.ranges:
            val = grille[rng.min_row - 1][rng.min_col - 1]
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    grille[r - 1][c - 1] = val
        t = Tableau(str(chemin), f"feuille « {ws.title} »")
        for i, row in enumerate(grille, start=1):
            t.ajouter(row, i)
        if t.lignes:
            out.append(t)
    wb.close()
    return out


# --- docx ---------------------------------------------------------------------
def lire_docx(chemin: Path) -> List[Tableau]:
    import docx
    doc = docx.Document(str(chemin))
    out = []
    for k, table in enumerate(doc.tables, start=1):
        t = Tableau(str(chemin), f"table {k}")
        for i, row in enumerate(table.rows, start=1):
            cellules, prev = [], None
            for cell in row.cells:
                # les cellules fusionnées horizontalement renvoient le même objet : on ne les répète pas
                if prev is not None and cell._tc is prev:
                    continue
                prev = cell._tc
                cellules.append(_texte(cell.text))
            t.ajouter(cellules, i)
        if t.lignes:
            out.append(t)
    return out


# --- pdf ----------------------------------------------------------------------
def lire_pdf(chemin: Path) -> List[Tableau]:
    import pdfplumber
    bruts: List[Tableau] = []
    with pdfplumber.open(str(chemin)) as pdf:
        for p, page in enumerate(pdf.pages, start=1):
            try:
                tables = page.extract_tables() or []
            except Exception:  # pragma: no cover - dépend du PDF
                tables = []
            for k, table in enumerate(tables, start=1):
                t = Tableau(str(chemin), f"page {p}, tableau {k}")
                for i, row in enumerate(table, start=1):
                    t.ajouter([_texte(c) for c in row], i)
                if t.lignes:
                    bruts.append(t)
    return _recoller(bruts)


def _recoller(tableaux: List[Tableau]) -> List[Tableau]:
    """Recolle les tableaux PDF de pages consécutives ayant le même nombre de colonnes."""
    out: List[Tableau] = []
    for t in tableaux:
        if out and _suite_probable(out[-1], t):
            prec = out[-1]
            lignes, numeros = t.lignes, t.numeros
            if lignes and prec.lignes and _meme_ligne(lignes[0], prec.lignes[0]):
                lignes, numeros = lignes[1:], numeros[1:]   # en-tête répété
            decal = (prec.numeros[-1] if prec.numeros else 0)
            prec.lignes.extend(lignes)
            prec.numeros.extend(decal + n for n in numeros)
            prec.emplacement = prec.emplacement.split(" à ")[0] + " à " + t.emplacement
            continue
        out.append(t)
    return out


def _suite_probable(a: Tableau, b: Tableau) -> bool:
    if a.nb_colonnes != b.nb_colonnes or a.nb_colonnes < 2:
        return False
    try:
        pa = int(a.emplacement.split("page ")[-1].split(",")[0])
        pb = int(b.emplacement.split("page ")[1].split(",")[0])
    except (ValueError, IndexError):
        return False
    return pb == pa + 1 and b.emplacement.endswith("tableau 1")


def _meme_ligne(a: List[str], b: List[str]) -> bool:
    return [x.lower() for x in a] == [x.lower() for x in b]


# --- csv ----------------------------------------------------------------------
def lire_csv(chemin: Path) -> List[Tableau]:
    data = Path(chemin).read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(data[:4096], delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
    t = Tableau(str(chemin), "csv")
    for i, row in enumerate(csv.reader(data.splitlines(), dialect), start=1):
        t.ajouter([_texte(c) for c in row], i)
    return [t] if t.lignes else []


# --- dispatch -----------------------------------------------------------------
LECTEURS = {
    ".xlsx": lire_xlsx, ".xlsm": lire_xlsx,
    ".docx": lire_docx,
    ".pdf": lire_pdf,
    ".csv": lire_csv, ".tsv": lire_csv, ".txt": lire_csv,
}


def lire_tableaux(chemin) -> List[Tableau]:
    p = Path(chemin)
    lecteur = LECTEURS.get(p.suffix.lower())
    if lecteur is None:
        raise ValueError(f"format non pris en charge : {p.suffix or '(sans extension)'} — "
                         f"attendus : {', '.join(sorted(LECTEURS))}")
    return lecteur(p)
