# extraction.py
"""
Extraction Router - Routage de l'extraction par format + parcours récursif.

Point d'entrée unique de l'étape 2 du pipeline d'indexation (note de
conception §2.1, §3). Deux responsabilités :

  1. `extract_document()` — route un fichier vers le bon extracteur selon son
     extension et retourne son texte (+ métadonnées légères).
  2. `walk_corpus()` — parcourt récursivement un répertoire et énumère les
     fichiers supportés, en portant le chemin comme signal de « famille »
     (le grand répertoire de tête, §10.2).

Le routage applique la stratégie de la spec :
    PDF        -> pdf_processing (cascade + OCR conditionnel)
    Word       -> docx_processing (.docx natif, .doc via COM Windows)
    PowerPoint -> pptx_processing (+ OCR images optionnel)
    Excel      -> excel_processing (par feuille, en-têtes préservés)
    Texte brut -> lecture directe (.txt / .md)
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Iterator, Callable

logger = logging.getLogger(__name__)

# =============================================================================
#  FORMATS SUPPORTÉS
# =============================================================================

PDF_EXTS = {".pdf"}
WORD_EXTS = {".docx", ".doc"}
PPTX_EXTS = {".pptx"}
EXCEL_EXTS = {".xlsx", ".xls", ".xlsm"}
# .csv volontairement EXCLU de l'indexation : données tabulaires brutes, souvent
# du bruit pour l'embedding sémantique. Les .csv déjà indexés sont purgés au run
# suivant (voir indexer : purge des fichiers dont l'extension n'est plus prise
# en charge).
TEXT_EXTS = {".txt", ".md"}

SUPPORTED_EXTS = PDF_EXTS | WORD_EXTS | PPTX_EXTS | EXCEL_EXTS | TEXT_EXTS

# Pièces jointes embarquées dans un PDF (fichier dans un fichier) : leur texte
# est extrait puis RATTACHÉ au document de tête (même chemin -> même source et
# même lien au clic). Les images sont exclues (rien à indexer sans OCR dédié),
# et l'imbrication est bornée en profondeur pour éviter toute boucle.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff",
              ".webp", ".svg", ".emf", ".wmf", ".ico", ".heic"}
_ATTACH_MAX_DEPTH = 3


@dataclass
class ExtractedDocument:
    """Résultat d'extraction d'un document."""
    path: str
    family: str                     # grand répertoire de tête (thème, §10.2)
    ext: str
    text: str
    method: str                     # extracteur utilisé
    char_count: int = 0
    ocr_used: bool = False
    error: Optional[str] = None
    # Échec TRANSITOIRE (verrou, hoquet SMB, fichier en cours de copie…) :
    # l'indexeur ne doit PAS enregistrer le document — il sera retenté au
    # prochain run. Un échec non transitoire (format inconnu, fichier
    # corrompu) est enregistré pour ne pas retraiter en boucle.
    transient: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
#  ROUTAGE PAR FORMAT
# =============================================================================

def extract_document(
    path: str,
    corpus_root: Optional[str] = None,
    use_ocr: bool = True,
    ocr_quality_threshold: float = 0.5,
    log=None,
    _depth: int = 0,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ExtractedDocument:
    """
    Extrait le texte d'un document en routant vers le bon extracteur.

    Args:
        path: chemin du fichier
        corpus_root: racine du corpus, sert à calculer la « famille » (chemin
                     relatif → grand répertoire de tête). Si None, la famille
                     est le répertoire parent immédiat.
        use_ocr: autorise l'OCR de secours pour les PDF (si sous le seuil) et
                 les images PPTX. Nécessite la clé mistral-small
                 (RIVAGE_SYNTH_KEY) configurée.
        ocr_quality_threshold: seuil de qualité déclenchant l'OCR PDF (§3.1)
        log: logger optionnel
        _depth: usage INTERNE — profondeur d'imbrication des pièces jointes
                (un PDF joint dans un PDF est ré-extrait via cette fonction,
                borné par _ATTACH_MAX_DEPTH). Ne pas renseigner.
        should_stop: arrêt coopératif (bouton Quitter de l'indexeur) — propagé
                jusqu'à la boucle OCR page à page : les pages en attente sont
                annulées, celles déjà payées restent au cache de reprise.

    Returns:
        ExtractedDocument (text vide + error renseigné en cas d'échec).
        Les pièces jointes non-image d'un PDF sont dans
        metadata["attachments"] = [(nom, texte), ...] — chunkées SANS numéro
        de page, sous le chemin du conteneur (voir chunking).
    """
    _log = log or logger
    ext = os.path.splitext(path)[1].lower()
    family = _family_of(path, corpus_root)

    doc = ExtractedDocument(path=path, family=family, ext=ext, text="", method="none")

    try:
        if ext in PDF_EXTS:
            _extract_pdf(doc, use_ocr, ocr_quality_threshold, _log, _depth,
                         should_stop)
        elif ext in WORD_EXTS:
            _extract_word(doc, _log)
        elif ext in PPTX_EXTS:
            _extract_pptx(doc, use_ocr, _log, should_stop=should_stop)
        elif ext in EXCEL_EXTS:
            _extract_excel(doc, _log)
        elif ext in TEXT_EXTS:
            _extract_text_file(doc, _log)
        else:
            doc.error = f"Format non supporté: {ext}"
            _log.debug(f"[extraction] {doc.error} ({path})")
            return doc
    except Exception as e:
        # Mémoire épuisée (MemoryError Python, std::bad_alloc natif) : ce n'est
        # pas une erreur d'extraction mais un fichier pathologique — relevé
        # tel quel pour que la résilience mémoire de l'indexeur (fichier sauté,
        # NON enregistré, run poursuivi) s'applique. Avalé ici, le document
        # était enregistré « vide » et jamais retenté (audit V2, B-5).
        if isinstance(e, MemoryError) or (
                isinstance(e, RuntimeError) and "bad_alloc" in str(e).lower()):
            raise
        # Message JAMAIS vide (audit V2, B-5) : str(PDFPasswordIncorrect()) ou
        # str(MemoryError()) sont vides, et un doc.error vide passait pour une
        # extraction réussie d'un document sans texte.
        msg = str(e).strip()
        doc.error = f"{type(e).__name__}: {msg}" if msg else type(e).__name__
        # Erreur d'ACCÈS (verrou, [WinError 64], fichier en cours de copie) :
        # transitoire — le document doit être retenté, pas figé (audit V2, A-2).
        doc.transient = isinstance(e, OSError)
        _log.warning(f"[extraction] Échec sur {path}: {doc.error}"
                     + (" (transitoire, fichier retenté au prochain run)"
                        if doc.transient else ""))
        return doc

    doc.char_count = len(doc.text)
    return doc


def _extract_pdf(doc, use_ocr, threshold, log, _depth=0, should_stop=None):
    from pdf_processing import extract_text_from_pdf, assess_extraction_quality

    got = False
    if use_ocr:
        # Extraction classique + OCR de secours conditionnel (§3.1)
        try:
            from pdf_processing import smart_extract_text_with_ocr
            result = smart_extract_text_with_ocr(
                doc.path, quality_threshold=threshold, log=log,
                should_stop=should_stop,
            )
            doc.text = result.get("text", "") or ""
            doc.method = result.get("method", "pdf")
            doc.ocr_used = bool(result.get("llm_ocr_used", False))
            doc.metadata["quality_score"] = result.get("quality_score")
            # Pages OCR en échec TRANSITOIRE (504 passerelle…) : le document
            # est incomplet mais RE-TENTABLE — l'indexeur ne l'enregistre pas
            # (sinon le hash serait figé et les pages manquantes perdues à
            # jamais) ; il sera repris au prochain run via le cache OCR.
            transient = int(result.get("ocr_transient_failures", 0) or 0)
            if transient:
                doc.metadata["ocr_transient_failures"] = transient
            # Visibilité du COÛT : pages réellement payées à l'API Vision.
            pages_ocr = int(result.get("pages_ocr", 0) or 0)
            if pages_ocr:
                doc.metadata["pages_ocr"] = pages_ocr
            got = True
        except Exception as e:
            log.debug(f"[extraction] smart OCR indisponible, extraction classique: {e}")

    if not got:
        doc.text = extract_text_from_pdf(doc.path)
        doc.method = "pdf_classic"
        doc.metadata["quality_score"] = assess_extraction_quality(doc.text).get("quality_score")

    # Pièces jointes embarquées (fichier dans le fichier) : leur texte est
    # extrait et RATTACHÉ ici même au document de tête (voir ci-dessous).
    _append_pdf_attachments(doc, use_ocr, threshold, log, _depth, should_stop)


# Limites des pièces jointes — CONFIGURABLES par l'utilisateur :
#  - RIVAGE_ATTACH_MAX_MB : taille max du FICHIER joint (défaut 20 Mo,
#    0 = illimité). Au-delà : ignoré avec un log.
#  - RIVAGE_ATTACH_TEXT_MAX_CHARS : plafond du TEXTE extrait d'une pièce
#    jointe (défaut 1 000 000 caractères, 0 = illimité) — un petit fichier
#    très dense contournerait sinon la limite de taille (audit v3) et
#    produirait des milliers de chunks à embedder pour une seule pièce jointe.
#    Au-delà : tronqué avec un log.
_ATTACH_MAX_MB_DEFAULT = 20
_ATTACH_TEXT_MAX_CHARS_DEFAULT = 1_000_000


def _env_limit(name: str, default: int) -> int:
    """Entier >= 0 depuis l'environnement (0 = illimité) ; défaut si invalide."""
    try:
        v = os.environ.get(name)
        return max(0, int(v)) if v is not None and v.strip() else default
    except (TypeError, ValueError):
        return default

# Marqueurs de page reconnus par chunking._split_pages : ils sont NEUTRALISÉS
# dans le texte des pièces jointes (les « pages » d'une PJ n'ont aucun sens dans
# le conteneur, et fausseraient les citations « page N » — audit).
import re as _re
_PAGE_MARKERS = _re.compile(r"\f|---\s*(?:Page|Slide)\s+\d+\s*---")


def _append_pdf_attachments(doc, use_ocr, threshold, log, depth,
                            should_stop=None):
    """
    Extrait les pièces jointes embarquées du PDF (fichier dans un fichier) vers
    un dossier TEMPORAIRE local, range leur texte dans
    `doc.metadata["attachments"]` = [(nom, texte), ...], puis EFFACE les
    fichiers temporaires.

    Choix clé (demande utilisateur) : les chunks issus des pièces jointes
    portent le chemin du CONTENEUR et **aucun numéro de page** — la source
    citée, donc le lien cliquable, est le document de tête, sans saut de page
    (voir chunking.chunk_extracted). Les marqueurs de page du texte des PJ sont
    neutralisés pour ne jamais polluer la pagination.

    Les images sont exclues, les PJ trop volumineuses ignorées. L'imbrication
    (pdf dans pdf dans pdf) est traitée récursivement, bornée par
    `_ATTACH_MAX_DEPTH`. AUCUNE erreur ici ne doit invalider le texte du
    conteneur déjà extrait (audit) : tout est enveloppé.
    """
    if depth >= _ATTACH_MAX_DEPTH:
        log.debug(f"[extraction] Profondeur max de pièces jointes atteinte "
                  f"({os.path.basename(doc.path)}) — imbrication plus profonde ignorée.")
        return
    try:
        from pdf_processing import extract_attachments_from_pdf
    except Exception:
        return

    import tempfile
    import shutil

    max_mb = _env_limit("RIVAGE_ATTACH_MAX_MB", _ATTACH_MAX_MB_DEFAULT)
    max_text = _env_limit("RIVAGE_ATTACH_TEXT_MAX_CHARS",
                          _ATTACH_TEXT_MAX_CHARS_DEFAULT)
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="rivage_pj_")
        res = extract_attachments_from_pdf(doc.path, output_dir=tmp_dir)
        paths = (res or {}).get("attachments_paths") or []
        collected = []
        for att in paths:
            name = os.path.basename(att)
            ext = os.path.splitext(att)[1].lower()
            if ext in IMAGE_EXTS:
                continue  # images exclues (rien à indexer sans OCR dédié)
            if not is_supported(att):
                log.info(f"[extraction] Pièce jointe non indexable ignorée : {name}")
                continue
            if _format_disabled(ext):
                log.info(f"[extraction] Pièce jointe ignorée (format désactivé "
                         f"par configuration) : {name}")
                continue
            try:
                if max_mb and os.path.getsize(att) > max_mb * 1024 * 1024:
                    log.warning(f"[extraction] Pièce jointe trop volumineuse "
                                f"ignorée ({name}, "
                                f"{os.path.getsize(att) // (1024*1024)} Mo > "
                                f"RIVAGE_ATTACH_MAX_MB={max_mb}).")
                    continue
                # Récursif : une PJ PDF peut elle-même contenir des PJ. La famille
                # n'a pas d'importance (les chunks portent le chemin du conteneur).
                sub = extract_document(
                    att, corpus_root=None, use_ocr=use_ocr,
                    ocr_quality_threshold=threshold, log=log, _depth=depth + 1,
                    should_stop=should_stop,
                )
                # Une PJ dont l'OCR a échoué transitoirement rend le CONTENEUR
                # incomplet mais re-tentable : propager au document de tête.
                if sub.metadata.get("ocr_transient_failures"):
                    doc.metadata["ocr_transient_failures"] = (
                        int(doc.metadata.get("ocr_transient_failures", 0))
                        + int(sub.metadata["ocr_transient_failures"]))
                # Coût : les pages OCR payées pour la PJ comptent au conteneur.
                if sub.metadata.get("pages_ocr"):
                    doc.metadata["pages_ocr"] = (
                        int(doc.metadata.get("pages_ocr", 0))
                        + int(sub.metadata["pages_ocr"]))
            except Exception as e:
                log.warning(f"[extraction] Pièce jointe illisible ({name}): {e}")
                continue
            text = (sub.text or "").strip()
            # Plafond sur le TEXTE extrait (audit v3, configurable) : un petit
            # fichier très dense contournerait la limite de taille fichier.
            if max_text and len(text) > max_text:
                log.warning(f"[extraction] Texte de la pièce jointe {name} "
                            f"tronqué ({len(text):,} > "
                            f"RIVAGE_ATTACH_TEXT_MAX_CHARS={max_text:,} car.).")
                text = text[:max_text]
            # Les PJ imbriquées de la PJ remontent aussi (récursion) — leur
            # texte est déjà dans sub.metadata ; on les aplatit ici.
            for sub_name, sub_text in sub.metadata.get("attachments", []):
                if max_text and len(sub_text) > max_text:
                    sub_text = sub_text[:max_text]
                collected.append((f"{name} / {sub_name}",
                                  _PAGE_MARKERS.sub("\n", sub_text)))
            if text:
                collected.append((name, _PAGE_MARKERS.sub("\n", text)))
        if collected:
            doc.metadata["attachments"] = collected
            log.info(f"[extraction] {len(collected)} pièce(s) jointe(s) rattachée(s) "
                     f"au document de tête {os.path.basename(doc.path)} "
                     f"(sans numéro de page).")
    except Exception as e:
        # Un échec sur les PJ (mkdtemp, disque plein, PyMuPDF...) ne doit JAMAIS
        # faire perdre le texte du conteneur déjà extrait (audit).
        log.warning(f"[extraction] Pièces jointes ignorées "
                    f"({os.path.basename(doc.path)}): {e}")
    finally:
        # Fichiers temporaires effacés systématiquement, même en cas d'erreur.
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _extract_word(doc, log):
    from docx_processing import extract_text_from_docx
    doc.text = extract_text_from_docx(doc.path)
    doc.method = "docx"


def _extract_pptx(doc, use_ocr, log, should_stop=None):
    from pptx_processing import extract_text_from_pptx
    # Maîtrise du coût : RIVAGE_OCR_IMAGES=0 coupe l'OCR Vision des images de
    # slides (souvent décoratives) sans toucher à l'OCR des PDF scannés.
    ocr_imgs = use_ocr and _env_limit("RIVAGE_OCR_IMAGES", 1) != 0
    info: dict = {}
    doc.text = extract_text_from_pptx(doc.path, ocr_images=ocr_imgs, log=log,
                                      stats_out=info, should_stop=should_stop)
    # Nombre d'images du document : sert au RATTRAPAGE OCR (un PPTX à images
    # indexé OCR décoché est marqué ocr_pending — voir indexer).
    if info.get("image_count"):
        doc.metadata["pptx_image_count"] = int(info["image_count"])
    # Images en échec OCR TRANSITOIRE (passerelle saturée, clé refusée) : même
    # contrat que les PDF — document incomplet mais re-tentable, l'indexeur ne
    # l'enregistre pas (audit V2, B-7).
    transient = int(info.get("ocr_transient_failures", 0) or 0)
    if transient:
        doc.metadata["ocr_transient_failures"] = transient
    doc.method = "pptx"
    # Ne marquer ocr_used que si du texte d'image a réellement été extrait (m5) :
    # extract_text_from_pptx insère « [Image: ... ] » pour chaque image OCRisée.
    doc.ocr_used = use_ocr and "[Image:" in doc.text


def _extract_excel(doc, log):
    from excel_processing import extract_text_from_xlsx
    doc.text = extract_text_from_xlsx(doc.path, log=log)
    doc.method = "xlsx"


def _sniff_text_encoding(raw: bytes) -> Optional[str]:
    """Encodage déduit de la signature (BOM) ou de la structure des octets.

    UTF-16 (sortie par défaut de PowerShell 5 `>` / `Out-File`, Notepad
    « Unicode ») se décode « sans erreur » en CP1252 — en bouillie de NUL et
    de « ÿþ » indexée telle quelle (audit V2, B-10). On le reconnaît à son BOM,
    ou sans BOM à la proportion d'octets nuls (un texte 8 bits n'en a aucun).
    """
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"                      # le BOM dit le boutisme
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"                   # BOM UTF-8 retiré
    sample = raw[:4096]
    if sample and sample.count(b"\x00") >= 0.3 * len(sample):
        # Sans BOM : UTF-16 LE (Windows) si les NUL sont en position impaire.
        impairs = sample[1::2].count(b"\x00")
        pairs = sample[0::2].count(b"\x00")
        return "utf-16-le" if impairs >= pairs else "utf-16-be"
    return None


def _extract_text_file(doc, log):
    # Beaucoup de fichiers .txt/.md sous Windows sont en CP1252, pas en UTF-8.
    # `errors="ignore"` supprimerait silencieusement les accents (m6) : on tente
    # UTF-8, puis CP1252, avant de tomber en dernier recours sur un remplacement.
    # Avant cela, les signatures (BOM, UTF-16) sont reconnues explicitement.
    with open(doc.path, "rb") as f:
        raw = f.read()
    sniffed = _sniff_text_encoding(raw)
    candidats = ([sniffed] if sniffed else []) + ["utf-8", "cp1252"]
    for encoding in candidats:
        try:
            doc.text = raw.decode(encoding)
            doc.method = f"text:{encoding}"
            return
        except UnicodeDecodeError:
            continue
    doc.text = raw.decode("utf-8", errors="replace")
    doc.method = "text:replace"


# =============================================================================
#  FAMILLE / THÈME (chemin = signal, §10.2)
# =============================================================================

def _family_of(path: str, corpus_root: Optional[str]) -> str:
    """
    Détermine la « famille » d'un document : le grand répertoire de tête.

    Avec corpus_root, c'est le premier segment du chemin relatif
    (« CVC/rapports/x.pdf » -> « CVC »). Sans, c'est le répertoire parent.
    """
    if corpus_root:
        try:
            rel = os.path.relpath(path, corpus_root)
            segments = rel.replace("\\", "/").split("/")
            if len(segments) > 1:
                return segments[0]
        except ValueError:
            pass  # chemins sur volumes différents (Windows)
    return os.path.basename(os.path.dirname(path)) or "racine"


# =============================================================================
#  PARCOURS RÉCURSIF DU CORPUS (étape 1)
# =============================================================================

# Fréquence de consultation de `should_stop` pendant le parcours (E-12) : en
# entrées examinées (fichiers et répertoires) — assez espacée pour ne rien
# coûter, assez serrée pour qu'un arrêt soit honoré en quelques secondes.
_WALK_STOP_EVERY = 500


def walk_corpus(
    corpus_root: str,
    families: Optional[List[str]] = None,
    exclude_families: Optional[List[str]] = None,
    skip_hidden: bool = True,
    log=None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Iterator[str]:
    """
    Parcourt récursivement le corpus et énumère les fichiers supportés.

    Args:
        corpus_root: racine à parcourir
        families: si fourni, ne descend que dans ces grands répertoires de tête
                  (filtre par thème, §10.2)
        exclude_families: grands répertoires de tête à NE PAS parcourir (thèmes
                  exclus de l'indexation) — jamais extraits ni encodés.
        skip_hidden: ignore les fichiers/répertoires cachés et temporaires
        log: logger optionnel
        should_stop: callable optionnel ; True = arrêt demandé. Consulté tous
                  les `_WALK_STOP_EVERY` entrées (fichiers ou répertoires
                  examinés), pas à chaque entrée : le parcours d'un gros
                  corpus prend des minutes sur un partage et l'arrêt demandé
                  n'était pas honoré avant sa fin (audit V2, E-12). Le
                  parcours s'arrête alors, incomplet (l'appelant le sait par
                  son propre should_stop).

    Yields:
        Chemins absolus des fichiers dont l'extension est supportée.
    """
    _log = log or logger
    corpus_root = os.path.abspath(corpus_root)

    if not os.path.isdir(corpus_root):
        raise NotADirectoryError(f"Répertoire introuvable: {corpus_root}")

    family_set = set(families) if families else None
    # Comparaison via normcase (m8) : les répertoires Windows sont insensibles
    # à la casse ; sous POSIX normcase est l'identité (comparaison exacte).
    exclude_nc = ({os.path.normcase(str(t).rstrip("/\\")) for t in exclude_families}
                  if exclude_families else set())
    # Limite de taille optionnelle (gros fichiers) : RIVAGE_MAX_FILE_MB, 0 =
    # illimité (défaut). Un fichier au-delà est SAUTÉ avec un avertissement —
    # sans être enregistré : baisser/retirer la limite le reprend au run suivant.
    try:
        max_mb = int(os.environ.get("RIVAGE_MAX_FILE_MB", "0") or "0")
    except ValueError:
        max_mb = 0
    max_bytes = max_mb * 1024 * 1024
    skipped_size = 0
    count = 0
    if not excel_enabled():
        _log.info("[extraction] Tableurs Excel non parcourus "
                  "(RIVAGE_INDEX_EXCEL=0) — les Excel déjà indexés restent "
                  "consultables ; nouveaux/modifiés ignorés.")
    if not txt_enabled():
        _log.info("[extraction] Fichiers .txt non parcourus "
                  "(RIVAGE_INDEX_TXT=0) — les .txt déjà indexés restent "
                  "consultables ; nouveaux/modifiés ignorés.")

    # Élaguer d'emblée les thèmes exclus au 1er niveau
    if exclude_nc:
        try:
            for d in list(os.listdir(corpus_root)):
                if os.path.normcase(d) in exclude_nc:
                    _log.info(f"[extraction] Thème exclu de l'indexation : {d}")
        except OSError:
            pass

    entries = 0  # entrées examinées (fichiers + répertoires), pour should_stop

    def _stop_requested() -> bool:
        return should_stop is not None and should_stop()

    for dirpath, dirnames, filenames in os.walk(corpus_root):
        entries += 1
        if entries % _WALK_STOP_EVERY == 0 and _stop_requested():
            _log.info(f"[extraction] Parcours interrompu à la demande après "
                      f"{count} fichier(s) supporté(s).")
            return
        if skip_hidden:
            # Élaguer les répertoires cachés / temporaires in-place
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "~", "$"))]

        rel = os.path.relpath(dirpath, corpus_root).replace("\\", "/")
        top = rel.split("/")[0]

        # Thème exclu : ne pas descendre
        if exclude_nc:
            if rel == ".":
                dirnames[:] = [d for d in dirnames
                               if os.path.normcase(d) not in exclude_nc]
            elif os.path.normcase(top) in exclude_nc:
                dirnames[:] = []
                continue

        # Filtre par famille (grand répertoire de tête)
        if family_set is not None:
            if top not in family_set and rel != ".":
                dirnames[:] = []  # ne pas descendre plus bas
                continue

        for name in filenames:
            entries += 1
            if entries % _WALK_STOP_EVERY == 0 and _stop_requested():
                _log.info(f"[extraction] Parcours interrompu à la demande "
                          f"après {count} fichier(s) supporté(s).")
                return
            if skip_hidden and name.startswith((".", "~", "$")):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in SUPPORTED_EXTS:
                continue
            # Formats excluables par configuration, sans purge de l'existant
            # (RIVAGE_INDEX_EXCEL=0, RIVAGE_INDEX_TXT=0).
            if _format_disabled(ext):
                continue
            full = os.path.join(dirpath, name)
            if max_bytes:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                if size > max_bytes:
                    skipped_size += 1
                    _log.warning(
                        f"[extraction] Fichier sauté (> {max_mb} Mo, "
                        f"RIVAGE_MAX_FILE_MB) : {full} "
                        f"({size // (1024*1024)} Mo)")
                    continue
            count += 1
            yield full

    _log.info(f"[extraction] {count} fichier(s) supporté(s) trouvé(s) sous {corpus_root}"
              + (f" ({skipped_size} sauté(s) > {max_mb} Mo)" if skipped_size else ""))


