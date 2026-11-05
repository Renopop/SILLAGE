# chunking.py
"""
Chunking - Découpage en fragments + enrichissement contextuel (§4.1)

Le texte extrait est découpé en fragments (chunks) de taille homogène, avec un
léger recouvrement pour ne pas couper une idée en deux. Chaque chunk porte ses
métadonnées : chemin du fichier (donc famille thématique), format, page/feuille
source, position. Ces métadonnées compensent le mauvais classement des
répertoires et servent au filtrage (§10.2) comme à la citation finale (§7).

On préfixe chaque chunk d'une ligne de contexte
(« Famille : CVC — Document : rapport_dim.pdf — Page 4 ») pour enrichir
l'embedding (§4.1).

Le découpeur est le repli INTÉGRÉ (portage fidèle de
RecursiveCharacterTextSplitter : mêmes séparateurs décroissants, mêmes
frontières, mêmes recouvrements — audit perf 2.1, I-8 : 75 fois plus rapide).
RIVAGE_SPLITTER=langchain rétablit `langchain-text-splitters` s'il est installé.

Paramètres par défaut : chunk_size=800, chunk_overlap=60 (réduit de 120 —
moins de duplication dans l'index) ; surchargeables par RIVAGE_CHUNK_SIZE /
RIVAGE_CHUNK_OVERLAP.
"""

import os
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 800
# Chevauchement réduit de 120 à 60 (taille de base) : ~15 % de texte dupliqué
# dans l'index -> ~7 %, soit autant de vecteurs ET de BM25 en moins, pour un
# effet marginal sur la qualité (le contexte de page/section est conservé).
# Surchargeables SANS CLI : RIVAGE_CHUNK_SIZE / RIVAGE_CHUNK_OVERLAP.
DEFAULT_CHUNK_OVERLAP = 60


def _resolve_chunk_params(chunk_size, chunk_overlap):
    """None -> variables d'environnement, sinon défauts ; overlap borné."""
    import os as _os

    def _env_int(name, default):
        try:
            v = _os.environ.get(name)
            return int(v) if v is not None and v.strip() else default
        except (TypeError, ValueError):
            return default

    if chunk_size is None:
        chunk_size = _env_int("RIVAGE_CHUNK_SIZE", DEFAULT_CHUNK_SIZE)
    if chunk_overlap is None:
        chunk_overlap = _env_int("RIVAGE_CHUNK_OVERLAP", DEFAULT_CHUNK_OVERLAP)
    chunk_size = max(100, int(chunk_size))
    chunk_overlap = min(max(0, int(chunk_overlap)), chunk_size // 2)
    return chunk_size, chunk_overlap
DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]

# Marqueurs de page insérés par les extracteurs (pdf: "\f", OCR/pptx: "--- Page N ---")
_PAGE_FEED = "\f"


@dataclass
class Chunk:
    """Un fragment de texte prêt à indexer."""
    text: str                       # texte enrichi (préfixe de contexte + contenu)
    content: str                    # contenu brut, sans le préfixe
    path: str
    family: str
    ext: str
    position: int                   # index du chunk dans le document
    page: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
#  SPLITTER (langchain si dispo, repli intégré sinon)
# =============================================================================

def _get_splitter(chunk_size: int, chunk_overlap: int):
    """
    Découpeur par défaut : le repli INTÉGRÉ (audit perf 2.1, I-8). Sur le banc,
    `RecursiveCharacterTextSplitter` coûtait 0,31 s/Mo (expressions régulières
    reconstruites à chaque niveau, listes recopiées à chaque retrait du
    recouvrement) contre quelques ms/Mo pour le repli, qui en est désormais le
    portage fidèle (mêmes frontières, même recouvrement — voir
    _FallbackSplitter). RIVAGE_SPLITTER=langchain rétablit l'ancien découpeur
    (comparaison, doute sur un corpus) ; s'il est absent, le repli est utilisé.
    """
    choice = (os.environ.get("RIVAGE_SPLITTER") or "").strip().lower()
    if choice == "langchain":
        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter
            return RecursiveCharacterTextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                separators=DEFAULT_SEPARATORS,
            )
        except ImportError:
            logger.debug("[chunking] RIVAGE_SPLITTER=langchain mais "
                         "langchain-text-splitters absent — repli intégré.")
    return _FallbackSplitter(chunk_size, chunk_overlap)


