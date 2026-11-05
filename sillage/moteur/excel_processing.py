# excel_processing.py
"""
Excel Processing Module - Traitement des fichiers tableur (.xlsx / .xls)

Une feuille technique n'est pas du texte plat mais de la donnée tabulaire
structurée (note de conception §3.4). On extrait feuille par feuille, en
préservant les en-têtes de colonnes, et on transforme chaque ligne en texte
enrichi de la forme :

    "Feuille=Dimensionnement | Débit=12 m3/h | Régime=7/12C"

Sans cette mise en forme, la similarité cosinus sur une grille de chiffres ne
donne rien.

Dépendances : pip install openpyxl pandas
"""

import os
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# =============================================================================
#  IMPORT (avec gestion d'erreur)
# =============================================================================

PANDAS_AVAILABLE = False
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    logger.warning(
        "[XLSX] pandas non installé. Installez-le avec: pip install pandas openpyxl"
    )

OPENPYXL_AVAILABLE = False
try:
    import openpyxl  # lecture directe des .xlsx (audit V2, B-8) ; pandas en repli
    OPENPYXL_AVAILABLE = True
except ImportError:
    logger.debug("[XLSX] openpyxl non installé")


# =============================================================================
#  NORMALISATION DES VALEURS
# =============================================================================

def _clean_value(value: Any) -> str:
    """
    Convertit une valeur de cellule en texte propre.
    Les cellules vides (None / NaN) deviennent une chaîne vide.

    Audit V2, B-8 : un flottant à valeur entière (5162004.0 — colonne
    d'entiers rendue float par une cellule vide, ou nombre saisi tel quel)
    est écrit SANS décimale (« 5162004 ») : BM25 ne retrouvait pas la
    référence « 5162004 » dans « 5162004.0 ».
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:                     # NaN
            return ""
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return str(value)
    # NaN / NaT pandas (chemin .xls)
    try:
        if PANDAS_AVAILABLE and pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass

    text = str(value).strip()
    # Réduire les espaces internes et les retours à la ligne
    text = " ".join(text.split())
    return text


def _clean_header(header: Any) -> str:
    """En-tête de colonne ; vide pour une cellule d'en-tête absente — y compris
    les « Unnamed: n » que pandas fabrique (audit V2, B-8) : la valeur est
    alors indexée seule, sans « Unnamed: 3= » devant."""
    h = _clean_value(header)
    if not h or h.lower().startswith("unnamed:"):
        return ""
    return h


def _row_to_text(sheet_name: str, headers: List[str], values: List[Any]) -> str:
    """
    Transforme une ligne en texte enrichi « col=val | col=val ».
    Préfixé du nom de feuille pour garder le contexte.
    Les colonnes vides sont omises ; une valeur sans en-tête est émise seule.
    """
    parts: List[str] = [f"Feuille={sheet_name}"]
    for header, value in zip(headers, values):
        cell = _clean_value(value)
        if cell:
            col = _clean_header(header)
            parts.append(f"{col}={cell}" if col else cell)
    # Une ligne sans aucune valeur ne porte que le nom de feuille -> ignorée
    if len(parts) == 1:
        return ""
    return " | ".join(parts)


def _hidden_sheets_included() -> bool:
    """Feuilles masquées : ignorées par défaut (brouillons, données de
    travail cachées à l'utilisateur) ; RIVAGE_INDEX_HIDDEN_SHEETS=1 les inclut."""
    return (os.environ.get("RIVAGE_INDEX_HIDDEN_SHEETS") or "").strip() == "1"


def _iter_sheets_openpyxl(file_path: str, log):
    """(nom, en-têtes, lignes) par feuille VISIBLE d'un .xlsx, lu par openpyxl
    en flux (`read_only`, `values_only`, valeurs calculées) : les entiers
    restent des entiers, aucune conversion de colonne (audit V2, B-8), et
    l'itération coûte quelques microsecondes par ligne au lieu des ~100 µs de
    `DataFrame.iterrows()`. La première ligne non vide donne les en-têtes."""
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    try:
        include_hidden = _hidden_sheets_included()
        for ws in wb.worksheets:
            if getattr(ws, "sheet_state", "visible") != "visible" and not include_hidden:
                log.info(f"[XLSX] Feuille masquée ignorée : {ws.title} "
                         f"(RIVAGE_INDEX_HIDDEN_SHEETS=1 pour l'inclure)")
                continue
            rows = ws.iter_rows(values_only=True)
            headers = None
            for row in rows:
                if any(v is not None and str(v).strip() for v in row):
                    headers = list(row)
                    break
            if headers is None:
                continue
            yield ws.title, headers, rows
    finally:
        wb.close()


def _iter_sheets_pandas(file_path: str, log):
    """Repli pandas (.xls binaires, ou openpyxl absent) : `dtype=object` pour
    que les entiers ne deviennent pas des flottants (audit V2, B-8)."""
    sheets = pd.read_excel(file_path, sheet_name=None, dtype=object)
    for sheet_name, df in sheets.items():
        if df is None or df.empty:
            continue
        yield sheet_name, [str(c) for c in df.columns], df.itertuples(index=False, name=None)


# =============================================================================
#  EXTRACTION PRINCIPALE
# =============================================================================

def extract_text_from_xlsx(
    file_path: str,
    include_sheet_titles: bool = True,
    log=None,
) -> str:
    """
    Extrait le contenu d'un classeur Excel en texte enrichi.

    Chaque feuille est lue avec sa première ligne comme en-têtes de colonnes.
    Chaque ligne de données devient une phrase « col=val | col=val »
    préfixée du nom de la feuille.

    Args:
        file_path: chemin du fichier .xlsx ou .xls
        include_sheet_titles: ajoute un titre « === Feuille: X === » par feuille
        log: logger optionnel

    Returns:
        Texte extrait, feuilles séparées par une ligne vide.

    Raises:
        RuntimeError: si ni openpyxl ni pandas ne sont installés
        FileNotFoundError: si le fichier n'existe pas
    """
    _log = log or logger

    if not (OPENPYXL_AVAILABLE or PANDAS_AVAILABLE):
        raise RuntimeError(
            "openpyxl n'est pas installé. Installez-le avec: pip install pandas openpyxl"
        )

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Fichier introuvable: {file_path}")

    filename = os.path.basename(file_path)
    _log.info(f"[XLSX] Extraction: {filename}")

    # .xlsx/.xlsm : openpyxl en flux (audit V2, B-8) ; .xls binaire (ou
    # openpyxl absent) : pandas avec dtype=object.
    ext = os.path.splitext(file_path)[1].lower()
    if OPENPYXL_AVAILABLE and ext in (".xlsx", ".xlsm"):
        feuilles = _iter_sheets_openpyxl(file_path, _log)
    elif PANDAS_AVAILABLE:
        feuilles = _iter_sheets_pandas(file_path, _log)
    else:
        raise RuntimeError(f"Impossible de lire le fichier Excel: format {ext} "
                           f"non pris en charge sans pandas")

    all_blocks: List[str] = []
    n_sheets = 0
    try:
        for sheet_name, headers, rows in feuilles:
            n_sheets += 1
            row_texts: List[str] = []
            for row in rows:
                line = _row_to_text(sheet_name, headers, list(row))
                if line:
                    row_texts.append(line)

            if not row_texts:
                continue

            block_parts: List[str] = []
            if include_sheet_titles:
                block_parts.append(f"=== Feuille: {sheet_name} ===")
            block_parts.extend(row_texts)
            all_blocks.append("\n".join(block_parts))
    except Exception as e:
        raise RuntimeError(f"Impossible de lire le fichier Excel: {e}")

    result = "\n\n".join(all_blocks)
    _log.info(
        f"[XLSX] {filename}: {n_sheets} feuille(s), {len(result)} caractères extraits"
    )
    return result


def extract_xlsx_metadata(file_path: str, log=None) -> Dict[str, Any]:
    """
    Extrait les métadonnées d'un classeur (nombre de feuilles, dimensions).

    Args:
        file_path: chemin du fichier .xlsx
        log: logger optionnel

    Returns:
        Dict avec filename, sheet_count, sheets (nom -> {rows, cols}).
    """
    _log = log or logger

    if not PANDAS_AVAILABLE:
        return {"error": "pandas non installé"}

    try:
        sheets = pd.read_excel(file_path, sheet_name=None)
    except Exception as e:
        _log.error(f"[XLSX] Erreur lecture métadonnées: {e}")
        return {"error": str(e)}

    sheet_info = {
        name: {"rows": int(df.shape[0]), "cols": int(df.shape[1])}
        for name, df in sheets.items()
        if df is not None
    }

    return {
        "filename": os.path.basename(file_path),
        "sheet_count": len(sheets),
        "sheets": sheet_info,
    }


def is_xlsx_available() -> bool:
    """Vérifie si le module Excel est utilisable."""
    return PANDAS_AVAILABLE or OPENPYXL_AVAILABLE
