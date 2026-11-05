"""Lot 4 — traçabilité des exigences vers la norme (CS-25, FAR 25, AMC…).

Trois temps :

1. **Proposer** (`tracer`) : la norme est indexée par le moteur embarqué
   (`sillage indexer <dossier de la norme> --index <index norme>`). Pour chaque
   exigence sans paragraphe de norme, une question RIVAGE (le texte de
   l'exigence) dans l'index de la norme ; les références de paragraphes
   (« CS 25.1309 », « AMC 25.1309 ») sont extraites des meilleurs passages,
   chacune avec le score de reranking du passage où elle apparaît. Le
   meilleur candidat au-dessus du seuil est **proposé** comme paragraphe
   retenu ; rien n'est figé.
2. **Relire** : la proposition s'exporte en classeur (une ligne par
   exigence : paragraphe proposé, candidats et scores, passage, document) ;
   l'ingénieur corrige la colonne « Norme retenue » et coche « Validé ».
3. **Figer** (`importer_tracabilite` puis `appliquer`) : le classeur relu
   devient la traçabilité du référentiel (champ `norme` des exigences). La
   grille signale alors tout écart sur une exigence tracée (« impact
   norme ») et liste la couverture de la norme : chaque paragraphe cité dans
   les documents de la norme, et les exigences qui s'y rattachent.

L'applicabilité d'un paragraphe reste une décision humaine : SILLAGE liste,
il ne tranche pas.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .referentiel import Referentiel
from .rivage import Moteur, Passage

# Références de paragraphes : « CS 25.1309 », « CS-25.1309(b) », « AMC 25.1309 », « FAR 25.1309 »,
# « 14 CFR 25.1309 », « CS-E 510 », « JAR 25.1309 ».
MOTIF_NORME = re.compile(
    r"(?<![A-Za-z0-9])(?:(CS-E|CS-APU|CS-P|CS-VLA|CS|AMC|FAR|JAR)[\s\-]*(\d{1,3})[.,](\d{1,4})([a-z]?)"
    r"|(14\s*CFR|CFR)\s*(\d{2})[.,](\d{3,4}))(?![0-9])", re.IGNORECASE)

SEUIL_RETENUE = 0.3     # score de reranking minimal pour proposer un paragraphe « retenu »


def references_norme(texte: str, motif: Optional[re.Pattern] = None) -> List[str]:
    """Références de paragraphes citées dans `texte`, normalisées (« CS 25.1309 », « AMC 25.1309 »,
    « FAR 25.1309 »), sans doublon, dans l'ordre d'apparition."""
    motif = motif or MOTIF_NORME
    out: List[str] = []
    for m in motif.finditer(texte or ""):
        if m.group(1):
            ref = f"{m.group(1).upper()} {m.group(2)}.{m.group(3)}"
        else:
            ref = f"FAR {m.group(6)}.{m.group(7)}"
        if ref not in out:
            out.append(ref)
    return out


def cle_norme(ref: str) -> Tuple[str, int, int]:
    m = re.match(r"([A-Z\-]+) (\d+)\.(\d+)", ref)
    return (m.group(1), int(m.group(2)), int(m.group(3))) if m else (ref, 0, 0)


@dataclass
class Candidat:
    norme: str
    score: Optional[float]
    passage: Passage

    def to_dict(self) -> dict:
        return {"norme": self.norme, "score": self.score, "passage": self.passage.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> "Candidat":
        return cls(d["norme"], d.get("score"), Passage.from_dict(d.get("passage", {})))


@dataclass
class Trace:
    id: str
    retenue: str = ""                 # paragraphe retenu (proposé, puis corrigé par l'ingénieur)
    validee: bool = False             # relu et validé
    candidats: List[Candidat] = field(default_factory=list)
    origine: str = ""                 # « référentiel » (déjà dans la spec), « proposée », « relue »
    erreur: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "retenue": self.retenue, "validee": self.validee, "origine": self.origine,
                "erreur": self.erreur, "candidats": [c.to_dict() for c in self.candidats]}

    @classmethod
    def from_dict(cls, d: dict) -> "Trace":
        return cls(id=d["id"], retenue=d.get("retenue", ""), validee=bool(d.get("validee", False)),
                   candidats=[Candidat.from_dict(c) for c in d.get("candidats", [])],
                   origine=d.get("origine", ""), erreur=d.get("erreur", ""))


@dataclass
class Tracabilite:
    index_dir: str = ""
    prefixes: List[str] = field(default_factory=list)
    seuil_retenue: float = SEUIL_RETENUE
    traces: Dict[str, Trace] = field(default_factory=dict)

    def resume(self) -> Dict[str, int]:
        r = {"total": len(self.traces), "retenues": 0, "validees": 0, "sans_candidat": 0, "referentiel": 0, "erreurs": 0}
        for t in self.traces.values():
            if t.retenue:
                r["retenues"] += 1
            if t.validee:
                r["validees"] += 1
            if not t.candidats and t.origine != "référentiel":
                r["sans_candidat"] += 1
            if t.origine == "référentiel":
                r["referentiel"] += 1
            if t.erreur:
                r["erreurs"] += 1
        return r

    def texte_resume(self) -> str:
        r = self.resume()
        return (f"Traçabilité : {r['total']} exigences — paragraphe retenu : {r['retenues']} "
                f"(dont déjà dans la spécification : {r['referentiel']}), validées : {r['validees']}, "
                f"sans candidat : {r['sans_candidat']}" + (f", erreurs : {r['erreurs']}" if r["erreurs"] else ""))

    def to_dict(self) -> dict:
        return {"sillage": "tracabilite", "version": 1, "index_dir": self.index_dir, "prefixes": self.prefixes,
                "seuil_retenue": self.seuil_retenue, "traces": [t.to_dict() for t in self.traces.values()]}

    @classmethod
    def from_dict(cls, d: dict) -> "Tracabilite":
        if d.get("sillage") != "tracabilite":
            raise ValueError("ce fichier n'est pas une traçabilité SILLAGE")
        t = cls(index_dir=d.get("index_dir", ""), prefixes=list(d.get("prefixes", [])),
                seuil_retenue=d.get("seuil_retenue", SEUIL_RETENUE))
        for x in d.get("traces", []):
            tr = Trace.from_dict(x)
            t.traces[tr.id] = tr
        return t

    def enregistrer(self, chemin) -> None:
        Path(chemin).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def charger(cls, chemin) -> "Tracabilite":
        p = Path(chemin)
        if p.suffix.lower() in (".xlsx", ".xlsm", ".csv"):
            return importer_tracabilite(p)
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))


def tracer(ref: Referentiel, moteur: Moteur, prefixes: Optional[Sequence[str]] = None,
           fichiers: Optional[Sequence[str]] = None, seulement_sans_norme: bool = True,
           n_passages: int = 5, seuil_retenue: float = SEUIL_RETENUE, motif: Optional[re.Pattern] = None,
           progression: Optional[Callable[[int, int, str], None]] = None) -> Tracabilite:
    """Propose, pour chaque exigence (sans paragraphe de norme, par défaut), les
    paragraphes de la norme cités par ses meilleurs passages dans l'index de la
    norme. Un embedding, une recherche hybride, un reranking par exigence."""
    t = Tracabilite(index_dir=moteur.index_dir, prefixes=list(prefixes or []), seuil_retenue=seuil_retenue)
    cibles = []
    for e in ref.exigences:
        if e.norme and seulement_sans_norme:
            t.traces[e.id] = Trace(id=e.id, retenue=_premiere_reference(e.norme), validee=True, origine="référentiel")
            continue
        cibles.append(e)
    for k, e in enumerate(cibles, start=1):
        if progression:
            progression(k, len(cibles), e.id)
        tr = Trace(id=e.id, origine="proposée")
        q = " ".join((e.texte or "").split()) or e.id_brut or e.id
        try:
            passages, reranked = moteur.passages(q, path_prefixes=prefixes, allowed_paths=fichiers,
                                                 rerank_k=max(1, n_passages))
        except Exception as exc:
            tr.erreur = f"{type(exc).__name__}: {exc}"
            passages, reranked = [], False
        meilleurs: Dict[str, Candidat] = {}
        for p in passages[:n_passages]:
            # Un passage peut citer plusieurs paragraphes (fin de l'un, titre du suivant, renvoi) :
            # la première référence du passage est la plus probable, les suivantes sont légèrement pénalisées.
            for k, refn in enumerate(references_norme(p.texte, motif)):
                score = (max(0.0, p.score - 0.02 * k) if p.score is not None else None) if reranked else None
                if refn not in meilleurs or (score or 0) > (meilleurs[refn].score or 0):
                    meilleurs[refn] = Candidat(refn, score, p)
        tr.candidats = sorted(meilleurs.values(), key=lambda c: -(c.score or 0))
        if tr.candidats and reranked and (tr.candidats[0].score or 0) >= seuil_retenue:
            tr.retenue = tr.candidats[0].norme
        if e.norme and not tr.retenue:
            tr.retenue = _premiere_reference(e.norme)
        t.traces[e.id] = tr
    return t


def _premiere_reference(texte: str) -> str:
    refs = references_norme(texte)
    return refs[0] if refs else texte.strip()


def appliquer(ref: Referentiel, t: Tracabilite, validees_seulement: bool = False) -> int:
    """Reporte les paragraphes retenus dans le champ `norme` des exigences ; renvoie le nombre d'exigences tracées."""
    n = 0
    for e in ref.exigences:
        tr = t.traces.get(e.id)
        if tr is None or not tr.retenue or (validees_seulement and not tr.validee):
            continue
        e.norme = tr.retenue
        n += 1
    return n


