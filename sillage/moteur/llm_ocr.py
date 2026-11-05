"""
LLM OCR Module - OCR par Vision LLM (API DALLEM)

Utilise le LLM vision DALLEM pour extraire le texte des pages PDF
scannées ou de mauvaise qualité.

Fonctionnalités:
- Conversion PDF vers images via PyMuPDF
- Envoi des images au LLM Vision
- Extraction de texte structuré
- Mode fallback automatique si OCR classique échoue
"""

import gc
import os
import io
import re
import base64
import logging
import random
import threading
import time
from typing import List, Dict, Any, Optional, Tuple, Callable
from dataclasses import dataclass

# Marqueurs de page reconnus par chunking._split_pages : neutralisés dans le
# texte d'UNE page OCRisée avant fusion `\f` (voir smart_ocr_pdf, audit v2).
_PAGE_MARKERS_RE = re.compile(r"\f|---\s*(?:Page|Slide)\s+\d+\s*---")

logger = logging.getLogger(__name__)

# =============================================================================
#  IMPORT CONFIGURATION DALLEM
# =============================================================================

try:
    from models_utils import (
        DALLEM_API_KEY,
        DALLEM_API_BASE,
        LLM_MODEL,
        create_http_client,
    )
    DALLEM_CONFIG_AVAILABLE = True
except ImportError:
    DALLEM_CONFIG_AVAILABLE = False
    DALLEM_API_KEY = None
    DALLEM_API_BASE = None
    LLM_MODEL = None

# =============================================================================
#  CONFIGURATION
# =============================================================================

# DPI adaptatif selon la RAM pour limiter la taille des pixmaps en mémoire.
# 200 DPI pour un PDF A4 = ~1654x2339 pixels = ~15 Mo/page en RGB non compressé.
# 150 DPI = ~1240x1754 = ~8.7 Mo/page : -40% de RAM par page rendue.
def _adaptive_dpi() -> int:
    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        return 150 if ram_gb <= 8 else 200
    except Exception:
        return 150  # défaut conservateur

DEFAULT_DPI = _adaptive_dpi()
MAX_IMAGE_SIZE = 2048
MAX_PAGES_PER_BATCH = 5
IMAGE_FORMAT = "PNG"


def _adaptive_ocr_workers() -> int:
    """Determine le nombre optimal de workers paralleles pour l'OCR.

    L'OCR est I/O-bound (appels API HTTP distants), donc on peut
    utiliser plus de workers que de CPUs. Limite par la RAM disponible
    car chaque worker garde ~20-50 Mo en memoire (image + base64).
    """
    import os
    cpu_count = os.cpu_count() or 4
    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        if ram_gb <= 8:
            max_by_ram = 3
        elif ram_gb <= 16:
            max_by_ram = 6
        elif ram_gb <= 32:
            max_by_ram = 10
        else:
            max_by_ram = 16
    except Exception:
        max_by_ram = 4
    # I/O-bound: on peut aller jusqu'a 2x le nombre de CPUs
    return min(max_by_ram, cpu_count * 2)


OCR_PARALLEL_WORKERS = _adaptive_ocr_workers()


def _env_int_ocr(name: str, default: int) -> int:
    """Entier >= 0 depuis l'environnement ; défaut si absent/invalide."""
    try:
        v = os.environ.get(name)
        return max(0, int(v)) if v is not None and v.strip() else default
    except (TypeError, ValueError):
        return default


def _env_float_ocr(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return max(0.0, float(v)) if v is not None and v.strip() else default
    except (TypeError, ValueError):
        return default


# --- Robustesse passerelle (504/429) -----------------------------------------
# Un 504 signale que la passerelle devant l'API Vision est SATURÉE : re-tenter
# vite et à pleine concurrence entretient la congestion. Réponse en deux temps :
#  - retries patients : délais exponentiels PLAFONNÉS (60 s), base allongée
#    pour la congestion, JITTER ±30 % pour désynchroniser les workers
#    (RIVAGE_OCR_RETRIES, défaut 6 ; RIVAGE_OCR_RETRY_BASE_S, défaut 2) ;
#  - concurrence ADAPTATIVE (_AdaptiveGate) : chaque 5xx/429 divise par 2 le
#    nombre de workers actifs (plancher 1), une série de succès le remonte
#    d'un cran — RIVAGE veut cesser d'entretenir lui-même la saturation.
#    Plafond manuel : RIVAGE_OCR_WORKERS (0 = auto).

_RETRY_CAP_S = 60.0
_CONGESTION_CODES = {429, 502, 503, 504}


def _is_transport_error(e: BaseException) -> bool:
    """Timeout, connexion coupée, DNS, proxy… : erreur de TRANSPORT, reconnue
    par son TYPE et non par son texte (audit V2, B-1 : `str(httpx.ReadTimeout)`
    vaut « timed out » et `str(RemoteProtocolError)` « Server disconnected… »,
    qu'un test sur le mot « timeout » ne voyait pas — la page revenait vide et
    NON transitoire, donc figée illisible pour toujours)."""
    try:
        import httpx
        if isinstance(e, httpx.TransportError):      # inclut TimeoutException
            return True
    except ImportError:
        pass
    try:
        import requests
        if isinstance(e, (requests.exceptions.Timeout,
                          requests.exceptions.ConnectionError)):
            return True
    except ImportError:
        pass
    import socket
    if isinstance(e, (socket.timeout, TimeoutError, ConnectionError)):
        return True
    s = str(e).lower()
    return "timeout" in s or "timed out" in s or "connection" in s


def _is_local_error(e: BaseException) -> bool:
    """Erreur de programmation ou de donnée locale (jamais transitoire)."""
    return isinstance(e, (ValueError, TypeError, KeyError, AttributeError,
                          IndexError))
_CONGESTION_FACTOR = 3.0     # base de délai × 3 quand la passerelle sature


# --- Maîtrise du COÛT OCR ----------------------------------------------------
# Chaque page OCRisée est un appel Vision payant. Trois garde-fous :
#  - RIVAGE_OCR_SMART_ROTATE (défaut 0) : la CONFIRMATION d'orientation par
#    LLM ajoutait un appel Vision par page dès que la détection locale disait
#    « 0° » (cas ultra-majoritaire) — soit ~2 appels par page pour un service
#    presque toujours inutile. Désactivée par défaut ; la détection LOCALE et
#    l'auto-rotation restent actives.
#  - pré-contrôle « page blanche » : une page sans texte ET sans contenu
#    visuel (ni image ni dessin — vérification PyMuPDF locale, gratuite)
#    n'est pas envoyée à l'OCR.
#  - RIVAGE_OCR_MAX_PAGES_DOC (défaut 0 = illimité) : plafond de pages OCR
#    par document — un scan géant ne peut plus consommer tout le budget.

def _smart_rotate_enabled(value) -> bool:
    """Résout smart_rotate=None selon RIVAGE_OCR_SMART_ROTATE (défaut 0)."""
    if value is not None:
        return bool(value)
    return _env_int_ocr("RIVAGE_OCR_SMART_ROTATE", 0) == 1


# Page « quasi vide » : moins de _QUASI_EMPTY_ALNUM caractères alphanumériques
# (numéro de page, « Annexe 3 »…) — une annexe scannée porte souvent ce seul
# résidu de texte, elle doit être traitée comme une page sans texte (B-2).
# Seuil bas : une page courte mais réelle (« Page trois lisible. ») reste du
# texte et n'est pas confondue avec une page blanche.
_QUASI_EMPTY_ALNUM = 10


def _is_quasi_empty(page_text: str) -> bool:
    """Vrai si la page n'a (presque) aucun texte exploitable."""
    if not page_text or not page_text.strip():
        return True
    n = 0
    for c in page_text:
        if c.isalnum():
            n += 1
            if n >= _QUASI_EMPTY_ALNUM:
                return False
    return True


def _page_has_visual_content(pdf_path: str, page_num: int, log=None,
                             doc=None) -> bool:
    """Vrai si la page contient une image ou des tracés vectoriels — c'est-à-
    dire quelque chose que l'OCR peut lire. Contrôle LOCAL (PyMuPDF), gratuit.
    En cas de doute (bibliothèque absente, erreur), True : ne jamais perdre de
    contenu pour économiser un appel.

    `doc` : document PyMuPDF déjà ouvert par l'appelant (audit perf 2.1,
    I-9 : smart_ocr_pdf l'ouvre une fois pour toutes ses pages) — non fermé
    ici ; sinon ouverture et fermeture locales.

    RIVAGE_OCR_BLANK_CHECK=0 désactive entièrement ce contrôle (toutes les
    pages sans texte partent en OCR, comme avant le lot d'économie) —
    interrupteur de diagnostic/secours.
    """
    _log = log or logger
    if _env_int_ocr("RIVAGE_OCR_BLANK_CHECK", 1) == 0:
        return True
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
    except ImportError:
        return True
    owned = doc is None
    try:
        if owned:
            doc = fitz.open(pdf_path)
        try:
            page = doc[page_num]
            if page.get_images(full=False):
                return True
            # Tracés vectoriels : surtout PAS page.get_drawings() — sur un
            # plan CAO (des dizaines de milliers de chemins, justement les
            # pages sans texte que l'on contrôle ici), il matérialise TOUT en
            # objets Python : std::bad_alloc constaté en production. Un
            # MINI-RENDU borné (~60 px, ≈10 Ko) suffit et coûte une allocation
            # constante : une page réellement blanche rend une image unie.
            side = max(page.rect.width, page.rect.height, 1.0)
            zoom = min(60.0 / side, 1.0)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            samples = bytes(pix.samples)
            return bool(samples) and any(b != samples[0] for b in samples)
        finally:
            if owned:
                doc.close()
    except Exception as e:
        _log.debug(f"[LLM-OCR] Contrôle page blanche impossible "
                   f"(page {page_num + 1}): {e} — OCR maintenu.")
        return True


class _AdaptiveGate:
    """Limiteur de concurrence partagé entre les workers OCR d'un document.

    `acquire`/`release` encadrent UNIQUEMENT l'appel HTTP (pas les attentes de
    retry : un worker qui dort ne consomme pas de slot). Sur congestion, la
    limite est divisée par 2 (plancher 1) ; après 2×limite succès consécutifs,
    elle remonte d'un cran jusqu'au maximum initial.
    """

    def __init__(self, max_slots: int, log=None):
        self._max = max(1, int(max_slots))
        self._limit = self._max
        self._active = 0
        self._streak = 0
        self._cond = threading.Condition()
        self._log = log or logger
        # Arrêt coopératif propagé aux pages EN VOL (audit V2, B-13) : après
        # « Quitter », une page en cours de retry attendait encore jusqu'à 6
        # tentatives (minutes de sommeil et appels payants) avant de rendre
        # la main. Le drapeau est relu par ocr_image_with_llm avant chaque
        # nouvel essai.
        self._stop = threading.Event()

    def request_stop(self) -> None:
        """Signale aux workers que plus aucun nouvel essai ne doit partir."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def wait_retry(self, delay: float) -> None:
        """Sommeil de retry INTERRUPTIBLE par l'arrêt (jusqu'à 60 s sinon)."""
        self._stop.wait(max(0.0, delay))

    def acquire(self) -> None:
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait(timeout=1.0)
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def report_congestion(self) -> None:
        with self._cond:
            self._streak = 0
            new = max(1, self._limit // 2)
            if new < self._limit:
                self._limit = new
                self._log.info(f"[LLM-OCR] Passerelle saturée (5xx/429) — "
                               f"concurrence réduite à {new} worker(s).")

    def report_success(self) -> None:
        with self._cond:
            self._streak += 1
            if self._limit < self._max and self._streak >= 2 * self._limit:
                self._limit += 1
                self._streak = 0
                self._log.info(f"[LLM-OCR] Passerelle rétablie — concurrence "
                               f"remontée à {self._limit} worker(s).")
                self._cond.notify_all()

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

OCR_SYSTEM_PROMPT = """Tu es un expert en OCR (reconnaissance optique de caractères).
Ta tâche est d'extraire le texte EXACT de l'image fournie.

