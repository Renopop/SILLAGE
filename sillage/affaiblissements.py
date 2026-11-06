"""Campagne lexicale des affaiblissements.

Un fournisseur qui se ménage une sortie l'écrit : « TBD », « TBC », « where
practicable », « best effort », « sous réserve », « dans la mesure du
possible », « except », « not covered »… Ces expressions se cherchent mot à
mot dans les documents du fournisseur (spécification, NCM) ; chaque
occurrence est rattachée aux identifiants d'exigences cités dans le même
paragraphe. Déterministe, sans le moteur de recherche ni la passerelle.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .identifiants import MotifIdentifiant
from .textes import paragraphes

EXPRESSIONS = [
    # anglais
    "TBD", "TBC", "TBS", "TBR", "to be defined", "to be confirmed", "to be determined", "to be specified",
    "where practicable", "where feasible", "as far as practicable", "as far as possible", "if possible",
    "best effort", "best endeavours", "best endeavors", "subject to", "except", "excluding", "excluded",
    "limited to", "not covered", "not included", "not applicable", "not yet", "later issue", "open point",
    "open item", "will be defined", "shall be defined", "to be agreed", "under discussion", "pending",
    "partially", "where applicable", "as appropriate", "reasonable efforts", "may", "should",
    # français
    "à définir", "à confirmer", "à préciser", "à valider", "dans la mesure du possible", "si possible",
    "sous réserve", "sauf", "hors périmètre", "non couvert", "non inclus", "non traité", "ultérieurement",
    "point ouvert", "en attente", "partiellement", "le cas échéant", "au mieux", "selon faisabilité",
    "à négocier", "à discuter",
]

# Expressions trop fréquentes pour être signalées seules : seulement si un identifiant est cité à proximité.
_FAIBLES = {"may", "should", "except", "sauf", "partially", "partiellement", "where applicable", "as appropriate",
            "le cas échéant", "pending", "en attente", "not applicable", "subject to", "sous réserve"}


def _sans_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _compiler(expressions: Iterable[str]) -> List[tuple]:
    out = []
    for e in expressions:
        n = _sans_accents(e)
        motif = r"(?<![a-z0-9])" + r"\s+".join(re.escape(m) for m in n.split()) + r"(?![a-z0-9])"
        out.append((e, re.compile(motif)))
    return out


@dataclass
class Occurrence:
    fichier: str
    reference: str
    expression: str
    contexte: str
    ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"fichier": self.fichier, "reference": self.reference, "expression": self.expression,
                "contexte": self.contexte, "ids": self.ids}


@dataclass
class Releve:
    occurrences: List[Occurrence] = field(default_factory=list)
    fichiers: List[str] = field(default_factory=list)
    erreurs: Dict[str, str] = field(default_factory=dict)

    def par_id(self) -> Dict[str, List[Occurrence]]:
        d: Dict[str, List[Occurrence]] = {}
        for o in self.occurrences:
            for i in o.ids:
                d.setdefault(i, []).append(o)
        return d

    def sans_id(self) -> List[Occurrence]:
        return [o for o in self.occurrences if not o.ids]

    def resume(self) -> Dict[str, int]:
        par_expr: Dict[str, int] = {}
        for o in self.occurrences:
            par_expr[o.expression] = par_expr.get(o.expression, 0) + 1
        return par_expr

    def texte_resume(self) -> str:
        r = self.resume()
        tete = f"Affaiblissements : {len(self.occurrences)} occurrence(s) dans {len(self.fichiers)} fichier(s), " \
               f"{len(self.par_id())} exigence(s) concernée(s), {len(self.sans_id())} sans identifiant"
        if r:
            tete += "\n  " + ", ".join(f"« {e} » ×{n}" for e, n in sorted(r.items(), key=lambda kv: -kv[1])[:12])
        for f, e in self.erreurs.items():
            tete += f"\n⚠ {Path(f).name} : {e}"
        return tete


def relever(fichiers: Sequence[str], motif: Optional[MotifIdentifiant] = None,
            expressions: Optional[Iterable[str]] = None, contexte: int = 100,
            ids_connus: Optional[Iterable[str]] = None) -> Releve:
    """Occurrences des expressions d'affaiblissement dans `fichiers`, chacune
    rattachée aux identifiants cités dans son paragraphe. Les expressions
    courantes (« may », « should », « sauf »…) ne sont retenues que si un
    identifiant est cité."""
    motif = motif or MotifIdentifiant()
    compiles = _compiler(expressions or EXPRESSIONS)
    connus = set(ids_connus) if ids_connus is not None else None
    r = Releve(fichiers=[str(f) for f in fichiers])
    for f in fichiers:
        try:
            paras = paragraphes(f)
        except Exception as exc:
            r.erreurs[str(f)] = f"{type(exc).__name__}: {exc}"
            continue
        for para in paras:
            texte = para.texte
            bas = _sans_accents(texte)
            ids = motif.extraire(texte)
            if connus is not None:
                ids = [i for i in ids if i in connus]
            for expr, rx in compiles:
                for m in rx.finditer(bas):
                    if expr.lower() in _FAIBLES and not ids:
                        continue
                    debut, fin = max(0, m.start() - contexte), min(len(texte), m.end() + contexte)
                    ctx = ("…" if debut else "") + texte[debut:fin].strip() + ("…" if fin < len(texte) else "")
                    r.occurrences.append(Occurrence(str(f), para.reference, expr, ctx, list(ids)))
    return r
