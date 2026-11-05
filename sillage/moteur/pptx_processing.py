# pptx_processing.py
"""
PPTX Processing Module - Traitement des fichiers PowerPoint

Extraction du contenu des fichiers .pptx:
- Texte des slides (titres, corps, placeholders)
- Notes du présentateur
- Tableaux
- Images avec OCR via LLM Vision

Intégration avec le système OCR LLM existant (llm_ocr.py) pour
extraire le texte des images embarquées dans les présentations.
"""

import os
import re
import io
import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# =============================================================================
#  IMPORT PPTX (avec gestion d'erreur)
# =============================================================================

PPTX_AVAILABLE = False
try:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    PPTX_AVAILABLE = True
except ImportError:
    logger.warning(
        "[PPTX] python-pptx non installé. "
        "Installez-le avec: pip install python-pptx"
    )

# =============================================================================
#  IMPORT OCR LLM (optionnel)
# =============================================================================

LLM_OCR_AVAILABLE = False
DALLEM_API_KEY = None
DALLEM_API_BASE = None
LLM_MODEL = None
create_http_client = None

try:
    from llm_ocr import (
        image_to_base64,
        ocr_image_with_llm,
        OCRResult,
    )
    from models_utils import (
        DALLEM_API_KEY,
        DALLEM_API_BASE,
        LLM_MODEL,
        create_http_client,
    )
    LLM_OCR_AVAILABLE = True
    logger.info("[PPTX] OCR LLM Vision disponible (mistral-small)")
except ImportError as e:
    logger.debug(f"[PPTX] Module OCR LLM non disponible: {e}")


# =============================================================================
#  DATA CLASSES
# =============================================================================

@dataclass
class SlideContent:
    """Contenu extrait d'une slide."""
    slide_number: int
    title: str = ""
    body_text: str = ""
    notes: str = ""
    table_text: str = ""
    image_texts: List[str] = field(default_factory=list)
    shape_count: int = 0
    image_count: int = 0


@dataclass
class PPTXExtractionResult:
    """Résultat de l'extraction d'un fichier PPTX."""
    filename: str
    total_slides: int
    slides: List[SlideContent]
    full_text: str
    images_processed: int = 0
    images_with_text: int = 0
    ocr_used: bool = False
    processing_time: float = 0.0
    errors: List[str] = field(default_factory=list)


# =============================================================================
#  EXTRACTION DE TEXTE
# =============================================================================

def extract_text_from_shape(shape) -> str:
    """
    Extrait le texte d'une forme PowerPoint.

    Gère les cas: TextFrame, Table, GroupShape.
    """
    texts = []

    try:
        # Texte standard (TextFrame)
        if shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                para_text = ""
                for run in paragraph.runs:
                    if run.text:
                        para_text += run.text
                if para_text.strip():
                    texts.append(para_text.strip())

        # Tableau
        if shape.has_table:
            table_text = extract_text_from_table(shape.table)
            if table_text:
                texts.append(table_text)

        # Groupe de formes (récursif)
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for sub_shape in shape.shapes:
                sub_text = extract_text_from_shape(sub_shape)
                if sub_text:
                    texts.append(sub_text)

    except Exception as e:
        logger.debug(f"[PPTX] Erreur extraction forme: {e}")

    return "\n".join(texts)


def extract_text_from_table(table) -> str:
    """
    Extrait le texte d'un tableau PowerPoint.

    Format: colonnes séparées par " | ", lignes séparées par newline.
    """
    rows_text = []

    try:
        for row in table.rows:
            cells_text = []
            for cell in row.cells:
                cell_text = ""
                if cell.text_frame:
                    for paragraph in cell.text_frame.paragraphs:
                        for run in paragraph.runs:
                            if run.text:
                                cell_text += run.text
                cells_text.append(cell_text.strip())

            if any(cells_text):
                rows_text.append(" | ".join(cells_text))

    except Exception as e:
        logger.debug(f"[PPTX] Erreur extraction tableau: {e}")

    return "\n".join(rows_text)


def _shape_image(shape):
    """Objet image d'une forme : image classique (PICTURE) OU placeholder
    image rempli (type PLACEHOLDER mais porteur d'un `.image`) — audit V2,
    B-14 : les images insérées dans un placeholder « Image » des modèles
    n'étaient ni comptées ni OCRisées. None si la forme n'est pas une image."""
    try:
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            return shape.image
        if getattr(shape, "is_placeholder", False) and hasattr(shape, "image"):
            return shape.image
    except Exception:
        return None
    return None