Instructions:
1. Extrais TOUT le texte visible, sans rien ajouter ni modifier
2. Préserve la structure (paragraphes, listes, tableaux)
3. Pour les tableaux, utilise des espaces ou | pour séparer les colonnes
4. Indique [illisible] pour les parties impossibles à lire
5. Conserve les numéros, références et codes exactement comme ils apparaissent
6. Si c'est un document réglementaire (EASA, FAA), fais très attention aux références (CS xx.xxx, AMC, GM)

Retourne UNIQUEMENT le texte extrait, sans commentaires ni explications."""

OCR_USER_PROMPT = """Extrais le texte de cette image de document.
Préserve la mise en page et la structure autant que possible.
Texte extrait:"""


# =============================================================================
#  DATA CLASSES
# =============================================================================

@dataclass
class OCRResult:
    """Résultat de l'OCR pour une page."""
    page_number: int
    text: str
    confidence: float  # 0-1 estimation de confiance
    processing_time: float  # secondes
    error: Optional[str] = None
    # Échec TRANSITOIRE (504/502/503/429, timeout, connexion) après épuisement
    # des retries : la page est re-tentable — le document ne doit pas être
    # enregistré tel quel (voir indexer), il sera repris au prochain run.
    transient: bool = False


@dataclass
class DocumentOCRResult:
    """Résultat de l'OCR pour un document complet."""
    total_pages: int
    processed_pages: int
    full_text: str
    page_results: List[OCRResult]
    total_time: float
    avg_confidence: float
    errors: List[str]
    # Nombre de pages en échec TRANSITOIRE (passerelle saturée…) : > 0 signifie
    # « résultat incomplet mais re-tentable » — à distinguer d'un échec définitif.
    transient_failures: int = 0


# =============================================================================
#  CACHE DE REPRISE OCR PAR PAGE (gros documents)
# =============================================================================
#
#  L'OCR d'un très gros PDF (des centaines/milliers de pages) dure des heures :
#  sans point de sauvegarde INTRA-fichier, un plantage à la page 900 reperdait
#  les 900 pages déjà payées à l'API Vision. Chaque page OCRisée est donc
#  écrite immédiatement dans un petit cache SQLite local, clé par la signature
#  du document (chemin + taille + mtime) : une relance reprend exactement à la
#  première page manquante. Désactivable par RIVAGE_OCR_CACHE=0 ; emplacement
#  par RIVAGE_OCR_CACHE_DIR (défaut ~/.rivage/ocr_cache). Les caches non
#  touchés depuis RIVAGE_OCR_CACHE_DAYS jours (défaut 14 ; 0 = jamais
#  nettoyés) sont supprimés au fil de l'eau.

_OCR_CACHE_DEFAULT_DAYS = 14


def _ocr_cache_max_age_s() -> float:
    """Durée de rétention du cache de reprise, en secondes (0 = illimitée).

    Réglable par RIVAGE_OCR_CACHE_DAYS : un corpus OCRisé par campagnes
    espacées (un gros scan repris trois semaines plus tard) perdait ses pages
    déjà payées avec la rétention fixe de 14 jours.
    """
    days = _env_int_ocr("RIVAGE_OCR_CACHE_DAYS", _OCR_CACHE_DEFAULT_DAYS)
    return float(days) * 24 * 3600


class _OcrPageCache:
    """Cache disque {page -> (texte, confiance)} pour UN document."""

    def __init__(self, conn, path, lock):
        self.conn = conn
        self.path = path
        self._lock = lock

    @classmethod
    def open_for(cls, pdf_path: str, log):
        """Ouvre (ou crée) le cache du document ; None si désactivé/erreur."""
        import sqlite3
        import hashlib
        import threading as _th
        if os.getenv("RIVAGE_OCR_CACHE", "1").strip().lower() in ("0", "false", "no"):
            return None
        try:
            st = os.stat(pdf_path)
            sig = f"{os.path.normcase(os.path.abspath(pdf_path))}|{st.st_size}|{int(st.st_mtime)}"
            cache_dir = os.getenv("RIVAGE_OCR_CACHE_DIR") or os.path.join(
                os.path.expanduser("~"), ".rivage", "ocr_cache")
            os.makedirs(cache_dir, exist_ok=True)
            cls._gc_old(cache_dir)
            name = hashlib.sha1(sig.encode("utf-8")).hexdigest() + ".db"
            path = os.path.join(cache_dir, name)
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("CREATE TABLE IF NOT EXISTS pages ("
                         " page INTEGER PRIMARY KEY, texte TEXT, confiance REAL)")
            conn.commit()
            return cls(conn, path, _th.Lock())
        except Exception as e:
            log.debug(f"[LLM-OCR] Cache de reprise indisponible: {e}")
            return None

    @staticmethod
    def _gc_old(cache_dir: str) -> None:
        max_age = _ocr_cache_max_age_s()
        if max_age <= 0:
            return                      # RIVAGE_OCR_CACHE_DAYS=0 : jamais nettoyé
        try:
            now = time.time()
            for name in os.listdir(cache_dir):
                if not name.endswith(".db"):
                    continue
                full = os.path.join(cache_dir, name)
                try:
                    if now - os.path.getmtime(full) > max_age:
                        os.remove(full)
                except OSError:
                    pass
        except OSError:
            pass

    def get_all(self) -> Dict[int, Tuple[str, float]]:
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT page, texte, confiance FROM pages").fetchall()
            return {int(p): (t or "", float(c or 0.0)) for p, t, c in rows}
        except Exception:
            return {}

    def put(self, page_num: int, texte: str, confiance: float) -> None:
        """Écrit UNE page immédiatement (commit) — c'est le point de reprise."""
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO pages(page, texte, confiance) "
                    "VALUES (?,?,?)", (page_num, texte, confiance))
                self.conn.commit()
        except Exception:
            pass  # le cache est un confort : ne jamais faire échouer l'OCR

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# =============================================================================
#  IMAGE UTILITIES
# =============================================================================