class _FallbackSplitter:
    """
    Découpage récursif, portage FIDÈLE de RecursiveCharacterTextSplitter
    (keep_separator=True, strip_whitespace=True, longueur = len) : on coupe
    sur le premier séparateur de la liste présent dans le texte (le séparateur
    reste en TÊTE du morceau qui le suit), les morceaux trop longs sont
    redécoupés avec les séparateurs suivants, et les morceaux courts sont
    recombinés en fragments ≤ chunk_size avec un recouvrement constitué de
    morceaux ENTIERS (jamais une coupe au milieu d'un mot). Les fragments sont
    débarrassés de leurs blancs de bord.

    Historique (audit perf 2.1, I-8) : l'ancien repli recouvrait par tranche
    de caractères (`texte[-overlap:]`, mots coupés), rejetait les morceaux
    blancs et coupait les lignes sans séparateur par fenêtres fixes — il
    divergeait de langchain sur les listes, les lignes longues et les textes
    de plusieurs pages. Ce portage produit les mêmes fragments, aux mêmes
    frontières, ce qui a permis d'en faire le découpeur par défaut.
    """

    def __init__(self, chunk_size: int, chunk_overlap: int,
                 separators: Optional[List[str]] = None):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = list(separators or DEFAULT_SEPARATORS)

    def split_text(self, text: str) -> List[str]:
        return self._split(text, self.separators)

    @staticmethod
    def _split_keep_separator(text: str, sep: str) -> List[str]:
        """Découpe sur `sep` en le conservant en tête du morceau suivant
        (keep_separator=True de langchain) ; morceaux vides écartés."""
        if not sep:
            return [c for c in text if c]
        parts = text.split(sep)
        pieces = [parts[0]] + [sep + p for p in parts[1:]]
        return [p for p in pieces if p]

    def _split(self, text: str, separators: List[str]) -> List[str]:
        final: List[str] = []
        # Premier séparateur présent dans le texte ; la chaîne vide (dernier
        # niveau) s'applique toujours.
        sep = separators[-1]
        next_separators: List[str] = []
        for i, s in enumerate(separators):
            if s == "":
                sep = s
                break
            if s in text:
                sep = s
                next_separators = separators[i + 1:]
                break

        good: List[str] = []
        for piece in self._split_keep_separator(text, sep):
            if len(piece) < self.chunk_size:
                good.append(piece)
                continue
            if good:
                final.extend(self._merge(good))
                good = []
            if not next_separators:
                final.append(piece)            # indivisible : rendu tel quel
            else:
                final.extend(self._split(piece, next_separators))
        if good:
            final.extend(self._merge(good))
        return final

    def _merge(self, pieces: List[str]) -> List[str]:
        """Recombinaison avec recouvrement par morceaux entiers (langchain
        `_merge_splits`, séparateur de jointure vide). La fenêtre courante est
        une deque : le retrait en tête est O(1) là où langchain recopie la
        liste à chaque morceau retiré."""
        from collections import deque
        chunks: List[str] = []
        current: deque = deque()
        total = 0
        size, overlap = self.chunk_size, self.chunk_overlap
        for piece in pieces:
            n = len(piece)
            if total + n > size:
                if current:
                    joined = "".join(current).strip()
                    if joined:
                        chunks.append(joined)
                    # Glisser la fenêtre : on retire en tête tant que le
                    # recouvrement conservé dépasse `overlap`, ou tant que le
                    # morceau à venir ne tient pas.
                    while total > overlap or (total + n > size and total > 0):
                        total -= len(current.popleft())
            current.append(piece)
            total += n
        joined = "".join(current).strip()
        if joined:
            chunks.append(joined)
        return chunks


# =============================================================================
#  ENRICHISSEMENT CONTEXTUEL
# =============================================================================

def _context_prefix(family: str, filename: str, page: Optional[int]) -> str:
    """Ligne de contexte préfixant chaque chunk (§4.1)."""
    parts = [f"Famille : {family}", f"Document : {filename}"]
    if page is not None:
        parts.append(f"Page {page}")
    return " — ".join(parts)


def _split_pages(text: str) -> List[Optional[int]]:
    """
    Découpe le texte en (page_number, page_text). Reconnaît le form-feed des
    PDF et les marqueurs « --- Page N --- » / « --- Slide N --- » des OCR/PPTX.
    Retourne une liste de (num_page_ou_None, texte).
    """
    import re
    # Le form-feed fait AUTORITÉ quand il est présent (audit v2) : une entrée
    # par page physique. Sinon, un simple « --- Page N --- » LITTÉRAL dans le
    # contenu (page OCRisée fusionnée, citation dans le texte) prendrait le
    # dessus et déraillerait la numérotation de tout le document.
    if _PAGE_FEED in text:
        return [(idx + 1, chunk) for idx, chunk in enumerate(text.split(_PAGE_FEED))]

    # Marqueurs explicites de page/slide (OCR complet, PPTX)
    marker = re.compile(r"---\s*(?:Page|Slide)\s+(\d+)\s*---")
    if marker.search(text):
        pages: List = []
        parts = marker.split(text)
        # parts = [avant, num1, texte1, num2, texte2, ...]
        preamble = parts[0].strip()
        if preamble:
            pages.append((None, preamble))
        for i in range(1, len(parts), 2):
            num = int(parts[i])
            body = parts[i + 1] if i + 1 < len(parts) else ""
            pages.append((num, body))
        return pages

    return [(None, text)]


