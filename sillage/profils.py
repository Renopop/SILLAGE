"""Profils fournisseurs : le choix de colonnes mémorisé une fois par fournisseur.

Un profil associe un rôle pivot (id, statut, justification, action, échéance,
référence, dérogation, commentaire) à un en-tête de colonne tel qu'il apparaît
dans les NCM de ce fournisseur. À la révision suivante, si les en-têtes
correspondent, le profil s'applique (chemin « profil ») ; sinon la
reconnaissance par contenu reprend et le profil est à confirmer de nouveau.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import dossier_profils

ROLES = ("id", "statut", "justification", "action", "echeance", "reference", "derogation", "commentaire")


def normaliser_en_tete(v: str) -> str:
    v = unicodedata.normalize("NFKD", str(v or ""))
    v = "".join(c for c in v if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9§/]+", " ", v)).strip()


def nom_fichier_profil(fournisseur: str) -> str:
    n = normaliser_en_tete(fournisseur).replace(" ", "_") or "sans_nom"
    return n + ".json"


@dataclass
class Profil:
    fournisseur: str
    colonnes: Dict[str, str] = field(default_factory=dict)   # rôle -> en-tête (brut)
    statuts: Dict[str, str] = field(default_factory=dict)    # valeur brute -> code pivot, propre à ce fournisseur
    note: str = ""

    def correspond(self, en_tetes: List[str]) -> bool:
        """Vrai si tous les en-têtes du profil sont présents (id et statut obligatoires)."""
        if "id" not in self.colonnes or "statut" not in self.colonnes:
            return False
        dispo = {normaliser_en_tete(h) for h in en_tetes}
        return all(normaliser_en_tete(h) in dispo for h in self.colonnes.values())

    def indices(self, en_tetes: List[str]) -> Dict[str, int]:
        norm = [normaliser_en_tete(h) for h in en_tetes]
        out = {}
        for role, h in self.colonnes.items():
            nh = normaliser_en_tete(h)
            if nh in norm:
                out[role] = norm.index(nh)
        return out

    def to_dict(self) -> dict:
        return {"sillage": "profil", "version": 1, "fournisseur": self.fournisseur,
                "colonnes": self.colonnes, "statuts": self.statuts, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "Profil":
        if d.get("sillage") != "profil":
            raise ValueError("ce fichier n'est pas un profil SILLAGE")
        return cls(fournisseur=d.get("fournisseur", ""), colonnes=dict(d.get("colonnes", {})),
                   statuts=dict(d.get("statuts", {})), note=d.get("note", ""))

    def enregistrer(self, dossier: Optional[Path] = None) -> Path:
        d = Path(dossier) if dossier else dossier_profils()
        d.mkdir(parents=True, exist_ok=True)
        p = d / nom_fichier_profil(self.fournisseur)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        return p

    @classmethod
    def charger(cls, fournisseur: str, dossier: Optional[Path] = None) -> Optional["Profil"]:
        d = Path(dossier) if dossier else dossier_profils()
        p = d / nom_fichier_profil(fournisseur)
        if not p.exists():
            return None
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None


def lister(dossier: Optional[Path] = None) -> List[Profil]:
    d = Path(dossier) if dossier else dossier_profils()
    out = []
    if d.exists():
        for p in sorted(d.glob("*.json")):
            try:
                out.append(Profil.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    return out