def _import_fitz():
    """PyMuPDF (`pymupdf`, ou l'ancien nom `fitz`) ; None si absent."""
    try:
        import pymupdf as fitz
        return fitz
    except ImportError:
        try:
            import fitz
            return fitz
        except ImportError:
            return None


def _open_pdf_document(pdf_path: str):
    """Ouvre le PDF avec PyMuPDF — UNE fois par page OCRisée (audit V2, B-13 :
    rasterisation, détection d'orientation et confirmation ouvraient chacune
    le document, soit trois ouvertures par page). None si PyMuPDF est absent."""
    fitz = _import_fitz()
    if fitz is None:
        return None
    return fitz.open(pdf_path)


def _close_pdf_document(doc) -> None:
    """Ferme un document PyMuPDF sans jamais lever (déjà fermé, None)."""
    if doc is None:
        return
    try:
        if getattr(doc, "is_closed", False):
            return
        doc.close()
    except Exception:
        pass


def detect_page_orientation(
    pdf_path: str,
    page_number: int,
    log=None,
    doc=None,
) -> int:
    """
    Détecte l'orientation d'une page PDF et retourne l'angle de correction.

    `doc` : document PyMuPDF déjà ouvert (partagé par l'appelant, non fermé
    ici) — sinon le fichier est ouvert et refermé localement.

    Returns:
        Angle de rotation à appliquer (0, 90, 180, 270) pour remettre droit
    """
    _log = log or logger

    fitz = _import_fitz()
    if fitz is None:
        return 0

    owned = doc is None
    try:
        if owned:
            doc = fitz.open(pdf_path)
        if page_number >= len(doc):
            return 0

        page = doc[page_number]

        # 1. Vérifier la rotation metadata de la page
        page_rotation = page.rotation
        if page_rotation != 0:
            _log.debug(f"[LLM-OCR] Page {page_number}: rotation metadata = {page_rotation}°")
            return 0

        # 2. Analyser le texte existant pour détecter l'orientation
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        blocks = text_dict.get("blocks", [])

        text_blocks = [b for b in blocks if b.get("type") == 0]

        if text_blocks:
            orientation = _analyze_text_direction(text_blocks, page.rect, _log)
            if orientation != 0:
                _log.info(f"[LLM-OCR] Page {page_number}: orientation texte détectée = {orientation}°")
                return orientation

        return 0

    except Exception as e:
        _log.warning(f"[LLM-OCR] Erreur détection orientation page {page_number}: {e}")
        return 0
    finally:
        if owned and doc:
            doc.close()


def _analyze_text_direction(blocks: List[Dict], page_rect, log) -> int:
    """
    Analyse la direction du texte dans les blocs pour détecter la rotation.
    """
    if not blocks:
        return 0

    horizontal_count = 0
    vertical_count = 0

    for block in blocks:
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue

            bbox = line.get("bbox", [0, 0, 0, 0])
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]

            if line_width > line_height * 2:
                horizontal_count += 1
            elif line_height > line_width * 2:
                vertical_count += 1

    total_lines = horizontal_count + vertical_count

    if total_lines == 0:
        return 0

    if vertical_count > horizontal_count * 2:
        log.debug(f"[LLM-OCR] Texte vertical détecté ({vertical_count} vs {horizontal_count} horizontal)")
        return 90

    return 0


def pdf_page_to_image(
    pdf_path: str,
    page_number: int,
    dpi: int = DEFAULT_DPI,
    max_size: int = MAX_IMAGE_SIZE,
    auto_rotate: bool = True,
    force_rotation: Optional[int] = None,
    log=None,
    doc=None,
) -> Optional[bytes]:
    """
    Convertit une page PDF en image PNG avec correction d'orientation.

    `doc` : document PyMuPDF déjà ouvert par l'appelant (non fermé ici) ;
    sinon ouverture et fermeture locales.
    """
    _log = log or logger

    fitz = _import_fitz()
    if fitz is None:
        _log.error("[LLM-OCR] PyMuPDF non installé")
        return None

    owned = doc is None
    try:
        if owned:
            doc = fitz.open(pdf_path)

        if page_number >= len(doc):
            _log.warning(f"[LLM-OCR] Page {page_number} n'existe pas (max={len(doc)-1})")
            return None

        page = doc[page_number]

        rotation = 0
        if force_rotation is not None:
            rotation = force_rotation % 360
        elif auto_rotate:
            rotation = detect_page_orientation(pdf_path, page_number, log=_log,
                                               doc=doc)

        zoom = dpi / 72.0
        # MÉMOIRE (std::bad_alloc) : borner le zoom AVANT de rasteriser. Une
        # page hors norme (plan A0…) à 200 DPI pèse ~9400×13300 px ≈ 375 Mo
        # alloués d'un coup — multipliés par les workers OCR parallèles, cela
        # épuisait la mémoire AVANT le contrôle max_size, qui arrivait trop
        # tard (rasterisation pleine résolution PUIS re-rendu réduit). La
        # taille attendue se calcule gratuitement depuis page.rect : le
        # PREMIER pixmap respecte désormais max_size.
        rect = page.rect
        est_max_px = max(rect.width, rect.height) * zoom
        if est_max_px > max_size > 0:
            zoom *= max_size / est_max_px
        mat = fitz.Matrix(zoom, zoom)

        if rotation != 0:
            mat = mat.prerotate(rotation)
            _log.info(f"[LLM-OCR] Page {page_number}: rotation appliquée = {rotation}°")

        pix = page.get_pixmap(matrix=mat)

        # Filet de sécurité (annotations débordantes, rect trompeur) : le
        # cas nominal ne repasse plus jamais ici.
        if pix.width > max_size or pix.height > max_size:
            scale = min(max_size / pix.width, max_size / pix.height)
            mat_scaled = fitz.Matrix(zoom * scale, zoom * scale)
            if rotation != 0:
                mat_scaled = mat_scaled.prerotate(rotation)
            # Libérer le premier pixmap surdimensionné avant d'en créer un second
            del pix
            pix = page.get_pixmap(matrix=mat_scaled)

        png_bytes = pix.tobytes("png")
        _log.debug(f"[LLM-OCR] Page {page_number}: {pix.width}x{pix.height} pixels")
        # Libérer le pixmap brut (non compressé, pèse lourd)
        del pix
        return png_bytes

    except Exception as e:
        _log.error(f"[LLM-OCR] Erreur conversion page {page_number}: {e}")
        return None
    finally:
        if owned and doc:
            doc.close()


