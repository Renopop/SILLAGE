"""Modèle pivot de SILLAGE.

Une ligne de NCM, quel que soit le fournisseur, devient une `LigneNCM` :
identifiant normalisé, statut pivot, justification, champs optionnels, et le
reste des colonnes conservé en brut. Chaque ligne connaît son origine (fichier,
emplacement, numéro de ligne) et le chemin par lequel elle a été lue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class Statut(str, Enum):
    CONFORME = "C"
    PARTIEL = "PC"
    NON_CONFORME = "NC"
    NON_APPLICABLE = "NA"
    INCONNU = "?"

    @property
    def libelle(self) -> str:
        return _LIBELLES[self]

    @classmethod
    def depuis_code(cls, code: str) -> "Statut":
        for s in cls:
            if s.value == code:
                return s
        return cls.INCONNU


_LIBELLES = {
    Statut.CONFORME: "conforme",
    Statut.PARTIEL: "partiellement conforme",
    Statut.NON_CONFORME: "non conforme",
    Statut.NON_APPLICABLE: "non applicable",
    Statut.INCONNU: "statut inconnu",
}

# Ordre de « gravité » pour qualifier un changement de statut entre révisions.
GRAVITE = {
    Statut.CONFORME: 0,
    Statut.NON_APPLICABLE: 0,
    Statut.PARTIEL: 1,
    Statut.NON_CONFORME: 2,
    Statut.INCONNU: 1,
}

# Chemins de lecture d'une ligne.
CHEMIN_TABLEAU = "tableau"    # colonnes reconnues par leur contenu
CHEMIN_PROFIL = "profil"      # colonnes données par le profil du fournisseur
CHEMIN_EXTRAIT = "extrait"    # réservé : statut déduit d'un texte libre (aucune lecture ne le produit aujourd'hui)

# Champs optionnels reconnus dans une NCM (clé pivot -> mots-clés d'en-tête).
# L'ordre compte : le plus spécifique d'abord (« Waiver ref » est une dérogation, pas une référence).
CHAMPS_OPTIONNELS = {
    "derogation": ("derog", "dérog", "derogation", "dérogation", "waiver", "deviation request", "concession",
                   "rfd", "rfw", "ecart accepte", "écart accepté"),
    "echeance": ("echeance", "échéance", "date", "due", "delai", "délai", "deadline", "target"),
    "action": ("action", "plan", "mitigation", "proposition", "proposal", "mesure", "remediation"),
    "reference": ("ref", "réf", "reference", "référence", "doc", "document", "para", "§",
                  "section", "clause", "chapitre", "chapter", "renvoi"),
    "commentaire": ("comment", "commentaire", "remarque", "remark", "note", "observation"),
}


@dataclass
class Origine:
    fichier: str
    emplacement: str = ""   # "feuille 'NCM'", "page 3, tableau 2", "table 1"
    ligne: int = 0          # numéro de ligne dans l'emplacement (1 = première)

    def __str__(self) -> str:
        parts = [Path(self.fichier).name]
        if self.emplacement:
            parts.append(self.emplacement)
        if self.ligne:
            parts.append(f"ligne {self.ligne}")
        return ", ".join(parts)


@dataclass
class Exigence:
    id: str                     # identifiant normalisé
    texte: str = ""
    id_brut: str = ""           # tel qu'écrit dans la spécification
    source: str = ""            # fichier de la spécification
    reference: str = ""         # paragraphe / emplacement dans la spécification
    norme: str = ""             # paragraphe de norme tracé (ex. « CS 25.1309 »)
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class LigneNCM:
    id: str
    statut: Statut
    statut_brut: str = ""
    justification: str = ""
    id_brut: str = ""
    champs: Dict[str, str] = field(default_factory=dict)   # action, echeance, reference, derogation, commentaire
    brut: Dict[str, str] = field(default_factory=dict)     # en-tête -> valeur, toutes colonnes
    origine: Optional[Origine] = None
    chemin: str = CHEMIN_TABLEAU

    def to_dict(self) -> dict:
        d = asdict(self)
        d["statut"] = self.statut.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LigneNCM":
        d = dict(d)
        d["statut"] = Statut.depuis_code(d.get("statut", "?"))
        o = d.get("origine")
        d["origine"] = Origine(**o) if isinstance(o, dict) else None
        return cls(**d)


@dataclass
class Diagnostic:
    """Exploitabilité d'une NCM, calculée avant toute analyse."""
    tableaux_lus: int = 0
    tableaux_retenus: int = 0
    lignes_total: int = 0            # lignes de données des tableaux retenus
    lignes_avec_id: int = 0
    lignes_pivot: int = 0            # après dépliage des plages et des cellules multiples
    statuts_reconnus: int = 0
    statuts_inconnus: Dict[str, int] = field(default_factory=dict)   # valeur brute -> occurrences
    colonnes: Dict[str, str] = field(default_factory=dict)           # rôle -> en-tête retenu
    chemin: str = CHEMIN_TABLEAU
    ids_referentiel: int = 0         # taille du référentiel si fourni
    ids_retrouves: int = 0           # identifiants du référentiel présents dans la NCM
    avertissements: List[str] = field(default_factory=list)

    @property
    def taux_ids(self) -> float:
        return self.lignes_avec_id / self.lignes_total if self.lignes_total else 0.0

    @property
    def taux_statuts(self) -> float:
        return self.statuts_reconnus / self.lignes_pivot if self.lignes_pivot else 0.0

    @property
    def taux_couverture(self) -> float:
        return self.ids_retrouves / self.ids_referentiel if self.ids_referentiel else 0.0

    def texte(self) -> str:
        lignes = [
            f"Tableaux lus : {self.tableaux_lus}, retenus : {self.tableaux_retenus} (chemin : {self.chemin})",
            f"Lignes : {self.lignes_total}, avec identifiant : {self.lignes_avec_id} ({self.taux_ids:.0%}), "
            f"lignes pivot : {self.lignes_pivot}",
            f"Statuts reconnus : {self.statuts_reconnus}/{self.lignes_pivot} ({self.taux_statuts:.0%})",
        ]
        if self.ids_referentiel:
            lignes.append(f"Identifiants du référentiel retrouvés : {self.ids_retrouves}/{self.ids_referentiel} "
                          f"({self.taux_couverture:.0%})")
        if self.colonnes:
            lignes.append("Colonnes : " + ", ".join(f"{k} = « {v} »" for k, v in self.colonnes.items()))
        if self.statuts_inconnus:
            vals = ", ".join(f"« {v} » ×{n}" for v, n in sorted(self.statuts_inconnus.items(), key=lambda kv: -kv[1]))
            lignes.append("Statuts inconnus : " + vals)
        for a in self.avertissements:
            lignes.append("⚠ " + a)
        return "\n".join(lignes)


