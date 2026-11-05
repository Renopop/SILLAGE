# plateforme.py
"""
Sonde et échelle de configurations par poste (V2.7, lot 2).

Exigence : chaque poste tourne au niveau le plus RAPIDE qu'il supporte
réellement — les postes récents gardent AVX512, les anciens descendent
d'exactement ce qu'il faut — sans réglage manuel, sans plafond commun au
parc. Ce n'est pas ce que le processeur ANNONCE qui décide, c'est ce qu'il
EXÉCUTE : un processus fils essaie l'échelle des configurations, de la plus
rapide à la plus sûre, sur un index fp16 minuscule ; la première qui
s'exécute sans mourir est retenue et mémorisée pour ce poste.

Pourquoi : depuis faiss-cpu 1.14, un seul `faiss.dll` à aiguillage
dynamique exécute du code scalaire, AVX2 ou AVX512 selon le poste. Sur
Windows, cette détection n'était pas compilée avant la 1.15.1 (niveau NONE
partout, lent mais indestructible) ; la 1.15.1 expose pour la première fois
le parc aux noyaux SIMD. Le lecteur 2.x a aussi introduit du code natif
nouveau (mmap, sous-index par vues, recherche filtrée multi-thread). Un
poste qui meurt sur l'un d'eux meurt SANS message en exécutable fenêtré.

Échelle (`echelle`) : pour chaque niveau SIMD candidat (AUTO = laisser
faiss choisir le plus haut, puis AVX2, puis NONE — seulement ceux que le
poste annonce) : tout activé → sans sous-index → sans mmap → sans rien.

Mécanique : `sonder` lance, par barreau, `sys.executable --sonde-plateforme
<barreau>` (en exécutable gelé c'est RIVAGE.exe lui-même, comme pour
l'extraction parallèle) avec les variables du barreau dans l'environnement ;
code de sortie 0 = retenu, tout autre code (instruction illégale, violation
d'accès, abort OpenMP, délai) = refusé. Résultat mémorisé dans
`rivage.config.json` (clé « plateforme ») avec la signature du poste
(processeur, cœurs, versions de faiss et de RIVAGE) : la sonde ne rejoue
que si la signature change, ou si le témoin de chargement (lot 3) l'exige.

Application (`appliquer`) : AVANT tout `import faiss`, poser les variables
du barreau retenu — `FAISS_SIMD_LEVEL`, `RIVAGE_FAISS_SHARDS`,
`RIVAGE_FAISS_MMAP`, `RIVAGE_RERANK_WORKERS` — SANS JAMAIS écraser une
valeur déjà posée par l'utilisateur (launch.bat, RIVAGE.env). Appelée par
le hook d'exécution PyInstaller (`hooks/rth_plateforme.py`) et par
`rivage_app.main()`. Seconde couche après l'import : `vector_index` abaisse
le niveau par `faiss.SIMDConfig.set_level` si besoin.

Surcharges manuelles (support) : `RIVAGE_SIMD_LEVEL` (point de départ de
l'échelle : NONE, AVX2, AVX512), `RIVAGE_MODE_SUR=1` (barreau le plus sûr,
sans sonde), `RIVAGE_SONDE=0` (aucune sonde : laisser faire faiss).

Ce module n'importe jamais faiss ni numpy au niveau module.
"""

import dataclasses
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ARG_FILS = "--sonde-plateforme"          # argument du processus fils
VAR_FILS = "RIVAGE_SONDE_FILS"           # posé dans l'environnement du fils
CLE_CONFIG = "plateforme"                # clé dans rivage.config.json
NIVEAUX = ("AUTO", "AVX512", "AVX2", "NONE")   # du plus rapide au plus sûr
ORDRE_SIMD = {"NONE": 0, "AVX2": 1, "AVX512": 2, "AVX512_VPOPCNT": 3, "AVX512_SPR": 4}
SONDE_TIMEOUT_S = 20.0
SONDE_TIMEOUT_TOTAL_S = 120.0
RATIO_MEMOIRE = 0.8                      # FAISS > 0,8 × mémoire libre = poste contraint

_applique: Optional["Barreau"] = None
_origine: str = ""