def detect_orientation_with_llm(
    image_bytes: bytes,
    http_client,
    api_key: str,
    api_base: str,
    model: str,
    log=None
) -> int:
    """
    Utilise le LLM Vision pour détecter l'orientation d'une image.
    """
    _log = log or logger

    orientation_prompt = """Analyse cette image de document.
Détermine si le document est correctement orienté pour la lecture.

Réponds UNIQUEMENT par un nombre:
- 0 si le document est droit (texte lisible normalement)
- 90 si le document est tourné de 90° dans le sens horaire (texte vertical, tête à droite)
- 180 si le document est à l'envers (texte inversé)
- 270 si le document est tourné de 90° dans le sens anti-horaire (texte vertical, tête à gauche)

Réponds uniquement par le nombre, sans explication."""

    image_b64 = image_to_base64(image_bytes)

    url = api_base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": orientation_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                            "detail": "low"
                        }
                    }
                ]
            }
        ],
        "temperature": 0,
        "max_tokens": 10,
    }

    try:
        resp = http_client.post(url, headers=headers, json=payload, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

        # Nombre ENTIER isolé (audit V2, B-9) : « 0 » est une sous-chaîne de
        # « 90 », « 180 » et « 270 » — le test `"0" in content` renvoyait
        # toujours 0, et chaque page payait cet appel pour rien.
        m = re.search(r"\b(0|90|180|270)\b", content)
        if m:
            angle = int(m.group(1))
            _log.info(f"[LLM-OCR] Orientation détectée par LLM: {angle}°")
            return angle

        _log.warning(f"[LLM-OCR] Réponse orientation non parsable: {content}")
        return 0

    except Exception as e:
        # Journaliser AUSSI le corps de la réponse : sur 4xx, la passerelle y
        # met la raison exacte (clé refusée pour le modèle, quota, etc.).
        try:
            body = (getattr(getattr(e, "response", None), "text", "") or "")[:300]
        except Exception:
            body = ""
        _log.warning(f"[LLM-OCR] Erreur détection orientation LLM: {e}"
                     + (f" — réponse passerelle: {body}" if body else ""))
        return 0


def image_to_base64(image_bytes: bytes) -> str:
    """Encode une image en base64."""
    return base64.b64encode(image_bytes).decode('utf-8')


def get_pdf_page_count(pdf_path: str, log=None, doc=None) -> int:
    """
    Retourne le nombre de pages d'un PDF, compté par PyMuPDF SEULEMENT (audit
    perf 2.1, I-9) : l'ancien double comptage pdfplumber + PyMuPDF ouvrait
    deux fois le document (pdfplumber parse tout le fichier en Python) pour
    produire un simple avertissement en cas de désaccord — un désaccord que
    smart_ocr_pdf gère de toute façon (découpage par pages jugé incohérent →
    OCR complet). pdfplumber n'est plus consulté que si PyMuPDF est absent.

    `doc` : document PyMuPDF déjà ouvert par l'appelant (non fermé ici).
    """
    _log = log or logger

    if doc is not None:
        try:
            return len(doc)
        except Exception as e:
            _log.debug(f"[LLM-OCR] Comptage sur document ouvert impossible: {e}")

    fitz = _import_fitz()
    if fitz is None:
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                return len(pdf.pages)
        except ImportError:
            _log.error("[LLM-OCR] PyMuPDF non installé")
            return 0
        except Exception as e:
            _log.error(f"[LLM-OCR] Erreur lecture PDF: {e}")
            return 0

    own = None
    try:
        own = fitz.open(pdf_path)
        return len(own)
    except Exception as e:
        _log.error(f"[LLM-OCR] Erreur lecture PDF: {e}")
        return 0
    finally:
        _close_pdf_document(own)


# =============================================================================
#  LLM VISION OCR
# =============================================================================

def ocr_image_with_llm(
    image_bytes: bytes,
    http_client,
    api_key: str,
    api_base: str,
    model: str,
    system_prompt: str = OCR_SYSTEM_PROMPT,
    user_prompt: str = OCR_USER_PROMPT,
    log=None,
    max_retries: Optional[int] = None,
    base_delay: Optional[float] = None,
    gate: Optional[_AdaptiveGate] = None,
    result_info: Optional[dict] = None,
) -> Tuple[str, float]:
    """
    Extrait le texte d'une image via LLM Vision.

    Robustesse passerelle : retries exponentiels plafonnés (60 s) avec jitter
    ±30 %, base allongée sur congestion (504/502/503/429). `gate` (optionnel)
    borne la concurrence pendant l'appel et s'adapte aux 5xx. `result_info`
    (dict optionnel) est renseigné en sortie : {"transient": bool} — True si
    l'échec final est RE-TENTABLE (l'appelant ne doit pas figer ce résultat).
    """
    _log = log or logger
    if max_retries is None:
        max_retries = max(1, _env_int_ocr("RIVAGE_OCR_RETRIES", 6))
    if base_delay is None:
        base_delay = _env_float_ocr("RIVAGE_OCR_RETRY_BASE_S", 2.0)

    image_b64 = image_to_base64(image_bytes)
    # La chaîne base64 double la taille de l'image en mémoire ; on n'a plus besoin
    # des bytes bruts une fois le payload construit.
    url = api_base.rstrip("/") + "/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                            "detail": "high"
                        }
                    }
                ]
            }
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    # Libérer la chaîne base64 maintenant qu'elle est intégrée dans le payload
    del image_b64

    if result_info is not None:
        result_info["transient"] = False

    last_error = None
    for attempt in range(max_retries):
        # Arrêt demandé (bouton Quitter) : aucun nouvel essai, même pour une
        # page déjà entamée — la page reste TRANSITOIRE (re-tentable au
        # prochain run), rien de payé n'est perdu (audit V2, B-13).
        if gate is not None and gate.stop_requested:
            if result_info is not None:
                result_info["transient"] = True
            _log.info("[LLM-OCR] Arrêt demandé — essai OCR abandonné "
                      "(page re-tentable au prochain run).")
            return "", 0.0
        try:
            # Le slot de concurrence n'encadre QUE l'appel HTTP : un worker en
            # attente de retry ne bloque pas les autres.
            if gate is not None:
                gate.acquire()
            try:
                resp = http_client.post(url, headers=headers, json=payload,
                                        timeout=120.0)
                resp.raise_for_status()
                data = resp.json()
            finally:
                if gate is not None:
                    gate.release()

            if gate is not None:
                gate.report_success()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

            if not content:
                _log.warning("[LLM-OCR] Réponse vide du LLM")
                return "", 0.0

            confidence = _estimate_ocr_confidence(content)
            _log.debug(f"[LLM-OCR] Texte extrait: {len(content)} chars, confiance: {confidence:.0%}")
            return content, confidence

        except Exception as e:
            last_error = e
            resp_obj = getattr(e, 'response', None)
            status_code = getattr(resp_obj, 'status_code', None)
            # Le CORPS de la réponse porte le diagnostic exact de la passerelle
            # (« key not allowed for model … ») — indispensable sur 4xx.
            try:
                resp_body = (getattr(resp_obj, "text", "") or "")[:300]
            except Exception:
                resp_body = ""

            # Congestion : 429 et TOUT 5xx (un 500 de la passerelle saturée
            # est aussi passager qu'un 504 — audit V2, B-1).
            is_congestion = (status_code in _CONGESTION_CODES
                             or (status_code is not None and status_code >= 500))
            # 401/403 : clé absente ou refusée POUR CE MODÈLE (une clé par
            # modèle sur la passerelle). Inutile de re-tenter dans l'appel,
            # mais l'échec est ENVIRONNEMENTAL : la page reste re-tentable —
            # le document ne doit pas être figé avec son texte dégradé, il
            # sera repris une fois la clé corrigée.
            is_auth = status_code in (401, 403)
            is_retriable = is_congestion or _is_transport_error(e)
            # Transitoire = re-tentable, ou auth, ou tout échec SANS code HTTP
            # 4xx qui ne soit pas une erreur locale (réponse non JSON d'une
            # passerelle en détresse, par exemple) : dans le doute, ne pas
            # figer le document.
            is_transient = (is_retriable or is_auth
                            or (status_code is None and not _is_local_error(e)))
            if is_congestion and gate is not None:
                gate.report_congestion()

            if attempt < max_retries - 1 and is_retriable:
                # Base ×3 sur congestion (la passerelle a besoin de MINUTES,
                # pas de secondes), plafond 60 s, jitter ±30 % pour ne pas
                # resynchroniser tous les workers sur les mêmes instants.
                base = base_delay * (_CONGESTION_FACTOR if is_congestion else 1.0)
                delay = min(_RETRY_CAP_S, base * (2 ** attempt)) * random.uniform(0.7, 1.3)
                log_fn = _log.info if is_congestion else _log.debug
                log_fn(
                    f"[LLM-OCR] Erreur OCR (tentative {attempt + 1}/{max_retries}): "
                    f"{type(e).__name__} {status_code or ''} - retry dans {delay:.1f}s"
                )
                # Avec un limiteur partagé, le sommeil est interruptible par
                # l'arrêt coopératif (audit V2, B-13) ; sans, sommeil simple.
                if gate is not None:
                    gate.wait_retry(delay)
                else:
                    time.sleep(delay)
            else:
                if result_info is not None:
                    result_info["transient"] = bool(is_transient)
                if is_auth:
                    extra = (" — clé API absente ou refusée pour ce modèle "
                             "(vérifiez RIVAGE_SYNTH_KEY : c'est elle qui sert "
                             "l'OCR Vision ; test : python rivage.py diag-synth). "
                             "Document re-tentable une fois la clé corrigée")
                elif is_transient:
                    extra = " (transitoire, page re-tentable)"
                else:
                    extra = ""
                _log.error(f"[LLM-OCR] Erreur appel LLM Vision{extra}: {e}"
                           + (f" — réponse passerelle: {resp_body}" if resp_body else ""))
                return "", 0.0

    if result_info is not None:
        result_info["transient"] = True
    _log.error(f"[LLM-OCR] Échec après {max_retries} tentatives: {last_error}")
    return "", 0.0


def _estimate_ocr_confidence(text: str) -> float:
    """Estime la confiance de l'OCR basée sur le texte extrait."""
    if not text:
        return 0.0

    confidence = 1.0

    if len(text) < 100:
        confidence -= 0.3
    elif len(text) < 500:
        confidence -= 0.1

    illisible_count = text.lower().count("[illisible]")
    if illisible_count > 0:
        confidence -= min(0.4, illisible_count * 0.1)

    alpha_count = sum(1 for c in text if c.isalpha())
    alpha_ratio = alpha_count / len(text) if text else 0

    if alpha_ratio < 0.3:
        confidence -= 0.2

    return max(0.0, min(1.0, confidence))


# =============================================================================
#  DOCUMENT OCR
# =============================================================================

