import gc
import io
import os
import re
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Callable, List, Dict, Any

from unidecode import unidecode

from pdfminer.high_level import extract_text
from pdfminer.layout import LAParams
from pdfminer.pdfdocument import PDFPasswordIncorrect
import pdfminer

# Suppression des warnings pdfminer (font encoding, etc.)
logging.getLogger('pdfminer').setLevel(logging.ERROR)

# Pour l'extraction via PyMuPDF
try:
    import pymupdf as fitz  # PyMuPDF / pymupdf
    logging.info("PyMuPDF importé avec succès")
except ImportError as e:
    fitz = None
    logging.error(f"Erreur lors de l'importation de PyMuPDF : {e}")

# Pour l'extraction améliorée des tableaux
try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
    logging.info("pdfplumber importé avec succès")
except ImportError:
    PDFPLUMBER_AVAILABLE = False
    logging.warning("pdfplumber non disponible - extraction des tableaux basique")

# -----------------------------------------------------------------------------
# Types
# -----------------------------------------------------------------------------

ProgressFn = Callable[[float, str], None]

# -----------------------------------------------------------------------------
# Logging de base
# -----------------------------------------------------------------------------

logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

# -----------------------------------------------------------------------------
# Utilitaires génériques
# -----------------------------------------------------------------------------

def clean_filename(filename: str) -> str:
    """
    Nettoie un nom de fichier pour le système de fichiers :
      - suppression des caractères surrogates (Unicode invalides)
      - translittération unicode -> ASCII
      - suppression/remplacement des caractères spéciaux
      - PRÉSERVE l'extension du fichier (.pdf, .docx, etc.)
    """
    # Séparer le nom de base et l'extension
    name_part, ext_part = os.path.splitext(filename)

    # Si pas de nom (juste extension), utiliser un nom par défaut
    if not name_part.strip():
        name_part = "attachment"

    # Supprimer les surrogates et caractères invalides du nom
    try:
        # Nettoyer les surrogates en les ignorant
        clean = name_part.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
        if not clean.strip():
            clean = "attachment"
    except Exception:
        clean = "attachment"

    # Translittération Unicode -> ASCII (seulement pour le nom)
    safe_name = unidecode(clean)
    safe_name = re.sub(r"[^\w\s-]", "_", safe_name)
    safe_name = re.sub(r"\s+", "_", safe_name)
    safe_name = re.sub(r"_{2,}", "_", safe_name)
    safe_name = safe_name.strip("_")

    # Nettoyer l'extension aussi (mais garder le point)
    if ext_part:
        # Nettoyer l'extension des surrogates
        try:
            clean_ext = ext_part.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
        except Exception:
            clean_ext = ext_part
        ext_part = clean_ext.lower()  # Extensions en minuscules

    # Recombiner nom + extension
    final_name = (safe_name or "attachment") + ext_part
    return final_name


# -----------------------------------------------------------------------------
# Fallback PyMuPDF pour l'extraction texte
# -----------------------------------------------------------------------------

def _extract_text_with_pymupdf(path: str, doc=None) -> str:
    """
    Fallback d'extraction texte via PyMuPDF si pdfminer échoue.

    `doc` : document PyMuPDF déjà ouvert par l'appelant (audit perf 2.1,
    I-9), non fermé ici ; sinon ouverture et fermeture locales.
    """
    if doc is None and fitz is None:
        raise RuntimeError("PyMuPDF non installé, impossible d'utiliser le fallback.")

    logging.info(f"[pdf_processing] Fallback PyMuPDF pour {path}")
    owned = doc is None
    if owned:
        doc = fitz.open(path)
    try:
        pages_text: List[str] = []
        for page in doc:
            try:
                page_text = page.get_text("text")
            except TypeError:
                # compat anciennes versions
                page_text = page.get_text()
            pages_text.append(page_text)
        # Form-feed entre pages (audit) : sans ce séparateur, le chunking ne
        # peut pas numéroter les pages et les citations « page N » sont perdues.
        return "\f".join(pages_text)
    finally:
        if owned:
            doc.close()


def _extract_with_pdfplumber(path: str) -> str:
    """
    Extraction PDF avec pdfplumber - meilleure gestion des tableaux.
    Extrait le texte normal ET formate les tableaux en markdown.
    Optimisé mémoire : libère chaque page après traitement.
    """
    if not PDFPLUMBER_AVAILABLE:
        raise RuntimeError("pdfplumber non disponible")

    logging.info(f"[pdf_processing] Extraction pdfplumber (tableaux) pour {path}")
    all_content: List[str] = []

    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for page_num in range(n_pages):
            page = pdf.pages[page_num]
            page_content: List[str] = []

            # Détecter les tableaux sur la page
            tables = page.extract_tables()

            if tables:
                # Page avec tableaux - extraire texte hors tableaux + tableaux formatés
                # Récupérer les bounding boxes des tableaux
                table_bboxes = []
                for table in page.find_tables():
                    table_bboxes.append(table.bbox)

                # Extraire le texte en excluant les zones de tableaux.
                # NB (audit) : pdfplumber passe des DICTS au filtre (obj["x0"]),
                # pas des objets à attributs — un hasattr(obj, 'x0') est toujours
                # faux et laissait TOUT passer : le contenu des tableaux était
                # dupliqué (texte brut + tableau markdown). _is_inside_bbox gère
                # les deux représentations.
                text_outside_tables = page.filter(
                    lambda obj: not any(
                        _is_inside_bbox(obj, bbox) for bbox in table_bboxes
                    )
                ).extract_text() or ""

                if text_outside_tables.strip():
                    page_content.append(text_outside_tables.strip())

                # Formater chaque tableau en markdown
                for idx, table in enumerate(tables):
                    if table and len(table) > 0:
                        formatted_table = _format_table_as_markdown(table)
                        if formatted_table:
                            page_content.append(f"\n[TABLEAU {idx + 1}]\n{formatted_table}")
            else:
                # Pas de tableau - extraction normale
                text = page.extract_text() or ""
                if text.strip():
                    page_content.append(text.strip())

            # UNE entrée par page, même vide (audit) : les pages sont jointes
            # par form-feed pour préserver la numérotation exacte — sauter les
            # pages vides décalerait toutes les citations « page N » suivantes.
            all_content.append("\n".join(page_content))

            # Libérer les caches internes de la page pour réduire la pression mémoire
            page.flush_cache()

    result = "\f".join(all_content)
    logging.info(f"[pdf_processing] pdfplumber: {len(result)} caractères extraits "
                 f"({n_pages} pages)")
    return result


def _obj_coord(obj, key, default=None):
    """Coordonnée d'un objet pdfplumber : dict (représentation réelle) ou attribut."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _is_inside_bbox(obj, bbox) -> bool:
    """Vérifie si un objet PDF est à l'intérieur d'une bounding box.
    Un objet sans coordonnées n'est jamais « dans » un tableau (conservé)."""
    x0 = _obj_coord(obj, "x0")
    top = _obj_coord(obj, "top")
    if x0 is None or top is None:
        return False
    bx0, btop, bx1, bbottom = bbox
    return (x0 >= bx0 and _obj_coord(obj, "x1", x0) <= bx1 and
            top >= btop and _obj_coord(obj, "bottom", top) <= bbottom)


def _format_table_as_markdown(table: List[List]) -> str:
    """
    Formate un tableau extrait en format markdown/texte lisible.
    """
    if not table or len(table) == 0:
        return ""

    # Nettoyer les cellules (remplacer None par "", nettoyer les espaces)
    cleaned_table = []
    for row in table:
        if row:
            cleaned_row = []
            for cell in row:
                if cell is None:
                    cleaned_row.append("")
                else:
                    # Nettoyer les retours à la ligne dans les cellules
                    cleaned_row.append(str(cell).replace("\n", " ").strip())
            cleaned_table.append(cleaned_row)

    if not cleaned_table:
        return ""

    # Calculer la largeur max de chaque colonne
    num_cols = max(len(row) for row in cleaned_table)
    col_widths = [0] * num_cols

    for row in cleaned_table:
        for i, cell in enumerate(row):
            if i < num_cols:
                col_widths[i] = max(col_widths[i], len(cell))

    # Formater en texte tabulé
    lines = []
    for row_idx, row in enumerate(cleaned_table):
        # Compléter les colonnes manquantes
        while len(row) < num_cols:
            row.append("")

        # Formater la ligne
        formatted_cells = []
        for i, cell in enumerate(row):
            formatted_cells.append(cell.ljust(col_widths[i]))
        # Remplissage de la dernière colonne retiré : des espaces de fin de
        # ligne n'alignent rien et gonflaient le texte indexé (audit V2, B-3).
        line = " | ".join(formatted_cells).rstrip()
        lines.append(line)

        # Ajouter une ligne de séparation après l'en-tête (première ligne)
        if row_idx == 0:
            separator = "-+-".join("-" * w for w in col_widths)
            lines.append(separator)

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Heuristiques de qualité du texte
# -----------------------------------------------------------------------------

