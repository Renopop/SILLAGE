"""Lecture des tableaux bruts : xlsx / xlsm, docx, pdf, csv.

Un `Tableau` est une liste de lignes de cellules texte, avec son origine
(fichier, feuille ou page) et le numéro de sa première ligne dans la source,
pour que chaque ligne pivot puisse être retrouvée.

- classeur : une feuille = un tableau ; les cellules fusionnées sont dépliées
  (valeur recopiée dans toute la zone) ; les lignes entièrement vides sont
  ignorées mais la numérotation d'origine est conservée ;
- Word : chaque table du document ;
- PDF : chaque tableau détecté par pdfplumber sur chaque page ; sans
  quadrillage, la page est lue ligne par ligne et les colonnes sont déduites
  des alignements du texte (`tableau_par_alignements`), les cellules hautes
  ressortant en lignes de suite que `lecture_ncm` recolle ; les tableaux
  coupés sur plusieurs pages sont recollés quand le nombre de colonnes est le
  même (l'en-tête répété est retiré) ; les pages scannées passent par l'OCR
  de secours si demandé ;
- csv : délimiteur détecté.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class Tableau:
    fichier: str
    emplacement: str
    lignes: List[List[str]] = field(default_factory=list)
    numeros: List[int] = field(default_factory=list)   # numéro de ligne d'origine pour chaque ligne
    ocr: bool = False                                   # tableau reconstruit depuis le texte OCR d'une page scannée

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
def tableau_par_alignements(page, fichier: str, numero_page: int) -> List[Tableau]:
    """Tableau d'une page PDF sans quadrillage, lu **ligne par ligne** : les
    mots sont regroupés en lignes de texte (même hauteur), les débuts de
    colonnes sont les abscisses où commencent des segments de texte (après un
    blanc large) sur au moins trois lignes, et chaque ligne de texte devient
    une ligne du tableau. Une cellule haute ressort donc en plusieurs lignes
    de suite, que la lecture de la NCM recolle (`lecture_ncm._regrouper`)."""
    mots = page.extract_words(x_tolerance=1.5, y_tolerance=2)
    if len(mots) < 6:
        return []
    lignes: List[List[dict]] = []
    for w in sorted(mots, key=lambda w: (w["top"], w["x0"])):
        if lignes and abs(lignes[-1][0]["top"] - w["top"]) <= 3:
            lignes[-1].append(w)
        else:
            lignes.append([w])
    for l in lignes:
        l.sort(key=lambda w: w["x0"])
    # débuts de segments : premier mot de la ligne, ou mot précédé d'un blanc large (> 1,2 × hauteur)
    debuts: List[float] = []
    for l in lignes:
        prec = None
        for w in l:
            if prec is None or w["x0"] - prec["x1"] > 1.2 * max(w["height"], 4.0):
                debuts.append(w["x0"])
            prec = w
    debuts.sort()
    groupes: List[List[float]] = []
    for x in debuts:
        if groupes and x - groupes[-1][-1] <= 4:
            groupes[-1].append(x)
        else:
            groupes.append([x])
    minimum = max(3, len(lignes) // 5)
    colonnes = sorted(sum(g) / len(g) for g in groupes if len(g) >= minimum)
    # valeurs centrées dans leur colonne (« Compliant », « Partially compliant », « N/A » ne
    # commencent pas à la même abscisse) : deux débuts voisins qui ne coexistent jamais sur
    # une même ligne appartiennent à la même colonne
    def debuts_ligne(l):
        out, prec = [], None
        for w in l:
            if prec is None or w["x0"] - prec["x1"] > 1.2 * max(w["height"], 4.0):
                out.append(w["x0"])
            prec = w
        return out
    lignes_debuts = [debuts_ligne(l) for l in lignes]
    fusionnees: List[float] = []
    for x in colonnes:
        if fusionnees:
            a = fusionnees[-1]
            coexistent = sum(1 for d in lignes_debuts
                             if any(abs(v - a) <= 4 for v in d) and any(abs(v - x) <= 4 for v in d))
            if coexistent < 2:
                continue
        fusionnees.append(x)
    colonnes = fusionnees
    if len(colonnes) < 2:
        return []
    t = Tableau(fichier, f"page {numero_page}, tableau 1")
    for i, l in enumerate(lignes, start=1):
        cellules = [""] * len(colonnes)
        for w in l:
            c = max((k for k, x in enumerate(colonnes) if x - 3 <= w["x0"]), default=0)
            cellules[c] = (cellules[c] + " " + w["text"]).strip() if cellules[c] else w["text"]
        t.ajouter(cellules, i)
    return [t] if len(t.lignes) >= 2 else []


def lire_pdf(chemin: Path, ocr=False, alignements: bool = False) -> List[Tableau]:
    """Tableaux détectés par pdfplumber ; avec `ocr` vrai, les pages sans
    couche texte passent par l'OCR de secours et leurs tableaux sont
    reconstruits depuis le texte rendu (voir `ocr.tableaux_depuis_texte`) ;
    avec `ocr == "forcer"`, toutes les pages passent par l'OCR, couche texte
    ignorée. Avec `alignements`, le quadrillage est ignoré et chaque page de
    texte est lue ligne par ligne (`tableau_par_alignements`) : c'est le
    repli quand les traits détectés ne donnent rien d'exploitable."""
    import pdfplumber
    bruts: List[Tableau] = []
    scannees: List[int] = []
    forcer = ocr == "forcer"
    with pdfplumber.open(str(chemin)) as pdf:
        for p, page in enumerate(pdf.pages, start=1):
            if forcer:
                scannees.append(p)
                continue
            tables = []
            if not alignements:
                try:
                    tables = page.extract_tables() or []
                except Exception:  # pragma: no cover - dépend du PDF
                    tables = []
            texte_page = (page.extract_text() or "").strip()
            if not tables and texte_page:
                # pas de quadrillage : lecture ligne par ligne, colonnes déduites des alignements
                bruts.extend(tableau_par_alignements(page, str(chemin), p))
                continue
            if not tables and ocr and not texte_page:
                scannees.append(p)
            for k, table in enumerate(tables, start=1):
                t = Tableau(str(chemin), f"page {p}, tableau {k}")
                for i, row in enumerate(table, start=1):
                    t.ajouter([_texte(c) for c in row], i)
                if t.lignes:
                    bruts.append(t)
    if scannees:
        from .ocr import pages_ocr, tableaux_depuis_texte
        textes = pages_ocr(chemin, scannees)
        for p in scannees:
            bruts.extend(tableaux_depuis_texte(textes.get(p, ""), str(chemin), p))
        bruts.sort(key=_page)
    return _recoller(bruts)


def _page(t: Tableau) -> int:
    try:
        return int(t.emplacement.split("page ")[1].split(",")[0])
    except (ValueError, IndexError):
        return 0


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


def lire_tableaux(chemin, ocr=False, alignements: bool = False) -> List[Tableau]:
    """`ocr` : OCR de secours des pages scannées (vrai) ou de toutes les pages
    (« forcer ») ; `alignements` : lecture ligne par ligne sans tenir compte
    du quadrillage (PDF seulement)."""
    p = Path(chemin)
    lecteur = LECTEURS.get(p.suffix.lower())
    if lecteur is None:
        raise ValueError(f"format non pris en charge : {p.suffix or '(sans extension)'} — "
                         f"attendus : {', '.join(sorted(LECTEURS))}")
    if lecteur is lire_pdf:
        return lire_pdf(p, ocr=ocr, alignements=alignements)
    return lecteur(p)