def _format_duration(seconds: float) -> str:
    """Formate une durée en secondes en format lisible."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m{secs:02d}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h{minutes:02d}m"


def _print_progress_bar(current: int, total: int, width: int = 40, prefix: str = "") -> None:
    """Affiche une barre de progression textuelle."""
    if total == 0:
        return
    percent = current / total
    filled = int(width * percent)
    bar = "█" * filled + "░" * (width - filled)
    print(f"\r  {prefix}[{bar}] {current}/{total} ({percent*100:.0f}%)", end="", flush=True)


def _ocr_single_page_worker(
    args: Tuple,
) -> OCRResult:
    """Worker pour l'OCR parallele d'une seule page.

    Effectue: rasterisation -> [smart_rotate] -> OCR LLM dans un seul thread.

    Audit V2, B-13 : le client HTTP du DOCUMENT (args[10], thread-safe) est
    partagé par tous les workers — un client (et son contexte TLS, 84 ms
    mesurés) par page n'apportait rien. Sans client transmis, le worker crée
    et ferme le sien (compatibilité). Le document PyMuPDF est ouvert UNE fois
    par page (contre trois : rasterisation, détection locale, confirmation).
    """
    (pdf_path, page_num, dpi, auto_rotate, smart_rotate,
     api_key, api_base, model, log_name, gate) = args[:10]
    shared_client = args[10] if len(args) > 10 else None

    _log = logging.getLogger(log_name) if log_name else logger
    page_start = time.time()

    def _fail(msg: str, transient: bool = False) -> OCRResult:
        return OCRResult(
            page_number=page_num + 1, text="", confidence=0.0,
            processing_time=time.time() - page_start, error=msg,
            transient=transient)

    # Arrêt déjà demandé quand la page sort de la file : ni rasterisation ni
    # appel — la page reste re-tentable.
    if gate is not None and gate.stop_requested:
        return _fail(f"Arrêt demandé avant la page {page_num + 1}", transient=True)

    owned_client = shared_client is None
    http_client = shared_client
    if owned_client:
        try:
            http_client = create_http_client()
        except Exception as e:
            return _fail(f"Erreur creation client HTTP: {e}")

    doc = None
    try:
        try:
            doc = _open_pdf_document(pdf_path)
        except Exception as e:
            return _fail(f"Erreur ouverture PDF page {page_num + 1}: {e}")

        # 1. Orientation locale (gratuite) puis rasterisation — une seule
        #    détection, réutilisée par la confirmation LLM optionnelle.
        local_rotation = 0
        if auto_rotate or smart_rotate:
            local_rotation = detect_page_orientation(pdf_path, page_num,
                                                     log=_log, doc=doc)
        image_bytes = pdf_page_to_image(
            pdf_path, page_num, dpi=dpi, auto_rotate=False,
            force_rotation=(local_rotation if auto_rotate else None),
            log=_log, doc=doc,
        )
        if not image_bytes:
            return _fail(f"Erreur conversion page {page_num + 1}")

        # 2. Smart rotation (detection locale d'abord, LLM seulement si necessaire)
        if smart_rotate and local_rotation == 0:
            llm_rotation = detect_orientation_with_llm(
                image_bytes=image_bytes, http_client=http_client,
                api_key=api_key, api_base=api_base, model=model, log=_log
            )
            if llm_rotation != 0:
                image_bytes = pdf_page_to_image(
                    pdf_path, page_num, dpi=dpi, auto_rotate=False,
                    force_rotation=llm_rotation, log=_log, doc=doc,
                )
                if not image_bytes:
                    return _fail(f"Erreur rotation page {page_num + 1}")

        # 3. OCR via LLM Vision
        info: dict = {}
        text, confidence = ocr_image_with_llm(
            image_bytes=image_bytes, http_client=http_client,
            api_key=api_key, api_base=api_base, model=model, log=_log,
            gate=gate, result_info=info,
        )

        # Liberer la memoire
        del image_bytes
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
        if owned_client:
            try:
                http_client.close()
            except Exception:
                pass

    processing_time = time.time() - page_start

    if text:
        return OCRResult(
            page_number=page_num + 1, text=text,
            confidence=confidence, processing_time=processing_time
        )
    else:
        transient = bool(info.get("transient"))
        return OCRResult(
            page_number=page_num + 1, text="", confidence=0.0,
            processing_time=processing_time,
            error=(f"OCR echoue page {page_num + 1}"
                   + (" (erreur transitoire — page re-tentable)" if transient else "")),
            transient=transient,
        )


def ocr_pdf_with_llm(
    pdf_path: str,
    http_client,
    api_key: str,
    api_base: str,
    model: str,
    pages: Optional[List[int]] = None,
    dpi: int = DEFAULT_DPI,
    auto_rotate: bool = True,
    smart_rotate: Optional[bool] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    log=None,
    should_stop: Optional[Callable[[], bool]] = None,
    total_pages: Optional[int] = None,
) -> DocumentOCRResult:
    """
    Effectue l'OCR d'un PDF complet via LLM Vision.

    Pipeline parallele:
    - Les pages sont traitees en parallele via ThreadPoolExecutor
    - Chaque worker rasterise + OCR sa page independamment
    - Le nombre de workers s'adapte a la RAM et au CPU de la machine
    - Sur 16 cores / 21 GB RAM: jusqu'a 10 pages en parallele

    `should_stop` : arrêt coopératif. Testé entre chaque page terminée — les
    pages en attente sont annulées, les pages EN VOL cessent leurs retries,
    celles déjà OCRisées restent au cache de reprise (rien de payé n'est
    perdu ; le run suivant reprend là).

    `http_client` : UN client par document, partagé par les workers (httpx
    est thread-safe) ; s'il est absent, un client est créé ici et fermé en
    sortie. `total_pages` : nombre de pages déjà connu de l'appelant (évite
    une relecture du PDF) — audit V2, B-13.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _log = log or logger
    start_time = time.time()
    # Coût : la confirmation d'orientation par LLM est OPT-IN
    # (RIVAGE_OCR_SMART_ROTATE=1) — voir bandeau « Maîtrise du COÛT OCR ».
    smart_rotate = _smart_rotate_enabled(smart_rotate)

    if total_pages is None:
        total_pages = get_pdf_page_count(pdf_path, log=_log)

    if total_pages == 0:
        return DocumentOCRResult(
            total_pages=0, processed_pages=0, full_text="",
            page_results=[], total_time=0, avg_confidence=0,
            errors=["Impossible de lire le PDF"]
        )

    # Client HTTP du document (audit V2, B-13) : créé ici seulement si
    # l'appelant n'en fournit pas, fermé en `finally` dans tous les cas où
    # il a été créé ici.
    owned_client = False
    if http_client is None and DALLEM_CONFIG_AVAILABLE:
        try:
            http_client = create_http_client()
            owned_client = True
        except Exception as e:
            _log.warning(f"[LLM-OCR] Client HTTP du document non créé ({e}) — "
                         f"chaque worker créera le sien.")
            http_client = None
    try:
        return _ocr_pdf_with_llm_dispatch(
            pdf_path, http_client, api_key, api_base, model, pages, dpi,
            auto_rotate, smart_rotate, progress_cb, _log, should_stop,
            total_pages, start_time, ThreadPoolExecutor, as_completed)
    finally:
        if owned_client:
            try:
                http_client.close()
            except Exception:
                pass


