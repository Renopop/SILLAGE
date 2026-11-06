"""OCR de secours pour les PDF scannés (spécification ou NCM).

Même mécanisme que l'indexeur de RIVAGE : une page est rendue en image et
lue par le modèle Vision de la passerelle (module `llm_ocr` du moteur
embarqué, clé `RIVAGE_SYNTH_KEY`, mêmes retries). Chaque page est un appel
payant : l'OCR ne se déclenche qu'à la demande.

Deux régimes, choisis par la valeur `ocr` que reçoivent les lecteurs :

- ``True`` (OCR de secours) : seules les pages **sans couche texte** partent
  en OCR ; et, si la couche texte ne donne aucune exigence ni aucun tableau
  exploitable (scan OCRisé par le scanner, texte dégradé), toutes les pages ;
- ``"forcer"`` : toutes les pages, sans tenir compte de la couche texte.

La consigne envoyée au modèle est propre à SILLAGE : les tableaux doivent
être rendus en lignes à barres verticales (« | »), une ligne par rangée, les
identifiants et les statuts recopiés tels quels ; `tableaux_depuis_texte`
les reconstruit. Les pages lues sont gardées dans un cache local
(`ocr_cache.sqlite` du dossier de données, clé : fichier, taille, date,
page, version de la consigne) et dans un mémo de session : relire ne
repaie rien.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .tableaux import Tableau

logger = logging.getLogger("sillage.ocr")

FORCER = "forcer"
VERSION_CONSIGNE = "2"
OUVRIERS = 3            # pages lues en parallèle (la passerelle borne de toute façon)

CONSIGNE_SYSTEME = """Tu es un expert en OCR (reconnaissance optique de caractères) de documents techniques aéronautiques.
Ta tâche est d'extraire le texte EXACT de l'image fournie.

Règles :
1. Extrais TOUT le texte visible, sans rien ajouter, résumer ni traduire.
2. Chaque TABLEAU est rendu en lignes à barres verticales, au format Markdown : une ligne par rangée du tableau,
   une cellule par colonne, l'en-tête en première ligne. Une cellule écrite sur plusieurs lignes dans l'image est
   recopiée sur UNE seule ligne, dans sa rangée. Les rangées de titre de section (sans identifiant) sont des rangées à part.
   Exemple : | 135 | [REQ_B2-3_34] | Last data set shall be delivered… | Compliant | Should be transferred to SOW. |
3. Les identifiants d'exigences (REQ_…, [REQ_B2-3_15], SRD-12…) et les valeurs de statut (Compliant, Partially compliant,
   Non compliant, N/A, C, PC, NC…) sont recopiés exactement, caractère par caractère.
4. Le texte hors tableaux (titres, paragraphes, notes) est recopié tel quel, un paragraphe par ligne.
5. Indique [illisible] pour ce qui ne peut pas être lu.

Retourne UNIQUEMENT le texte extrait, sans commentaire."""

CONSIGNE_UTILISATEUR = "Extrais le texte de cette page. Rends chaque tableau en lignes « | … | … | », une ligne par rangée."

_MEMO: Dict[Tuple[str, int, float], Dict[int, str]] = {}   # (chemin, taille, mtime) -> page -> texte


def disponible() -> Tuple[bool, str]:
    """(utilisable, raison) : le moteur embarqué et la clé du modèle Vision."""
    from . import rivage
    if not rivage.disponible():
        return False, rivage.message_absent()
    try:
        import llm_ocr
    except Exception as exc:   # pragma: no cover - dépend du poste
        return False, f"module OCR du moteur inutilisable ({exc})"
    if not getattr(llm_ocr, "DALLEM_CONFIG_AVAILABLE", False) or not getattr(llm_ocr, "DALLEM_API_KEY", None):
        return False, "clé du modèle Vision absente (RIVAGE_SYNTH_KEY dans RIVAGE_keys.env)"
    return True, ""


def pages_sans_texte(pdf: Path) -> List[int]:
    """Numéros (à partir de 1) des pages sans couche texte."""
    import pdfplumber
    with pdfplumber.open(str(pdf)) as doc:
        return [n for n, page in enumerate(doc.pages, start=1) if not (page.extract_text() or "").strip()]


def _toutes(p: Path) -> List[int]:
    import pdfplumber
    with pdfplumber.open(str(p)) as doc:
        return list(range(1, len(doc.pages) + 1))


# --- cache local -----------------------------------------------------------------
def _cache():
    from .config import dossier_donnees
    d = dossier_donnees()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(d / "ocr_cache.sqlite"))
    conn.execute("CREATE TABLE IF NOT EXISTS pages (cle TEXT PRIMARY KEY, texte TEXT NOT NULL, date REAL NOT NULL)")
    return conn


def _cle_page(cle: Tuple[str, int, float], n: int) -> str:
    return f"{cle[0]}|{cle[1]}|{cle[2]:.0f}|{n}|v{VERSION_CONSIGNE}"


def pages_ocr(pdf, pages: Optional[List[int]] = None,
              progression: Optional[Callable[[float, str], None]] = None, log=None) -> Dict[int, str]:
    """Texte OCR des pages demandées (numéros à partir de 1 ; toutes par défaut).

    Lève RuntimeError si l'OCR est indisponible ou si une page n'a pas pu
    être lue (passerelle saturée, clé refusée) : un référentiel partiel
    serait pire qu'une erreur franche. Les pages lues restent au cache."""
    p = Path(pdf)
    st = p.stat()
    cle = (str(p.resolve()), st.st_size, st.st_mtime)
    memo = _MEMO.setdefault(cle, {})
    voulues = list(pages or _toutes(p))
    manquantes = [n for n in voulues if n not in memo]
    _log = log or logger
    if manquantes:
        try:
            conn = _cache()
        except Exception as exc:   # pragma: no cover - dossier de données inaccessible
            _log.warning("cache OCR indisponible : %s", exc)
            conn = None
        if conn is not None:
            for n in list(manquantes):
                row = conn.execute("SELECT texte FROM pages WHERE cle = ?", (_cle_page(cle, n),)).fetchone()
                if row is not None:
                    memo[n] = row[0]
                    manquantes.remove(n)
    if manquantes:
        ok, raison = disponible()
        if not ok:
            raise RuntimeError(f"OCR de secours indisponible : {raison}")
        _log.info("OCR de secours : %d page(s) de %s", len(manquantes), p.name)
        resultats, echecs = _lire_pages(p, manquantes, progression, _log)
        for n, texte in resultats.items():
            memo[n] = texte
            if conn is not None:
                try:
                    conn.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?)", (_cle_page(cle, n), texte, time.time()))
                    conn.commit()
                except Exception:   # pragma: no cover
                    pass
        if conn is not None:
            conn.close()
        if echecs:
            raise RuntimeError("OCR de secours incomplet — " + " ; ".join(echecs[:5]) +
                               (" …" if len(echecs) > 5 else "") + " (relancer : les pages lues sont au cache)")
    elif 'conn' in locals() and conn is not None:
        conn.close()
    return {n: memo.get(n, "") for n in voulues}