# --- barreaux et échelle ------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Barreau:
    simd: str = "AUTO"
    shards: bool = True
    mmap: bool = True
    pool: bool = True

    def vers_environnement(self) -> Dict[str, str]:
        """Variables à poser pour ce barreau. AUTO = ne rien imposer à faiss
        (il choisit le plus haut niveau qu'il détecte)."""
        env: Dict[str, str] = {}
        if self.simd != "AUTO":
            env["FAISS_SIMD_LEVEL"] = self.simd
        if not self.shards:
            env["RIVAGE_FAISS_SHARDS"] = "0"
        if not self.mmap:
            env["RIVAGE_FAISS_MMAP"] = "0"
        if not self.pool:
            env["RIVAGE_RERANK_WORKERS"] = "1"
        return env

    def libelle(self) -> str:
        simd = "niveau SIMD automatique" if self.simd == "AUTO" else f"SIMD {self.simd}"
        opts = [("sous-index" if self.shards else "sans sous-index"),
                ("mmap" if self.mmap else "sans mmap"),
                ("reranking parallèle" if self.pool else "reranking séquentiel")]
        return f"{simd}, " + ", ".join(opts)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> "Barreau":
        if not isinstance(d, dict):
            raise ValueError("barreau invalide")
        simd = str(d.get("simd", "AUTO")).upper()
        if simd not in NIVEAUX:
            raise ValueError(f"niveau SIMD inconnu : {simd}")
        return cls(simd=simd, shards=bool(d.get("shards", True)),
                   mmap=bool(d.get("mmap", True)), pool=bool(d.get("pool", True)))


BARREAU_SUR = Barreau(simd="NONE", shards=False, mmap=False, pool=False)


def capacites() -> Dict[str, Any]:
    """Capacités annoncées par le système + mémoire (diagnostic.py) et
    signature du poste."""
    from diagnostic import capacites_processeur, memoire_mo, version_faiss, _version_rivage
    import platform
    cap = dict(capacites_processeur())
    cap.update(memoire_mo())
    cap["processeur"] = platform.processor() or platform.machine() or "?"
    cap["coeurs"] = os.cpu_count() or 0
    cap["faiss"] = version_faiss() or "?"
    cap["rivage"] = _version_rivage()
    cap["signature"] = hashlib.sha1("|".join(
        str(cap[k]) for k in ("processeur", "coeurs", "faiss", "rivage", "avx2", "avx512f")
    ).encode("utf-8")).hexdigest()[:12]
    return cap


def niveaux_candidats(cap: Dict[str, Any], depart: Optional[str] = None) -> List[str]:
    """Niveaux SIMD à essayer, du plus rapide au plus sûr, selon ce que le
    poste annonce. AUTO (faiss détecte, donc le plus haut) n'est utile que
    s'il peut différer d'AVX2 explicite (poste avec AVX512) ; sans AVX2
    annoncé, seul NONE. Capacités inconnues : tout essayer. `depart`
    (RIVAGE_SIMD_LEVEL) tronque par le haut."""
    avx2, avx512 = cap.get("avx2"), cap.get("avx512f")
    if avx2 is False:
        niveaux = ["NONE"]
    elif avx512 is True or avx512 is None:
        niveaux = ["AUTO", "AVX2", "NONE"]
    else:
        niveaux = ["AUTO", "NONE"]           # AVX2 annoncé, pas d'AVX512 : AUTO = AVX2
    if depart:
        d = str(depart).strip().upper()
        if d in ORDRE_SIMD:
            seuil = ORDRE_SIMD[d]
            niveaux = [n for n in niveaux
                       if n != "AUTO" and ORDRE_SIMD.get(n, 9) <= seuil] or ["NONE"]
            if d not in niveaux and d in ("AVX512", "AVX2"):
                niveaux.insert(0, d)
    return niveaux


def echelle(cap: Dict[str, Any], depart: Optional[str] = None) -> List[Barreau]:
    out: List[Barreau] = []
    for simd in niveaux_candidats(cap, depart):
        out += [Barreau(simd, True, True, True),
                Barreau(simd, False, True, True),
                Barreau(simd, False, False, True),
                Barreau(simd, False, False, False)]
    return out