def list_families(corpus_root: str) -> List[str]:
    """
    Liste les grands répertoires de tête (thèmes disponibles, §10.2).
    """
    corpus_root = os.path.abspath(corpus_root)
    if not os.path.isdir(corpus_root):
        return []
    return sorted(
        name for name in os.listdir(corpus_root)
        if os.path.isdir(os.path.join(corpus_root, name))
        and not name.startswith((".", "~", "$"))
    )


def excel_enabled() -> bool:
    """Vrai si les tableurs Excel (.xlsx/.xls/.xlsm) sont indexés.

    RIVAGE_INDEX_EXCEL=0 (case « Indexer les tableurs Excel » de l'indexeur)
    arrête d'en indexer de NOUVEAUX : ils ne sont plus parcourus (nouveaux et
    modifiés ignorés, pièces jointes Excel comprises). Les Excel DÉJÀ indexés
    RESTENT dans l'index et consultables — l'exclusion ne purge rien
    (volonté utilisateur ; seul `is_supported`, statique, pilote la purge).
    Réactiver reprend l'indexation, rattrapant les modifiés entre-temps.
    """
    return (os.environ.get("RIVAGE_INDEX_EXCEL", "1").strip() != "0")


def txt_enabled() -> bool:
    """Vrai si les fichiers .txt sont indexés (RIVAGE_INDEX_TXT, défaut 1).

    Même sémantique que les Excel, SANS case dans l'interface : à 0, plus
    aucun NOUVEAU .txt n'est indexé (parcours et pièces jointes), mais les
    .txt déjà indexés RESTENT consultables — rien n'est retiré. Les .md ne
    sont pas concernés.
    """
    return (os.environ.get("RIVAGE_INDEX_TXT", "1").strip() != "0")