def _lire_pages(p: Path, pages: List[int], progression, log) -> Tuple[Dict[int, str], List[str]]:
    """Rend chaque page en image et l'envoie au modèle Vision avec la
    consigne de SILLAGE ; un client HTTP par document, partagé."""
    import llm_ocr
    client = llm_ocr.create_http_client()
    resultats: Dict[int, str] = {}
    echecs: List[str] = []
    fait = [0]

    def une(n: int):
        image = llm_ocr.pdf_page_to_image(str(p), n - 1, log=log)
        if not image:
            return n, "", "rendu de la page impossible"
        info: dict = {}
        texte, _conf = llm_ocr.ocr_image_with_llm(image_bytes=image, http_client=client, api_key=llm_ocr.DALLEM_API_KEY,
                                                  api_base=llm_ocr.DALLEM_API_BASE, model=llm_ocr.LLM_MODEL,
                                                  system_prompt=CONSIGNE_SYSTEME, user_prompt=CONSIGNE_UTILISATEUR,
                                                  log=log, result_info=info)
        if not texte:
            return n, "", str(info.get("error") or info.get("erreur") or "aucun texte rendu")
        return n, texte, ""

    try:
        with ThreadPoolExecutor(max_workers=min(OUVRIERS, len(pages))) as pool:
            for n, texte, erreur in pool.map(une, pages):
                fait[0] += 1
                if progression:
                    progression(fait[0] / len(pages), f"OCR page {n} ({fait[0]}/{len(pages)})")
                if erreur:
                    echecs.append(f"page {n} : {erreur}")
                else:
                    resultats[n] = texte
    finally:
        try:
            client.close()
        except Exception:   # pragma: no cover
            pass
    return resultats, echecs


# --- tableaux depuis le texte OCR ------------------------------------------------
_SEPARATEUR_MD = re.compile(r"^:?-{2,}:?$")
_COLONNES_ESPACES = re.compile(r"\s{3,}")


def tableaux_depuis_texte(texte: str, fichier: str, page: int) -> List[Tableau]:
    """Tableaux d'une page OCR : les lignes consécutives contenant au moins
    deux barres verticales forment un tableau (séparateurs « --- » du
    Markdown ignorés) ; à défaut de barres, des lignes consécutives dont les
    cellules sont séparées par trois espaces ou plus. Les lignes sont
    complétées au nombre de colonnes ; un tableau a au moins deux lignes."""
    out = _tableaux(texte, fichier, page, barres=True)
    if not out:
        out = _tableaux(texte, fichier, page, barres=False)
    return out


def _tableaux(texte: str, fichier: str, page: int, barres: bool) -> List[Tableau]:
    out: List[Tableau] = []
    courant: Optional[Tableau] = None
    k = 0
    for i, ligne in enumerate(texte.splitlines(), start=1):
        s = ligne.strip()
        if barres:
            est_rangee = s.count("|") >= 2
            cellules = [c.strip() for c in s.strip("|").split("|")] if est_rangee else []
        else:
            cellules = [c.strip() for c in _COLONNES_ESPACES.split(s)] if s else []
            est_rangee = len(cellules) >= 2
        if est_rangee and all(not c or _SEPARATEUR_MD.fullmatch(c) for c in cellules):
            continue
        if est_rangee:
            if courant is None:
                k += 1
                courant = Tableau(fichier, f"page {page}, tableau {k}", ocr=True)
            courant.ajouter(cellules, i)
        elif courant is not None:
            out.append(courant)
            courant = None
    if courant is not None:
        out.append(courant)
    for t in out:
        nb = t.nb_colonnes
        t.lignes = [l + [""] * (nb - len(l)) for l in t.lignes]
    return [t for t in out if len(t.lignes) >= 2]


def oublier() -> None:
    """Vide le mémo de session (tests)."""
    _MEMO.clear()


__all__ = ["FORCER", "disponible", "pages_sans_texte", "pages_ocr", "tableaux_depuis_texte", "oublier"]