# --- sonde ---------------------------------------------------------------------

def _commande_fils(barreau: Barreau) -> List[str]:
    arg = json.dumps(barreau.to_dict())
    if getattr(sys, "frozen", False):
        return [sys.executable, ARG_FILS, arg]
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rivage_app.py")
    return [sys.executable, script, ARG_FILS, arg]


def _lancer_fils(barreau: Barreau, timeout_s: float = SONDE_TIMEOUT_S) -> Tuple[int, str]:
    """(code de sortie, dernière ligne de stderr) du processus fils."""
    env = dict(os.environ)
    for k in ("FAISS_SIMD_LEVEL", "RIVAGE_FAISS_SHARDS", "RIVAGE_FAISS_MMAP",
              "RIVAGE_RERANK_WORKERS"):
        env.pop(k, None)                    # le barreau décide, pas l'héritage
    env.update(barreau.vers_environnement())
    env[VAR_FILS] = "1"
    kwargs: Dict[str, Any] = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(_commande_fils(barreau), env=env, timeout=timeout_s,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, **kwargs)
    except subprocess.TimeoutExpired:
        return 124, "délai dépassé"
    except Exception as e:
        return 125, str(e)
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
    return int(proc.returncode), (err[-1] if err else (out[-1] if out else ""))


def sonder(barreaux: List[Barreau], lancer=None, timeout_s: float = SONDE_TIMEOUT_S,
           total_s: float = SONDE_TIMEOUT_TOTAL_S) -> Tuple[Barreau, List[Dict[str, Any]]]:
    """Premier barreau qui s'exécute sans mourir, et la liste des refusés
    ({barreau, code, detail}). Tous refusés (ou budget épuisé) → le barreau
    le plus sûr, sans sonde : mieux vaut un lecteur lent qu'aucun."""
    from diagnostic import ecrire_journal
    lancer = lancer or _lancer_fils
    refuses: List[Dict[str, Any]] = []
    t0 = time.monotonic()
    for b in barreaux:
        if time.monotonic() - t0 > total_s:
            ecrire_journal(f"[plateforme] sonde : budget de {total_s:.0f} s épuisé")
            break
        code, detail = lancer(b, timeout_s)
        if code == 0:
            ecrire_journal(f"[plateforme] sonde : retenu « {b.libelle()} »")
            return b, refuses
        refuses.append({"barreau": b.to_dict(), "code": code, "detail": detail[:200]})
        ecrire_journal(f"[plateforme] sonde : refusé « {b.libelle()} » (code {code}"
                       f"{', ' + detail[:120] if detail else ''})")
    ecrire_journal("[plateforme] sonde : aucun barreau retenu — barreau le plus sûr")
    return BARREAU_SUR, refuses