@dataclass
class NCM:
    fournisseur: str = ""
    revision: str = ""
    fichier: str = ""
    lignes: List[LigneNCM] = field(default_factory=list)
    diagnostic: Diagnostic = field(default_factory=Diagnostic)

    def par_id(self) -> Dict[str, List[LigneNCM]]:
        d: Dict[str, List[LigneNCM]] = {}
        for l in self.lignes:
            d.setdefault(l.id, []).append(l)
        return d

    @property
    def ids(self) -> List[str]:
        vus = []
        s = set()
        for l in self.lignes:
            if l.id not in s:
                s.add(l.id)
                vus.append(l.id)
        return vus

    def to_dict(self) -> dict:
        return {
            "sillage": "ncm", "version": 1,
            "fournisseur": self.fournisseur, "revision": self.revision, "fichier": self.fichier,
            "diagnostic": asdict(self.diagnostic),
            "lignes": [l.to_dict() for l in self.lignes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NCM":
        if d.get("sillage") != "ncm":
            raise ValueError("ce fichier n'est pas une NCM normalisée SILLAGE")
        diag = Diagnostic(**d.get("diagnostic", {}))
        return cls(fournisseur=d.get("fournisseur", ""), revision=d.get("revision", ""),
                   fichier=d.get("fichier", ""), diagnostic=diag,
                   lignes=[LigneNCM.from_dict(x) for x in d.get("lignes", [])])

    def enregistrer(self, chemin) -> None:
        Path(chemin).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def charger(cls, chemin) -> "NCM":
        return cls.from_dict(json.loads(Path(chemin).read_text(encoding="utf-8")))
