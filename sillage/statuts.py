"""Dictionnaire des statuts de conformité.

Chaque fournisseur a son vocabulaire (« C / PC / NC / NA », « Compliant /
Partially / Non-compliant », « Full / Partial / Deviation », « Oui / Partiel /
Non »…). Le dictionnaire ramène tout à quatre valeurs pivot et s'enrichit : une
valeur inconnue est signalée dans le diagnostic, l'utilisateur dit à quoi elle
correspond, elle rejoint `statuts.json` dans le dossier de données.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, Optional

from .config import fichier_statuts
from .modele import Statut

_DEFAUT: Dict[str, Statut] = {}


def _ajouter(statut: Statut, *valeurs: str) -> None:
    for v in valeurs:
        _DEFAUT[normaliser_valeur(v)] = statut


def normaliser_valeur(v: str) -> str:
    """Minuscules, sans accents, ponctuation réduite, espaces simples."""
    v = unicodedata.normalize("NFKD", str(v or ""))
    v = "".join(c for c in v if not unicodedata.combining(c)).lower()
    v = re.sub(r"[^a-z0-9/]+", " ", v)
    return re.sub(r"\s+", " ", v).strip()


_ajouter(Statut.CONFORME,
         "c", "co", "fc", "conforme", "compliant", "comply", "complies", "compliance", "full",
         "fully compliant", "full compliance", "fully", "yes", "oui", "ok", "accepted", "accepte",
         "met", "satisfied", "satisfait", "totalement conforme", "complet", "complete", "y", "o")
_ajouter(Statut.PARTIEL,
         "pc", "p", "pco", "partiel", "partielle", "partiellement conforme", "partially", "partial",
         "partially compliant", "partial compliance", "partiellement", "part", "en partie",
         "partially met", "partly", "partly compliant", "limited", "limite")
_ajouter(Statut.NON_CONFORME,
         "nc", "n", "nco", "non conforme", "non-conforme", "non compliant", "non-compliant",
         "not compliant", "noncompliant", "no", "non", "deviation", "dev", "ko", "not met",
         "not satisfied", "non satisfait", "refuse", "rejected", "rejete", "ecart", "gap",
         "non conformite", "non conformity", "nok")
_ajouter(Statut.NON_APPLICABLE,
         "na", "n/a", "n a", "not applicable", "non applicable", "sans objet", "so", "s/o",
         "inapplicable", "not relevant", "hors perimetre", "out of scope", "n.a", "nap")

_MOTS_PAR_STATUT = {
    Statut.NON_APPLICABLE: ("not applicable", "non applicable", "sans objet", "out of scope"),
    Statut.PARTIEL: ("partial", "partiel", "partly", "en partie"),
    Statut.NON_CONFORME: ("non compliant", "non conforme", "not compliant", "noncompliant",
                          "deviation", "not met", "ecart"),
    Statut.CONFORME: ("compliant", "conforme", "complies", "comply"),
}


class DictionnaireStatuts:
    def __init__(self, ajouts: Optional[Dict[str, str]] = None):
        self._dico: Dict[str, Statut] = dict(_DEFAUT)
        self._ajouts: Dict[str, Statut] = {}
        for k, v in (ajouts or {}).items():
            self.ajouter(k, Statut.depuis_code(v) if not isinstance(v, Statut) else v, persistant=False)

    # --- reconnaissance ---------------------------------------------------
    def reconnaitre(self, valeur: str) -> Optional[Statut]:
        """Statut pivot pour une valeur de cellule, ou None si inconnue.

        Essais successifs : valeur entière ; valeur avant une parenthèse, un
        deux-points ou un tiret (« C (see note 3) », « PC - voir §4 ») ; premier
        mot ; enfin mots-clés contenus (le plus spécifique d'abord).
        """
        if valeur is None:
            return None
        brut = str(valeur).strip()
        if not brut:
            return None
        n = normaliser_valeur(brut)
        if n in self._dico:
            return self._dico[n]
        tete = re.split(r"[(:\-–—;,/\n]", brut, 1)[0]
        nt = normaliser_valeur(tete)
        if nt and nt in self._dico:
            return self._dico[nt]
        premier = n.split(" ", 1)[0]
        if premier in self._dico and len(premier) <= 3:
            return self._dico[premier]
        for statut, mots in _MOTS_PAR_STATUT.items():
            if any(m in n for m in mots):
                return statut
        return None

    # --- enrichissement -----------------------------------------------------
    def ajouter(self, valeur: str, statut: Statut, persistant: bool = True) -> None:
        n = normaliser_valeur(valeur)
        if not n:
            return
        self._dico[n] = statut
        self._ajouts[n] = statut
        if persistant:
            self.enregistrer()

    def ajouts(self) -> Dict[str, str]:
        return {k: v.value for k, v in self._ajouts.items()}

    def valeurs(self) -> Dict[str, str]:
        return {k: v.value for k, v in sorted(self._dico.items())}

    # --- persistance ----------------------------------------------------------
    def enregistrer(self, chemin: Optional[Path] = None) -> Path:
        p = Path(chemin) if chemin else fichier_statuts()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.ajouts(), ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        return p

    @classmethod
    def charger(cls, chemin: Optional[Path] = None) -> "DictionnaireStatuts":
        p = Path(chemin) if chemin else fichier_statuts()
        ajouts: Dict[str, str] = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    ajouts = {str(k): str(v) for k, v in data.items()}
            except (OSError, ValueError):
                ajouts = {}
        return cls(ajouts)