def executer_sonde(arg_json: str) -> int:
    """Corps du processus FILS : exécute, pour un barreau, tout ce que le
    lecteur fera — index fp16 minuscule écrit puis relu (mmap et sous-index
    selon le barreau), recherche simple, recherche filtrée par bitmap et par
    lot d'identifiants (sélecteurs natifs), recherches en parallèle. Sort
    avec 0 si tout passe ; 3 sur exception Python ; un plantage natif donne
    le code du système. Écrit sur stdout le niveau SIMD effectif."""
    import tempfile
    import traceback
    try:
        barreau = Barreau.from_dict(json.loads(arg_json or "{}"))
        for k, v in barreau.vers_environnement().items():
            os.environ[k] = v
        os.environ.setdefault("RIVAGE_FAISS_QUANT", "16")       # le format du lecteur
        import numpy as np
        import faiss
        from vector_index import VectorIndex
        niveau = "?"
        try:
            niveau = str(faiss.SIMDConfig.get_level_name())
        except Exception:
            pass
        d, n = 64, 512
        rng = np.random.default_rng(7)
        vecs = rng.standard_normal((n, d)).astype("float32")
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        ids = [1000 + 3 * i for i in range(n)]
        vi = VectorIndex(d)
        vi.add(vecs, ids)
        with tempfile.TemporaryDirectory(prefix="rivage_sonde_") as tmp:
            chemin = os.path.join(tmp, "sonde.faiss")
            vi.save(chemin)
            lec = VectorIndex.load_lecteur(chemin, local=True, mmap=barreau.mmap,
                                           shards=(2 if barreau.shards else 0))
            q = vecs[7]
            r = lec.search(q, 5)
            if not r or r[0][0] != ids[7]:
                raise RuntimeError(f"recherche simple incohérente : {r[:1]}")
            bitmap = np.zeros(max(ids) + 1, dtype=bool)
            bitmap[ids[::3]] = True
            r = lec.search(q, 5, allowed_ids=bitmap)
            if not r or not all(bitmap[f] for f, _ in r):
                raise RuntimeError("recherche filtrée (bitmap) incohérente")
            r = lec.search(q, 5, allowed_ids=set(ids[::7]))
            if not r or not all(f in set(ids[::7]) for f, _ in r):
                raise RuntimeError("recherche filtrée (lot) incohérente")
            if barreau.pool:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=4) as ex:
                    res = list(ex.map(lambda i: lec.search(vecs[i], 3, allowed_ids=bitmap),
                                      range(16)))
                if any(not x for x in res):
                    raise RuntimeError("recherches parallèles incohérentes")
            # Lecture seule : rien à fermer ; le dossier temporaire part avec le with.
            del lec
        try:                                  # exécutable fenêtré : stdout peut manquer
            print(json.dumps({"niveau": niveau, "barreau": barreau.to_dict()}))
            sys.stdout.flush()
        except Exception:
            pass
        return 0
    except Exception:
        try:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
        except Exception:
            pass
        return 3


# --- mémoire du poste -----------------------------------------------------------

def lire_memo() -> Optional[Dict[str, Any]]:
    try:
        import config
        memo = config.load_config().get(CLE_CONFIG)
        return memo if isinstance(memo, dict) else None
    except Exception:
        return None


def memoriser(barreau: Barreau, cap: Dict[str, Any], refuses: List[Dict[str, Any]],
              origine: str = "sonde") -> None:
    try:
        import config
        ancien = lire_memo() or {}
        # Les compteurs de témoins (lot 3) survivent à une nouvelle sonde.
        temoins = (ancien.get("temoins")
                   if ancien.get("signature") in (None, cap.get("signature")) else None)
        config.save_config({CLE_CONFIG: {
            "barreau": barreau.to_dict(), "signature": cap.get("signature"),
            "faiss": cap.get("faiss"), "rivage": cap.get("rivage"),
            "processeur": cap.get("processeur"), "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "origine": origine, "refuses": refuses[-12:],
            "temoins": temoins if isinstance(temoins, dict) else {},
        }})
    except Exception as e:
        logger.warning(f"[plateforme] barreau non mémorisé : {e}")


def reinitialiser() -> None:
    """Oublie le barreau, les refus et le témoin du poste : la prochaine
    exécution relance la sonde depuis le haut de l'échelle."""
    try:
        import config
        config.save_config({CLE_CONFIG: None})
    except Exception as e:
        logger.warning(f"[plateforme] réinitialisation impossible : {e}")
    effacer_temoin()


# --- témoin de chargement (lot 3) -----------------------------------------------
#
# Écrit AVANT de charger la base, effacé après la PREMIÈRE RECHERCHE RÉUSSIE
# (pas seulement après le chargement : la première recherche exécute les
# noyaux SIMD) et à la fermeture normale. Présent au démarrage suivant, il
# signifie que le processus est mort entre les deux : le barreau qu'il porte
# est écarté pour cette session et la sonde rejouée ; deux témoins
# consécutifs au même barreau le marquent refusé jusqu'à réinitialisation.

TEMOIN = "chargement_en_cours.json"
_temoin_efface = False
_message_demarrage: Optional[str] = None


def temoin_path() -> str:
    from diagnostic import dossier_diagnostic
    return os.path.join(dossier_diagnostic(), TEMOIN)


def _barreau_courant() -> Barreau:
    """Le barreau appliqué, ou celui que l'environnement décrit (réglages
    manuels, lancement sans sonde)."""
    if _applique is not None:
        return _applique
    env = os.environ
    return Barreau(simd=(env.get("FAISS_SIMD_LEVEL") or "AUTO").strip().upper() or "AUTO",
                   shards=env.get("RIVAGE_FAISS_SHARDS", "").strip() != "0",
                   mmap=env.get("RIVAGE_FAISS_MMAP", "1").strip() != "0",
                   pool=env.get("RIVAGE_RERANK_WORKERS", "3").strip() != "1")


