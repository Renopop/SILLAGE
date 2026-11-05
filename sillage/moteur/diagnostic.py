# diagnostic.py
"""
Diagnostic de plateforme (V3, lot 1 : observabilité).

Deux services, sans aucune dépendance hors bibliothèque standard, et sans
jamais lever vers l'appelant (un diagnostic qui empêche de démarrer serait
pire que pas de diagnostic) :

  - `activer_journal_plantage()` : journal de plantage NATIF. Un exécutable
    fenêtré (PyInstaller, console=False) meurt sans laisser de trace sur
    une violation d'accès, une instruction illégale ou un abort : stderr
    n'existe pas. `faulthandler` écrit la pile Python de tous les threads
    au moment du plantage dans un fichier du miroir local du poste
    (%LOCALAPPDATA%\\rivage_cache\\diagnostic\\rivage_plantage.log), avec
    un en-tête daté par démarrage. Un fichier vide après un plantage est
    lui-même une information : le processus a été tué de l'extérieur.

  - `fiche_plateforme()` : ce qui tourne réellement — version de RIVAGE,
    Python, exécutable gelé ou script, version de faiss et NIVEAU SIMD
    effectif (aiguillage dynamique depuis faiss 1.14 : le même faiss.dll
    exécute du code scalaire, AVX2 ou AVX512 selon le poste), processeur,
    cœurs, mémoire, capacités AVX2 / AVX512 annoncées par le système.
    Affichée dans le panneau « Base chargée et prête » du lecteur et dans
    le journal de l'indexeur ; écrite en tête du journal de plantage.

faiss n'est importé ici que s'il l'est DÉJÀ (ou sur demande explicite) :
l'ordre d'import compte, la sonde de plateforme (lot 2) doit poser ses
variables avant le premier `import faiss`.
"""

import datetime
import faulthandler
import importlib
import logging
import os
import platform
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

JOURNAL_PLANTAGE = "rivage_plantage.log"
_JOURNAL_MAX_OCTETS = 5 * 1024 * 1024       # au-delà, le journal repart de zéro
_journal = None                               # fichier ouvert (faulthandler en a besoin)
_journal_chemin: Optional[str] = None


# --- emplacements -------------------------------------------------------------

def dossier_cache_local() -> str:
    """Racine du cache local du poste — même règle que index_cache
    (RIVAGE_LOCAL_CACHE_DIR, sinon %LOCALAPPDATA%\\rivage_cache, sinon
    ~/.rivage/rivage_cache), sans importer index_cache (qui importe faiss)."""
    base = os.environ.get("RIVAGE_LOCAL_CACHE_DIR")
    if base:
        return base
    root = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".rivage")
    return os.path.join(root, "rivage_cache")


def dossier_diagnostic() -> str:
    return os.path.join(dossier_cache_local(), "diagnostic")


# --- journal de plantage ------------------------------------------------------