def _format_disabled(ext: str) -> bool:
    """Formats excluables par CONFIGURATION, sans purge de l'existant.

    Utilisé au parcours du corpus et pour les pièces jointes — jamais par la
    purge (is_supported reste statique : désactiver n'enlève rien de l'index).
    """
    if ext in EXCEL_EXTS:
        return not excel_enabled()
    if ext == ".txt":
        return not txt_enabled()
    return False


def is_supported(path: str) -> bool:
    """True si l'extension du fichier est prise en charge.

    Volontairement STATIQUE (indépendant de RIVAGE_INDEX_EXCEL) : cette
    fonction sert à la purge « extension plus prise en charge » de l'indexeur
    — désactiver les Excel ne doit PAS retirer ceux déjà indexés.
    """
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTS


# =============================================================================
#  PRÉPARATION D'UN FICHIER (extraction + découpage + dédup), EXÉCUTABLE DANS
#  UN PROCESSUS DE TRAVAIL (audit perf 2.1, I-5)
# =============================================================================
#
# L'extraction (pdfplumber à 180 ms par page, OCR Vision, Word/PowerPoint)
# est la phase la plus longue de l'indexation et n'occupait qu'un cœur.
# `prepare_document` regroupe tout ce qui, pour un fichier, ne touche NI au
# registre, NI à l'index FAISS, NI aux segments de contenu — donc tout ce qui
# peut s'exécuter dans un autre processus — et rend un objet TRANSPORTABLE
# (picklable) : les fragments prêts à embedder plus les quelques indicateurs
# dont l'indexeur a besoin (erreur, caractère transitoire, pages OCR payées,
# besoin de rattrapage OCR…). Le texte intégral n'est pas transporté.
#
# Les fonctions `_worker_init` / `_worker_prepare` sont les points d'entrée
# du pool de processus de l'indexeur : définies AU NIVEAU DU MODULE (jamais
# de lambda ni de fermeture) pour être importables par un processus démarré
# en mode `spawn` (Windows, et choix uniforme de l'indexeur). Ce module est
# léger à importer ; les extracteurs lourds (PyMuPDF, pdfplumber, OCR…) ne
# sont chargés qu'au premier fichier qui en a besoin, dans chaque processus.

