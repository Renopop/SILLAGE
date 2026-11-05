# version.py
"""
Version de RIVAGE — source unique, sans aucune dépendance.

Lue par pyproject (à maintenir en cohérence), `rivage.py --version`, le
titre des fenêtres et `rivage.py config`. Historique dans CHANGELOG.md.

Numérotation : MAJEUR.MINEUR.CORRECTIF. Le MAJEUR change quand l'index doit
être reconstruit ou quand la configuration n'est plus compatible (V2 : nouvel
embedder, index fp16, clés par modèle).
"""

__version__ = "2.7.6"
VERSION_LABEL = f"RIVAGE {__version__}"