def ecrire_temoin(index_dir: str = "") -> Optional[str]:
    """Jamais bloquant. Retourne le chemin écrit, ou None."""
    global _temoin_efface
    try:
        from diagnostic import version_faiss, _version_rivage
        chemin = temoin_path()
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        b = _barreau_courant()
        if b.simd not in NIVEAUX:
            b = Barreau()
        data = {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "rivage": _version_rivage(),
                "faiss": version_faiss(), "barreau": b.to_dict(), "pid": os.getpid(),
                "index_dir": index_dir}
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, chemin)
        _temoin_efface = False
        return chemin
    except Exception as e:
        logger.debug(f"[plateforme] témoin non écrit : {e}")
        return None


def lire_temoin() -> Optional[Dict[str, Any]]:
    try:
        with open(temoin_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def effacer_temoin() -> None:
    global _temoin_efface
    _temoin_efface = True
    try:
        os.remove(temoin_path())
    except OSError:
        pass
    except Exception:
        pass


def marquer_recherche_reussie() -> None:
    """Appelé par le lecteur après une recherche réussie : efface le témoin
    une fois par processus (les noyaux SIMD ont tourné sans mourir)."""
    if not _temoin_efface:
        effacer_temoin()


def message_demarrage() -> Optional[str]:
    """Texte à afficher au démarrage quand un témoin a été trouvé."""
    return _message_demarrage


def _traiter_temoin(cap_rivage: str) -> Tuple[List[Barreau], bool]:
    """Au démarrage : (barreaux à écarter, persister dans la mémoire du
    poste). Un témoin d'une autre version de RIVAGE est ignoré ; le compteur
    par barreau vit dans la mémoire du poste (`temoins`)."""
    global _message_demarrage
    t = lire_temoin()
    if not t:
        return [], False
    effacer_temoin()
    if t.get("rivage") != cap_rivage:
        return [], False
    try:
        b = Barreau.from_dict(t.get("barreau"))
    except ValueError:
        return [], False
    cle = json.dumps(b.to_dict(), sort_keys=True)
    n = 1
    try:
        import config
        memo = config.load_config().get(CLE_CONFIG) or {}
        compteurs = dict(memo.get("temoins") or {}) if isinstance(memo, dict) else {}
        n = int(compteurs.get(cle, 0)) + 1
        compteurs[cle] = n
        config.save_config({CLE_CONFIG: dict(memo, temoins=compteurs)})
    except Exception:
        pass
    _message_demarrage = (
        f"Le chargement précédent ({t.get('date', '?')}) ne s'est pas terminé "
        f"avec la configuration « {b.libelle()} » : elle est écartée"
        + (" définitivement pour ce poste" if n >= 2 else " pour cette session")
        + " et la sonde est rejouée. « Réessayer en mode optimisé » annule cette "
          "mesure.")
    try:
        from diagnostic import ecrire_journal
        ecrire_journal(f"[plateforme] témoin trouvé : {_message_demarrage}")
    except Exception:
        pass
    return [b], n >= 2


def _refuses_memo(memo: Optional[Dict[str, Any]], cap: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not memo or memo.get("signature") != cap.get("signature"):
        return []
    r = memo.get("refuses")
    return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []


def barreau_du_poste(force: bool = False, lancer=None,
                     refuses_supplementaires: Optional[List[Barreau]] = None,
                     persister_ecartes: bool = True) -> Tuple[Barreau, str]:
    """(barreau, origine) : « mode sûr », « sans sonde », « mémorisé » ou
    « sonde ». `refuses_supplementaires` : barreaux à écarter d'office
    (témoin de chargement, lot 3) — mémorisés comme refusés si
    `persister_ecartes`, sinon écartés pour cet appel seulement."""
    if os.environ.get("RIVAGE_MODE_SUR", "").strip() == "1":
        return BARREAU_SUR, "mode sûr"
    if os.environ.get("RIVAGE_SONDE", "1").strip() == "0":
        return Barreau(), "sans sonde"
    cap = capacites()
    memo = lire_memo()
    ecartes = {json.dumps(b.to_dict(), sort_keys=True) for b in (refuses_supplementaires or [])}
    if memo and not force and not ecartes and memo.get("signature") == cap.get("signature"):
        try:
            return Barreau.from_dict(memo.get("barreau")), "mémorisé"
        except ValueError:
            pass
    ech = echelle(cap, depart=os.environ.get("RIVAGE_SIMD_LEVEL"))
    deja = {json.dumps(r.get("barreau"), sort_keys=True) for r in _refuses_memo(memo, cap)}
    candidats = [b for b in ech
                 if json.dumps(b.to_dict(), sort_keys=True) not in deja | ecartes] or [BARREAU_SUR]
    barreau, refuses = sonder(candidats, lancer=lancer)
    memoriser(barreau, cap, _refuses_memo(memo, cap)
              + ([{"barreau": json.loads(e), "code": -1, "detail": "témoin"} for e in ecartes]
                 if persister_ecartes else [])
              + refuses)
    return barreau, "sonde"


# --- application ----------------------------------------------------------------

def est_fils() -> bool:
    return bool(os.environ.get(VAR_FILS)) or ARG_FILS in sys.argv[1:3]


def appliquer(lancer=None) -> Optional[Barreau]:
    """Pose dans l'environnement les variables du barreau du poste — sans
    écraser une valeur déjà présente. À appeler AVANT tout import de faiss.
    Idempotent ; sans effet dans le processus fils de la sonde."""
    global _applique, _origine
    if est_fils():
        return None
    if _applique is not None:
        return _applique
    try:
        # Témoin d'un chargement précédent non terminé (lot 3) : son barreau
        # est écarté et la sonde rejouée.
        try:
            from diagnostic import _version_rivage
            ecartes, persister = _traiter_temoin(_version_rivage())
        except Exception:
            ecartes, persister = [], False
        barreau, origine = barreau_du_poste(lancer=lancer, refuses_supplementaires=ecartes,
                                            persister_ecartes=persister)
        if ecartes:
            origine = "sonde après témoin"
    except Exception as e:
        logger.warning(f"[plateforme] sonde impossible ({e}) : configuration par défaut.")
        barreau, origine = Barreau(), "erreur"
    for k, v in barreau.vers_environnement().items():
        os.environ.setdefault(k, v)
    _applique, _origine = barreau, origine
    try:
        from diagnostic import ecrire_journal
        ecrire_journal(f"[plateforme] barreau {origine} : {barreau.libelle()}")
    except Exception:
        pass
    return barreau


def barreau_applique() -> Tuple[Optional[Barreau], str]:
    return _applique, _origine


def niveau_simd_demande() -> Optional[str]:
    v = os.environ.get("FAISS_SIMD_LEVEL", "").strip().upper()
    return v or None


def contrainte_memoire(taille_faiss_octets: int,
                       memoire_disponible_mo: Optional[int] = None) -> Optional[str]:
    """Message si le fichier FAISS dépasse RATIO_MEMOIRE × mémoire libre :
    l'index plat est lu en entier à chaque question ; au-delà de la mémoire
    libre, Windows le relit depuis le disque à chaque fois. None sinon.
    L'appelant coupe alors sous-index et pool (threads et tampons en plus)
    et garde le mmap (seule voie qui tient)."""
    if memoire_disponible_mo is None:
        from diagnostic import memoire_mo
        memoire_disponible_mo = memoire_mo().get("disponible")
    if not memoire_disponible_mo or taille_faiss_octets <= 0:
        return None
    faiss_mo = taille_faiss_octets / (1024 * 1024)
    if faiss_mo <= RATIO_MEMOIRE * memoire_disponible_mo:
        return None
    return (f"Base plus grande que la mémoire libre ({faiss_mo / 1024:.1f} Go pour "
            f"{memoire_disponible_mo / 1024:.1f} Go libres) : recherche ralentie — "
            f"réindexer en 8 bits (RIVAGE_FAISS_QUANT=8) ou libérer de la mémoire.")