def _ocr_pdf_with_llm_dispatch(
    pdf_path, http_client, api_key, api_base, model, pages, dpi,
    auto_rotate, smart_rotate, progress_cb, _log, should_stop,
    total_pages, start_time, ThreadPoolExecutor, as_completed,
) -> DocumentOCRResult:
    """Corps de ocr_pdf_with_llm (séparé pour garantir la fermeture du client)."""

    if pages is None:
        pages_to_process = list(range(total_pages))
    else:
        pages_to_process = [p for p in pages if 0 <= p < total_pages]

    # --- Cache de reprise : restaurer les pages déjà OCRisées ----------------
    results_by_page: Dict[int, OCRResult] = {}
    cache = _OcrPageCache.open_for(pdf_path, _log)
    if cache is not None:
        cached = cache.get_all()
        for p in pages_to_process:
            if p in cached:
                texte, conf = cached[p]
                results_by_page[p] = OCRResult(
                    page_number=p + 1, text=texte, confidence=conf,
                    processing_time=0.0)
        if results_by_page:
            _log.info(f"[LLM-OCR] Reprise : {len(results_by_page)}/"
                      f"{len(pages_to_process)} page(s) restaurée(s) du cache "
                      f"— seules les pages manquantes seront OCRisées.")

    pages_to_dispatch = [p for p in pages_to_process if p not in results_by_page]
    # Arrêt coopératif AVANT tout appel : ne rien dispatcher (bouton Quitter
    # pressé pendant l'extraction classique qui précède, par exemple).
    stopped = bool(should_stop and should_stop())
    if stopped and pages_to_dispatch:
        _log.info(f"[LLM-OCR] Arrêt demandé — OCR non lancé "
                  f"({len(pages_to_dispatch)} page(s) laissée(s) pour le prochain run).")
        pages_to_dispatch = []
    n_pages = len(pages_to_dispatch)
    # Plafond manuel de parallélisme (RIVAGE_OCR_WORKERS, 0 = auto RAM/CPU) :
    # utile quand la passerelle Vision est dimensionnée petit.
    workers_cap = _env_int_ocr("RIVAGE_OCR_WORKERS", 0) or OCR_PARALLEL_WORKERS
    n_workers = min(workers_cap, OCR_PARALLEL_WORKERS, max(n_pages, 1))
    filename = os.path.basename(pdf_path)
    # Limiteur ADAPTATIF partagé : chaque 5xx/429 divise la concurrence
    # effective par 2 (plancher 1), les succès la remontent — voir _AdaptiveGate.
    gate = _AdaptiveGate(n_workers, _log)

    print(f"\n{'='*60}")
    print(f"OCR LLM VISION - DEMARRAGE PARALLELE")
    print(f"{'='*60}")
    print(f"  Fichier: {filename}")
    print(f"  Pages a traiter: {n_pages}/{total_pages}"
          + (f" ({len(results_by_page)} restaurees du cache)"
             if results_by_page else ""))
    print(f"  Workers paralleles: {n_workers}")
    print(f"  Resolution: {dpi} DPI")
    print(f"{'─'*60}")

    _log.info(
        f"[LLM-OCR] Demarrage OCR parallele: {n_pages}/{total_pages} pages, "
        f"{n_workers} workers"
    )

    # Preparer les arguments pour chaque page
    log_name = _log.name if hasattr(_log, 'name') else None
    # args[10] : client HTTP partagé du document (audit V2, B-13) — le gate
    # reste en position 9 (contrat des workers).
    worker_args = [
        (pdf_path, page_num, dpi, auto_rotate, smart_rotate,
         api_key, api_base, model, log_name, gate, http_client)
        for page_num in pages_to_dispatch
    ]

    # Execution parallele
    completed_count = 0

    if worker_args:
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            future_to_page = {
                executor.submit(_ocr_single_page_worker, args): args[1]
                for args in worker_args
            }

            for future in as_completed(future_to_page):
                page_num = future_to_page[future]
                completed_count += 1

                try:
                    result = future.result()
                    results_by_page[page_num] = result

                    # Point de reprise : chaque page réussie est persistée
                    # IMMÉDIATEMENT — un plantage ne reperd plus les pages
                    # déjà payées à l'API Vision.
                    if cache is not None and result.text:
                        cache.put(page_num, result.text, result.confidence)

                    status = "OK" if result.text else f"ERREUR: {result.error}"
                    _log.debug(
                        f"[LLM-OCR] Page {page_num + 1}: {status} "
                        f"({result.processing_time:.1f}s)"
                    )
                except Exception as e:
                    _log.error(f"[LLM-OCR] Exception page {page_num + 1}: {e}")
                    results_by_page[page_num] = OCRResult(
                        page_number=page_num + 1, text="", confidence=0.0,
                        processing_time=0, error=str(e)
                    )

                # Progression
                if progress_cb:
                    progress = completed_count / n_pages
                    progress_cb(progress, f"OCR page {completed_count}/{n_pages}")
                _print_progress_bar(completed_count, n_pages, prefix="  ")

                # GC periodique
                if completed_count % 10 == 0:
                    gc.collect()

                # Arrêt coopératif : annuler les pages pas encore lancées puis
                # sortir. Les pages déjà réussies viennent d'être mises au
                # cache — le prochain run reprendra exactement ici. Le `with`
                # n'attend plus que les quelques pages déjà en vol.
                if should_stop is not None and should_stop():
                    stopped = True
                    cancelled = sum(1 for f in future_to_page if f.cancel())
                    # Pages EN VOL : plus aucun nouvel essai ni sommeil de
                    # retry (audit V2, B-13) — le `with` ne les attend que
                    # le temps de l'appel en cours.
                    gate.request_stop()
                    _log.info(
                        f"[LLM-OCR] Arrêt demandé — {cancelled} page(s) en "
                        f"attente annulée(s) ; {len(results_by_page)} page(s) "
                        f"acquise(s) conservée(s) au cache de reprise.")
                    break

    if cache is not None:
        cache.close()

    print()

    # Reassembler dans l'ordre des pages
    page_results: List[OCRResult] = []
    errors: List[str] = []
    all_texts: List[str] = []

    for page_num in pages_to_process:
        result = results_by_page.get(page_num)
        if result is None:
            result = OCRResult(
                page_number=page_num + 1, text="", confidence=0.0,
                processing_time=0, error=f"Page {page_num + 1} non traitee"
            )
        page_results.append(result)

        if result.text:
            all_texts.append(f"--- Page {result.page_number} ---\n{result.text}")
        if result.error:
            errors.append(result.error)

    total_time = time.time() - start_time
    processed_pages = sum(1 for r in page_results if r.text)
    transient_failures = sum(1 for r in page_results if r.transient)
    confidences = [r.confidence for r in page_results if r.confidence > 0]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    full_text = "\n\n".join(all_texts)

    # Calcul du speedup
    sequential_time = sum(r.processing_time for r in page_results)
    speedup = sequential_time / total_time if total_time > 0 else 1.0

    print(f"{'─'*60}")
    print(f"OCR LLM TERMINE")
    print(f"  Pages traitees: {processed_pages}/{n_pages}")
    if errors:
        print(f"  Erreurs: {len(errors)}")
    if transient_failures:
        print(f"  Dont transitoires (passerelle saturee, re-tentables): "
              f"{transient_failures}")
    print(f"  Confiance moyenne: {avg_confidence:.0%}")
    print(f"  Caracteres extraits: {len(full_text):,}")
    print(f"  Temps total: {_format_duration(total_time)} (speedup {speedup:.1f}x vs sequentiel)")
    print(f"  Workers utilises: {n_workers}")
    print(f"{'='*60}\n")

    _log.info(
        f"[LLM-OCR] OCR termine: {processed_pages}/{n_pages} pages en "
        f"{_format_duration(total_time)} (speedup {speedup:.1f}x, {n_workers} workers)"
    )

    return DocumentOCRResult(
        total_pages=total_pages, processed_pages=processed_pages,
        full_text=full_text, page_results=page_results,
        total_time=total_time, avg_confidence=avg_confidence, errors=errors,
        transient_failures=transient_failures,
    )


# =============================================================================
#  SMART OCR (FALLBACK AUTOMATIQUE)
# =============================================================================