# --- relecture en classeur ------------------------------------------------------------------
COLONNES_TRACABILITE = ["Identifiant", "Exigence", "Norme retenue", "Validé", "Candidats (score)",
                        "Passage", "Document", "Origine", "Erreur"]


def exporter_tracabilite(ref: Referentiel, t: Tracabilite, chemin) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "Traçabilité"
    ws.append(COLONNES_TRACABILITE)
    for i, w in enumerate((14, 60, 16, 8, 40, 60, 30, 12, 30), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
        c = ws.cell(1, i)
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0B4F6C")
    ws.freeze_panes = "A2"
    exigences = ref.par_id()
    for ident in ref.ids:
        tr = t.traces.get(ident)
        e = exigences[ident]
        if tr is None:
            ws.append([ident, e.texte, e.norme, "", "", "", "", "", ""])
            continue
        meilleur = tr.candidats[0].passage if tr.candidats else None
        ws.append([
            ident, e.texte, tr.retenue, "oui" if tr.validee else "non",
            " ; ".join(f"{c.norme} ({c.score:.2f})" if c.score is not None else c.norme for c in tr.candidats),
            " ".join(meilleur.texte.split())[:600] if meilleur else "",
            (Path(meilleur.chemin).name + (f", page {meilleur.page}" if meilleur.page else "")) if meilleur else "",
            tr.origine, tr.erreur,
        ])
        r = ws.max_row
        for col in (2, 5, 6):
            ws.cell(r, col).alignment = Alignment(wrap_text=True, vertical="top")
        if not tr.retenue:
            ws.cell(r, 3).fill = PatternFill("solid", fgColor="FFC7CE")
        elif not tr.validee:
            ws.cell(r, 3).fill = PatternFill("solid", fgColor="FFEB9C")
    ws.auto_filter.ref = ws.dimensions
    p = Path(chemin)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(p))
    return p