# =============================================================================
#  API PUBLIQUE
# =============================================================================

def chunk_document(
    text: str,
    path: str,
    family: str,
    ext: str = "",
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
    add_context_prefix: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
    doc_label: Optional[str] = None,
    force_no_page: bool = False,
) -> List[Chunk]:
    """
    Découpe le texte d'un document en chunks enrichis.

    Args:
        text: texte extrait du document
        path: chemin du fichier (pour métadonnées et citation)
        family: grand répertoire de tête (thème, §10.2)
        ext: extension du fichier
        chunk_size: taille cible des chunks (défaut 800)
        chunk_overlap: recouvrement entre chunks (défaut 120)
        add_context_prefix: préfixe chaque chunk d'une ligne de contexte
        metadata: métadonnées additionnelles à propager sur chaque chunk
        doc_label: libellé « Document : ... » du préfixe de contexte (défaut :
            nom du fichier). Sert aux pièces jointes (« x.pdf — PJ : note.docx »).
        force_no_page: force page=None sur tous les chunks (pièces jointes :
            leurs « pages » n'ont aucun sens dans le document de tête).

    Returns:
        Liste de Chunk, dans l'ordre du document.
    """
    if not text or not text.strip():
        return []

    chunk_size, chunk_overlap = _resolve_chunk_params(chunk_size, chunk_overlap)
    splitter = _get_splitter(chunk_size, chunk_overlap)
    filename = doc_label or os.path.basename(path)
    base_meta = dict(metadata or {})

    chunks: List[Chunk] = []
    position = 0

    for page_num, page_text in _split_pages(text):
        if force_no_page:
            page_num = None
        if not page_text.strip():
            continue

        for raw in splitter.split_text(page_text):
            content = raw.strip()
            if not content:
                continue

            if add_context_prefix:
                prefix = _context_prefix(family, filename, page_num)
                enriched = f"{prefix}\n{content}"
            else:
                enriched = content

            chunks.append(Chunk(
                text=enriched,
                content=content,
                path=path,
                family=family,
                ext=ext or os.path.splitext(path)[1].lower(),
                position=position,
                page=page_num,
                metadata=base_meta,
            ))
            position += 1

    return chunks


def chunk_extracted(extracted,
                    chunk_size: Optional[int] = None,
                    chunk_overlap: Optional[int] = None,
                    add_context_prefix: bool = True) -> List[Chunk]:
    """
    Variante prenant directement un `ExtractedDocument` (extraction.py).
    Signature EXPLICITE (audit v2) : un `**kwargs` transmis aux deux appels
    internes entrait en collision avec `metadata`/`doc_label`/`force_no_page`
    (TypeError dépendant des données — seulement si le document a des PJ).

    Les pièces jointes embarquées (metadata["attachments"] = [(nom, texte)])
    sont chunkées SÉPARÉMENT, avec **page=None** et sous le chemin du document
    de tête : la citation pointe le conteneur SANS numéro de page (les pages
    d'une pièce jointe n'ont aucun sens dans le conteneur — demande
    utilisateur / audit), et le clic ouvre le document de tête.
    """
    base_meta = {"extract_method": extracted.method, "ocr_used": extracted.ocr_used}
    chunks = chunk_document(
        text=extracted.text,
        path=extracted.path,
        family=extracted.family,
        ext=extracted.ext,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        add_context_prefix=add_context_prefix,
        metadata=base_meta,
    )
    filename = os.path.basename(extracted.path)
    for name, atext in (extracted.metadata or {}).get("attachments", []):
        chunks.extend(chunk_document(
            text=atext,
            path=extracted.path,                     # source = document de tête
            family=extracted.family,
            ext=extracted.ext,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            add_context_prefix=add_context_prefix,
            metadata={**base_meta, "attachment": name},
            doc_label=f"{filename} — Pièce jointe : {name}",
            force_no_page=True,                      # jamais de « page N »
        ))
    return chunks