def _iter_shapes_recursive(shapes):
    """Parcourt les formes en entrant dans les groupes (imbriqués compris)."""
    for shape in shapes:
        yield shape
        try:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                for sub in _iter_shapes_recursive(shape.shapes):
                    yield sub
        except Exception:
            continue


def _image_format(image) -> str:
    content_type = (getattr(image, "content_type", "") or "").lower()
    for key, fmt in (("png", "png"), ("jpeg", "jpeg"), ("jpg", "jpeg"),
                     ("gif", "gif"), ("bmp", "bmp"), ("tiff", "tiff")):
        if key in content_type:
            return fmt
    return "png"


def _image_sha1(image, blob: bytes) -> str:
    """Empreinte de CONTENU de l'image (python-pptx l'expose ; sinon calculée
    sur les octets) — clé de déduplication par document (audit V2, B-6)."""
    try:
        s = image.sha1
        if s:
            return str(s)
    except Exception:
        pass
    import hashlib
    return hashlib.sha1(blob).hexdigest()


def extract_slide_images_with_ids(slide) -> List[Tuple[bytes, str, str]]:
    """Images d'une slide (images classiques, placeholders image, groupes)
    sous forme de (octets, format, sha1)."""
    images: List[Tuple[bytes, str, str]] = []
    try:
        for shape in _iter_shapes_recursive(slide.shapes):
            image = _shape_image(shape)
            if image is None:
                continue
            try:
                blob = image.blob
                images.append((blob, _image_format(image), _image_sha1(image, blob)))
            except Exception as e:
                logger.debug(f"[PPTX] Erreur extraction image: {e}")
    except Exception as e:
        logger.debug(f"[PPTX] Erreur parcours shapes: {e}")
    return images


def extract_images_from_slide(slide) -> List[Tuple[bytes, str]]:
    """
    Extrait les images d'une slide.

    Returns:
        Liste de tuples (image_bytes, format) ex: (b'...', 'png')
    """
    return [(b, fmt) for (b, fmt, _sha) in extract_slide_images_with_ids(slide)]


# Image DÉCORATIVE (logo, bandeau, fond de modèle) : présente sur plus de
# _DECORATIVE_SLIDE_RATIO des slides d'un deck d'au moins _DECORATIVE_MIN_SLIDES
# slides — elle ne porte pas de contenu propre à une slide et n'est jamais
# OCRisée (audit V2, B-6 : un logo de 40 Ko sur 120 slides = 120 appels
# payants et 120 répétitions dans l'index).
_DECORATIVE_SLIDE_RATIO = 0.30
_DECORATIVE_MIN_SLIDES = 4


def _decorative_image_hashes(prs, log=None) -> set:
    """Empreintes des images présentes sur plus de 30 % des slides (deck
    d'au moins 4 slides). Passe locale, gratuite (lecture des sha1)."""
    _log = log or logger
    try:
        slides = list(prs.slides)
    except Exception:
        return set()
    n_slides = len(slides)
    if n_slides < _DECORATIVE_MIN_SLIDES:
        return set()
    counts: Dict[str, int] = {}
    for slide in slides:
        seen_here = set()
        try:
            for shape in _iter_shapes_recursive(slide.shapes):
                image = _shape_image(shape)
                if image is None:
                    continue
                try:
                    h = _image_sha1(image, image.blob)
                except Exception:
                    continue
                seen_here.add(h)
        except Exception:
            continue
        for h in seen_here:
            counts[h] = counts.get(h, 0) + 1
    decorative = {h for h, n in counts.items()
                  if n / float(n_slides) > _DECORATIVE_SLIDE_RATIO}
    if decorative:
        _log.info(f"[PPTX] {len(decorative)} image(s) décorative(s) (présente(s) "
                  f"sur plus de {int(_DECORATIVE_SLIDE_RATIO * 100)} % des "
                  f"{n_slides} slides) — exclue(s) de l'OCR.")
    return decorative


def _chart_text(chart) -> str:
    """Texte d'un graphique : titre, catégories et noms de séries (audit V2,
    B-14 : les graphiques étaient entièrement ignorés)."""
    parts: List[str] = []
    try:
        if getattr(chart, "has_title", False):
            title = chart.chart_title.text_frame.text.strip()
            if title:
                parts.append(title)
    except Exception:
        pass
    categories: List[str] = []
    series: List[str] = []
    try:
        for plot in chart.plots:
            if not categories:
                try:
                    categories = [str(c).strip() for c in plot.categories
                                  if c is not None and str(c).strip()]
                except Exception:
                    categories = []
            try:
                for s in plot.series:
                    name = (s.name or "").strip()
                    if name and name not in series:
                        series.append(name)
            except Exception:
                continue
    except Exception:
        pass
    if categories:
        parts.append("Catégories : " + ", ".join(categories))
    if series:
        parts.append("Séries : " + ", ".join(series))
    return ("[Graphique] " + "\n".join(parts)) if parts else ""