# Seuil « texte court » PAR PAGE (audit V2, B-15) : un seuil fixe de 500
# caractères rendait suspect tout document court (une attestation d'une page,
# une fiche) et déclenchait les trois extracteurs pour rien ; à l'inverse,
# 500 caractères sur 40 pages n'étaient pas suspects.
SUSPECT_CHARS_PER_PAGE = 150


def _is_text_suspect(text: str, min_chars: int = 500,
                     min_ratio_printable: float = 0.85,
                     n_pages: Optional[int] = None) -> bool:
    """
    Détecte un texte "suspect" : trop court, rempli de caractères non imprimables, etc.
    Permet de décider si on tente un fallback (PyMuPDF) même si pdfminer n'a pas levé d'erreur.
    Avec `n_pages`, le seuil de longueur est proportionnel au nombre de pages
    (SUSPECT_CHARS_PER_PAGE × n_pages, audit V2, B-15) ; sinon `min_chars`.
    """
    if not text:
        return True

    if n_pages:
        min_chars = SUSPECT_CHARS_PER_PAGE * max(1, int(n_pages))

    # Trop peu de texte -> suspect
    if len(text) < min_chars:
        logging.warning(f"[pdf_processing] Texte très court ({len(text)} caractères "
                        f"pour un seuil de {min_chars}) — suspect.")
        return True

    # Ratio de caractères imprimables
    printable = sum(1 for c in text if c.isprintable() or c in "\n\r\t\f")
    total = len(text)
    if total == 0:
        return True
    ratio = printable / total
    if ratio < min_ratio_printable:
        logging.warning(
            f"[pdf_processing] Texte avec beaucoup de caractères non imprimables "
            f"(ratio imprimables={ratio:.2f}) — suspect."
        )
        return True

    return False


# -----------------------------------------------------------------------------
#  DÉTECTION QUALITÉ OCR / EXTRACTION
# -----------------------------------------------------------------------------

# Rendu tabulaire maison (voir _format_table_as_markdown) : marqueur
# « [TABLEAU n] », ligne séparatrice « -----+-----+----- » sous l'en-tête,
# cellules alignées par remplissage d'espaces et séparées par « | ».
_TABLE_MARKER_RE = re.compile(r"^\s*\[TABLEAU\s+\d+\]\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*-+(?:\s*\+\s*-+)+\s*$")
# Nombre minimal de cellules non vides pour qu'un tableau soit considéré
# « rempli » : en dessous, ce n'est pas une preuve d'extraction propre.
TABLE_MIN_CELLS = 4
# Score attribué à une page dont le contenu est UNIQUEMENT un tableau rempli,
# et plancher d'une page qui en contient un : pdfplumber n'a pu le construire
# qu'à partir de caractères réellement présents dans le PDF — c'est la preuve
# d'une extraction propre, pas d'un scan illisible.
_TABLE_PAGE_SCORE = 0.8


def _strip_table_rendering(text: str):
    """Sépare le texte hors tableau du rendu tabulaire.

    Retourne (texte_hors_tableau, nb_cellules_non_vides). Les marqueurs
    « [TABLEAU n] », les lignes séparatrices et les lignes de cellules
    (« a | b | c ») sont retirés du texte évalué ; les cellules non vides sont
    comptées. Les lignes « a | b » ne sont traitées comme des lignes de
    tableau que si le texte porte bien la signature du rendu maison (marqueur
    ou séparateur) : un « | » isolé dans un texte courant reste du texte.

    Audit V2, B-3 : les séparateurs déclenchaient « caractères répétés », le
    remplissage « espaces excessifs », les cellules numériques « peu de
    lettres / peu de mots » — une page tableau parfaitement extraite était
    notée 0,0 à 0,17 et partait en OCR Vision payant, dont la sortie
    remplaçait ensuite le texte propre.
    """
    if not text or "|" not in text:
        return text, 0
    lines = text.split("\n")
    signature = any(_TABLE_MARKER_RE.match(l) or _TABLE_SEPARATOR_RE.match(l)
                    for l in lines)
    if not signature:
        return text, 0
    kept: List[str] = []
    cells = 0
    for line in lines:
        if _TABLE_MARKER_RE.match(line) or _TABLE_SEPARATOR_RE.match(line):
            continue
        if "|" in line:
            cells += sum(1 for c in line.split("|") if c.strip())
            continue
        kept.append(line)
    return "\n".join(kept), cells