def activer_journal_plantage(dossier: Optional[str] = None) -> Optional[str]:
    """Active faulthandler vers `<dossier>/rivage_plantage.log` (ajout),
    précédé d'un en-tête daté. Idempotent : un second appel rend le chemin
    déjà actif. Retourne None (sans lever) si le dossier est inaccessible."""
    global _journal, _journal_chemin
    if _journal is not None:
        return _journal_chemin
    try:
        dossier = dossier or dossier_diagnostic()
        os.makedirs(dossier, exist_ok=True)
        chemin = os.path.join(dossier, JOURNAL_PLANTAGE)
        try:
            if os.path.getsize(chemin) > _JOURNAL_MAX_OCTETS:
                os.remove(chemin)
        except OSError:
            pass
        f = open(chemin, "a", encoding="utf-8", errors="replace")
        f.write(f"=== démarrage {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                f"RIVAGE {_version_rivage()} pid {os.getpid()} "
                f"({'exécutable' if est_gele() else 'script'}) ===\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
        _journal, _journal_chemin = f, chemin
        return chemin
    except Exception as e:                                   # jamais bloquant
        logger.debug(f"[diagnostic] journal de plantage inactif : {e}")
        return None


def journal_plantage_actif() -> Optional[str]:
    return _journal_chemin


def ecrire_journal(texte: str) -> None:
    """Ligne libre dans le journal de plantage (fiche plateforme, barreau
    retenu par la sonde…) : lisible même après un plantage ultérieur."""
    if _journal is None:
        return
    try:
        _journal.write(texte.rstrip("\n") + "\n")
        _journal.flush()
    except Exception:
        pass


# --- fiche plateforme ---------------------------------------------------------

def est_gele() -> bool:
    return bool(getattr(sys, "frozen", False))


def _version_rivage() -> str:
    try:
        from version import __version__
        return __version__
    except Exception:
        return "?"


def version_faiss() -> Optional[str]:
    """Version de faiss par les métadonnées du paquet (embarquées par
    PyInstaller via collect_all), sans importer faiss."""
    try:
        from importlib import metadata
        return metadata.version("faiss-cpu")
    except Exception:
        pass
    try:
        from importlib import metadata
        return metadata.version("faiss")
    except Exception:
        return None


def niveau_simd_faiss(charger: bool = False) -> Dict[str, Any]:
    """{"niveau": "AVX2"|"AVX512"|"NONE"|…|None, "dynamique": bool|None,
    "charge": bool}. Sans `charger`, n'importe pas faiss : « non chargé »
    si l'import n'a pas encore eu lieu."""
    mod = sys.modules.get("faiss")
    if mod is None and charger:
        try:
            mod = importlib.import_module("faiss")
        except Exception:
            mod = None
    if mod is None:
        return {"niveau": None, "dynamique": None, "charge": False}
    out: Dict[str, Any] = {"niveau": None, "dynamique": None, "charge": True}
    cfg = getattr(mod, "SIMDConfig", None)
    if cfg is None:
        # faiss < 1.14 : variante choisie par le chargeur Python (swigfaiss_avx2…)
        try:
            opts = str(mod.get_compile_options())
            out["niveau"] = ("AVX512" if "AVX512" in opts else
                             "AVX2" if "AVX2" in opts else "NONE")
        except Exception:
            out["niveau"] = "?"
        out["dynamique"] = False
        return out
    try:
        out["niveau"] = str(cfg.get_level_name())
    except Exception:
        out["niveau"] = "?"
    try:
        out["dynamique"] = bool(cfg.has_dynamic_dispatch())
    except Exception:
        out["dynamique"] = None
    return out


def memoire_mo() -> Dict[str, Optional[int]]:
    """{"totale", "disponible"} en Mo — Windows : GlobalMemoryStatusEx ;
    Linux : /proc/meminfo ; sinon None."""
    out: Dict[str, Optional[int]] = {"totale": None, "disponible": None}
    try:
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                out["totale"] = int(st.ullTotalPhys // (1024 * 1024))
                out["disponible"] = int(st.ullAvailPhys // (1024 * 1024))
        elif os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", "r", encoding="ascii", errors="replace") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    kio = v.strip().split()
                    if not kio:
                        continue
                    if k == "MemTotal":
                        out["totale"] = int(kio[0]) // 1024
                    elif k == "MemAvailable":
                        out["disponible"] = int(kio[0]) // 1024
    except Exception:
        pass
    return out


def capacites_processeur() -> Dict[str, Optional[bool]]:
    """{"avx2", "avx512f"} annoncés par le SYSTÈME (pas par faiss) —
    Windows : IsProcessorFeaturePresent (40 = AVX2, 41 = AVX512F) ;
    Linux : drapeaux de /proc/cpuinfo ; None = inconnu."""
    out: Dict[str, Optional[bool]] = {"avx2": None, "avx512f": None}
    try:
        if sys.platform.startswith("win"):
            import ctypes
            k32 = ctypes.windll.kernel32
            out["avx2"] = bool(k32.IsProcessorFeaturePresent(40))
            out["avx512f"] = bool(k32.IsProcessorFeaturePresent(41))
        elif os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo", "r", encoding="ascii", errors="replace") as f:
                for line in f:
                    if line.startswith("flags"):
                        flags = set(line.partition(":")[2].split())
                        out["avx2"] = "avx2" in flags
                        out["avx512f"] = "avx512f" in flags
                        break
    except Exception:
        pass
    return out


def fiche_plateforme(charger_faiss: bool = False) -> Dict[str, Any]:
    """Tout ce qui décrit le poste et le binaire en cours d'exécution."""
    mem = memoire_mo()
    cap = capacites_processeur()
    simd = niveau_simd_faiss(charger=charger_faiss)
    fiche: Dict[str, Any] = {
        "rivage": _version_rivage(),
        "python": platform.python_version(),
        "gele": est_gele(),
        "systeme": f"{platform.system()} {platform.release()}",
        "processeur": platform.processor() or platform.machine() or "?",
        "coeurs": os.cpu_count() or 0,
        "memoire_totale_mo": mem["totale"],
        "memoire_disponible_mo": mem["disponible"],
        "avx2": cap["avx2"],
        "avx512f": cap["avx512f"],
        "faiss": version_faiss(),
        "faiss_simd": simd["niveau"],
        "faiss_dynamique": simd["dynamique"],
        "faiss_charge": simd["charge"],
        "journal_plantage": _journal_chemin,
        "build": lire_build_info(),
    }
    return fiche


def lire_build_info() -> Optional[Dict[str, Any]]:
    """`build_info.json` écrit par build_exe.bat à côté de l'exécutable
    (lot 4) : date de construction, commit, versions. None en script."""
    try:
        base = (os.path.dirname(os.path.abspath(sys.executable)) if est_gele()
                else os.path.dirname(os.path.abspath(__file__)))
        chemin = os.path.join(base, "build_info.json")
        if not os.path.isfile(chemin):
            return None
        import json
        with open(chemin, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _oui_non(v: Optional[bool]) -> str:
    return "?" if v is None else ("oui" if v else "non")


def fiche_texte(fiche: Dict[str, Any]) -> List[str]:
    """Lignes alignées (≤ 80 colonnes) pour le panneau d'accueil et les
    journaux."""
    mem_t, mem_d = fiche.get("memoire_totale_mo"), fiche.get("memoire_disponible_mo")
    mem = ("?" if mem_t is None else
           f"{mem_t / 1024:.1f} Go" + (f" (libre {mem_d / 1024:.1f} Go)" if mem_d is not None else ""))
    if not fiche.get("faiss_charge"):
        simd = "non chargé"
    else:
        simd = f"{fiche.get('faiss_simd') or '?'}"
        if fiche.get("faiss_dynamique") is True:
            simd += " (aiguillage dynamique)"
        elif fiche.get("faiss_dynamique") is False:
            simd += " (variante fixe)"
    lignes = [
        f"  RIVAGE         : {fiche.get('rivage')} "
        f"({'exécutable' if fiche.get('gele') else 'script'}, Python {fiche.get('python')})",
        f"  faiss          : {fiche.get('faiss') or 'absent'} — niveau SIMD : {simd}",
        f"  Processeur     : {str(fiche.get('processeur'))[:60]}, {fiche.get('coeurs')} cœur(s), "
        f"AVX2 {_oui_non(fiche.get('avx2'))}, AVX512 {_oui_non(fiche.get('avx512f'))}",
        f"  Mémoire        : {mem}",
    ]
    build = fiche.get("build") or {}
    if build:
        lignes.append(f"  Construction   : {build.get('date', '?')} "
                      f"(commit {str(build.get('commit', '?'))[:10]})")
    if fiche.get("journal_plantage"):
        lignes.append(f"  Journal plantage : {fiche['journal_plantage']}")
    return [l[:80] if len(l) > 80 and "Journal" not in l else l for l in lignes]


def journaliser_fiche(log=None, charger_faiss: bool = False) -> Dict[str, Any]:
    """Fiche dans le logger donné ET dans le journal de plantage."""
    fiche = fiche_plateforme(charger_faiss=charger_faiss)
    lignes = fiche_texte(fiche)
    for l in lignes:
        (log or logger).info("[plateforme]" + l)
        ecrire_journal(l)
    return fiche