@dataclass
class PreparedDocument:
    """Résultat de `prepare_document` : fragments prêts + indicateurs."""
    path: str
    family: str
    ext: str
    method: str = "none"
    error: Optional[str] = None
    transient: bool = False         # erreur d'ACCÈS : à retenter (A-2)
    has_text: bool = False
    has_attachments: bool = False
    quality_score: Optional[float] = None
    pptx_image_count: int = 0
    pages_ocr: int = 0              # pages payées à l'API Vision
    ocr_transient_failures: int = 0  # pages OCR en échec re-tentable
    # Arrêt demandé constaté juste après l'extraction : texte possiblement
    # partiel (OCR interrompu page à page) — l'indexeur ne l'enregistre pas.
    interrupted: bool = False
    chunks: list = field(default_factory=list)   # List[chunking.Chunk]
    deduped: int = 0                # fragments écartés par la dédup intra-fichier

    @property
    def has_content(self) -> bool:
        return self.has_text or self.has_attachments


def prepare_document(path: str, corpus_root: Optional[str], use_ocr: bool,
                     ocr_quality_threshold: float, dedup_threshold: float,
                     log=None, should_stop: Optional[Callable[[], bool]] = None,
                     extract_fn=None) -> PreparedDocument:
    """
    Extraction → découpage → dédoublonnage INTRA-fichier (§4.2 : jamais entre
    fichiers, pour ne pas mal attribuer un fragment partagé), sans aucun effet
    sur le registre ni l'index. Les erreurs mémoire (MemoryError,
    std::bad_alloc) REMONTENT telles quelles : l'indexeur les traite comme un
    fichier pathologique à sauter (résilience mémoire), qu'elles viennent
    d'un processus de travail ou du processus principal.

    `extract_fn` : extracteur injectable (par défaut `extract_document`) — le
    processus principal passe le sien, un processus de travail celui de ce
    module.
    """
    _log = log or logger
    extract = extract_fn or extract_document
    doc = extract(path, corpus_root=corpus_root, use_ocr=use_ocr,
                  ocr_quality_threshold=ocr_quality_threshold, log=_log,
                  should_stop=should_stop)
    meta = doc.metadata or {}
    prep = PreparedDocument(
        path=doc.path, family=doc.family, ext=doc.ext, method=doc.method,
        error=doc.error, transient=bool(getattr(doc, "transient", False)),
        has_text=bool((doc.text or "").strip()),
        has_attachments=bool(meta.get("attachments")),
        quality_score=meta.get("quality_score"),
        pptx_image_count=int(meta.get("pptx_image_count", 0) or 0),
        pages_ocr=int(meta.get("pages_ocr", 0) or 0),
        ocr_transient_failures=int(meta.get("ocr_transient_failures", 0) or 0),
        interrupted=bool(should_stop is not None and should_stop()),
    )
    # Inutile de découper un document qui ne sera pas enregistré avec des
    # fragments (erreur, vide, OCR incomplet, arrêt demandé) : l'indexeur
    # décide de son sort sur les seuls indicateurs.
    if (prep.error or not prep.has_content or prep.ocr_transient_failures
            or prep.interrupted):
        return prep
    from chunking import chunk_extracted
    from dedup import make_deduper
    chunks = chunk_extracted(doc)
    deduper = make_deduper(threshold=dedup_threshold)
    kept = []
    for ch in chunks:
        if deduper.is_duplicate(ch.content):
            prep.deduped += 1
        else:
            kept.append(ch)
    prep.chunks = kept
    return prep