def assess_extraction_quality(text: str) -> Dict[str, Any]:
    """
    Évalue la qualité de l'extraction de texte d'un PDF.

    Détecte les problèmes courants:
    - OCR de mauvaise qualité
    - PDF image sans texte
    - Encodage corrompu
    - Texte bruité ou incohérent

    Note: Adapté pour les documents techniques (aéronautique, etc.) qui contiennent
    beaucoup de références numériques (P/N, S/N, ATA codes) sans pénaliser le score.

    Returns:
        Dict avec:
        - quality_score: 0.0 (mauvais) à 1.0 (excellent)
        - quality_level: "excellent", "good", "fair", "poor", "unusable"
        - issues: Liste des problèmes détectés
        - metrics: Métriques détaillées
        - recommendations: Suggestions d'amélioration
        - is_technical: True si document technique détecté
        - table_cells: cellules non vides du rendu tabulaire (0 si aucun
          tableau) — une page avec table_cells >= TABLE_MIN_CELLS a été
          extraite proprement et ne doit jamais partir en OCR (audit V2, B-3)
    """
    if not text:
        return {
            "quality_score": 0.0,
            "quality_level": "unusable",
            "issues": ["Aucun texte extrait"],
            "metrics": {},
            "recommendations": ["Vérifier si le PDF est une image scannée sans OCR"],
            "is_technical": False,
            "table_cells": 0,
        }

    # Audit V2, B-3 : la qualité se juge sur le texte HORS tableau ; le rendu
    # tabulaire (séparateurs, remplissage, cellules numériques) est exclu de
    # toutes les métriques et compté à part en cellules non vides.
    core_text, table_cells = _strip_table_rendering(text)
    has_table = table_cells >= TABLE_MIN_CELLS
    if has_table:
        if not core_text.strip():
            # Page constituée UNIQUEMENT d'un tableau rempli : extraction
            # propre par construction (pdfplumber a lu de vrais caractères).
            return {
                "quality_score": _TABLE_PAGE_SCORE,
                "quality_level": "good",
                "issues": [],
                "metrics": {"table_cells": table_cells, "raw_score": _TABLE_PAGE_SCORE},
                "recommendations": [f"Tableau extrait proprement ({table_cells} cellules)"],
                "total_chars": len(text),
                "word_count": 0,
                "is_technical": False,
                "table_cells": table_cells,
            }
        text = core_text

    issues = []
    recommendations = []
    metrics = {}
    metrics["table_cells"] = table_cells

    total_chars = len(text)

    # 0. Détection de document technique (aéronautique, etc.)
    # Patterns typiques: P/N, S/N, ATA, Rev, références numériques
    technical_patterns = [
        r'\bP/?N\b',                    # P/N ou PN (Part Number)
        r'\bS/?N\b',                    # S/N ou SN (Serial Number)
        r'\bATA\s*\d',                  # ATA codes (ATA 21, ATA 52-10-00)
        r'\b\d{2}-\d{2}-\d{2}\b',       # Format XX-XX-XX (ATA chapters)
        r'\bRev\.?\s*[A-Z0-9]',         # Rev A, Rev. 1
        r'\bFig\.?\s*\d',               # Fig 1, Figure 2
        r'\bRef\.?\s*\d',               # Ref 1, Reference
        r'\b[A-Z]{2,4}-?\d{3,}',        # Codes like AB-12345, ABC123
        r'\bEASA|FAA|TCDS|STC\b',       # Aviation authorities
        r'\bIPC|CMM|AMM|SRM\b',         # Manual types
    ]

    technical_matches = 0
    for pattern in technical_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        technical_matches += len(matches)

    # Document technique si > 5 patterns trouvés par 10000 caractères
    technical_density = (technical_matches / total_chars) * 10000 if total_chars > 0 else 0
    is_technical = technical_density > 5 or technical_matches > 20
    metrics["technical_indicators"] = technical_matches
    metrics["is_technical"] = is_technical

    # 1. Ratio de caractères imprimables
    printable_chars = sum(1 for c in text if c.isprintable() or c in "\n\r\t\f ")
    printable_ratio = printable_chars / total_chars if total_chars > 0 else 0
    metrics["printable_ratio"] = round(printable_ratio, 3)

    if printable_ratio < 0.85:
        issues.append(f"Beaucoup de caractères non imprimables ({(1-printable_ratio)*100:.1f}%)")
        recommendations.append("Possible corruption d'encodage - essayer avec un autre extracteur")

    # 2. Ratio de caractères alphabétiques ET alphanumériques
    alpha_chars = sum(1 for c in text if c.isalpha())
    alpha_ratio = alpha_chars / total_chars if total_chars > 0 else 0
    metrics["alpha_ratio"] = round(alpha_ratio, 3)

    # Pour documents techniques: ratio alphanumérique (lettres + chiffres)
    alnum_chars = sum(1 for c in text if c.isalnum())
    alnum_ratio = alnum_chars / total_chars if total_chars > 0 else 0
    metrics["alphanumeric_ratio"] = round(alnum_ratio, 3)

    # Avertissement seulement si ratio alphanumérique aussi bas (pas juste alpha)
    if alpha_ratio < 0.4 and alnum_ratio < 0.5:
        issues.append(f"Faible ratio de lettres ({alpha_ratio*100:.1f}%) - possible bruit OCR")
        recommendations.append("Le document pourrait être principalement graphique ou mal OCRisé")

    # 3. Détection de patterns OCR dégradé
    # Caractères typiques d'OCR raté: ﬁ, ﬂ, ligatures mal reconnues, répétitions
    ocr_noise_patterns = [
        (r'[ﬀﬁﬂﬃﬄ]{2,}', "Ligatures mal décodées"),
        (r'(.)\1{4,}', "Caractères répétés excessivement"),
        (r'[^\w\s.,;:!?\'\"()-]{5,}', "Séquences de symboles incohérentes"),
        (r'[A-Z]{20,}', "Très longues séquences majuscules"),
        (r'\b[bcdfghjklmnpqrstvwxz]{6,}\b', "Mots sans voyelles (OCR raté)"),
    ]

    ocr_issues_found = []
    for pattern, description in ocr_noise_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if len(matches) > 3:  # Seuil de tolérance
            ocr_issues_found.append(description)

    if ocr_issues_found:
        issues.extend(ocr_issues_found)
        metrics["ocr_noise_types"] = len(ocr_issues_found)
        recommendations.append("Qualité OCR dégradée - considérer un re-scan ou OCR amélioré")
    else:
        metrics["ocr_noise_types"] = 0

    # 4. Densité de mots reconnaissables
    words = re.findall(r'\b[a-zA-ZÀ-ÿ]{2,}\b', text)
    word_count = len(words)
    words_per_1000_chars = (word_count / total_chars) * 1000 if total_chars > 0 else 0
    metrics["words_per_1000_chars"] = round(words_per_1000_chars, 1)

    if words_per_1000_chars < 50:
        issues.append(f"Très peu de mots reconnaissables ({words_per_1000_chars:.0f}/1000 chars)")
        recommendations.append("Document probablement graphique ou extraction échouée")

    # 5. Détection de texte vertical/colonnes mal extraites
    short_lines = sum(1 for line in text.split('\n') if 0 < len(line.strip()) < 5)
    total_lines = max(len(text.split('\n')), 1)
    short_line_ratio = short_lines / total_lines
    metrics["short_line_ratio"] = round(short_line_ratio, 3)

    if short_line_ratio > 0.3:
        issues.append(f"Beaucoup de lignes très courtes ({short_line_ratio*100:.1f}%)")
        recommendations.append("Possible problème d'extraction de colonnes ou de mise en page")

    # 6. Cohérence des espaces
    multiple_spaces = len(re.findall(r' {3,}', text))
    space_issues = multiple_spaces / (total_chars / 1000) if total_chars > 1000 else multiple_spaces
    metrics["excessive_spaces_per_1000"] = round(space_issues, 1)

    if space_issues > 10:
        issues.append("Espaces excessifs détectés")
        recommendations.append("Mise en page complexe mal interprétée")

    # 7. Calcul du score global
    score = 1.0

    # Pénalités (adaptées pour documents techniques)
    if printable_ratio < 0.95:
        score -= (0.95 - printable_ratio) * 2

    # Pour documents techniques: utiliser ratio alphanumérique au lieu d'alphabétique
    # Les documents techniques contiennent beaucoup de chiffres (P/N, ATA, etc.)
    if is_technical:
        # Pénalité réduite basée sur le ratio alphanumérique
        if alnum_ratio < 0.5:
            score -= (0.5 - alnum_ratio) * 1.0  # Pénalité plus douce
    else:
        # Documents normaux: pénalité sur ratio alphabétique
        if alpha_ratio < 0.5:
            score -= (0.5 - alpha_ratio) * 1.5

    if metrics["ocr_noise_types"] > 0:
        score -= metrics["ocr_noise_types"] * 0.1

    if words_per_1000_chars < 100:
        score -= (100 - words_per_1000_chars) / 200

    # Pour documents techniques: tolérance sur les lignes courtes (tableaux fréquents)
    short_line_threshold = 0.4 if is_technical else 0.2
    if short_line_ratio > short_line_threshold:
        score -= (short_line_ratio - short_line_threshold) * 0.5

    # Page mixte (texte + tableau rempli) : le tableau est une preuve
    # d'extraction propre — le score ne descend pas sous _TABLE_PAGE_SCORE
    # quelle que soit la note du texte courant (audit V2, B-3).
    if has_table:
        score = max(score, _TABLE_PAGE_SCORE)

    # Normaliser entre 0 et 1
    score = max(0.0, min(1.0, score))
    metrics["raw_score"] = round(score, 3)

    # Déterminer le niveau de qualité
    if score >= 0.9:
        quality_level = "excellent"
    elif score >= 0.75:
        quality_level = "good"
    elif score >= 0.5:
        quality_level = "fair"
    elif score >= 0.25:
        quality_level = "poor"
    else:
        quality_level = "unusable"

    # Ajouter des recommandations générales si peu de problèmes
    if not recommendations:
        if quality_level in ("excellent", "good"):
            recommendations.append("Extraction de bonne qualité - aucune action requise")
        else:
            recommendations.append("Vérifier manuellement quelques pages du document")

    # Log si document technique détecté
    if is_technical and quality_level in ("excellent", "good", "fair"):
        recommendations.insert(0, f"Document technique détecté ({technical_matches} indicateurs)")

    return {
        "quality_score": round(score, 2),
        "quality_level": quality_level,
        "issues": issues,
        "metrics": metrics,
        "recommendations": recommendations,
        "total_chars": total_chars,
        "word_count": word_count,
        "is_technical": is_technical,
        "table_cells": table_cells,
    }


def log_extraction_quality(
    text: str,
    filename: str,
    log=None,
    warn_threshold: float = 0.6
) -> Dict[str, Any]:
    """
    Évalue et log la qualité d'extraction d'un PDF.

    Args:
        text: Texte extrait
        filename: Nom du fichier pour le logging
        log: Logger optionnel
        warn_threshold: Score en dessous duquel on émet un warning

    Returns:
        Résultat de assess_extraction_quality()
    """
    _log = log or logging

    quality = assess_extraction_quality(text)

    # Logging selon le niveau de qualité
    score = quality["quality_score"]
    level = quality["quality_level"]

    if score >= 0.9:
        _log.info(f"[QUALITY] ✅ {filename}: Excellent ({score:.0%})")
    elif score >= 0.75:
        _log.info(f"[QUALITY] ✓ {filename}: Good ({score:.0%})")
    elif score >= warn_threshold:
        _log.warning(f"[QUALITY] ⚠️ {filename}: Fair ({score:.0%}) - {', '.join(quality['issues'][:2])}")
    else:
        _log.warning(
            f"[QUALITY] ❌ {filename}: {level.upper()} ({score:.0%})\n"
            f"  Issues: {', '.join(quality['issues'])}\n"
            f"  Recommendations: {', '.join(quality['recommendations'])}"
        )

    return quality


def _split_pages(text: str) -> List[str]:
    """
    Sépare le texte en pages.
    pdfminer insère généralement un caractère form feed '\f' entre les pages.
    Si aucun form feed n'est trouvé, on considère qu'il n'y a qu'une page.
    """
    if "\f" in text:
        pages = text.split("\f")
    else:
        pages = [text]
    return [p for p in pages]


