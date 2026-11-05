import sys

# Processus fils de la sonde de plateforme (exécutable gelé) : exécute un
# barreau et sort, AVANT tout autre import — comme le lanceur de RIVAGE.
if len(sys.argv) > 1 and sys.argv[1] == "--sonde-plateforme":
    from sillage.moteur import activer
    activer()
    import plateforme
    sys.exit(plateforme.executer_sonde(sys.argv[2] if len(sys.argv) > 2 else "{}"))

import multiprocessing
multiprocessing.freeze_support()

from .cli import main

sys.exit(main())
