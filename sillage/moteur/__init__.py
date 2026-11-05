"""Le moteur de RIVAGE, embarqué dans SILLAGE.

Copie des modules de RIVAGE (voir `VERSION_RIVAGE.txt` et
`outils/synchroniser_rivage.py`), conservés tels quels avec leurs imports
plats (`from retrieval import …`). `activer()` rend ce dossier importable et
règle les fichiers de configuration :

- `RIVAGE.env` et `RIVAGE_keys.env` sont cherchés d'abord dans le dossier de
  l'application SILLAGE (dossier de l'exécutable, ou racine du dépôt), puis
  là où RIVAGE les cherche d'habitude : les mêmes clés et les mêmes réglages
  servent aux deux outils ;
- la sonde de plateforme (postes anciens) est appliquée avant le premier
  import de faiss, comme le fait le lanceur de RIVAGE.

SILLAGE ne dépend d'aucune installation de RIVAGE sur le poste. Seule la
passerelle d'IA (embedding, reranking, OCR) reste externe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
_ACTIVE = False


def dossier_application() -> Path:
    """Dossier de l'application SILLAGE : celui de l'exécutable gelé, sinon la racine du dépôt."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return DOSSIER.parents[1]


def version_rivage() -> str:
    try:
        premiere = (DOSSIER / "VERSION_RIVAGE.txt").read_text(encoding="utf-8").splitlines()[0]
        return premiere.replace("RIVAGE", "").strip()
    except (OSError, IndexError):
        return "?"


def activer() -> Path:
    """Rend les modules du moteur importables (idempotent) et renvoie leur dossier."""
    global _ACTIVE
    if _ACTIVE:
        return DOSSIER
    d = str(DOSSIER)
    if d not in sys.path:
        sys.path.insert(0, d)
    app = dossier_application()
    for var, nom in (("RIVAGE_ENV_FILE", "RIVAGE.env"), ("RIVAGE_KEYS_FILE", "RIVAGE_keys.env")):
        if not os.environ.get(var) and (app / nom).is_file():
            os.environ[var] = str(app / nom)
    try:
        import plateforme  # noqa: F401
        plateforme.appliquer()
    except Exception:
        pass
    _ACTIVE = True
    return DOSSIER