def smart_ocr_pdf(
    pdf_path: str,
    http_client,
    api_key: str,
    api_base: str,
    model: str,
    quality_threshold: float = 0.5,
    fallback_pages_only: bool = True,
    auto_rotate: bool = True,
    smart_rotate: Optional[bool] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    log=None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    OCR intelligent avec fallback automatique.

    1. Tente l'extraction classique (PyMuPDF/pdfminer)
    2. Évalue la qualité de chaque page
    3. Utilise LLM OCR uniquement pour les pages de mauvaise qualité

    Le document PyMuPDF est ouvert UNE fois ici (audit perf 2.1, I-9 : un
    PDF texte de 30 pages était ouvert 7 fois avant tout appel Vision —
    comptage, cascade d'extraction, contrôle de contenu visuel page à page)
    et transmis au comptage, à l'extraction classique et aux contrôles de
    page ; il est fermé en `finally` sur tous les chemins, et dès avant le
    lancement de l'OCR (les workers, en threads, ouvrent le leur).
    """
    _log = log or logger
    doc = None
    try:
        doc = _open_pdf_document(pdf_path)
    except Exception as e:
        _log.debug(f"[SMART-OCR] Ouverture PyMuPDF impossible ({e}) — "
                   f"les étapes ouvriront le document elles-mêmes.")
        doc = None
    try:
        return _smart_ocr_pdf_impl(
            pdf_path, http_client, api_key, api_base, model, quality_threshold,
            fallback_pages_only, auto_rotate, smart_rotate, progress_cb, _log,
            should_stop, doc)
    finally:
        _close_pdf_document(doc)


def _smart_ocr_pdf_impl(pdf_path, http_client, api_key, api_base, model,
                        quality_threshold, fallback_pages_only, auto_rotate,
                        smart_rotate, progress_cb, _log, should_stop, doc):
    """Corps de smart_ocr_pdf (séparé pour garantir la fermeture du document
    PyMuPDF partagé, `doc` — None si PyMuPDF est absent ou le PDF illisible)."""
    try:
        from pdf_processing import (
            extract_text_from_pdf,
            assess_extraction_quality
        )
    except ImportError:
        _log.error("[LLM-OCR] Impossible d'importer pdf_processing")
        return {"error": "Module pdf_processing non disponible"}

    # Document partagé transmis SEULEMENT s'il est ouvert : les fonctions
    # gardent ainsi leur appel historique (et leur signature minimale) quand
    # il n'y en a pas.
    doc_kw = {"doc": doc} if doc is not None else {}
    filename = os.path.basename(pdf_path)
    total_pages = get_pdf_page_count(pdf_path, log=_log, **doc_kw)

    _log.info(f"[SMART-OCR] Analyse du PDF: {pdf_path}")

    # 1. Extraction classique
    if progress_cb:
        progress_cb(0.1, "Extraction classique du texte...")

    try:
        classic_text = extract_text_from_pdf(pdf_path, **doc_kw)
    except Exception as e:
        _log.warning(f"[SMART-OCR] Extraction classique échouée: {e}")
        classic_text = ""

    # 2. Évaluer la qualité globale
    if progress_cb:
        progress_cb(0.2, "Évaluation de la qualité...")

    quality = assess_extraction_quality(classic_text)
    quality_score = quality.get("quality_score", 0)

    _log.info(f"[SMART-OCR] Qualité extraction classique: {quality_score:.0%}")

    # --- Analyse PAGE PAR PAGE (locale, gratuite), AVANT la porte globale ----
    # L'extraction classique sépare les pages par form-feed `\f` : chaque page
    # est classée (vide avec contenu visuel / blanche / tableau rempli /
    # faible / correcte). Cette analyse sert aux DEUX portes :
    #  - score global ≥ seuil : passe ciblée bon marché — seules les pages
    #    sans texte qui ont un contenu visuel sont OCRisées (audit V2, B-2 :
    #    300 pages correctes + 2 annexes scannées donnaient 1,0 → « pas besoin
    #    d'OCR », les annexes restaient vides pour toujours) ;
    #  - score global < seuil : OCR ciblé des pages sous le seuil (2 pages
    #    scannées dans un PDF de 300 → 2 appels, pas 300).
    # Une page où pdfplumber a trouvé un TABLEAU REMPLI n'est jamais OCRisée
    # (audit V2, B-3 : elle était notée ~0 à cause du rendu tabulaire, payée,
    # puis son texte propre remplacé par la sortie du modèle).
    try:
        import pdf_processing as _pp
        table_min_cells = int(getattr(_pp, "TABLE_MIN_CELLS", 4))
    except Exception:
        table_min_cells = 4
    pages = None                       # None = toutes les pages
    classic_pages: List[str] = []      # découpage cohérent → fusion par page
    blank_skipped = 0                  # pages blanches non payées (coût évité)
    table_pages_kept = 0               # pages tableau gardées (coût évité)
    targeted_ok = False                # analyse page par page effectuée
    empty_visual: List[int] = []       # sans texte, avec contenu visuel
    weak: List[int] = []               # avec texte, sous le seuil (hors tableau)
    # Découpage par pages renseigné DÈS qu'il est cohérent, que le ciblage
    # soit demandé ou non (audit V2, B-12) : c'est lui qui permet, quand le
    # plafond RIVAGE_OCR_MAX_PAGES_DOC coupe un OCR complet, de garder le
    # texte classique des pages non OCRisées — sans lui, `full_text` du
    # modèle remplaçait tout le document et ces pages étaient perdues,
    # contrairement à ce que le journal annonçait.
    if total_pages > 1 and classic_text:
        split = classic_text.split("\f")
        # pdfminer laisse un \f final : dernière entrée vide à ignorer.
        if len(split) == total_pages + 1 and not split[-1].strip():
            split = split[:-1]
        if len(split) == total_pages:
            classic_pages = split
    if fallback_pages_only and classic_pages:
        targeted_ok = True
        for i, ptext in enumerate(classic_pages):
            if _is_quasi_empty(ptext):
                # Page (quasi) sans texte : à OCRiser… SAUF si elle est
                # réellement BLANCHE (ni image ni dessin — mini-rendu
                # local gratuit) : un appel Vision n'y trouverait rien.
                if _page_has_visual_content(pdf_path, i, _log, **doc_kw):
                    empty_visual.append(i)
                else:
                    blank_skipped += 1
                continue
            pq = assess_extraction_quality(ptext)
            if int(pq.get("table_cells", 0) or 0) >= table_min_cells:
                table_pages_kept += 1
                continue
            if pq.get("quality_score", 0) < quality_threshold:
                weak.append(i)
        if blank_skipped:
            _log.info(f"[SMART-OCR] {blank_skipped} page(s) blanche(s) "
                      f"sautée(s) — appel(s) Vision évité(s).")
        if table_pages_kept:
            _log.info(f"[SMART-OCR] {table_pages_kept} page(s) à tableau "
                      f"extrait proprement conservée(s) — appel(s) Vision "
                      f"évité(s).")

    def _classic_result(method: str = "classic") -> Dict[str, Any]:
        return {
            "text": classic_text,
            "method": method,
            "quality_score": quality_score,
            "llm_ocr_used": False,
            "pages_ocr": 0,
            "ocr_blank_skipped": blank_skipped,
            "ocr_table_pages_kept": table_pages_kept,
        }

    # 3. Décider ce qui part à l'OCR Vision
    if quality_score >= quality_threshold:
        if targeted_ok and empty_visual:
            # Audit V2, B-2 : document globalement bon mais pages scannées
            # vides — passe ciblée sur ces seules pages.
            pages = sorted(empty_visual)
            shown = ", ".join(str(p + 1) for p in pages[:15])
            more = "…" if len(pages) > 15 else ""
            _log.info(f"[SMART-OCR] Qualité globale suffisante ({quality_score:.0%}) "
                      f"mais {len(pages)} page(s) sans texte avec contenu visuel "
                      f"(pages {shown}{more}) — OCR ciblé de ces pages.")
        else:
            _log.info("[SMART-OCR] Qualité suffisante, pas besoin de LLM OCR")
            return _classic_result()
    else:
        _log.info(f"[SMART-OCR] Qualité insuffisante ({quality_score:.0%}), "
                  f"utilisation LLM OCR...")
        if targeted_ok:
            bad = sorted(set(empty_visual) | set(weak))
            if bad and len(bad) < total_pages:
                pages = bad
                shown = ", ".join(str(p + 1) for p in bad[:15])
                more = "…" if len(bad) > 15 else ""
                _log.info(f"[SMART-OCR] OCR ciblé : {len(bad)}/{total_pages} "
                          f"page(s) sous le seuil (pages {shown}{more}).")
            elif not bad:
                # Aucune page individuellement fautive (blanches, tableaux,
                # ou score global plombé par des pénalités cumulées sur tout
                # le document) : rien à payer, texte classique rendu tel quel.
                _log.info("[SMART-OCR] Aucune page à OCRiser après analyse "
                          "page par page — OCR Vision non lancé.")
                return _classic_result()
            else:
                _log.info(f"[SMART-OCR] Toutes les pages sous le seuil — "
                          f"OCR complet ({total_pages} pages).")
        elif fallback_pages_only and total_pages > 1 and classic_text:
            _log.info(f"[SMART-OCR] Découpage par pages incohérent "
                      f"({len(classic_text.split(chr(12)))} vs {total_pages}) "
                      f"— OCR complet.")

    # Arrêt coopératif : ne pas ENTAMER l'OCR Vision si la sortie est demandée.
    # Le texte classique est rendu tel quel ; l'indexeur n'enregistrera pas un
    # document extrait pendant un arrêt, le fichier sera repris au prochain run.
    if should_stop is not None and should_stop():
        _log.info("[SMART-OCR] Arrêt demandé — OCR Vision non lancé.")
        return _classic_result()

    if progress_cb:
        progress_cb(0.3, "Démarrage OCR LLM Vision...")

    # Plafond de BUDGET par document (RIVAGE_OCR_MAX_PAGES_DOC, 0 = illimité) :
    # au-delà, les pages excédentaires gardent leur texte classique — choix
    # assumé (non re-tenté), clairement journalisé.
    cap = _env_int_ocr("RIVAGE_OCR_MAX_PAGES_DOC", 0)
    capped = 0
    if cap:
        if pages is not None and len(pages) > cap:
            capped = len(pages) - cap
            pages = pages[:cap]
        elif pages is None and total_pages > cap:
            capped = total_pages - cap
            pages = list(range(cap))
        if capped:
            _log.warning(f"[SMART-OCR] Budget OCR : plafond de {cap} page(s) "
                         f"par document (RIVAGE_OCR_MAX_PAGES_DOC) — "
                         f"{capped} page(s) non OCRisée(s), texte classique "
                         f"conservé pour celles-ci.")

    def ocr_progress(ratio, msg):
        if progress_cb:
            adjusted = 0.3 + ratio * 0.65
            progress_cb(adjusted, msg)

    # Plus besoin du document partagé : le refermer AVANT l'OCR (minutes à
    # heures sur un gros scan) — pas de descripteur tenu sur le partage
    # pendant ce temps ; les workers ouvrent le leur (B-13).
    _close_pdf_document(doc)
    ocr_result = ocr_pdf_with_llm(
        pdf_path=pdf_path, http_client=http_client,
        api_key=api_key, api_base=api_base, model=model,
        pages=pages, auto_rotate=auto_rotate, smart_rotate=smart_rotate,
        progress_cb=ocr_progress, log=_log, should_stop=should_stop,
        total_pages=total_pages,
    )

    if progress_cb:
        progress_cb(1.0, "OCR terminé")

    llm_used = True
    if classic_pages:
        # Fusion PAGE PAR PAGE : une page OCRisée remplace sa version classique
        # seulement si celle-ci était réellement vide ou illisible — jamais une
        # page tableau ni une page correcte (audit V2, B-3 : en OCR complet,
        # `avg_confidence > quality_score` remplaçait TOUT le texte propre).
        # Jointes par `\f` : la numérotation des citations « page N » reste
        # exacte. Les marqueurs de page ÉMIS PAR LE MODÈLE dans le texte d'une
        # page (« --- Page N --- », form-feed) sont neutralisés (audit v2) :
        # injectés entre des `\f`, ils dérailleraient la pagination au chunking.
        if targeted_ok:
            replaceable = set(empty_visual) | set(weak)
        else:
            # OCR complet demandé (fallback_pages_only=False) : toute page
            # OCRisée remplace sa version classique ; les pages NON OCRisées
            # (plafond RIVAGE_OCR_MAX_PAGES_DOC) gardent leur texte classique
            # (audit V2, B-12).
            replaceable = set(range(len(classic_pages)))
        ocr_by_page = {r.page_number - 1: r for r in ocr_result.page_results}
        merged = []
        replaced = 0
        for i, ptext in enumerate(classic_pages):
            r = ocr_by_page.get(i)
            if r is not None and r.text.strip() and i in replaceable:
                merged.append(_PAGE_MARKERS_RE.sub("\n", r.text))
                replaced += 1
            else:
                merged.append(ptext)
        final_text = "\f".join(merged)
        if ocr_result.processed_pages == 0:
            # Toutes les pages ciblées ont échoué : le texte est 100 % classique,
            # ne pas l'étiqueter OCR (audit v2 — métadonnées trompeuses).
            method = "llm_ocr_failed"
            llm_used = False
        elif replaced == 0:
            method = "classic_preferred"
        elif replaced == total_pages:
            method = "llm_ocr"
        else:
            method = "llm_ocr_partial"
    elif ocr_result.avg_confidence > quality_score:
        final_text = ocr_result.full_text
        method = "llm_ocr"
    else:
        final_text = classic_text
        method = "classic_preferred"

    return {
        "text": final_text,
        "method": method,
        "quality_score": quality_score,
        "llm_ocr_used": llm_used,
        "llm_ocr_confidence": ocr_result.avg_confidence,
        "pages_ocr": ocr_result.processed_pages,
        "total_pages": ocr_result.total_pages,
        "ocr_time": ocr_result.total_time,
        "ocr_errors": ocr_result.errors,
        # Pages en échec TRANSITOIRE (504…) : > 0 => résultat incomplet mais
        # re-tentable — l'indexeur n'enregistre pas le document (voir indexer).
        "ocr_transient_failures": ocr_result.transient_failures,
        # Visibilité du coût : pages blanches évitées, pages tableau
        # conservées, pages plafonnées.
        "ocr_blank_skipped": blank_skipped,
        "ocr_table_pages_kept": table_pages_kept,
        "ocr_pages_capped": capped,
    }


# =============================================================================
#  UTILITY FUNCTIONS
# =============================================================================

def ocr_single_page(
    pdf_path: str,
    page_number: int,
    http_client,
    api_key: str,
    api_base: str,
    model: str,
    dpi: int = DEFAULT_DPI,
    log=None
) -> OCRResult:
    """OCR d'une seule page via LLM Vision."""
    _log = log or logger
    start_time = time.time()

    page_idx = page_number - 1
    image_bytes = pdf_page_to_image(pdf_path, page_idx, dpi=dpi, log=_log)

    if not image_bytes:
        return OCRResult(
            page_number=page_number, text="", confidence=0.0,
            processing_time=time.time() - start_time,
            error="Erreur conversion page en image"
        )

    text, confidence = ocr_image_with_llm(
        image_bytes=image_bytes, http_client=http_client,
        api_key=api_key, api_base=api_base, model=model, log=_log
    )

    return OCRResult(
        page_number=page_number, text=text, confidence=confidence,
        processing_time=time.time() - start_time,
        error=None if text else "OCR n'a retourné aucun texte"
    )


def is_vision_model_available(
    http_client,
    api_key: str,
    api_base: str,
    model: str,
    log=None
) -> bool:
    """Vérifie si le modèle vision est disponible et fonctionnel."""
    _log = log or logger

    test_image = base64.b64encode(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
        b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff'
        b'\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82'
    ).decode('utf-8')

    url = api_base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Test"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{test_image}"}
                    }
                ]
            }
        ],
        "max_tokens": 10,
    }

    try:
        resp = http_client.post(url, headers=headers, json=payload, timeout=30.0)
        if resp.status_code == 200:
            _log.info(f"[LLM-OCR] Modèle vision {model} disponible")
            return True
        else:
            _log.warning(f"[LLM-OCR] Modèle vision {model} non disponible: {resp.status_code}")
            return False
    except Exception as e:
        _log.warning(f"[LLM-OCR] Test modèle vision échoué: {e}")
        return False