def _remove_repeated_headers_footers(
    pages: List[str],
    min_pages: int = 3,
    min_occ_ratio: float = 0.7
) -> List[str]:
    """
    Supprime les entêtes / pieds de page répétés sur la majorité des pages.
    """
    if len(pages) < min_pages:
        return pages

    first_lines: List[str] = []
    last_lines: List[str] = []

    for p in pages:
        lines = [l.rstrip() for l in p.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        lines = [l for l in lines if l.strip() != ""]
        if not lines:
            first_lines.append("")
            last_lines.append("")
            continue
        first_lines.append(lines[0].strip())
        last_lines.append(lines[-1].strip())

    def _find_repeated(candidates: List[str]) -> List[str]:
        freq: Dict[str, int] = {}
        for l in candidates:
            if not l:
                continue
            freq[l] = freq.get(l, 0) + 1

        res: List[str] = []
        n_pages = len(pages)
        for line, count in freq.items():
            if count >= int(min_occ_ratio * n_pages):
                if len(line) > 2:
                    res.append(line)
        return res

    repeated_first = _find_repeated(first_lines)
    repeated_last = _find_repeated(last_lines)

    if repeated_first:
        logging.info(f"[pdf_processing] Headers répétés détectés et supprimés : {repeated_first}")
    if repeated_last:
        logging.info(f"[pdf_processing] Footers répétés détectés et supprimés : {repeated_last}")

    new_pages: List[str] = []

    for p in pages:
        lines = p.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # Suppression alignée sur la DÉTECTION (audit v2) : la détection lit la
        # première/dernière ligne NON VIDE — supprimer seulement idx==0 laissait
        # l'en-tête en place dès qu'une ligne blanche le précédait (fréquent
        # avec pdfminer).
        first_ne = next((i for i, l in enumerate(lines) if l.strip()), None)
        last_ne = next((i for i in range(len(lines) - 1, -1, -1)
                        if lines[i].strip()), None)
        cleaned: List[str] = []
        for idx, raw in enumerate(lines):
            line = raw.strip()
            if idx == first_ne and line in repeated_first:
                continue
            if idx == last_ne and line in repeated_last:
                continue
            cleaned.append(raw)
        new_pages.append("\n".join(cleaned))

    return new_pages

# -----------------------------------------------------------------------------
# Nettoyage du bruit / watermarks PDF récurrents
# -----------------------------------------------------------------------------

NOISE_PATTERNS = [
    r"Country of Export: US EAR 9E991",
    r"Extraterritorial Reach: US EAR 9E991",
    r"Use of this Document Subject to Restrictions on Title Page",
    r"SFT1005/09/14",
    r"CAGE 55820",
    r"FROZEN\s+docum?ent issued from Dassault Aviation repository",
    r"FROZEN\s+document issued from Dassault Aviation repository",
]

NOISE_REGEXES = [re.compile(p, re.IGNORECASE) for p in NOISE_PATTERNS]


def _clean_pdf_text_noise(text: str) -> str:
    """
    Supprime quelques lignes de bruit très répétitives (watermarks, mentions
    export) et certaines lignes ultra-courtes typiques de texte vertical bruité.

    Les form-feeds `\\f` (séparateurs de pages) sont PRÉSERVÉS (audit) : le
    nettoyage opère page par page — une ligne supprimée ou vidée en tête de
    page emportait son `\\f`, décalant toutes les citations « page N ».
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\f".join(_clean_page_noise(p) for p in text.split("\f"))


# Sigles courts qui ne sont JAMAIS du bruit (audit V2, B-11) : la règle qui
# supprimait toute ligne de 1 à 3 majuscules/chiffres effaçait « ATA »,
# « AMM », « ISO », « 1 », « 2 », « A » — références et numéros de liste des
# documents techniques. Liste blanche consultée avant toute suppression.
_NOISE_WHITELIST = frozenset({
    "ATA", "AMM", "CMM", "IPC", "SRM", "SB", "AD", "TR", "SIL", "QTY", "FIG",
    "REV", "P/N", "PN", "S/N", "SN", "N/A", "NA", "ISO", "EN", "DIN", "MIL",
    "STD", "REF", "NO", "N°", "OK", "KO", "ID", "KG", "MM", "CM", "KM", "HZ",
    "KW", "MW", "V", "A", "W", "K", "M", "I", "X", "Y", "Z",
})


def _clean_page_noise(page_text: str) -> str:
    """Nettoie une page : watermarks connus, et lignes d'UN caractère
    (majuscule, chiffre ou tiret) ISOLÉES entre deux lignes vides — résidus
    de texte vertical ou de colonnes bruitées. Une ligne courte collée à du
    texte (numéro de liste « 1 », puce « A », sigle) est conservée, de même
    que tout sigle de la liste blanche (audit V2, B-11 : « ATA », « AMM »,
    « ISO », « 1 », « 2 » disparaissaient)."""
    lines = page_text.split("\n")
    stripped = [l.strip() for l in lines]
    cleaned_lines: List[str] = []

    for i, raw_line in enumerate(lines):
        line = stripped[i]

        if not line:
            cleaned_lines.append("")
            continue

        # Watermarks / disclaimers connus
        if any(rx.search(line) for rx in NOISE_REGEXES):
            continue

        # Caractère isolé entre deux lignes vides (ou bords de page), hors
        # liste blanche : bruit de colonne verticale.
        if (len(line) == 1 and re.fullmatch(r"[A-Z0-9\-]", line)
                and line.upper() not in _NOISE_WHITELIST
                and (i == 0 or not stripped[i - 1])
                and (i == len(lines) - 1 or not stripped[i + 1])):
            continue

        cleaned_lines.append(raw_line)

    return "\n".join(cleaned_lines).strip()

# -----------------------------------------------------------------------------
#  EXTRACTION TEXTE PDF (robuste, utilisée partout)
# -----------------------------------------------------------------------------

def _env_int_pdf(name: str, default: int) -> int:
    """Entier depuis l'environnement (0 = désactivé), défaut si invalide."""
    try:
        v = os.environ.get(name)
        return int(v) if v is not None and v.strip() else default
    except (TypeError, ValueError):
        return default


def extract_text_from_pdf(
    path: str,
    progress_cb: Optional[ProgressFn] = None,
    doc=None,
) -> str:
    """
    Extraction texte robuste depuis un PDF en conservant les sauts de ligne.

    Stratégie :
      1) tentative avec pdfplumber (meilleure gestion des tableaux)
      2) si erreur → fallback pdfminer
      3) si texte suspect → fallback PyMuPDF et choix du meilleur
      4) nettoyage des headers/footers répétés
      5) nettoyage léger du bruit (watermarks récurrents, texte vertical très bruité)

    `doc` : document PyMuPDF déjà ouvert par l'appelant (audit perf 2.1,
    I-9 — smart_ocr_pdf), utilisé pour le comptage de pages et le chemin
    PyMuPDF au lieu de rouvrir le fichier ; jamais fermé ici. Les ouvertures
    de pdfplumber et pdfminer, qui ont leur propre analyseur, demeurent.
    """
    if progress_cb:
        progress_cb(0.05, f"Ouverture PDF : {os.path.basename(path)}")

    text_final: str = ""
    extraction_method: str = ""

    # --- Très gros PDF : chemin LÉGER (gros fichiers, audit) -----------------
    # pdfplumber (détection de tableaux page à page) et pdfminer (parsing
    # Python du document entier) consomment une mémoire énorme sur un PDF de
    # plusieurs centaines de Mo / milliers de pages — jusqu'au plantage.
    # Au-delà des seuils, on extrait directement via PyMuPDF (C, streaming
    # page à page, mémoire quasi constante), en conservant les \f.
    light_pages = _env_int_pdf("RIVAGE_PDF_LIGHT_PAGES", 300)
    light_mb = _env_int_pdf("RIVAGE_PDF_LIGHT_MB", 150)
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
    except OSError:
        size_mb = 0
    n_pages_quick = 0
    if doc is not None:
        try:
            n_pages_quick = doc.page_count
        except Exception:
            n_pages_quick = 0
    elif fitz is not None and (light_pages or light_mb):
        try:
            _d = fitz.open(path)
            n_pages_quick = _d.page_count
            _d.close()
        except Exception:
            n_pages_quick = 0
    if ((light_pages and n_pages_quick > light_pages)
            or (light_mb and size_mb > light_mb)):
        logging.info(
            f"[pdf_processing] PDF volumineux ({n_pages_quick} pages, "
            f"{size_mb:.0f} Mo) : extraction légère PyMuPDF directe "
            f"(tableaux non formatés — seuils RIVAGE_PDF_LIGHT_PAGES/"
            f"RIVAGE_PDF_LIGHT_MB).")
        try:
            text_final = _extract_text_with_pymupdf(path, doc=doc)
            extraction_method = "pymupdf_light"
        except Exception as e:
            logging.warning(f"[pdf_processing] Chemin léger échoué ({e}) — "
                            f"cascade classique.")
            text_final = ""

    # 1) Tentative pdfplumber (meilleure gestion des tableaux)
    if not text_final and PDFPLUMBER_AVAILABLE:
        try:
            text_final = _extract_with_pdfplumber(path)
            extraction_method = "pdfplumber"
            logging.info(f"[pdf_processing] Extraction pdfplumber OK pour {path}")
        except Exception as e:
            logging.warning(f"[pdf_processing] Erreur pdfplumber sur {path}: {e}")
            text_final = ""

    # Nombre de pages pour le seuil « suspect » proportionnel (audit V2,
    # B-15) : compté par PyMuPDF ci-dessus, sinon par les `\f` du texte (les
    # trois extracteurs en émettent un par page).
    def _n_pages_pour(texte: str) -> int:
        return n_pages_quick or (texte.count("\f") + 1 if texte else 1)

    # Remplacement d'un extracteur par le suivant SUR LE SCORE de qualité,
    # jamais sur la seule longueur (audit V2, B-15) : pdfminer, plus bavard,
    # remplaçait pdfplumber et son rendu tabulaire dès qu'il produisait plus
    # de caractères.
    def _meilleur(candidat: str, actuel: str) -> bool:
        if not candidat or not candidat.strip():
            return False
        if not actuel or not actuel.strip():
            return True
        try:
            s_cand = assess_extraction_quality(candidat).get("quality_score", 0.0)
            s_act = assess_extraction_quality(actuel).get("quality_score", 0.0)
        except Exception:
            return False
        return s_cand > s_act

    # 2) Fallback pdfminer si pdfplumber a échoué, non utilisé ou non disponible.
    #    JAMAIS sur le chemin léger (pdfminer sur un PDF géant = le plantage
    #    mémoire que le chemin léger évite ; un texte pauvre y est normal — PDF
    #    scanné — et sera routé vers l'OCR par l'évaluation de qualité).
    if extraction_method != "pymupdf_light" and (
            not text_final
            or _is_text_suspect(text_final, n_pages=_n_pages_pour(text_final))):
        try:
            laparams = LAParams()
            with open(path, "rb") as f:
                text_pdfminer = extract_text(f, laparams=laparams)

            if _meilleur(text_pdfminer, text_final):
                text_final = text_pdfminer
                extraction_method = "pdfminer"
                logging.info(f"[pdf_processing] Extraction pdfminer retenue "
                             f"(score meilleur) pour {path}")
        except PDFPasswordIncorrect:
            logging.error(f"[pdf_processing] PDF protégé par mot de passe : {path}")
            raise
        except Exception as e:
            logging.warning(f"[pdf_processing] Erreur pdfminer sur {path}: {e}")

    # Libérer la mémoire des extracteurs précédents avant un éventuel 3e essai
    gc.collect()

    # 3) Fallback PyMuPDF si texte toujours suspect ou vide (inutile si le
    #    chemin léger — déjà PyMuPDF — a été pris)
    if extraction_method != "pymupdf_light" and (
            not text_final
            or _is_text_suspect(text_final, n_pages=_n_pages_pour(text_final))):
        try:
            text_pymupdf = _extract_text_with_pymupdf(path, doc=doc)

            if _meilleur(text_pymupdf, text_final):
                logging.info(
                    f"[pdf_processing] Texte PyMuPDF choisi (score meilleur, "
                    f"len={len(text_pymupdf)}) vs {extraction_method} "
                    f"(len={len(text_final or '')})."
                )
                text_final = text_pymupdf
                extraction_method = "pymupdf"
            elif not text_final:
                text_final = text_pymupdf
                extraction_method = "pymupdf"
        except Exception as e2:
            logging.error(f"[pdf_processing] Fallback PyMuPDF échoué pour {path}: {e2}")
            if not text_final:
                raise

    # Normalisation minimale des fins de ligne
    text_final = text_final.replace("\r\n", "\n").replace("\r", "\n")

    # 4) Nettoyage headers/footers répétés page par page
    try:
        pages = _split_pages(text_final)
        pages_clean = _remove_repeated_headers_footers(pages)
        text_final = "\f".join(pages_clean)
    except Exception as e:
        logging.warning(f"[pdf_processing] Erreur durant la suppression headers/footers: {e}")

    # 5) Nettoyage du bruit / watermarks
    try:
        text_final = _clean_pdf_text_noise(text_final)
    except Exception as e:
        logging.warning(f"[pdf_processing] Erreur durant le nettoyage du texte: {e}")
        text_final = text_final.strip()

    if progress_cb:
        method_info = f" (via {extraction_method})" if extraction_method else ""
        progress_cb(1.0, f"Lecture PDF terminée{method_info}")

    return text_final

# -----------------------------------------------------------------------------
#  EXTRACTION DES PIECES JOINTES PDF (logique inspirée de test.py)
# -----------------------------------------------------------------------------

def _unique_path(base_dir: str, filename: str) -> str:
    """Retourne un chemin dans base_dir qui n'existe pas encore (suffixe _1, _2, ...)."""
    filename = os.path.basename(filename) or "attachment"
    root, ext = os.path.splitext(filename)
    candidate = os.path.join(base_dir, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(base_dir, f"{root}_{i}{ext}")
        i += 1
    return candidate


def _extract_embedded_files(doc, out_dir: str) -> List[str]:
    """
    Extrait les fichiers intégrés (/EmbeddedFiles) du PDF.
    Retourne la liste des chemins de fichiers extraits.
    """
    extracted: List[str] = []

    try:
        # Nouvelle API (PyMuPDF récent)
        if hasattr(doc, "embfile_names"):
            names = list(doc.embfile_names())
            if names:
                logging.info(f"[pdf_processing] Found {len(names)} embedded file(s) via embfile_names().")
            for name in names:
                try:
                    data = doc.embfile_get(name)

                    filename = None
                    if hasattr(doc, "embfile_info"):
                        try:
                            info = doc.embfile_info(name)
                            if isinstance(info, dict):
                                filename = (
                                    info.get("filename")
                                    or info.get("name")
                                    or None
                                )
                        except Exception as e_info:
                            logging.warning(
                                f"[pdf_processing] embfile_info() failed for '{name}' "
                                f"({type(e_info).__name__}: {e_info}). Using raw name."
                            )

                    if not filename:
                        filename = name or "embedded_file"

                    # Nettoyer le nom de fichier (surrogates, caractères invalides)
                    filename = clean_filename(filename)
                    out_path = _unique_path(out_dir, filename)
                    with open(out_path, "wb") as f:
                        f.write(data)
                    extracted.append(out_path)
                    logging.info(f"[pdf_processing] Extracted embedded file: {out_path}")

                except Exception as e:
                    logging.warning(
                        f"[pdf_processing] Failed to extract embedded file '{name}': {e}"
                    )
                    traceback.print_exc()

        # Ancienne API (fallback)
        elif hasattr(doc, "embeddedFileNames"):
            names = list(doc.embeddedFileNames())
            if names:
                logging.info(
                    f"[pdf_processing] Found {len(names)} embedded file(s) via embeddedFileNames()."
                )
            for name in names:
                try:
                    data = doc.embeddedFileGet(name)

                    filename = None
                    if hasattr(doc, "embeddedFileInfo"):
                        try:
                            info = doc.embeddedFileInfo(name)
                            if isinstance(info, dict):
                                filename = (
                                    info.get("filename")
                                    or info.get("name")
                                    or None
                                )
                        except Exception as e_info:
                            logging.warning(
                                f"[pdf_processing] embeddedFileInfo() failed for '{name}' "
                                f"({type(e_info).__name__}: {e_info}). Using raw name."
                            )

                    if not filename:
                        filename = name or "embedded_file"

                    # Nettoyer le nom de fichier (surrogates, caractères invalides)
                    filename = clean_filename(filename)
                    out_path = _unique_path(out_dir, filename)
                    with open(out_path, "wb") as f:
                        f.write(data)
                    extracted.append(out_path)
                    logging.info(f"[pdf_processing] Extracted embedded file (old API): {out_path}")

                except Exception as e:
                    logging.warning(
                        f"[pdf_processing] Failed to extract embedded file '{name}' (old API): {e}"
                    )
                    traceback.print_exc()
        else:
            logging.info(
                "[pdf_processing] This PyMuPDF version exposes no embedded-file API on Document."
            )

    except Exception as e:
        logging.error(f"[pdf_processing] Error while extracting embedded files: {e}")
        traceback.print_exc()

    return extracted


def _extract_attachment_annotations(doc, out_dir: str) -> List[str]:
    """
    Extrait les pièces jointes posées en ANNOTATION FileAttachment (trombone /
    commentaire Acrobat) — complément des /EmbeddedFiles (audit v3).
    Chaque annotation est tentée individuellement : un échec n'interrompt pas
    les autres.
    """
    extracted: List[str] = []
    try:
        for page in doc:
            try:
                annots = page.annots()
            except Exception:
                continue
            if not annots:
                continue
            for annot in annots:
                try:
                    # Type 17 = FileAttachment (PyMuPDF: PDF_ANNOT_FILE_ATTACHMENT)
                    if annot.type[0] != 17:
                        continue
                    data = annot.get_file()
                    if not data:
                        continue
                    info = annot.info or {}
                    filename = clean_filename(
                        info.get("filename") or info.get("content")
                        or info.get("name") or "annotation_pj")
                    out_path = _unique_path(out_dir, filename)
                    with open(out_path, "wb") as f:
                        f.write(data)
                    extracted.append(out_path)
                    logging.info(f"[pdf_processing] Annotation FileAttachment "
                                 f"extraite: {out_path}")
                except Exception as e:
                    logging.warning(f"[pdf_processing] Annotation PJ ignorée "
                                    f"(page {page.number + 1}): {e}")
    except Exception as e:
        logging.warning(f"[pdf_processing] Parcours des annotations PJ échoué: {e}")
    if extracted:
        logging.info(f"[pdf_processing] {len(extracted)} annotation(s) "
                     f"FileAttachment extraite(s).")
    return extracted


def _create_pdf_without_attachments(doc, out_path: str) -> None:
    """
    Crée un nouveau PDF sans pièces jointes.
    (On recopie les pages dans un nouveau document.)
    """
    new_doc = None
    try:
        new_doc = fitz.open()
        for page in doc:
            new_doc.insert_pdf(doc, from_page=page.number, to_page=page.number)
        new_doc.save(out_path)
        logging.info(f"[pdf_processing] Created PDF without attachments: {out_path}")
    except Exception as e:
        logging.error(
            f"[pdf_processing] Failed to create PDF without attachments: {e}"
        )
        traceback.print_exc()
    finally:
        if new_doc:
            new_doc.close()


def extract_attachments_from_pdf(
    pdf_path: str,
    output_dir: Optional[str] = None,
    create_clean_pdf: bool = False,
    doc=None,
) -> Dict[str, Any]:
    """
    Extrait les pièces jointes (Embedded Files + annotations FileAttachment)
    et, si `create_clean_pdf`, crée un PDF sans pièces jointes dans output_dir.

    `create_clean_pdf=False` par défaut (audit) : la copie page à page du PDF
    complet est coûteuse et l'indexation n'en a pas besoin — elle n'utilise que
    `attachments_paths`. La copie n'est de toute façon créée que s'il existe au
    moins une pièce jointe.

    `doc` : document PyMuPDF déjà ouvert par l'appelant (audit perf 2.1,
    I-9), non fermé ici ; sinon ouverture et fermeture locales. L'appel de
    l'indexeur (extraction._append_pdf_attachments) suit l'extraction du
    texte, dans un autre module : il ouvre encore le sien.

    Retourne un dict :
    {
        "attachments_paths": [ ... chemins des PJ ... ],
        "pdf_without_attachments_path": "/.../monPDF_sansPJ.pdf" ou None,
        "temp_dir": "/tmp/pdf_attachments_xyz" ou None (si créé automatiquement)
    }

    Si output_dir est None, un répertoire temporaire est créé et son chemin
    est retourné dans "temp_dir" pour permettre le nettoyage par l'appelant.
    """
    if fitz is None:
        logging.error("[pdf_processing] PyMuPDF (pymupdf) n'est pas installé.")
        return {
            "attachments_paths": [],
            "pdf_without_attachments_path": None,
            "temp_dir": None
        }

    pdf_path = os.path.abspath(pdf_path)

    if not os.path.exists(pdf_path):
        logging.error(f"[pdf_processing] PDF not found: {pdf_path}")
        return {
            "attachments_paths": [],
            "pdf_without_attachments_path": None,
            "temp_dir": None
        }

    # Garder trace si on a créé le répertoire temporaire
    created_temp_dir = None
    if output_dir is None:
        import tempfile
        output_dir = tempfile.mkdtemp(prefix="pdf_attachments_")
        created_temp_dir = output_dir
        logging.info(f"[pdf_processing] Répertoire temporaire créé : {output_dir}")

    os.makedirs(output_dir, exist_ok=True)

    logging.info(f"[pdf_processing] Target PDF for attachment extraction: {pdf_path}")
    logging.info(f"[pdf_processing] Attachments will be extracted into: {output_dir}")

    owned = doc is None
    if owned:
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            logging.error(f"[pdf_processing] Cannot open PDF with PyMuPDF: {e}")
            traceback.print_exc()
            return {"attachments_paths": [], "pdf_without_attachments_path": None, "temp_dir": created_temp_dir}

    attachments_paths: List[str] = []
    pdf_without_path: Optional[str] = None

    try:
        # 1) Extraction des fichiers embarqués (/EmbeddedFiles)
        attachments_paths.extend(_extract_embedded_files(doc, output_dir))

        # 1b) Annotations FileAttachment (pièce jointe « commentaire » Acrobat,
        #     audit v3 : elles étaient silencieusement ignorées alors que le
        #     docstring les annonçait).
        attachments_paths.extend(_extract_attachment_annotations(doc, output_dir))

        # 2) Copie « sans pièces jointes » : seulement sur demande ET s'il y a
        #    réellement des PJ (audit : recopier page à page chaque PDF du
        #    corpus à chaque indexation coûtait cher pour rien).
        if create_clean_pdf and attachments_paths:
            pdf_basename = os.path.basename(pdf_path)
            base_root, base_ext = os.path.splitext(pdf_basename)
            pdf_without_path = os.path.join(output_dir, f"{base_root}_sans_pj{base_ext}")
            _create_pdf_without_attachments(doc, pdf_without_path)

    finally:
        if owned:
            doc.close()

    if not attachments_paths:
        logging.info(
            "[pdf_processing] No embedded files found in PDF."
        )

    return {
        "attachments_paths": attachments_paths,
        "pdf_without_attachments_path": pdf_without_path,
        "temp_dir": created_temp_dir,  # Retourner le chemin du répertoire temporaire créé
    }

# -----------------------------------------------------------------------------
# ORCHESTRATEUR GLOBAL
# -----------------------------------------------------------------------------

def extract_all_texts_from_pdf(
    pdf_path: str,
    output_dir: Optional[str] = None,
    progress_cb: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    """
    Pipeline complet :
      1) Extraction robuste du texte du PDF principal
      2) Extraction des pièces jointes (Embedded + FileAttachment)
      3) Création d'un PDF sans pièces jointes + extraction texte robuste
      4) Extraction du texte des pièces jointes qui sont des PDF

    Retourne un dict :
    {
        "main_pdf_text": <str>,
        "main_pdf_no_attachments_text": <str ou None>,
        "attachments": [
            {
                "name": <str>,
                "path": <str>,
                "size": <int>,
                "is_pdf": <bool>,
                "text": <str ou None>,
            },
            ...
        ],
    }
    """
    pdf_path = os.path.abspath(pdf_path)
    if output_dir is None:
        import tempfile
        output_dir = tempfile.mkdtemp(prefix="pdf_all_texts_")
        logging.info(f"[pdf_processing] Répertoire temporaire créé : {output_dir}")

    os.makedirs(output_dir, exist_ok=True)

    def _sub_progress(start: float, end: float) -> ProgressFn:
        """Fabrique un wrapper de progress_cb pour une sous-étape."""
        def wrapper(ratio: float, message: str) -> None:
            if progress_cb:
                overall = start + (end - start) * max(0.0, min(1.0, ratio))
                progress_cb(overall, message)
        return wrapper

    # 1) Texte du PDF principal
    if progress_cb:
        progress_cb(0.0, "Début : extraction texte PDF principal")
    main_text = extract_text_from_pdf(
        pdf_path,
        progress_cb=_sub_progress(0.0, 0.4) if progress_cb else None,
    )

    # 2) Extraction pièces jointes + PDF sans PJ
    if progress_cb:
        progress_cb(0.4, "Extraction des pièces jointes et création du PDF sans pièces jointes")
    # Seul demandeur légitime de la copie « sans pièces jointes » (audit v2 :
    # create_clean_pdf est désormais opt-in — l'indexation n'en a pas besoin).
    attachments_info = extract_attachments_from_pdf(
        pdf_path, output_dir=output_dir, create_clean_pdf=True)
    attachments_paths = attachments_info.get("attachments_paths", [])
    pdf_without_path = attachments_info.get("pdf_without_attachments_path")

    # 3) Texte du PDF sans pièces jointes
    main_no_pj_text: Optional[str] = None
    if pdf_without_path and os.path.exists(pdf_without_path):
        if progress_cb:
            progress_cb(0.6, "Extraction texte du PDF sans pièces jointes")
        main_no_pj_text = extract_text_from_pdf(
            pdf_without_path,
            progress_cb=_sub_progress(0.6, 0.8) if progress_cb else None,
        )

    # 4) Texte des pièces jointes PDF (PARALLÉLISÉ)
    attachments_result: List[Dict[str, Any]] = []
    if progress_cb:
        progress_cb(0.8, "Extraction texte des pièces jointes PDF")

    # Identifier les PDFs à traiter en parallèle
    pdf_attachments = []
    non_pdf_attachments = []

    for path in attachments_paths:
        try:
            name = os.path.basename(path)
            size = os.path.getsize(path)
        except OSError:
            name = os.path.basename(path)
            size = -1

        is_pdf = name.lower().endswith(".pdf")
        att_info = {"name": name, "path": path, "size": size, "is_pdf": is_pdf, "text": None}

        if is_pdf:
            pdf_attachments.append(att_info)
        else:
            non_pdf_attachments.append(att_info)

    # Fonction worker pour extraction parallèle
    def _extract_attachment_text(att_info: Dict[str, Any]) -> Dict[str, Any]:
        try:
            att_info["text"] = extract_text_from_pdf(att_info["path"])
        except Exception as e:
            logging.error(f"[pdf_processing] Erreur extraction texte PJ PDF '{att_info['path']}': {e}")
            att_info["text"] = None
        return att_info

    # Extraction parallèle des pièces jointes PDF (max 2 workers pour limiter la RAM)
    if pdf_attachments:
        num_workers = min(len(pdf_attachments), 2)
        logging.info(f"[pdf_processing] Extraction parallèle de {len(pdf_attachments)} pièces jointes PDF avec {num_workers} workers")

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(_extract_attachment_text, att): att for att in pdf_attachments}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    attachments_result.append(result)
                except Exception as e:
                    att = futures[future]
                    logging.error(f"[pdf_processing] Erreur worker PJ '{att['name']}': {e}")
                    att["text"] = None
                    attachments_result.append(att)

    # Ajouter les pièces jointes non-PDF
    attachments_result.extend(non_pdf_attachments)

    if progress_cb:
        progress_cb(1.0, "Extraction complète terminée")

    return {
        "main_pdf_text": main_text,
        "main_pdf_no_attachments_text": main_no_pj_text,
        "attachments": attachments_result,
    }


# =============================================================================
#  LLM OCR INTEGRATION (Phase 3 - Vision LLM Fallback)
# =============================================================================

# Import optionnel du module LLM OCR
try:
    from llm_ocr import (
        smart_ocr_pdf,
        ocr_pdf_with_llm,
        ocr_single_page,
        is_vision_model_available,
        OCRResult,
        DocumentOCRResult,
    )
    LLM_OCR_AVAILABLE = True
except ImportError:
    LLM_OCR_AVAILABLE = False


def extract_text_with_llm_ocr(
    path: str,
    http_client,
    api_key: str,
    api_base: str,
    model: str,
    quality_threshold: float = 0.5,
    use_smart_fallback: bool = True,
    progress_cb: Optional[ProgressFn] = None,
    log=None,
    should_stop=None,
) -> Dict[str, Any]:
    """
    Extraction de texte PDF avec fallback OCR par LLM Vision.

    Cette fonction combine l'extraction classique (pdfplumber/pdfminer/PyMuPDF)
    avec un fallback automatique vers un LLM Vision pour les PDF scannés
    ou de mauvaise qualité.

    Args:
        path: Chemin du fichier PDF
        http_client: Client HTTP pour les appels API
        api_key: Clé API du LLM Vision
        api_base: URL de base de l'API
        model: Nom du modèle vision (ex: "mistral-small-4")
        quality_threshold: Seuil de qualité (0-1) en dessous duquel on utilise LLM OCR
        use_smart_fallback: Si True, n'utilise LLM OCR que si la qualité est insuffisante
        progress_cb: Callback de progression (ratio, message)
        log: Logger optionnel

    Returns:
        Dict contenant:
        - text: Texte extrait
        - method: Méthode utilisée ("classic", "llm_ocr", "hybrid")
        - quality_score: Score de qualité de l'extraction classique
        - llm_ocr_used: True si LLM OCR a été utilisé
        - llm_ocr_confidence: Confiance de l'OCR LLM (si utilisé)
        - pages_ocr: Nombre de pages traitées par OCR LLM
        - total_pages: Nombre total de pages
        - extraction_details: Détails supplémentaires

    Example:
        >>> import httpx
        >>> client = httpx.Client()
        >>> result = extract_text_with_llm_ocr(
        ...     path="scan.pdf",
        ...     http_client=client,
        ...     api_key="votre-cle-mistral-small",
        ...     api_base="https://litellm.dassault-aviation.pro/v1",
        ...     model="mistral-small-4",
        ...     quality_threshold=0.6
        ... )
        >>> print(f"Méthode: {result['method']}, Qualité: {result['quality_score']:.0%}")
    """
    _log = log or logging

    if not LLM_OCR_AVAILABLE:
        _log.warning("[pdf_processing] Module llm_ocr non disponible, extraction classique uniquement")
        text = extract_text_from_pdf(path, progress_cb=progress_cb)
        quality = assess_extraction_quality(text)
        return {
            "text": text,
            "method": "classic",
            "quality_score": quality.get("quality_score", 0),
            "llm_ocr_used": False,
            "llm_ocr_confidence": None,
            "pages_ocr": 0,
            "total_pages": 0,
            "extraction_details": quality,
        }

    if use_smart_fallback:
        # Mode intelligent: extraction classique puis fallback si nécessaire
        if progress_cb:
            progress_cb(0.05, "Extraction classique du texte...")

        try:
            result = smart_ocr_pdf(
                pdf_path=path,
                http_client=http_client,
                api_key=api_key,
                api_base=api_base,
                model=model,
                quality_threshold=quality_threshold,
                fallback_pages_only=True,
                progress_cb=progress_cb,
                log=_log,
                should_stop=should_stop
            )

            return {
                "text": result.get("text", ""),
                "method": result.get("method", "unknown"),
                "quality_score": result.get("quality_score", 0),
                "llm_ocr_used": result.get("llm_ocr_used", False),
                "llm_ocr_confidence": result.get("llm_ocr_confidence"),
                "pages_ocr": result.get("pages_ocr", 0),
                "total_pages": result.get("total_pages", 0),
                "extraction_details": result,
            }

        except Exception as e:
            _log.error(f"[pdf_processing] Erreur smart_ocr_pdf: {e}")
            # Fallback sur extraction classique
            text = extract_text_from_pdf(path, progress_cb=progress_cb)
            quality = assess_extraction_quality(text)
            return {
                "text": text,
                "method": "classic_fallback",
                "quality_score": quality.get("quality_score", 0),
                "llm_ocr_used": False,
                "llm_ocr_confidence": None,
                "pages_ocr": 0,
                "total_pages": 0,
                "extraction_details": {"error": str(e), "quality": quality},
            }

    else:
        # Mode forcé: utiliser LLM OCR directement
        if progress_cb:
            progress_cb(0.05, "Démarrage OCR LLM Vision...")

        try:
            ocr_result = ocr_pdf_with_llm(
                pdf_path=path,
                http_client=http_client,
                api_key=api_key,
                api_base=api_base,
                model=model,
                progress_cb=progress_cb,
                log=_log
            )

            return {
                "text": ocr_result.full_text,
                "method": "llm_ocr",
                "quality_score": None,  # Pas d'extraction classique
                "llm_ocr_used": True,
                "llm_ocr_confidence": ocr_result.avg_confidence,
                "pages_ocr": ocr_result.processed_pages,
                "total_pages": ocr_result.total_pages,
                "extraction_details": {
                    "total_time": ocr_result.total_time,
                    "errors": ocr_result.errors,
                    "page_results": [
                        {
                            "page": r.page_number,
                            "confidence": r.confidence,
                            "time": r.processing_time,
                            "error": r.error,
                        }
                        for r in ocr_result.page_results
                    ],
                },
            }

        except Exception as e:
            _log.error(f"[pdf_processing] Erreur ocr_pdf_with_llm: {e}")
            return {
                "text": "",
                "method": "error",
                "quality_score": None,
                "llm_ocr_used": True,
                "llm_ocr_confidence": 0,
                "pages_ocr": 0,
                "total_pages": 0,
                "extraction_details": {"error": str(e)},
            }


def check_llm_ocr_available(
    http_client,
    api_key: str,
    api_base: str,
    model: str,
    log=None
) -> bool:
    """
    Vérifie si l'OCR par LLM Vision est disponible et fonctionnel.

    Args:
        http_client: Client HTTP
        api_key: Clé API
        api_base: URL de base de l'API
        model: Nom du modèle vision

    Returns:
        True si le LLM Vision est disponible et fonctionnel
    """
    _log = log or logging

    if not LLM_OCR_AVAILABLE:
        _log.info("[pdf_processing] Module llm_ocr non installé")
        return False

    try:
        return is_vision_model_available(
            http_client=http_client,
            api_key=api_key,
            api_base=api_base,
            model=model,
            log=_log
        )
    except Exception as e:
        _log.warning(f"[pdf_processing] Test LLM Vision échoué: {e}")
        return False


# =============================================================================
#  FONCTIONS DALLEM SIMPLIFIÉES
# =============================================================================

def extract_text_with_dallem(
    path: str,
    quality_threshold: float = 0.5,
    force_llm_ocr: bool = False,
    progress_cb: Optional[ProgressFn] = None,
    log=None,
    should_stop=None,
) -> Dict[str, Any]:
    """
    Extraction de texte PDF avec DALLEM Vision.

    Fonction simplifiée qui utilise automatiquement l'API DALLEM
    configurée dans models_utils.py.

    Args:
        path: Chemin du fichier PDF
        quality_threshold: Seuil de qualité pour déclencher l'OCR LLM (0-1)
        force_llm_ocr: Si True, utilise toujours DALLEM OCR (pas de fallback classique)
        progress_cb: Callback de progression (ratio, message)
        log: Logger optionnel

    Returns:
        Dict contenant:
        - text: Texte extrait
        - method: Méthode utilisée
        - llm_ocr_used: True si DALLEM OCR a été utilisé
        - pages_ocr: Nombre de pages traitées par OCR
        - total_pages: Nombre total de pages

    Example:
        >>> # Mode intelligent (fallback si qualité < 50%)
        >>> result = extract_text_with_dallem("document.pdf")
        >>> print(result["text"])

        >>> # Forcer l'utilisation de DALLEM pour tout
        >>> result = extract_text_with_dallem("scan.pdf", force_llm_ocr=True)
    """
    _log = log or logging

    if not LLM_OCR_AVAILABLE:
        _log.error("[pdf_processing] Module llm_ocr non disponible")
        # Fallback sur extraction classique
        text = extract_text_from_pdf(path, progress_cb=progress_cb)
        quality = assess_extraction_quality(text)
        return {
            "text": text,
            "method": "classic_only",
            "quality_score": quality.get("quality_score", 0),
            "llm_ocr_used": False,
            "pages_ocr": 0,
            "total_pages": 0,
            "error": "Module llm_ocr non disponible",
        }

    try:
        from llm_ocr import (
            smart_ocr_with_dallem,
            ocr_pdf_with_dallem,
            check_dallem_vision_available,
            DALLEM_CONFIG_AVAILABLE,
        )
    except ImportError as e:
        _log.error(f"[pdf_processing] Import llm_ocr échoué: {e}")
        text = extract_text_from_pdf(path, progress_cb=progress_cb)
        return {
            "text": text,
            "method": "classic_fallback",
            "llm_ocr_used": False,
            "pages_ocr": 0,
            "total_pages": 0,
            "error": str(e),
        }

    if not DALLEM_CONFIG_AVAILABLE:
        _log.error("[pdf_processing] Configuration OCR Vision non disponible (models_utils.py)")
        text = extract_text_from_pdf(path, progress_cb=progress_cb)
        return {
            "text": text,
            "method": "classic_fallback",
            "llm_ocr_used": False,
            "pages_ocr": 0,
            "total_pages": 0,
            "error": "Configuration OCR Vision non disponible",
        }

    if force_llm_ocr:
        # Mode forcé: utiliser DALLEM OCR directement
        _log.info(f"[pdf_processing] OCR Vision forcé pour: {path}")

        if progress_cb:
            progress_cb(0.05, "Démarrage OCR Vision...")

        try:
            ocr_result = ocr_pdf_with_dallem(
                pdf_path=path,
                progress_cb=progress_cb,
                log=_log
            )

            return {
                "text": ocr_result.full_text,
                "method": "dallem_ocr",
                "quality_score": None,
                "llm_ocr_used": True,
                "llm_ocr_confidence": ocr_result.avg_confidence,
                "pages_ocr": ocr_result.processed_pages,
                "total_pages": ocr_result.total_pages,
                "ocr_time": ocr_result.total_time,
                "errors": ocr_result.errors,
                "ocr_transient_failures": getattr(ocr_result, "transient_failures", 0),
            }

        except Exception as e:
            _log.error(f"[pdf_processing] OCR Vision échoué: {e}")
            return {
                "text": "",
                "method": "error",
                "llm_ocr_used": True,
                "pages_ocr": 0,
                "total_pages": 0,
                "error": str(e),
            }

    else:
        # Mode intelligent: extraction classique puis DALLEM si nécessaire
        _log.info(f"[pdf_processing] Smart OCR Vision pour: {path}")

        try:
            result = smart_ocr_with_dallem(
                pdf_path=path,
                quality_threshold=quality_threshold,
                progress_cb=progress_cb,
                log=_log,
                should_stop=should_stop
            )

            return {
                "text": result.get("text", ""),
                "method": result.get("method", "unknown"),
                "quality_score": result.get("quality_score"),
                "llm_ocr_used": result.get("llm_ocr_used", False),
                "llm_ocr_confidence": result.get("llm_ocr_confidence"),
                "pages_ocr": result.get("pages_ocr", 0),
                "total_pages": result.get("total_pages", 0),
                "ocr_transient_failures": result.get("ocr_transient_failures", 0),
            }

        except Exception as e:
            _log.error(f"[pdf_processing] Smart OCR Vision échoué: {e}")
            # Fallback sur extraction classique
            text = extract_text_from_pdf(path, progress_cb=progress_cb)
            quality = assess_extraction_quality(text)
            return {
                "text": text,
                "method": "classic_fallback",
                "quality_score": quality.get("quality_score", 0),
                "llm_ocr_used": False,
                "pages_ocr": 0,
                "total_pages": 0,
                "error": str(e),
            }


# =============================================================================
#  FONCTION OCR SMART (API DALLEM)
# =============================================================================


def smart_extract_text_with_ocr(
    path: str,
    quality_threshold: float = 0.5,
    force_ocr: bool = False,
    progress_cb: Optional[ProgressFn] = None,
    log=None,
    should_stop=None,
) -> Dict[str, Any]:
    """
    Extraction de texte PDF avec OCR intelligent via DALLEM Vision.

    Args:
        path: Chemin du fichier PDF
        quality_threshold: Seuil de qualité pour déclencher l'OCR (0-1)
        force_ocr: Si True, utilise toujours l'OCR (pas de fallback classique)
        progress_cb: Callback de progression (ratio, message)
        log: Logger optionnel

    Returns:
        Dict contenant:
        - text: Texte extrait
        - method: Méthode utilisée
        - llm_ocr_used: True si OCR a été utilisé
        - pages_ocr: Nombre de pages traitées par OCR
        - total_pages: Nombre total de pages
    """
    _log = log or logging

    _log.info(f"[pdf_processing] Utilisation de l'OCR Vision (mistral-small)")
    return extract_text_with_dallem(
        path=path,
        quality_threshold=quality_threshold,
        force_llm_ocr=force_ocr,
        progress_cb=progress_cb,
        log=_log,
        should_stop=should_stop,
    )