# =============================================================================
#  OCR DES IMAGES (MODE-AWARE)
# =============================================================================
#
#  RaGME_UP - OCR pour PPTX via DALLEM Vision
#  Auteur: Renaud LOISON
# =============================================================================


# L'API Vision reçoit un en-tête `data:image/png` : il faut donc lui envoyer
# du PNG RÉEL, borné en taille — comme les pages PDF rasterisées (2048 px).
# Or les images PPTX partaient BRUTES : un TIFF/BMP/GIF (ou un EMF/WMF que
# l'extraction étiquette « png » par défaut) provoquait un 400 Bad Request,
# et une photo de 20 Mo pouvait dépasser les limites de la passerelle.
_VISION_MAX_PX = 2048
# Refus de décodage au-delà (bombe de décompression) : 25 MP ≈ 75 Mo RGB —
# très au-dessus de toute capture/photo légitime de slide.
_VISION_MAX_DECODE_PIXELS = 25_000_000


def _png_dims_from_header(data: bytes):
    """(largeur, hauteur) lues dans l'en-tête PNG (IHDR), sans décodage.
    (0, 0) si ce n'est pas un PNG — le garde s'efface alors."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        import struct
        try:
            return struct.unpack(">II", data[16:24])
        except struct.error:
            pass
    return 0, 0


def _normalize_image_for_vision(image_bytes: bytes, fmt: str, log=None):
    """Convertit une image de slide en PNG ≤ 2048 px (PyMuPDF, local, gratuit).
    Retourne les octets PNG, ou None si le format est inconvertible
    (EMF/WMF vectoriels…) — l'image est alors ignorée AVANT tout appel payant.
    """
    _log = log or logger
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
    except ImportError:
        # Pas de convertisseur : ne laisser passer que les formats sûrs.
        return image_bytes if (fmt or "").lower() in ("png", "jpeg", "jpg") else None
    try:
        # BOMBE DE DÉCOMPRESSION : un petit fichier peut décrire une image
        # démesurée (20000×20000 => >1 Go décodé). Lire les dimensions dans
        # l'EN-TÊTE, sans décoder, et refuser l'aberrant — AVANT toute
        # allocation native. image_profile est ENVELOPPÉ LARGE : selon la
        # version de PyMuPDF il peut être absent OU cassé (TypeError constaté)
        # — dans ce cas le garde s'efface (repli en-tête PNG), il ne doit
        # JAMAIS rejeter une image valide par accident.
        w = h = 0
        try:
            prof = fitz.image_profile(image_bytes) or {}
            w = int(prof.get("width", 0) or 0)
            h = int(prof.get("height", 0) or 0)
        except Exception:
            w, h = _png_dims_from_header(image_bytes)
        if w * h > _VISION_MAX_DECODE_PIXELS:
            _log.warning(f"[PPTX] Image démesurée ({w}×{h}) ignorée — "
                         f"décodage refusé (protection mémoire).")
            return None
        pix = fitz.Pixmap(image_bytes)
        # Espaces de couleur exotiques (CMJN, masques) -> RGB.
        if pix.colorspace is None or pix.colorspace.n > 3:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        while max(pix.width, pix.height) > _VISION_MAX_PX:
            pix.shrink(1)              # divise les dimensions par 2 (en place)
        return pix.tobytes("png")
    except Exception as e:
        _log.info(f"[PPTX] Image non convertible en PNG ({fmt}) — ignorée "
                  f"avant appel Vision : {e}")
        return None


def ocr_images_with_llm(
    images: List[Tuple[bytes, str]],
    http_client=None,
    api_key: str = None,
    api_base: str = None,
    model: str = None,
    log=None,
    stats_out: Optional[dict] = None,
    should_stop=None,
) -> List[str]:
    """
    Effectue l'OCR sur une liste d'images via DALLEM Vision.

    Args:
        images: Liste de (image_bytes, format)
        http_client: Client HTTP (optionnel, créé si absent)
        api_key: Clé API (optionnel, utilise DALLEM par défaut)
        api_base: URL API (optionnel)
        model: Modèle vision (optionnel)
        log: Logger
        stats_out: dict où "transient" compte les images en échec OCR
                   transitoire (document re-tentable, voir extraction)
        should_stop: arrêt coopératif, relu entre deux images

    Returns:
        Liste de textes extraits (un par image)
    """
    _log = log or logger

    if not images:
        _log.debug("[PPTX] Aucune image à traiter")
        return []

    # Maîtrise du coût : les petites images (logos, puces, pictogrammes) ne
    # portent pas de texte utile — en dessous de RIVAGE_OCR_IMAGE_MIN_KB
    # (défaut 5 Ko, 0 = tout OCRiser), l'appel Vision est évité.
    try:
        _v = os.environ.get("RIVAGE_OCR_IMAGE_MIN_KB")
        min_kb = max(0, int(_v)) if _v is not None and _v.strip() else 5
    except (TypeError, ValueError):
        min_kb = 5
    if min_kb:
        kept = [(b, f) for (b, f) in images if len(b) >= min_kb * 1024]
        skipped = len(images) - len(kept)
        if skipped:
            _log.info(f"[PPTX] {skipped} petite(s) image(s) < {min_kb} Ko "
                      f"ignorée(s) (RIVAGE_OCR_IMAGE_MIN_KB) — appel(s) "
                      f"Vision évité(s).")
        images = kept
        if not images:
            return []

    print(f"  [PPTX] OCR de {len(images)} image(s)...")

    texts = []

    if not LLM_OCR_AVAILABLE:
        print("  [PPTX] OCR LLM non disponible - vérifiez DALLEM_API_KEY")
        _log.warning("[PPTX] OCR LLM non disponible - images ignorées")
        return []

    _log.info("[PPTX] Utilisation de l'OCR Vision (mistral-small)")
    print("  [PPTX] OCR Vision (mistral-small)")

    # Configuration par défaut (DALLEM)
    if api_key is None:
        api_key = DALLEM_API_KEY
    if api_base is None:
        api_base = DALLEM_API_BASE
    if model is None:
        model = LLM_MODEL
    if http_client is None:
        http_client = create_http_client()

    for i, (image_bytes, fmt) in enumerate(images):
        # Arrêt coopératif (bouton Quitter) ENTRE deux images : un deck de
        # 300 images ne doit pas continuer à payer des appels Vision après la
        # demande d'arrêt (audit V2, B-7). Le document ne sera pas enregistré
        # (should_stop est relu par l'indexeur après l'extraction).
        if should_stop is not None and should_stop():
            _log.info(f"[PPTX] Arrêt demandé : OCR interrompu après {i} image(s).")
            break
        try:
            print(f"      Image {i+1}/{len(images)} ({fmt}, {len(image_bytes)//1024}KB)...", end=" ", flush=True)
            _log.debug(f"[PPTX] OCR image {i+1}/{len(images)} ({fmt})...")

            # PNG réel ≤ 2048 px — l'en-tête envoyé déclare image/png ; une
            # image brute d'un autre format provoquait un 400 Bad Request.
            normalized = _normalize_image_for_vision(image_bytes, fmt, _log)
            if normalized is None:
                print("format non convertible - ignoree")
                continue

            info: dict = {}
            text, confidence = ocr_image_with_llm(
                image_bytes=normalized,
                http_client=http_client,
                api_key=api_key,
                api_base=api_base,
                model=model,
                log=_log,
                result_info=info,
            )

            if text and confidence > 0.3:
                texts.append(text)
                print(f"OK {len(text)} chars (conf: {confidence:.0%})")
                _log.debug(f"[PPTX] Image {i+1}: {len(text)} chars, confiance {confidence:.0%}")
            elif not text and info.get("transient"):
                # Échec TRANSITOIRE (504 épuisés, timeout, clé refusée) : ce
                # n'est pas « pas de texte » — compté pour que l'indexeur ne
                # fige pas le document (audit V2, B-7).
                if stats_out is not None:
                    stats_out["transient"] = stats_out.get("transient", 0) + 1
                print("échec transitoire (image re-tentable)")
                _log.warning(f"[PPTX] Image {i+1}: échec OCR transitoire — "
                             f"document re-tentable au prochain run.")
            else:
                print(f"pas de texte (conf: {confidence:.0%})")
                _log.debug(f"[PPTX] Image {i+1}: pas de texte significatif")

        except Exception as e:
            print(f"Erreur: {e}")
            _log.warning(f"[PPTX] Erreur OCR Vision image {i+1}: {e}")

    print(f"  [PPTX] OCR terminé: {len(texts)}/{len(images)} images avec texte")
    return texts


# =============================================================================
#  EXTRACTION PRINCIPALE
# =============================================================================

def extract_slide_content(
    slide,
    slide_number: int,
    ocr_images: bool = False,
    http_client=None,
    log=None,
    ocr_stats: Optional[dict] = None,
    should_stop=None,
    image_cache: Optional[dict] = None,
    decorative: Optional[set] = None,
) -> SlideContent:
    """
    Extrait le contenu complet d'une slide.

    Args:
        slide: Objet Slide de python-pptx
        slide_number: Numéro de la slide (1-indexed)
        ocr_images: Si True, effectue l'OCR sur les images
        http_client: Client HTTP pour OCR
        log: Logger
        ocr_stats: compteur partagé des échecs OCR transitoires, et des
                   appels évités par déduplication ("duplicates",
                   "decorative")
        should_stop: arrêt coopératif (propagé à l'OCR des images)
        image_cache: {sha1: True} des images déjà OCRisées dans CE document
                     (audit V2, B-6) — une image n'est envoyée qu'une fois,
                     son texte n'est pas réinséré sur les slides suivantes
        decorative: empreintes des images décoratives (jamais OCRisées)

    Returns:
        SlideContent avec tout le contenu extrait
    """
    _log = log or logger

    content = SlideContent(slide_number=slide_number)

    # Compteurs
    image_count = 0
    shape_count = 0

    # Textes collectés
    body_texts = []
    table_texts = []

    try:
        for shape in slide.shapes:
            shape_count += 1

            # Titre : placeholders TITLE (1) et CENTER_TITLE (3) — pas 2, qui
            # est BODY (audit V2, B-14 : le premier corps de texte d'une slide
            # sans titre passait pour son titre).
            if shape.has_text_frame and shape.is_placeholder:
                try:
                    placeholder_type = int(shape.placeholder_format.type)
                    if placeholder_type in (1, 3):
                        if shape.text.strip() and not content.title:
                            content.title = shape.text.strip()
                            continue
                except Exception:
                    pass

            # Images (classiques ET placeholders image remplis)
            if _shape_image(shape) is not None:
                image_count += 1

            # Graphiques : titre, catégories, séries (audit V2, B-14)
            if getattr(shape, "has_chart", False):
                try:
                    chart_text = _chart_text(shape.chart)
                    if chart_text:
                        body_texts.append(chart_text)
                except Exception as e:
                    _log.debug(f"[PPTX] Erreur extraction graphique: {e}")

            # Tableaux
            if shape.has_table:
                table_text = extract_text_from_table(shape.table)
                if table_text:
                    table_texts.append(table_text)

            # Texte standard
            elif shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    para_text = ""
                    for run in paragraph.runs:
                        if run.text:
                            para_text += run.text
                    if para_text.strip():
                        body_texts.append(para_text.strip())

            # Groupes (récursif)
            elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                for sub_shape in shape.shapes:
                    sub_text = extract_text_from_shape(sub_shape)
                    if sub_text:
                        body_texts.append(sub_text)
                for sub_shape in _iter_shapes_recursive(shape.shapes):
                    if _shape_image(sub_shape) is not None:
                        image_count += 1

        # Notes du présentateur
        if slide.has_notes_slide and slide.notes_slide:
            notes_frame = slide.notes_slide.notes_text_frame
            if notes_frame:
                notes_text = notes_frame.text.strip()
                if notes_text:
                    content.notes = notes_text

        # Assembler
        content.body_text = "\n".join(body_texts)
        content.table_text = "\n".join(table_texts)
        content.shape_count = shape_count
        content.image_count = image_count

        # OCR des images si demandé
        if ocr_images and image_count > 0:
            _log.debug(f"[PPTX] Slide {slide_number}: {image_count} images détectées, extraction...")
            found = extract_slide_images_with_ids(slide)
            _log.debug(f"[PPTX] Slide {slide_number}: {len(found)} images extraites")
            # Déduplication par CONTENU au niveau document (audit V2, B-6) :
            # une image décorative (logo, bandeau) n'est jamais envoyée ; une
            # image déjà vue sur une slide précédente (ou deux fois sur
            # celle-ci) n'est ni re-payée ni réinsérée dans le texte.
            images: List[Tuple[bytes, str]] = []
            dup = deco = 0
            for blob, fmt, sha in found:
                if decorative and sha in decorative:
                    deco += 1
                    continue
                if image_cache is not None:
                    if sha in image_cache:
                        dup += 1
                        continue
                    image_cache[sha] = True
                images.append((blob, fmt))
            if (dup or deco) and ocr_stats is not None:
                ocr_stats["duplicates"] = ocr_stats.get("duplicates", 0) + dup
                ocr_stats["decorative"] = ocr_stats.get("decorative", 0) + deco
            if dup or deco:
                _log.debug(f"[PPTX] Slide {slide_number}: {dup} doublon(s) et "
                           f"{deco} image(s) décorative(s) non OCRisé(s).")
            if images:
                image_texts = ocr_images_with_llm(
                    images=images,
                    http_client=http_client,
                    log=_log,
                    stats_out=ocr_stats,
                    should_stop=should_stop,
                )
                content.image_texts = image_texts
            elif not found:
                _log.warning(f"[PPTX] Slide {slide_number}: {image_count} images comptées mais 0 extraites")
        elif ocr_images:
            _log.debug(f"[PPTX] Slide {slide_number}: aucune image")

    except Exception as e:
        print(f"  ❌ [PPTX] Erreur slide {slide_number}: {e}")
        _log.error(f"[PPTX] Erreur extraction slide {slide_number}: {e}")

    return content


def format_slide_text(content: SlideContent, include_metadata: bool = True) -> str:
    """
    Formate le contenu d'une slide en texte lisible.

    Args:
        content: SlideContent à formater
        include_metadata: Si True, inclut le numéro de slide

    Returns:
        Texte formaté
    """
    parts = []

    if include_metadata:
        parts.append(f"--- Slide {content.slide_number} ---")

    if content.title:
        parts.append(f"# {content.title}")

    if content.body_text:
        parts.append(content.body_text)

    if content.table_text:
        parts.append("\n[Tableau]")
        parts.append(content.table_text)

    if content.notes:
        parts.append("\n[Notes]")
        parts.append(content.notes)

    if content.image_texts:
        parts.append("\n[Texte extrait des images]")
        for i, img_text in enumerate(content.image_texts, 1):
            parts.append(f"Image {i}: {img_text}")

    return "\n".join(parts)


# =============================================================================
#  FONCTIONS PUBLIQUES
# =============================================================================

def extract_text_from_pptx(
    file_path: str,
    ocr_images: bool = False,
    include_notes: bool = True,
    include_tables: bool = True,
    log=None,
    stats_out: Optional[dict] = None,
    should_stop=None,
) -> str:
    """
    Extrait le texte d'un fichier PPTX.

    Args:
        file_path: Chemin vers le fichier .pptx
        ocr_images: Si True, effectue l'OCR sur les images (nécessite LLM)
        include_notes: Si True, inclut les notes du présentateur
        include_tables: Si True, inclut le contenu des tableaux
        log: Logger

    Returns:
        Texte extrait, nettoyé et formaté

    Raises:
        RuntimeError: Si python-pptx n'est pas installé
        FileNotFoundError: Si le fichier n'existe pas
    """
    _log = log or logger

    if not PPTX_AVAILABLE:
        raise RuntimeError(
            "python-pptx n'est pas installé. "
            "Installez-le avec: pip install python-pptx"
        )

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Fichier introuvable: {file_path}")

    filename = os.path.basename(file_path)
    _log.info(f"[PPTX] Extraction: {filename}")
    print(f"\n📊 [PPTX] Extraction: {filename}")

    try:
        prs = Presentation(file_path)
    except Exception as e:
        raise RuntimeError(f"Impossible d'ouvrir le fichier PPTX: {e}")

    print(f"  📑 Slides: {len(prs.slides)}")
    print(f"  🔍 OCR images: {'Oui' if ocr_images else 'Non'}")
    print(f"  🤖 LLM OCR disponible: {'Oui' if LLM_OCR_AVAILABLE else 'Non'}")

    # Client HTTP pour OCR (créé une seule fois)
    http_client = None
    if ocr_images:
        if LLM_OCR_AVAILABLE:
            http_client = create_http_client()
            print(f"  ✅ Client HTTP créé pour OCR")
        else:
            print(f"  ⚠️ OCR demandé mais LLM non disponible - vérifiez DALLEM_API_KEY")

    all_texts = []
    total_images = 0
    images_with_text = 0
    ocr_stats: dict = {"transient": 0, "duplicates": 0, "decorative": 0}
    # Déduplication par document (audit V2, B-6) : images décoratives
    # repérées en une passe locale, cache des empreintes déjà OCRisées.
    decorative: set = set()
    image_cache: dict = {}
    if ocr_images:
        decorative = _decorative_image_hashes(prs, _log)

    try:
        for i, slide in enumerate(prs.slides, 1):
            # Arrêt coopératif entre deux slides (l'indexeur n'enregistrera pas
            # un document extrait partiellement).
            if should_stop is not None and should_stop():
                _log.info(f"[PPTX] Arrêt demandé : extraction interrompue à la slide {i}.")
                break
            content = extract_slide_content(
                slide=slide,
                slide_number=i,
                ocr_images=ocr_images,
                http_client=http_client,
                log=_log,
                ocr_stats=ocr_stats,
                should_stop=should_stop,
                image_cache=image_cache,
                decorative=decorative,
            )

            # Construire le texte de la slide
            parts = []
            parts.append(f"--- Slide {i} ---")

            if content.title:
                parts.append(f"# {content.title}")

            if content.body_text:
                parts.append(content.body_text)

            if include_tables and content.table_text:
                parts.append(content.table_text)

            if include_notes and content.notes:
                parts.append(f"[Notes: {content.notes}]")

            if content.image_texts:
                for img_text in content.image_texts:
                    parts.append(f"[Image: {img_text}]")
                    images_with_text += 1

            total_images += content.image_count

            slide_text = "\n".join(parts)
            if slide_text.strip():
                all_texts.append(slide_text)
    finally:
        # Client HTTP du document fermé dans tous les cas (audit V2, B-13).
        if http_client is not None:
            try:
                http_client.close()
            except Exception:
                pass

    calls_avoided = int(ocr_stats.get("duplicates", 0)) + int(ocr_stats.get("decorative", 0))

    # Récapitulatif
    print(f"\n  {'─'*50}")
    print(f"  ✅ [PPTX] Extraction terminée")
    print(f"     📑 Slides: {len(prs.slides)}")
    print(f"     🖼️ Images détectées: {total_images}")
    print(f"     📝 Images avec texte OCR: {images_with_text}")
    if calls_avoided:
        print(f"     ♻️ Appels Vision évités (doublons/décoratives): {calls_avoided}")
    print(f"     📄 Caractères extraits: {len(''.join(all_texts)):,}")
    print(f"  {'─'*50}\n")

    _log.info(
        f"[PPTX] Extraction terminée: {len(prs.slides)} slides, "
        f"{total_images} images, {images_with_text} avec texte OCR"
        + (f", {calls_avoided} appel(s) Vision évité(s) par déduplication"
           if calls_avoided else "")
    )

    # Compteurs pour l'appelant (rattrapage OCR : un PPTX à images indexé
    # sans OCR est marqué ocr_pending — voir extraction/_extract_pptx).
    if stats_out is not None:
        stats_out["image_count"] = total_images
        stats_out["images_with_text"] = images_with_text
        stats_out["ocr_transient_failures"] = int(ocr_stats.get("transient", 0))
        # Coût évité par déduplication (audit V2, B-6).
        stats_out["images_duplicates_skipped"] = int(ocr_stats.get("duplicates", 0))
        stats_out["images_decorative_skipped"] = int(ocr_stats.get("decorative", 0))
        stats_out["ocr_calls_avoided"] = calls_avoided

    return "\n\n".join(all_texts)


def process_pptx_with_ocr(
    file_path: str,
    quality_threshold: float = 0.5,
    force_ocr: bool = False,
    log=None
) -> Dict[str, Any]:
    """
    Traite un PPTX avec OCR intelligent sur les images.

    Cette fonction:
    1. Extrait le texte standard (slides, notes, tableaux)
    2. Évalue si les images contiennent probablement du texte
    3. Applique l'OCR LLM sur les images pertinentes

    Args:
        file_path: Chemin vers le fichier .pptx
        quality_threshold: Seuil de qualité pour déclencher l'OCR
        force_ocr: Si True, force l'OCR sur toutes les images
        log: Logger

    Returns:
        Dict avec:
        - text: Texte complet extrait
        - method: "classic" ou "ocr"
        - slides_count: Nombre de slides
        - images_count: Nombre d'images
        - images_ocr: Nombre d'images traitées en OCR
        - ocr_used: True si OCR utilisé
    """
    _log = log or logger

    if not PPTX_AVAILABLE:
        return {
            "text": "",
            "error": "python-pptx non installé",
            "method": "error"
        }

    import time
    start_time = time.time()

    filename = os.path.basename(file_path)
    _log.info(f"[PPTX] Traitement avec OCR: {filename}")

    try:
        prs = Presentation(file_path)
    except Exception as e:
        return {
            "text": "",
            "error": f"Impossible d'ouvrir: {e}",
            "method": "error"
        }

    # Statistiques
    total_slides = len(prs.slides)
    total_images = 0
    images_ocr = 0
    ocr_used = False

    # Déterminer si on doit faire l'OCR
    should_ocr = force_ocr

    if not should_ocr:
        # Compter les images pour décider (placeholders image compris, B-14)
        for slide in prs.slides:
            for shape in _iter_shapes_recursive(slide.shapes):
                if _shape_image(shape) is not None:
                    total_images += 1

        # Si beaucoup d'images, probablement utile de faire l'OCR
        # Heuristique: > 2 images par slide en moyenne
        if total_images > total_slides * 2:
            _log.info(f"[PPTX] {total_images} images détectées - OCR recommandé")
            should_ocr = LLM_OCR_AVAILABLE

    # Extraction
    text = extract_text_from_pptx(
        file_path=file_path,
        ocr_images=should_ocr,
        include_notes=True,
        include_tables=True,
        log=_log
    )

    # Recompter les images OCR si utilisé
    if should_ocr:
        ocr_used = True
        # Compter les "[Image:" dans le texte
        images_ocr = text.count("[Image:")

    processing_time = time.time() - start_time

    result = {
        "text": text,
        "method": "ocr" if ocr_used else "classic",
        "slides_count": total_slides,
        "images_count": total_images,
        "images_ocr": images_ocr,
        "ocr_used": ocr_used,
        "processing_time": processing_time,
    }

    _log.info(
        f"[PPTX] Terminé: {total_slides} slides, "
        f"{total_images} images, OCR={ocr_used}, "
        f"temps={processing_time:.1f}s"
    )

    return result


def extract_pptx_metadata(file_path: str, log=None) -> Dict[str, Any]:
    """
    Extrait les métadonnées d'un fichier PPTX.

    Args:
        file_path: Chemin vers le fichier .pptx
        log: Logger

    Returns:
        Dict avec les métadonnées (titre, auteur, slides, etc.)
    """
    _log = log or logger

    if not PPTX_AVAILABLE:
        return {"error": "python-pptx non installé"}

    try:
        prs = Presentation(file_path)

        # Propriétés du document
        core_props = prs.core_properties

        metadata = {
            "filename": os.path.basename(file_path),
            "slides_count": len(prs.slides),
            "title": core_props.title or "",
            "author": core_props.author or "",
            "subject": core_props.subject or "",
            "keywords": core_props.keywords or "",
            "created": str(core_props.created) if core_props.created else "",
            "modified": str(core_props.modified) if core_props.modified else "",
            "last_modified_by": core_props.last_modified_by or "",
        }

        # Compter les éléments
        total_shapes = 0
        total_images = 0
        total_tables = 0
        slides_with_notes = 0

        for slide in prs.slides:
            for shape in slide.shapes:
                total_shapes += 1
                if _shape_image(shape) is not None:
                    total_images += 1
                if shape.has_table:
                    total_tables += 1

            if slide.has_notes_slide and slide.notes_slide:
                if slide.notes_slide.notes_text_frame.text.strip():
                    slides_with_notes += 1

        metadata.update({
            "total_shapes": total_shapes,
            "total_images": total_images,
            "total_tables": total_tables,
            "slides_with_notes": slides_with_notes,
        })

        return metadata

    except Exception as e:
        _log.error(f"[PPTX] Erreur extraction métadonnées: {e}")
        return {"error": str(e)}


# =============================================================================
#  NORMALISATION
# =============================================================================

def normalize_whitespace(text: str) -> str:
    """
    Normalise les espaces dans le texte en conservant les sauts de ligne.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    norm_lines = []
    for line in lines:
        line = re.sub(r"[ \t]+", " ", line)
        norm_lines.append(line.strip())
    return "\n".join(norm_lines).strip()


# =============================================================================
#  COMPATIBILITÉ AVEC LE PIPELINE
# =============================================================================

def is_pptx_available() -> bool:
    """Vérifie si le module PPTX est disponible."""
    return PPTX_AVAILABLE


def is_pptx_ocr_available() -> bool:
    """Vérifie si l'OCR des images PPTX est disponible."""
    return PPTX_AVAILABLE and LLM_OCR_AVAILABLE
