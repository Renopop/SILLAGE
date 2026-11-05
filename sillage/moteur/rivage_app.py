"""Point d'entrée minimal du processus FILS de la sonde de plateforme — propre
à SILLAGE (pas une copie de RIVAGE, non écrasé par la synchronisation).

`plateforme._commande_fils` lance, en mode script, `rivage_app.py` à côté de
`plateforme.py` avec `--sonde-plateforme <json>` : le fils exécute un barreau
(niveau SIMD, sous-index, mmap) sur un mini index et sort avec le code 0 s'il
survit. En exécutable gelé, c'est l'exécutable de SILLAGE lui-même qui
reçoit l'argument (voir `sillage/__main__.py`).
"""

import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--sonde-plateforme":
        import plateforme
        sys.exit(plateforme.executer_sonde(sys.argv[2] if len(sys.argv) > 2 else "{}"))
    sys.exit(0)
