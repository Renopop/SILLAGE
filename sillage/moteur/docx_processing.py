# docx_processing.py
import docx
import re
import os
import csv
import io
import json
import time
import tempfile
import threading
import shutil
import subprocess
import logging


# =============================================================================
#  BLINDAGE .doc — un fichier corrompu ne doit JAMAIS bloquer l'indexation
# =============================================================================
# La conversion .doc -> .docx pilote Microsoft Word par COM. Un .doc corrompu,
# protégé par mot de passe ou mal renommé peut figer Word (dialogue modal
# invisible, boucle de récupération) et bloquer tout le run. Quatre gardes :
#   1. Signature OLE vérifiée AVANT d'ouvrir Word (8 octets) : les fichiers
#      tronqués/renommés sont écartés instantanément.
#   2. Quarantaine persistée : un .doc qui a fait échouer la conversion est
#      sauté aux runs suivants tant qu'il n'a pas changé (taille+mtime).
#   3. Ouverture Word durcie : macros désactivées, aucune boîte de dialogue
#      (mot de passe factice -> échec immédiat au lieu d'une invite modale).
#   4. Délai maximal (RIVAGE_DOC_TIMEOUT_S, défaut 120 s, 0 = illimité) : la
#      conversion tourne dans un thread ; à l'échéance, l'instance Word DÉDIÉE
#      (DispatchEx) est tuée par PID et le fichier part en quarantaine.

# Signature OLE2 ("D0 CF 11 E0 A1 B1 1A E1") : obligatoire pour un vrai .doc.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_DOC_TIMEOUT_S_DEFAULT = 120.0


def _is_ole_file(path: str) -> bool:
    """Vrai si le fichier commence par la signature OLE d'un .doc binaire."""
    try:
        with open(path, "rb") as f:
            return f.read(8) == _OLE_MAGIC
    except OSError:
        return False


def _doc_timeout_s() -> float:
    try:
        v = os.environ.get("RIVAGE_DOC_TIMEOUT_S")
        return max(0.0, float(v)) if v is not None and v.strip() else _DOC_TIMEOUT_S_DEFAULT
    except (TypeError, ValueError):
        return _DOC_TIMEOUT_S_DEFAULT


# --- Quarantaine persistée ---------------------------------------------------
# ~/.rivage/doc_quarantine.json : {clé: {size, mtime, reason, ts}}. La clé est
# le chemin absolu normalisé. Si le fichier change (taille ou mtime), l'entrée
# est ignorée puis remplacée : réparer/remplacer le .doc suffit pour retenter.

def _quarantine_enabled() -> bool:
    return os.environ.get("RIVAGE_DOC_QUARANTINE", "1").strip() != "0"


def _quarantine_file() -> str:
    base = os.environ.get("RIVAGE_DOC_QUARANTINE_DIR") or os.path.join(
        os.path.expanduser("~"), ".rivage")
    return os.path.join(base, "doc_quarantine.json")


def _quarantine_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _quarantine_load() -> dict:
    try:
        with open(_quarantine_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _quarantine_reason(path: str) -> "str | None":
    """Raison de quarantaine si le fichier y est ET n'a pas changé, sinon None."""
    if not _quarantine_enabled():
        return None
    entry = _quarantine_load().get(_quarantine_key(path))
    if not isinstance(entry, dict):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    if entry.get("size") == st.st_size and entry.get("mtime") == int(st.st_mtime):
        return str(entry.get("reason") or "échec précédent")
    return None


def _quarantine_add(path: str, reason: str) -> None:
    """Mémorise l'échec (best effort : ne doit jamais lever)."""
    if not _quarantine_enabled():
        return
    try:
        st = os.stat(path)
        data = _quarantine_load()
        data[_quarantine_key(path)] = {
            "size": st.st_size, "mtime": int(st.st_mtime),
            "reason": reason[:300], "ts": int(time.time()),
        }
        # Borne dure : garder les 1000 entrées les plus récentes.
        if len(data) > 1000:
            keep = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))[-1000:]
            data = dict(keep)
        qfile = _quarantine_file()
        os.makedirs(os.path.dirname(qfile), exist_ok=True)
        tmp = qfile + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, qfile)
        logging.warning(f"[docx_processing] {os.path.basename(path)} mis en "
                        f"QUARANTAINE ({reason}) — il sera sauté tant qu'il "
                        f"n'aura pas été modifié ou réparé.")
    except Exception:
        pass


