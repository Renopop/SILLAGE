# hooks/rth_plateforme.py — hook d'EXÉCUTION PyInstaller (runtime hook).
#
# PyInstaller l'exécute dans l'exécutable gelé AVANT le premier import de
# l'application (RIVAGE.spec, `runtime_hooks`). C'est la garantie d'ordre
# dont la sonde de plateforme a besoin : faiss lit FAISS_SIMD_LEVEL dans un
# initialiseur statique de faiss.dll, donc au moment précis de `import
# faiss` — les variables du barreau retenu doivent exister avant.
#
# Un échec ici ne doit JAMAIS empêcher le démarrage : tout est enveloppé.
# Dans le processus fils de la sonde (--sonde-plateforme), `appliquer` est
# sans effet (il détecte l'argument et la variable RIVAGE_SONDE_FILS).
try:
    import plateforme as _rivage_plateforme
    _rivage_plateforme.appliquer()
except Exception:
    pass
