"""Lanceur de SILLAGE — script et exécutable gelé (PyInstaller).

    python sillage_app.py              interface graphique
    python sillage_app.py grille …     ligne de commande (mêmes commandes que `python -m sillage`)

Ordre d'exécution, comme le lanceur de RIVAGE :
1. processus FILS de la sonde de plateforme (`--sonde-plateforme`) : exécute
   un barreau et sort, avant tout autre import ;
2. `multiprocessing.freeze_support()` (extraction parallèle en exécutable) ;
3. stdout / stderr absents en exécutable fenêtré : remplacés par un puits ;
4. interface graphique sans argument, ligne de commande sinon.
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def _fix_streams() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main() -> int:
    _fix_streams()
    if len(sys.argv) > 1:
        from sillage.cli import main as cli
        return cli()
    try:
        from sillage.gui import main as gui
    except ImportError as exc:   # tkinter absent
        print(f"Interface graphique indisponible ({exc}). Ligne de commande : sillage_app.py --help", file=sys.stderr)
        return 1
    return gui()


if __name__ == "__main__":
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, APP_DIR)
    if len(sys.argv) > 1 and sys.argv[1] == "--sonde-plateforme":
        from sillage.moteur import activer
        activer()
        import plateforme
        sys.exit(plateforme.executer_sonde(sys.argv[2] if len(sys.argv) > 2 else "{}"))
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