# =============================================================================
#  FONCTIONS DE COMMODITÉ DALLEM
# =============================================================================

def ocr_pdf_with_dallem(
    pdf_path: str,
    pages: Optional[List[int]] = None,
    dpi: int = DEFAULT_DPI,
    auto_rotate: bool = True,
    smart_rotate: Optional[bool] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    log=None
) -> DocumentOCRResult:
    """OCR d'un PDF en utilisant l'API DALLEM configurée."""
    _log = log or logger

    if not DALLEM_CONFIG_AVAILABLE:
        raise RuntimeError(
            "Configuration DALLEM non disponible. "
            "Vérifiez que models_utils.py est accessible et configuré."
        )

    # Un client par DOCUMENT, fermé en sortie (audit V2, B-13 : jamais fermé).
    http_client = create_http_client()
    try:
        return ocr_pdf_with_llm(
            pdf_path=pdf_path, http_client=http_client,
            api_key=DALLEM_API_KEY, api_base=DALLEM_API_BASE, model=LLM_MODEL,
            pages=pages, dpi=dpi, auto_rotate=auto_rotate, smart_rotate=smart_rotate,
            progress_cb=progress_cb, log=_log
        )
    finally:
        try:
            http_client.close()
        except Exception:
            pass


def smart_ocr_with_dallem(
    pdf_path: str,
    quality_threshold: float = 0.5,
    fallback_pages_only: bool = True,
    auto_rotate: bool = True,
    smart_rotate: Optional[bool] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    log=None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """OCR intelligent avec DALLEM et fallback automatique."""
    _log = log or logger

    if not DALLEM_CONFIG_AVAILABLE:
        raise RuntimeError(
            "Configuration DALLEM non disponible. "
            "Vérifiez que models_utils.py est accessible et configuré."
        )

    # Un client par DOCUMENT, partagé par les workers OCR et fermé en sortie
    # (audit V2, B-13).
    http_client = create_http_client()
    try:
        return smart_ocr_pdf(
            pdf_path=pdf_path, http_client=http_client,
            api_key=DALLEM_API_KEY, api_base=DALLEM_API_BASE, model=LLM_MODEL,
            quality_threshold=quality_threshold, fallback_pages_only=fallback_pages_only,
            auto_rotate=auto_rotate, smart_rotate=smart_rotate,
            progress_cb=progress_cb, log=_log, should_stop=should_stop,
        )
    finally:
        try:
            http_client.close()
        except Exception:
            pass


def check_dallem_vision_available(log=None) -> bool:
    """Vérifie si DALLEM est disponible et supporte les images."""
    _log = log or logger

    if not DALLEM_CONFIG_AVAILABLE:
        _log.warning("[LLM-OCR] Configuration OCR Vision non disponible")
        return False

    http_client = create_http_client()
    try:
        return is_vision_model_available(
            http_client=http_client,
            api_key=DALLEM_API_KEY,
            api_base=DALLEM_API_BASE,
            model=LLM_MODEL,
            log=_log
        )
    finally:
        try:
            http_client.close()
        except Exception:
            pass


def check_ocr_available(log=None) -> Dict[str, Any]:
    """
    Vérifie la disponibilité des capacités OCR (DALLEM Vision).
    """
    _log = log or logger

    dallem_available = check_dallem_vision_available(log=_log)

    return {
        "available": dallem_available,
        "mode": "api",
        "backend": "dallem",
        "details": {
            "api_base": DALLEM_API_BASE if DALLEM_CONFIG_AVAILABLE else None,
        }
    }