# État d'un processus de travail (initialisé par `_worker_init`).
_WORKER_STATE: Dict[str, Any] = {}

# Bibliothèques bavardes réduites au niveau WARNING dans chaque processus de
# travail (même liste que l'interface de l'indexeur) : leurs messages de
# diagnostic inonderaient sinon la file de journalisation inter-processus.
_NOISY_LOGGERS = ("pdfminer", "pdfplumber", "PIL", "fontTools", "fitz",
                  "httpx", "httpcore", "urllib3")


def _worker_init(log_queue, stop_event, log_level: int, log_name: str,
                 ocr_workers: int) -> None:
    """Initialisation d'un processus de travail du pool d'extraction (I-5).

    - journalisation : TOUT ce que le processus émet part dans `log_queue`
      (QueueHandler) ; le processus principal le rejoue dans ses propres
      loggers (QueueListener) — les messages des workers apparaissent donc
      dans le journal de l'indexeur comme s'ils venaient de lui ;
    - `stop_event` : événement partagé posé par le processus principal sur
      demande d'arrêt, consulté par la boucle OCR page à page (comme le
      `should_stop` historique) ;
    - `ocr_workers` : plafond de pages OCR en parallèle POUR CE PROCESSUS
      (RIVAGE_OCR_WORKERS réparti entre les processus, voir indexer), pour
      que la concurrence totale vers la passerelle Vision reste celle d'un
      indexeur à un seul processus.
    """
    import logging.handlers
    root = logging.getLogger()
    root.handlers[:] = [logging.handlers.QueueHandler(log_queue)]
    root.setLevel(log_level)
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if ocr_workers and ocr_workers > 0:
        os.environ["RIVAGE_OCR_WORKERS"] = str(int(ocr_workers))
    _WORKER_STATE["stop"] = stop_event
    _WORKER_STATE["log"] = log_name


def _worker_prepare(path: str, corpus_root: Optional[str], use_ocr: bool,
                    ocr_quality_threshold: float,
                    dedup_threshold: float) -> PreparedDocument:
    """Point d'entrée exécuté dans un processus de travail : prépare UN
    fichier (voir `prepare_document`). Importable (niveau module)."""
    stop_event = _WORKER_STATE.get("stop")
    should_stop = stop_event.is_set if stop_event is not None else None
    log = logging.getLogger(_WORKER_STATE.get("log") or __name__)
    return prepare_document(path, corpus_root, use_ocr, ocr_quality_threshold,
                            dedup_threshold, log=log, should_stop=should_stop)
