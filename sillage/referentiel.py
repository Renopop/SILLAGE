"""Le référentiel : la spécification sous forme de liste d'exigences identifiées.

Deux sources :

- un **tableur** (xlsx, csv) avec une colonne d'identifiants, une colonne de
  texte (la plus longue) et, si présente, une colonne de norme (en-tête
  contenant « norme », « standard », « CS », « trace »…) ;
- un **document** (docx, txt, md, pdf) : chaque paragraphe qui commence par
  un identifiant est une exigence ; les paragraphes suivants sans identifiant
  la complètent jusqu'au prochain identifiant ou titre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .identifiants import MotifIdentifiant, deduire_motif, trier
from .modele import Exigence
from .profils import normaliser_en_tete
from .tableaux import lire_tableaux
from .textes import Para, paragraphes_docx, paragraphes_pdf, paragraphes_texte

_MOTS_NORME = ("norme", "standard", "cs 25", "cs25", "cs-25", "far", "part 25", "trace", "tracab", "regulation",
               "reglement", "règlement", "exigence source", "source requirement", "applicable")
_MOTS_TEXTE = ("exigence", "requirement", "texte", "text", "description", "libelle", "libellé", "enonce", "énoncé", "statement")


@dataclass
class Referentiel:
    source: str = ""
    exigences: List[Exigence] = field(default_factory=list)
    motif: MotifIdentifiant = field(default_factory=MotifIdentifiant)

    @property
    def ids(self) -> List[str]:
        return [e.id for e in self.exigences]

    def par_id(self) -> Dict[str, Exigence]:
        return {e.id: e for e in self.exigences}

    def __len__(self) -> int:
        return len(self.exigences)


def lire_referentiel(chemin, motif: Optional[MotifIdentifiant] = None) -> Referentiel:
    p = Path(chemin)
    ext = p.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".csv", ".tsv"):
        exigences = _depuis_tableur(p, motif or MotifIdentifiant())
    elif ext == ".docx":
        exigences = _depuis_paragraphes(p, paragraphes_docx(p), motif or MotifIdentifiant())
    elif ext in (".txt", ".md"):
        exigences = _depuis_paragraphes(p, paragraphes_texte(p), motif or MotifIdentifiant())
    elif ext == ".pdf":
        exigences = _depuis_paragraphes(p, paragraphes_pdf(p), motif or MotifIdentifiant())
    else:
        raise ValueError(f"format de référentiel non pris en charge : {ext or '(sans extension)'}")
    exigences = _dedoublonner(exigences)
    ref = Referentiel(source=str(p), exigences=exigences)
    ref.motif = motif or deduire_motif(ref.ids)
    return ref


def _dedoublonner(exigences: List[Exigence]) -> List[Exigence]:
    vus: Dict[str, Exigence] = {}
    for e in exigences:
        if e.id in vus:
            if e.texte and e.texte not in vus[e.id].texte:
                vus[e.id].texte = (vus[e.id].texte + "\n" + e.texte).strip()
            continue
        vus[e.id] = e
    return [vus[i] for i in trier(list(vus))]


# --- tableur -------------------------------------------------------------------
def _depuis_tableur(p: Path, motif: MotifIdentifiant) -> List[Exigence]:
    out: List[Exigence] = []
    for t in lire_tableaux(p):
        nb = t.nb_colonnes
        if nb == 0:
            continue
        def cell(l, c):
            return l[c] if c < len(l) else ""
        scores = [sum(1 for l in t.lignes if len(cell(l, c)) <= 80 and motif.trouver(cell(l, c))) for c in range(nb)]
        col_id = max(range(nb), key=lambda c: scores[c])
        if scores[col_id] == 0:
            continue
        en_tete = 0 if not motif.trouver(cell(t.lignes[0], col_id)) else None
        en_tetes = [cell(t.lignes[0], c) for c in range(nb)] if en_tete == 0 else [""] * nb
        donnees = t.lignes[1:] if en_tete == 0 else t.lignes
        numeros = t.numeros[1:] if en_tete == 0 else t.numeros
        col_norme = None
        col_texte = None
        for c, h in enumerate(en_tetes):
            nh = normaliser_en_tete(h)
            if c == col_id or not nh:
                continue
            if col_norme is None and any(m in nh for m in (normaliser_en_tete(x) for x in _MOTS_NORME)):
                col_norme = c
            elif col_texte is None and any(m in nh for m in (normaliser_en_tete(x) for x in _MOTS_TEXTE)):
                col_texte = c
        if col_texte is None:
            cand = [c for c in range(nb) if c not in (col_id, col_norme)]
            if cand:
                col_texte = max(cand, key=lambda c: sum(len(cell(l, c)) for l in donnees))
        for l, num in zip(donnees, numeros):
            ids = motif.extraire(cell(l, col_id)) if len(cell(l, col_id)) <= 80 else []
            if not ids:
                continue
            extra = {en_tetes[c] or f"colonne {c + 1}": cell(l, c) for c in range(nb)
                     if c not in (col_id, col_texte, col_norme) and cell(l, c)}
            for ident in ids:
                out.append(Exigence(id=ident, texte=cell(l, col_texte) if col_texte is not None else "",
                                    id_brut=cell(l, col_id), source=str(p),
                                    reference=f"{t.emplacement}, ligne {num}",
                                    norme=cell(l, col_norme) if col_norme is not None else "", extra=extra))
    return out


# --- documents -------------------------------------------------------------------
def _depuis_paragraphes(p: Path, paras: Iterable[Para], motif: MotifIdentifiant) -> List[Exigence]:
    out: List[Exigence] = []
    courante: Optional[Exigence] = None
    for para in paras:
        # une ligne de PDF peut contenir plusieurs exigences : on coupe devant chaque identifiant en tête de ligne
        ident = motif.commence_par_id(para.texte)
        if ident:
            occ = motif.trouver(para.texte)
            texte = para.texte[occ[0][3]:].lstrip(" \t:-–—.·")
            courante = Exigence(id=ident, texte=texte, id_brut=occ[0][1], source=str(p), reference=para.reference)
            out.append(courante)
        elif para.titre:
            courante = None
        elif courante is not None:
            courante.texte = (courante.texte + "\n" + para.texte).strip()
    return out
