"""Emplacements des données utilisateur de SILLAGE.

`SILLAGE_HOME` surcharge tout ; sinon `%LOCALAPPDATA%\\sillage` sous Windows,
`~/.sillage` ailleurs. Aucune donnée fournisseur n'est écrite dans le dépôt.
"""

from __future__ import annotations

import os
from pathlib import Path


def dossier_donnees() -> Path:
    env = os.environ.get("SILLAGE_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local and os.name == "nt":
        return Path(local) / "sillage"
    return Path.home() / ".sillage"


def fichier_statuts() -> Path:
    return dossier_donnees() / "statuts.json"


def dossier_profils() -> Path:
    return dossier_donnees() / "profils"


def dossier_index(fournisseur: str = "") -> Path:
    """Index RIVAGE des documents d'un fournisseur, propre à SILLAGE (local)."""
    from .profils import nom_fichier_profil
    nom = nom_fichier_profil(fournisseur)[:-5] if fournisseur else "defaut"
    return dossier_donnees() / "index" / nom