# --- Suivi du processus Word dédié -------------------------------------------

def _winword_pids() -> set:
    """PID des WINWORD.EXE en cours (Windows) — via tasklist, sans dépendance."""
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout or ""
    except Exception:
        return set()
    pids = set()
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2:
            try:
                pids.add(int(row[1]))
            except (TypeError, ValueError):
                continue
    return pids


def _kill_pid(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def _convert_doc_worker(doc_path: str, temp_dir: str, shared: dict) -> None:
    """Corps de la conversion, exécuté dans un thread dédié (voir timeout).

    Renseigne shared["docx_path"] ou shared["error"], et shared["word_pid"]
    (PID de l'instance Word DÉDIÉE, tuable sans toucher aux Word de
    l'utilisateur) dès qu'il est connu.
    """
    import win32com.client
    import pythoncom

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        pids_before = _winword_pids()
        # DispatchEx : instance Word NEUVE et dédiée (Dispatch réutiliserait
        # une session Word déjà ouverte par l'utilisateur — la tuer au timeout
        # détruirait son travail).
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        try:
            # msoAutomationSecurityForceDisable = 3 : aucune macro exécutée.
            word.AutomationSecurity = 3
        except Exception:
            pass
        # AUCUNE boîte de dialogue possible : ces invites bloquent l'automation
        # (elles attendent un clic qui ne viendra jamais). En particulier,
        # l'avertissement « ce document contient des marques de révision ou des
        # commentaires » affiché à l'ENREGISTREMENT même quand DisplayAlerts
        # est coupé — c'est lui qui suspendait l'indexation.
        for opt, val in (
            ("WarnBeforeSavingPrintingSendingMarkup", False),  # révisions/commentaires
            ("SavePropertiesPrompt", False),                   # propriétés du document
            ("ConfirmConversions", False),                     # choix du convertisseur
            ("SaveNormalPrompt", False),                       # modèle Normal.dotm modifié
        ):
            try:
                setattr(word.Options, opt, val)
            except Exception:
                pass
        try:
            word.FeatureInstall = 0    # msoFeatureInstallNone : pas d'installeur Office
        except Exception:
            pass
        new_pids = _winword_pids() - pids_before
        if len(new_pids) == 1:
            shared["word_pid"] = new_pids.pop()

        abs_doc_path = os.path.abspath(doc_path)

        # Ouverture DURCIE : lecture seule, aucune conversion interactive,
        # aucun dialogue d'encodage ; le mot de passe FACTICE fait échouer
        # immédiatement les fichiers protégés au lieu d'afficher une invite
        # modale invisible qui bloquerait le run.
        doc = word.Documents.Open(
            abs_doc_path, ReadOnly=True, ConfirmConversions=False,
            AddToRecentFiles=False, PasswordDocument="__rivage__",
            WritePasswordDocument="__rivage__", NoEncodingDialog=True,
            Visible=False,
        )

        # Accepter AUTOMATIQUEMENT toutes les révisions (suivi des
        # modifications) — jamais bloquant : un échec (document protégé…)
        # n'empêche pas la conversion, le texte visible est enregistré tel quel.
        try:
            doc.TrackRevisions = False     # ne pas en créer de nouvelles
        except Exception:
            pass
        try:
            if doc.Revisions.Count > 0:
                logging.info(f"[docx_processing] Acceptation de {doc.Revisions.Count} révision(s)")
                doc.AcceptAllRevisions()
        except Exception as e:
            logging.warning(f"[docx_processing] Révisions non acceptées ({e}) — "
                            f"conversion poursuivie.")

        # Supprimer tous les commentaires — appel global d'abord (rapide sur
        # les documents très commentés), boucle en repli, jamais bloquant.
        try:
            if doc.Comments.Count > 0:
                logging.info(f"[docx_processing] Suppression de {doc.Comments.Count} commentaire(s)")
                try:
                    doc.DeleteAllComments()
                except Exception:
                    # Supprimer du dernier au premier pour éviter les problèmes d'index
                    for i in range(doc.Comments.Count, 0, -1):
                        doc.Comments(i).Delete()
        except Exception as e:
            logging.warning(f"[docx_processing] Commentaires non supprimés ({e}) — "
                            f"conversion poursuivie.")

        # Chemin de sortie (fichier temporaire)
        base_name = os.path.splitext(os.path.basename(doc_path))[0]
        docx_path = os.path.join(temp_dir, f"{base_name}.docx")

        # Sauvegarder en .docx (format 16 = wdFormatXMLDocument)
        doc.SaveAs2(os.path.abspath(docx_path), FileFormat=16)
        shared["docx_path"] = docx_path
        logging.info(f"[docx_processing] Conversion Word réussie: {docx_path}")

    except Exception as e:
        shared["error"] = str(e)

    finally:
        if doc:
            try:
                doc.Close(False)
            except Exception:
                pass
        if word:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def convert_doc_to_docx(doc_path: str) -> str:
    """
    Convertit un fichier .doc en .docx via Microsoft Word (Windows).

    Requiert:
    - Windows avec Microsoft Word installé
    - pywin32 (pip install pywin32)

    Traitement automatique:
    - Accepte toutes les révisions (suivi des modifications)
    - Supprime tous les commentaires
    - Stocke le .docx dans un dossier temporaire (l'original n'est pas modifié)

    Blindage (voir bandeau en tête de section) : signature OLE vérifiée avant
    d'ouvrir Word, quarantaine des fichiers déjà en échec, ouverture sans
    dialogue possible, délai maximal RIVAGE_DOC_TIMEOUT_S avec mise à mort de
    l'instance Word dédiée. AUCUN .doc ne peut bloquer l'indexation : au pire
    il est sauté avec une erreur claire.

    Retourne le chemin vers le fichier .docx créé (dans un dossier temporaire).
    Le fichier temporaire doit être supprimé par l'appelant après usage.
    """
    if not os.path.isfile(doc_path):
        raise FileNotFoundError(f"Fichier introuvable: {doc_path}")

    # Garde 1 : un vrai .doc commence par la signature OLE. Fichier tronqué,
    # corrompu ou renommé (txt/WordPerfect/rtf rebaptisé .doc) : écarté SANS
    # solliciter Word. Vérifié à chaque run (coût : 8 octets).
    if not _is_ole_file(doc_path):
        raise RuntimeError(
            "fichier .doc invalide (signature OLE absente : corrompu, tronqué "
            "ou autre format renommé en .doc) — conversion Word non tentée"
        )

    # Garde 2 : déjà en échec et inchangé depuis -> sauté immédiatement.
    reason = _quarantine_reason(doc_path)
    if reason:
        raise RuntimeError(
            f"fichier en quarantaine après un échec précédent ({reason}) — "
            f"réparez ou remplacez le fichier pour retenter"
        )

    if os.name != 'nt':
        raise RuntimeError(
            "La conversion .doc → .docx nécessite Windows avec Microsoft Word. "
            "Sur Linux/Mac, convertir manuellement les fichiers .doc en .docx."
        )

    try:
        import win32com.client  # noqa: F401
        import pythoncom        # noqa: F401
    except ImportError:
        raise RuntimeError(
            "pywin32 non installé. Exécutez: pip install pywin32\n"
            "Puis redémarrez Python/l'application."
        )

    temp_dir = tempfile.mkdtemp(prefix="doc_convert_")
    logging.info(f"[docx_processing] Conversion .doc → .docx: {os.path.basename(doc_path)}")

    # Garde 4 : la conversion tourne dans un thread surveillé. À l'échéance,
    # l'instance Word dédiée est tuée par PID : le thread abandonné meurt sur
    # l'erreur COM qui s'ensuit, le fichier part en quarantaine, et le RUN
    # CONTINUE avec le document suivant.
    shared: dict = {}
    worker = threading.Thread(
        target=_convert_doc_worker, args=(doc_path, temp_dir, shared),
        daemon=True, name="rivage-doc-convert",
    )
    timeout_s = _doc_timeout_s()
    worker.start()
    worker.join(timeout_s if timeout_s > 0 else None)

    if worker.is_alive():
        pid = shared.get("word_pid")
        if pid:
            _kill_pid(pid)
        worker.join(10)
        shutil.rmtree(temp_dir, ignore_errors=True)
        msg = (f"conversion Word bloquée plus de {int(timeout_s)} s "
               f"(RIVAGE_DOC_TIMEOUT_S) — instance Word "
               f"{'tuée' if pid else 'abandonnée'}")
        _quarantine_add(doc_path, msg)
        raise RuntimeError(f"Erreur lors de la conversion avec Word: {msg}")

    if "docx_path" in shared:
        return shared["docx_path"]

    shutil.rmtree(temp_dir, ignore_errors=True)
    err = shared.get("error", "échec inconnu")
    _quarantine_add(doc_path, err)
    raise RuntimeError(
        f"Erreur lors de la conversion avec Word: {err}\n"
        f"Vérifiez que Microsoft Word est installé et fonctionne."
    )


# --- Parcours du XML WordprocessingML ----------------------------------------
# Audit V2, B-4 : `Paragraph.text` de python-docx ne lit que les runs directs
# d'un paragraphe. Disparaissaient donc, sans erreur : les CONTRÔLES DE
# CONTENU (w:sdt — pages de garde et modèles : référence, révision, titre),
# les RÉVISIONS NON ACCEPTÉES (w:ins — texte inséré en mode suivi), les ZONES
# DE TEXTE (w:txbxContent — cartouches, encadrés), ainsi que les en-têtes et
# pieds de page (jamais lus). On parcourt donc le XML nous-mêmes : tout `w:t`
# d'un bloc est pris, SAUF sous `w:del` (texte supprimé en mode suivi) et sous
# `mc:Fallback` (copie de compatibilité d'une zone de texte : Word écrit le
# même contenu dans mc:Choice ET mc:Fallback — le lire deux fois le
# dupliquerait).
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _w(tag: str) -> str:
    return "{%s}%s" % (_W_NS, tag)


_W_P, _W_TBL, _W_T = _w("p"), _w("tbl"), _w("t")
_W_SDT, _W_SDT_CONTENT, _W_INS = _w("sdt"), _w("sdtContent"), _w("ins")
_W_SECT_PR = _w("sectPr")
# Sous-arbres sans texte visible, ou dont le texte ne doit pas être lu.
_SKIP_TAGS = frozenset({
    _w("del"), _w("delText"), _w("delInstrText"), _w("instrText"),
    _w("sdtPr"), _w("sdtEndPr"), _w("pPr"), _w("rPr"), _w("tblPr"),
    _w("trPr"), _w("tcPr"), _w("moveFrom"), _w("commentRangeStart"),
    _w("commentRangeEnd"), _w("commentReference"), _w("footnoteReference"),
    _w("endnoteReference"),
    "{%s}Fallback" % _MC_NS,
})
# Éléments de texte « spéciaux » (même rendu que python-docx).
_TEXT_TAGS = {
    _W_T: None,                       # None = le texte de l'élément
    _w("tab"): "\t",
    _w("ptab"): "\t",
    _w("br"): "\n",
    _w("cr"): "\n",
    _w("noBreakHyphen"): "-",
}


def _texte_bloc(el) -> str:
    """Texte visible d'un bloc (paragraphe, run, contenu d'un contrôle…) :
    tous les `w:t` descendants dans l'ordre du document, hors révisions
    supprimées et copies de compatibilité, contrôles de contenu en ligne,
    insertions et zones de texte COMPRIS (audit V2, B-4). Les paragraphes
    imbriqués (zones de texte) sont séparés par un saut de ligne."""
    parts = []

    def walk(node):
        for child in node.iterchildren():
            tag = child.tag
            if not isinstance(tag, str) or tag in _SKIP_TAGS:
                continue                      # commentaires XML, sous-arbres exclus
            if tag in _TEXT_TAGS:
                rendu = _TEXT_TAGS[tag]
                parts.append((child.text or "") if rendu is None else rendu)
            elif tag == _W_P:
                walk(child)
                parts.append("\n")
            else:
                walk(child)

    walk(el)
    return "".join(parts).strip("\n")


def _render_blocks(container, parent, parts) -> None:
    """Rend les blocs d'un conteneur (corps, en-tête, contenu d'un contrôle)
    dans l'ordre : paragraphes, tableaux, contrôles de contenu (récursifs)."""
    for child in container.iterchildren():
        tag = child.tag
        if not isinstance(tag, str) or tag in _SKIP_TAGS or tag == _W_SECT_PR:
            continue
        if tag == _W_P:
            parts.append(_texte_bloc(child))
        elif tag == _W_TBL:
            rendered = _render_docx_table(child, parent)
            if rendered:
                parts.append(rendered)
        elif tag in (_W_SDT, _W_SDT_CONTENT, _W_INS):
            _render_blocks(child, parent, parts)
        else:
            txt = _texte_bloc(child)
            if txt:
                parts.append(txt)


def _render_docx_cell(tc, parent) -> str:
    """
    Texte d'une cellule : paragraphes directs + TABLEAUX IMBRIQUÉS (audit v2 :
    `_Cell.text` ne lit que les w:p enfants directs — un tableau dans une
    cellule disparaissait sans erreur). Le tableau imbriqué est rendu en ligne.
    Les contrôles de contenu d'une cellule sont lus comme ailleurs (B-4).
    """
    parts = []

    def walk(container):
        for child in container.iterchildren():
            tag = child.tag
            if not isinstance(tag, str) or tag in _SKIP_TAGS:
                continue
            if tag == _W_P:
                txt = _texte_bloc(child).strip()
                if txt:
                    parts.append(txt)
            elif tag == _W_TBL:
                nested = _render_docx_table(child, parent)
                if nested:
                    parts.append(nested.replace("\n", " ; "))
            elif tag in (_W_SDT, _W_SDT_CONTENT, _W_INS):
                walk(child)

    walk(tc)
    return " ".join(parts)


def _render_docx_table(tbl_element, parent) -> str:
    """
    Rend un tableau en texte lisible « cellule | cellule | … » (convention
    Excel §3.4), en dédupliquant les FUSIONS (audit v2) : `row.cells` répète la
    même cellule pour chaque position de grille (fusion horizontale) et renvoie
    la cellule racine sur chaque ligne couverte (fusion verticale) — les
    nomenclatures Word polluaient l'index de duplications. On itère donc les
    `w:tc` réels de chaque ligne (pas de répétition gridSpan) en sautant les
    continuations verticales (vMerge='continue').
    """
    from docx.table import Table
    from docx.oxml.ns import qn
    table = Table(tbl_element, parent)
    rows = []
    for row in table.rows:
        cells = []
        for tc in row._tr.tc_lst:
            if tc.vMerge == "continue":
                continue  # continuation verticale : texte déjà émis à la racine
            # Fusion horizontale HÉRITÉE <w:hMerge> (anciens Word/convertisseurs,
            # audit v3) : contrairement au gridSpan moderne, chaque colonne
            # couverte garde un w:tc réel marqué hMerge='continue' (val absent =
            # continue) → cellules vides parasites « A |  | B » sans ce saut.
            tc_pr = tc.find(qn("w:tcPr"))
            if tc_pr is not None:
                hm = tc_pr.find(qn("w:hMerge"))
                if hm is not None and hm.get(qn("w:val"), "continue") == "continue":
                    continue
            cells.append(_render_docx_cell(tc, table))
        if any(c.strip() for c in cells):
            rows.append(" | ".join(cells))
    return "[TABLEAU]\n" + "\n".join(rows) if rows else ""


def _hdr_ftr_texts(doc, attr: str) -> list:
    """Textes distincts des en-têtes (`attr`=« header ») ou pieds
    (« footer ») de toutes les sections — variantes première page et pages
    paires comprises — dédoublonnés, ordre d'apparition (audit V2, B-4)."""
    vus = set()
    out = []
    for section in doc.sections:
        for prefix in ("", "first_page_", "even_page_"):
            try:
                hf = getattr(section, prefix + attr)
                # Une définition absente (en-tête lié au précédent, ou aucun)
                # n'est pas créée : on ne touche pas au document.
                if not getattr(hf, "_has_definition", False):
                    continue
                element = hf._definition.element
            except Exception:
                continue
            parts: list = []
            _render_blocks(element, doc, parts)
            txt = "\n".join(p for p in parts if p and p.strip()).strip()
            if txt and txt not in vus:
                vus.add(txt)
                out.append(txt)
    return out


def docx_to_text(docx_path):
    """
    Extrait le texte brut d'un fichier .docx : paragraphes ET TABLEAUX (fusions
    dédupliquées, tableaux imbriqués inclus), dans l'ordre du document, plus
    les contrôles de contenu, révisions non acceptées et zones de texte
    (audit V2, B-4). Les en-têtes et pieds de page, dédoublonnés, sont émis
    UNE fois par document, en tête du texte, sous les marqueurs
    « [EN-TÊTE] » / « [PIED DE PAGE] » (une référence de document ou un
    cartouche y vivent souvent ; ils étaient invisibles à la recherche).

    NB (audit) : `doc.paragraphs` de python-docx n'inclut PAS le contenu des
    tableaux — les données tabulaires (dimensionnement, nomenclatures…)
    étaient absentes de l'index sans aucune erreur. On parcourt donc le corps
    du document élément par élément (w:p / w:tbl / w:sdt).
    """
    doc = docx.Document(docx_path)
    parts = []
    for marker, attr in (("[EN-TÊTE]", "header"), ("[PIED DE PAGE]", "footer")):
        try:
            textes = _hdr_ftr_texts(doc, attr)
        except Exception as e:
            logging.warning(f"[docx_processing] {attr} illisible ({e}) — ignoré.")
            textes = []
        if textes:
            parts.append(marker + "\n" + "\n".join(textes))
    _render_blocks(doc.element.body, doc, parts)
    # On garde un \n entre chaque bloc
    return "\n".join(parts)

def normalize_whitespace(text: str) -> str:
    """
    Normalise les espaces dans le texte EN CONSERVANT les sauts de ligne.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    norm_lines = []
    for line in lines:
        # Réduire les suites d'espaces/tabulations
        line = re.sub(r"[ \t]+", " ", line)
        norm_lines.append(line.strip())
    return "\n".join(norm_lines).strip()

def extract_text_from_docx(file_path: str) -> str:
    """
    Extrait et nettoie le texte d'un fichier DOCX ou DOC.

    Pour les fichiers .doc (ancien format), convertit automatiquement en .docx
    via Microsoft Word avant extraction (Windows uniquement).

    Args:
        file_path: Chemin vers le fichier .doc ou .docx

    Returns:
        Texte extrait et nettoyé
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".doc":
        # Convertir .doc → .docx avec Microsoft Word
        temp_docx = None
        try:
            temp_docx = convert_doc_to_docx(file_path)
            raw_text = docx_to_text(temp_docx)
        finally:
            # Nettoyer le fichier temporaire
            if temp_docx and os.path.isfile(temp_docx):
                temp_dir = os.path.dirname(temp_docx)
                shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        # Fichier .docx natif
        raw_text = docx_to_text(file_path)

    return normalize_whitespace(raw_text)

def extract_paragraphs_from_docx(docx_path):
    """
    Extrait les paragraphes individuels d'un fichier DOCX.
    """
    doc = docx.Document(docx_path)
    paragraphs = [para.text.strip() for para in doc.paragraphs if para.text.strip()]
    return paragraphs

def extract_sections_from_docx(docx_path, heading_styles=None):
    """
    Extrait les sections d'un fichier DOCX en se basant sur les styles de titre.
    heading_styles est une liste de noms de styles considérés comme des titres de sections.
    """
    if heading_styles is None:
        heading_styles = ["Heading 1", "Heading 2", "Titre 1", "Titre 2"]

    doc = docx.Document(docx_path)
    sections = []
    current_section_title = None
    current_section_content = []

    for para in doc.paragraphs:
        style_name = para.style.name if para.style else ""

        if style_name in heading_styles:
            # Sauvegarder la section précédente
            if current_section_title or current_section_content:
                sections.append({
                    "title": current_section_title,
                    "content": "\n".join(current_section_content).strip(),
                })
                current_section_content = []

            # Nouveau titre de section
            current_section_title = para.text.strip()
        else:
            # Contenu de la section en cours
            if para.text.strip():
                current_section_content.append(para.text.strip())

    # Ajouter la dernière section si présente
    if current_section_title or current_section_content:
        sections.append({
            "title": current_section_title,
            "content": "\n".join(current_section_content).strip(),
        })

    return sections

def extract_text_from_tables(docx_path):
    """
    Extrait le texte contenu dans les tableaux d'un fichier DOCX.
    """
    doc = docx.Document(docx_path)
    table_texts = []

    for table in doc.tables:
        for row in table.rows:
            row_text = []
            for cell in row.cells:
                cell_text = cell.text.strip()
                if cell_text:
                    row_text.append(cell_text)
            if row_text:
                table_texts.append(" | ".join(row_text))

    return "\n".join(table_texts)