def importer_tracabilite(chemin) -> Tracabilite:
    """Le classeur relu par l'ingénieur : colonnes « Identifiant », « Norme retenue », « Validé »."""
    from .identifiants import MotifIdentifiant
    from .profils import normaliser_en_tete
    from .tableaux import lire_tableaux
    motif = MotifIdentifiant()
    t = Tracabilite()
    for tab in lire_tableaux(chemin):
        if not tab.lignes:
            continue
        en_tetes = [normaliser_en_tete(h) for h in tab.lignes[0]]
        def col(*noms):
            for n in noms:
                if n in en_tetes:
                    return en_tetes.index(n)
            return None
        c_id, c_norme, c_valide = col("identifiant", "id"), col("norme retenue", "norme"), col("valide", "validee")
        if c_id is None or c_norme is None:
            continue
        for l in tab.lignes[1:]:
            cell = l[c_id] if c_id < len(l) else ""
            ids = motif.extraire(cell) if len(cell) <= 80 else []
            if not ids:
                continue
            norme = (l[c_norme] if c_norme < len(l) else "").strip()
            valide = (l[c_valide] if c_valide is not None and c_valide < len(l) else "").strip().lower() in ("oui", "yes", "x", "1", "vrai", "true")
            for ident in ids:
                t.traces[ident] = Trace(id=ident, retenue=_premiere_reference(norme) if norme else "", validee=valide,
                                        origine="relue")
    if not t.traces:
        raise ValueError("aucune ligne de traçabilité (colonnes attendues : Identifiant, Norme retenue, Validé)")
    return t


# --- couverture de la norme --------------------------------------------------------------------
@dataclass
class Couverture:
    paragraphes: Dict[str, int] = field(default_factory=dict)          # paragraphe -> occurrences dans les documents de la norme
    exigences: Dict[str, List[str]] = field(default_factory=dict)      # paragraphe -> identifiants tracés

    def lignes(self) -> List[Tuple[str, int, List[str]]]:
        tous = set(self.paragraphes) | set(self.exigences)
        return [(p, self.paragraphes.get(p, 0), self.exigences.get(p, [])) for p in sorted(tous, key=cle_norme)]

    def non_couverts(self) -> List[str]:
        return [p for p, _n, ids in self.lignes() if not ids]

    def texte_resume(self) -> str:
        lignes = self.lignes()
        return (f"Couverture de la norme : {len(lignes)} paragraphe(s) cité(s), "
                f"{sum(1 for _p, _n, ids in lignes if ids)} avec au moins une exigence, "
                f"{len(self.non_couverts())} sans exigence (applicabilité à décider)")


def paragraphes_cites(fichiers: Iterable[str], motif: Optional[re.Pattern] = None) -> Dict[str, int]:
    """Paragraphes de norme cités dans des documents (la norme elle-même, ou une liste d'applicabilité)."""
    from .textes import paragraphes
    out: Dict[str, int] = {}
    for f in fichiers:
        try:
            paras = paragraphes(f)
        except Exception:
            continue
        for para in paras:
            for r in references_norme(para.texte, motif):
                out[r] = out.get(r, 0) + 1
    return out


def couverture(ref: Referentiel, cites: Optional[Dict[str, int]] = None) -> Couverture:
    c = Couverture(paragraphes=dict(cites or {}))
    for e in ref.exigences:
        for r in references_norme(e.norme):
            c.exigences.setdefault(r, []).append(e.id)
    return c
