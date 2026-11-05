# retrieval.py
"""
Requête : retrieval hybride, fusion, reranking, regroupement - §6

Chaîne de requête (temps réel, lecture seule) :
    7. Retrieval hybride BM25 + FAISS      -> top 30-50   (§6.1)
    8. Reranking cross-encoder             -> top 8-12    (§6.2)
    9. Regroupement par thème / source                    (§6.3)

La fusion utilise la Reciprocal Rank Fusion (RRF), qui combine les rangs sans
normaliser des scores hétérogènes (§6.1). Le filtre par thème (famille) est
appliqué avant le retrieval (§10.2). Le reranking passe par l'API interne
bge-reranker-v2-m3 (§6.2, §12) ; en son absence, on conserve l'ordre RRF.
"""

import os
import sqlite3
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_RETRIEVE_K = 50      # top 30-50 combinés (§6.1)
DEFAULT_RERANK_K = 20        # extraits transmis au LLM (V2 ; spec initiale : 8-12)
RRF_K = 60                   # constante RRF (§6.1)

# --- Complétude de la réponse ------------------------------------------------
# Le reranking choisit les meilleurs extraits SANS regarder de quel document
# ils viennent : un gros document découpé en centaines de fragments pouvait à
# lui seul occuper les 12 places, et la réponse ne citait que lui alors que
# d'autres documents étaient pertinents. Un plafond par document force la
# couverture (0 = pas de plafond, comportement d'avant).
DEFAULT_MAX_PER_DOC = 3

# Facteur appliqué au score du dernier extrait retenu pour repérer les
# « presque retenus » : un candidat écarté dont le score reste au-dessus de ce
# seuil signale que la réponse est probablement partielle (§6.4).
_NEAR_MISS_RATIO = 0.9
# Marge ABSOLUE employée à la place du ratio quand le score de référence est
# hors de [0, 1] (audit V2, D-5) : un reranker peut renvoyer des logits (−3,2 ;
# 0,0 ; 4,8). Sur un score négatif, le ratio 0,9 produisait une tolérance
# nulle (seuil = référence) ; sur un très petit score positif, du bruit.
_NEAR_MISS_MARGIN = 0.5

# Taille des lots envoyés au reranker (audit V2, D-9) : une passerelle qui
# plafonne le nombre de textes par appel acceptait les 2 textes du calibrage
# puis refusait chaque appel réel (50 textes) — l'ordre RRF était présenté
# comme un re-tri. Réglable : RIVAGE_RERANK_BATCH.
DEFAULT_RERANK_BATCH = 32


def _seuil_comparable(ref: float) -> float:
    """Seuil en dessous duquel un score n'est plus « comparable » à `ref`.

    Espace stable (audit V2, D-5) : ratio 0,9 quand la référence est une
    probabilité (0 < ref ≤ 1), marge absolue de 0,5 sinon (logits, scores
    négatifs ou nuls). Centralisé ici pour que le signal d'incomplétude
    (`_near_miss_chunks`) et le critère d'arrêt de « Nourrir plus profond »
    (`query.approfondir`) appliquent exactement la même règle.
    """
    if ref <= 0 or ref > 1:
        return ref - _NEAR_MISS_MARGIN
    return ref * _NEAR_MISS_RATIO


def _env_int(name: str, default: int) -> int:
    """Lit une variable d'environnement entière sans faire échouer la requête."""
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning(f"[retrieval] {name}={val!r} non entier — "
                       f"valeur par défaut {default} utilisée.")
        return default


def _rerank_batch() -> int:
    """Taille de lot du reranking (RIVAGE_RERANK_BATCH, défaut 32, minimum 1)."""
    return max(1, _env_int("RIVAGE_RERANK_BATCH", DEFAULT_RERANK_BATCH))


# Lots de reranking envoyés EN PARALLÈLE (audit perf 2.1, L-7) : les lots
# étaient sérialisés — 13 allers-retours en série par tour d'approfondissement
# à vivier 400 (2,6 s à 200 ms de latence simulée ; 0,8 s à 4 lots en vol).
# Même valeur par défaut que la synthèse étagée (RIVAGE_SYNTH_WORKERS).
DEFAULT_RERANK_WORKERS = 3


def _rerank_workers() -> int:
    """Lots de reranking en vol simultanément (RIVAGE_RERANK_WORKERS, défaut 3,
    minimum 1 = séquentiel)."""
    return max(1, _env_int("RIVAGE_RERANK_WORKERS", DEFAULT_RERANK_WORKERS))


def _resolve_ks(retrieve_k: Optional[int],
                rerank_k: Optional[int]) -> Tuple[int, int]:
    """Résout la taille du vivier et le nombre d'extraits finaux.

    Un argument explicite (CLI, test) prime ; sinon RIVAGE_RETRIEVE_K /
    RIVAGE_RERANK_K ; sinon les défauts de la spec. Monter `rerank_k` est le
    levier le plus direct sur la complétude, mais chaque extrait supplémentaire
    allonge le prompt de synthèse — donc le coût de chaque question sur une
    passerelle facturée. Défaut 20 depuis la V2 (12 en V1) : environ
    2 000 tokens de plus par question, assumés au profit de la complétude.
    """
    rk = retrieve_k if retrieve_k is not None else _env_int(
        "RIVAGE_RETRIEVE_K", DEFAULT_RETRIEVE_K)
    nk = rerank_k if rerank_k is not None else _env_int(
        "RIVAGE_RERANK_K", DEFAULT_RERANK_K)
    return max(1, rk), max(1, nk)


def _max_per_doc() -> int:
    """Nombre maximal d'extraits provenant d'un même document (0 = illimité)."""
    return max(0, _env_int("RIVAGE_MAX_PER_DOC", DEFAULT_MAX_PER_DOC))


def _apply_doc_quota(chunks: List["RetrievedChunk"], k: int,
                     max_per_doc: int) -> List["RetrievedChunk"]:
    """Retient `k` extraits en limitant ceux issus d'un même document.

    L'ordre de pertinence est conservé. Le trop-plein n'est pas jeté : il
    complète la sélection si le quota ne suffit pas à atteindre `k` (corpus où
    peu de documents répondent). On ne renvoie donc JAMAIS moins d'extraits
    qu'avant l'introduction du quota — seulement mieux répartis.
    """
    if max_per_doc <= 0:
        return chunks[:k]
    retenus: List["RetrievedChunk"] = []
    surplus: List["RetrievedChunk"] = []
    par_doc: Dict[str, int] = {}
    for c in chunks:
        if len(retenus) >= k:
            break
        n = par_doc.get(c.chemin, 0)
        if n < max_per_doc:
            retenus.append(c)
            par_doc[c.chemin] = n + 1
        else:
            surplus.append(c)
    for c in surplus:
        if len(retenus) >= k:
            break
        retenus.append(c)
    return retenus


def _near_miss_chunks(ordonnes: List["RetrievedChunk"],
                      retenus: List["RetrievedChunk"]) -> List["RetrievedChunk"]:
    """Candidats écartés dont le score reste comparable au MOINS BON retenu —
    signal d'une réponse probablement partielle, ET matière première de
    l'empilement (« Empiler les écartés », §6.5).

    La référence est le score MINIMAL des retenus (audit V2, D-1), pas le
    dernier élément de la liste : quand le quota par document ne remplit pas
    `k`, le trop-plein (scores élevés) est ajouté APRÈS les retenus faibles,
    et `retenus[-1]` était alors un score élevé — un passage à 0,60 était
    écarté sans signal alors qu'un 0,40 était retenu. C'est aussi la
    référence de `query._score_ref` (critère d'arrêt de « Nourrir plus
    profond ») : les deux signaux restent cohérents.

    Sans score de reranking, aucun signal n'est émis : liste vide.
    """
    scores = [c.rerank_score for c in retenus if c.rerank_score is not None]
    if not scores:
        return []
    seuil = _seuil_comparable(min(scores))
    gardes = {id(c) for c in retenus}
    return [c for c in ordonnes
            if id(c) not in gardes
            and c.rerank_score is not None
            and c.rerank_score >= seuil]


def _near_misses(ordonnes: List["RetrievedChunk"],
                 retenus: List["RetrievedChunk"]) -> Tuple[int, int]:
    """(nombre d'extraits écartés comparables, nombre de documents distincts)."""
    proches = _near_miss_chunks(ordonnes, retenus)
    return len(proches), len({c.chemin for c in proches})



@dataclass
class RetrievedChunk:
    faiss_id: int
    texte: str
    chemin: str
    famille: str
    page: Optional[int]
    score: float = 0.0
    rerank_score: Optional[float] = None


@dataclass
class RetrievalResult:
    question: str
    chunks: List[RetrievedChunk]
    groups: Dict[str, List[RetrievedChunk]] = field(default_factory=dict)
    reranked: bool = False
    # Signal de complétude : candidats écartés dont le score restait
    # comparable au dernier extrait retenu (§6.4). Non nul = il reste
    # probablement de la matière, l'utilisateur peut élargir.
    near_miss_extraits: int = 0
    near_miss_documents: int = 0
    # Les écartés eux-mêmes (score comparable), pour les empiler sans refaire
    # la recherche (§6.5).
    ecartes: List[RetrievedChunk] = field(default_factory=list)


# =============================================================================
#  FUSION RRF (§6.1)
# =============================================================================

def reciprocal_rank_fusion(
    rankings: List[List[int]], k: int = RRF_K
) -> List[Tuple[int, float]]:
    """
    Reciprocal Rank Fusion : combine plusieurs classements par rang.
    `rankings` : listes d'ids ordonnés (ex. [ids_bm25, ids_faiss]).
    Retourne [(id, score)] trié par score décroissant.
    """
    scores: Dict[int, float] = {}
    for ranking in rankings:
        for rang, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rang)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# =============================================================================
#  RERANKER (API interne bge-reranker-v2-m3, §6.2)
# =============================================================================

def _rerank_body(style: str, model: str, query: str, documents: List[str],
                 top_k: int) -> dict:
    """Corps de requête de reranking selon le style de passerelle."""
    if style == "tei":
        # HuggingFace text-embeddings-inference : {query, texts} -> liste de scores
        return {"query": query, "texts": list(documents)}
    if style == "cohere-nomodel":
        return {"query": query, "documents": list(documents), "top_n": top_k}
    # "cohere" (défaut) : Cohere / Jina / OpenAI-like
    return {"model": model, "query": query,
            "documents": list(documents), "top_n": top_k}


def _parse_rerank(data, n_docs: int) -> Optional[List[Tuple[int, float]]]:
    """
    Normalise une réponse de reranking hétérogène en [(index_original, score)].
    Gère : {"results":[{index, relevance_score|score}]}, liste nue
    [{index, score|relevance_score}], {"scores":[...]} et liste nue de flottants
    (alignées sur l'ordre des documents). Retourne None si inexploitable.

    Prudence (audit) : des dicts SANS champ `index` ne sont interprétés comme
    « alignés sur l'ordre des documents » que si la liste couvre TOUS les
    documents (len == n_docs). Une liste tronquée sans index est très
    probablement TRIÉE PAR PERTINENCE (sémantique Cohere top_n) : l'alignement
    positionnel plaquerait les scores sur les mauvais candidats — on préfère
    retourner None (l'appelant conserve l'ordre RRF, comportement sûr).
    """
    items = data
    if isinstance(data, dict):
        items = data.get("results")
        if items is None and isinstance(data.get("scores"), list):
            items = data["scores"]
        if items is None:
            return None
    if not isinstance(items, list):
        return None
    dicts_without_index = any(
        isinstance(r, dict) and r.get("index") is None for r in items)
    if dicts_without_index and len(items) != n_docs:
        return None  # tronquée + sans index : attribution positionnelle risquée
    # Même garde pour les SCALAIRES (audit v2) : une liste nue de flottants
    # tronquée est très probablement triée par pertinence (top_n honoré) —
    # l'alignement positionnel plaquerait les scores sur les mauvais candidats.
    # TEI renvoie toujours un score par document (len == n_docs) : aucun format
    # légitime n'est refusé.
    has_scalars = any(isinstance(r, (int, float)) and not isinstance(r, bool)
                      for r in items)
    if has_scalars and len(items) != n_docs:
        return None
    out: List[Tuple[int, float]] = []
    for pos, r in enumerate(items):
        if isinstance(r, dict):
            idx = r.get("index")
            if idx is None:
                idx = pos  # liste complète : alignement positionnel plausible
            score = r.get("relevance_score", r.get("score"))
            if score is None:
                continue
        elif isinstance(r, (int, float)):
            idx, score = pos, r  # liste nue de scores alignée sur les documents
        else:
            continue
        try:
            i = int(idx)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_docs:
            out.append((i, float(score)))
    return out or None


# Endpoints/formats candidats, essayés dans l'ordre jusqu'au premier qui répond
# 200 avec un corps exploitable (calibrage, comme pour l'embedding). Chaque
# entrée est (suffixe_url, avec_v1, style). La passerelle interne expose souvent
# le reranker à la RACINE du modèle (sans /v1) façon TEI, d'où le 404 sur
# /v1/rerank. On sonde donc les deux emplacements et les deux formats.
_RERANK_CANDIDATES = [
    ("/rerank", True,  "cohere"),        # {base}/v1/rerank   (Cohere/Jina)
    ("/rerank", False, "tei"),           # {root}/rerank      (TEI)
    ("/rerank", True,  "tei"),           # {base}/v1/rerank   (TEI)
    ("/rerank", False, "cohere"),        # {root}/rerank      (Cohere)
    ("/rerank", True,  "cohere-nomodel"),
    ("/rerank", False, "cohere-nomodel"),
    ("/rerank/", False, "tei"),          # variantes avec slash final
    ("/rerank/", True,  "cohere"),
]

_RERANK_STYLE_PRESETS = {"cohere", "cohere-nomodel", "tei"}

# Cache PROCESSUS du calibrage (audit) : un RerankerClient est créé à CHAQUE
# question (HybridRetriever jetable) — sans cache, chaque question re-sonderait
# les endpoints (latence x8 si le service ne répond pas). Clé (api_base, model) ;
# valeur ("ok", url, style) ou ("disabled", ts) — un service en échec est
# re-sondé après _RERANK_DISABLED_RETRY_S (il a pu revenir).
_RERANK_CAL_LOCK = threading.Lock()
_RERANK_CAL_CACHE: Dict[tuple, tuple] = {}
_RERANK_DISABLED_RETRY_S = 300.0


class RerankerClient:
    """
    Client de reranking via l'API interne (§6.2). Optionnel.

    L'endpoint et le format exacts varient selon la passerelle : au premier
    appel, on CALIBRE en essayant plusieurs combinaisons (URL avec/sans `/v1`,
    format Cohere/TEI) et on retient la première acceptée. Forçage possible :
    RIVAGE_RERANK_URL (URL complète) et RIVAGE_RERANK_STYLE (cohere|tei|
    cohere-nomodel). En cas d'échec total, le client se désactive (available()
    devient faux) pour ne pas re-sonder à chaque requête — l'ordre RRF est
    conservé (§6.1).
    """

    def __init__(self, api_base=None, api_key=None, model=None, http_client=None):
        try:
            from models_utils import (
                RERANK_API_BASE, RERANK_MODEL, RERANK_API_KEY, create_http_client,
            )
        except ImportError:
            RERANK_API_BASE = RERANK_MODEL = RERANK_API_KEY = None
            create_http_client = None
        self.api_base = api_base or RERANK_API_BASE
        # Clé DÉDIÉE au reranker (RIVAGE_RERANK_KEY) — une clé par modèle.
        self.api_key = api_key or RERANK_API_KEY
        self.model = model or RERANK_MODEL
        self._own = http_client is None
        if http_client is None and create_http_client is not None:
            http_client = create_http_client()
        self.http = http_client
        # Spec calibrée : (url, style). None = pas encore calibré. L'état
        # « indisponible » vit dans le cache PROCESSUS (fenêtre de re-sondage),
        # jamais figé sur l'instance (audit v2 : un client longue durée doit
        # pouvoir re-sonder après la fenêtre).
        self._spec: Optional[Tuple[str, str]] = None
        forced_url = os.getenv("RIVAGE_RERANK_URL", "").strip()
        forced_style = os.getenv("RIVAGE_RERANK_STYLE", "").strip().lower()
        if forced_url:
            style = forced_style if forced_style in _RERANK_STYLE_PRESETS else "cohere"
            self._spec = (forced_url, style)

    def available(self) -> bool:
        if not (self.http and self.api_base and self.api_key):
            return False
        # Un endpoint FORCÉ (RIVAGE_RERANK_URL) ou déjà calibré prime sur la
        # fenêtre « indisponible » du cache (audit v2) : c'est précisément le
        # remède que le message d'échec recommande.
        if self._spec is not None:
            return True
        # Service marqué indisponible par une instance précédente : rester
        # silencieusement en RRF jusqu'à la fenêtre de re-sondage.
        cached = _RERANK_CAL_CACHE.get((self.api_base, self.model))
        if (cached and cached[0] == "disabled"
                and time.monotonic() - cached[1] < _RERANK_DISABLED_RETRY_S):
            return False
        return True

    def _root(self) -> str:
        """Racine du modèle (api_base sans le `/v1` final)."""
        base = self.api_base.rstrip("/")
        return base[:-3].rstrip("/") if base.endswith("/v1") else base

    def _candidate_urls(self):
        base = self.api_base.rstrip("/")
        root = self._root()
        for suffix, with_v1, style in _RERANK_CANDIDATES:
            prefix = base if with_v1 else root
            yield (prefix + suffix, style)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, url: str, style: str, query, documents, top_k,
              timeout: float = 60.0):
        body = _rerank_body(style, self.model, query, documents, top_k)
        return self.http.post(url, headers=self._headers(), json=body,
                              timeout=timeout)

    def _calibrate(self, log) -> None:
        """
        Sonde les candidats et adopte le premier exploitable. Sinon désactive.
        Le résultat (succès OU échec) est mémorisé au niveau PROCESSUS : les
        questions suivantes ne re-sondent pas (audit).
        """
        key = (self.api_base, self.model)
        with _RERANK_CAL_LOCK:
            cached = _RERANK_CAL_CACHE.get(key)
            if cached:
                if cached[0] == "ok":
                    self._spec = (cached[1], cached[2])
                    return
                if time.monotonic() - cached[1] < _RERANK_DISABLED_RETRY_S:
                    return  # fenêtre active : pas de re-sondage, _spec reste None
                # Fenêtre expirée : on retente (le service a pu revenir).
            probe_docs = ["texte de calibration numéro un", "second document témoin"]
            seen = set()
            for url, style in self._candidate_urls():
                if (url, style) in seen:
                    continue
                seen.add((url, style))
                try:
                    # Timeout de SONDE court (audit v2) : le timeout de service
                    # (60 s) appliqué à 8 sondes sous verrou bloquait la 1re
                    # question jusqu'à ~8 min sur un endpoint en trou noir.
                    resp = self._post(url, style, "calibration", probe_docs, 2,
                                      timeout=8.0)
                except Exception as e:
                    _raise_if_tls_rerank(e)
                    continue  # réseau : candidat suivant
                if resp.status_code >= 400:
                    continue  # 404/422 : mauvais endpoint/format, candidat suivant
                try:
                    parsed = _parse_rerank(resp.json(), len(probe_docs))
                except Exception:
                    parsed = None
                # Sonde STRICTE (audit v2) : exiger exactement les index {0, 1}.
                # Une passerelle 1-based renverrait {1, 2} (le 2 filtré) — un
                # calibrage laxiste décalerait ensuite tous les scores d'un cran.
                if parsed and {i for i, _ in parsed} == {0, 1}:
                    self._spec = (url, style)
                    _RERANK_CAL_CACHE[key] = ("ok", url, style)
                    log.info("[retrieval] reranking calibré : url=%s style=%s", url, style)
                    return
            _RERANK_CAL_CACHE[key] = ("disabled", time.monotonic())
            log.warning(
                "[retrieval] reranking indisponible : aucun endpoint/format accepté "
                "(essais /v1/rerank et /rerank, formats Cohere/TEI). Ordre RRF "
                f"conservé (nouvel essai dans {_RERANK_DISABLED_RETRY_S:.0f} s). "
                "Forcez si besoin RIVAGE_RERANK_URL / RIVAGE_RERANK_STYLE.")

    def rerank(self, query: str, documents: List[str], top_k: int, log=None) -> List[Tuple[int, float]]:
        """
        Reclasse `documents` pour `query`. Retourne [(index_original, score)]
        limité à top_k. Lève en cas d'échec (l'appelant conserve l'ordre RRF).

        Lotissement (audit V2, D-9) : les documents partent par lots de
        RIVAGE_RERANK_BATCH (32), RIVAGE_RERANK_WORKERS (3) lots en vol à la
        fois (audit perf 2.1, L-7), et les résultats sont FUSIONNÉS PAR SCORE —
        un cross-encoder note chaque paire (question, texte) indépendamment,
        les scores sont donc comparables d'un lot à l'autre. Chaque lot
        demande `top_k` résultats : tout élément du top-k global figure dans
        le top-k de son lot. Si UN lot échoue (HTTP, réseau), tout le re-tri
        est réputé indisponible (exception → `reranked=False` chez
        l'appelant) : un classement partiel mêlé d'ordre RRF serait
        présenté comme un re-tri. Un lot à la réponse inexploitable donne
        de même une liste vide (ordre RRF conservé).
        """
        _log = log or logger
        if self._spec is None:
            self._calibrate(_log)
        if self._spec is None:
            raise RuntimeError("reranking non calibré (aucun endpoint valide)")
        url, style = self._spec
        taille = _rerank_batch()
        debuts = list(range(0, len(documents), taille))

        def un_lot(debut: int):
            lot = list(documents[debut:debut + taille])
            resp = self._post(url, style, query, lot, min(top_k, len(lot)))
            resp.raise_for_status()
            return _parse_rerank(resp.json(), len(lot))

        # Lots en PARALLÈLE (audit perf 2.1, L-7) : `httpx.Client` est
        # thread-safe ; les résultats sont rangés par lot puis fusionnés dans
        # l'ordre d'origine (tri stable par score : les ex æquo gardent
        # l'ordre des documents, comme en séquentiel). Première erreur ->
        # les lots non commencés sont annulés et l'exception relevée sans
        # attendre ceux en vol (dont le résultat est ignoré).
        workers = min(_rerank_workers(), len(debuts))
        resultats: Dict[int, Optional[List[Tuple[int, float]]]] = {}
        if workers <= 1:
            for debut in debuts:
                parsed = un_lot(debut)
                if not parsed:
                    return []
                resultats[debut] = parsed
        else:
            ex = ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="rivage-rerank")
            try:
                futures = {ex.submit(un_lot, debut): debut for debut in debuts}
                for fut in as_completed(futures):
                    resultats[futures[fut]] = fut.result()   # relève l'erreur du lot
                    if not resultats[futures[fut]]:
                        break                                 # réponse inexploitable
            except BaseException:
                ex.shutdown(wait=False, cancel_futures=True)
                raise
            ex.shutdown(wait=False, cancel_futures=True)
            if any(not resultats.get(d) for d in debuts):
                return []
        merged: List[Tuple[int, float]] = []
        for debut in debuts:
            merged.extend((debut + i, s) for i, s in resultats[debut])
        merged.sort(key=lambda t: t[1], reverse=True)
        return merged[:top_k]

    def close(self):
        if self._own and self.http:
            try:
                self.http.close()
            except Exception:
                pass


def _raise_if_tls_rerank(e: Exception) -> None:
    """Message actionnable si l'exception est une erreur de certificat TLS."""
    msg = str(e).lower()
    if "certificate" in msg or "certificat" in msg or "ssl" in msg:
        raise RuntimeError(
            "Certificat TLS de l'API interne non validé (CERTIFICATE_VERIFY_FAILED). "
            "Renseignez DALLEM_CA_BUNDLE (bundle CA .pem) ou, en repli, "
            f"DALLEM_VERIFY_SSL=false. Détail : {e}"
        )


# =============================================================================
#  RETRIEVER HYBRIDE
# =============================================================================

class HybridRetriever:
    """
    Retrieval hybride BM25 + FAISS avec fusion RRF, reranking et regroupement.

    Ouvre les index en lecture seule (§8.5). `from_index_dir` vérifie à chaque
    requête la génération publiée (manifest) et obtient ses ressources du
    cache lecteur (index_cache) : index et portée précalculée en mémoire,
    connexions au registre et au magasin prêtées puis rendues par `close()`.
    """

    def __init__(self, vector_index, lexical_index, registry_ro,
                 embedder, reranker: Optional[RerankerClient] = None,
                 content_store=None, own_embedder: bool = False,
                 own_reranker: bool = False, portee=None, lecteur=None):
        self.vindex = vector_index
        self.lindex = lexical_index
        self.registry = registry_ro
        self.embedder = embedder
        self.reranker = reranker
        self.content_store = content_store
        # Ownership (audit) : close() ne doit fermer QUE ce que le retriever a
        # créé lui-même. Fermer un embedder/reranker INJECTÉ par l'appelant le
        # casserait pour la question suivante (dégradation silencieuse en BM25
        # seul) — même motif que own_synth dans query.answer_question.
        self._own_embedder = own_embedder
        self._own_reranker = own_reranker
        # Portée précalculée (audit perf 2.1, L-1) : celle du cache lecteur,
        # ou construite à la demande depuis le registre (retriever assemblé
        # à la main, hors cache).
        self._portee = portee
        # Ressources EMPRUNTÉES au cache lecteur (L-9) : `close()` les rend au
        # pool au lieu de les fermer ; sans `lecteur`, registre et magasin
        # appartiennent à ce retriever et sont fermés.
        self._lecteur = lecteur
        self._casse = False          # paire à jeter à la restitution (RB-1)

    @classmethod
    def from_index_dir(cls, index_dir: str, embedder=None, reranker=None, log=None):
        """Charge la génération publiée (via le manifest) depuis le stockage réseau."""
        own_embedder = embedder is None
        own_reranker = reranker is None
        lecteur = None
        created_embedder = created_reranker = None
        try:
            # Cache par GÉNÉRATION (réseau lent, base volumineuse) : FAISS,
            # BM25 et portée précalculée ne sont rechargés que si l'indexeur
            # a publié une nouvelle génération — sinon réutilisés depuis la
            # mémoire (et, au premier chargement, depuis le miroir disque
            # local). Registre et magasin de contenu sont EMPRUNTÉS au pool de
            # connexions (audit perf 2.1, L-9 : ils étaient rouverts à chaque
            # question, et le manifest lu deux fois) ; `close()` les rend.
            # Voir index_cache.
            from index_cache import get_lecteur, rendre_lecteur
            lecteur = get_lecteur(index_dir, log=log)

            if embedder is None:
                # Profil INTERACTIF (audit V2, C-1) : from_index_dir sert le
                # lecteur (GUI, CLI query) ; les retries patients de
                # l'indexeur retardaient le repli BM25 de plusieurs minutes.
                from embeddings import EmbeddingClient
                embedder = created_embedder = EmbeddingClient(interactive=True)
            if reranker is None:
                reranker = created_reranker = RerankerClient()
        except Exception:
            # Rendre / fermer les ressources déjà obtenues avant de relever
            # (pas de fuite de handles, qui gênerait un os.replace de
            # l'indexeur sur SMB). Les clients créés LOCALEMENT sont aussi
            # fermés (sockets).
            if lecteur is not None:
                rendre_lecteur(lecteur)
            for res in (created_embedder, created_reranker):
                try:
                    if res is not None:
                        res.close()
                except Exception:
                    pass
            raise

        return cls(lecteur.vindex, lecteur.lindex, lecteur.registry, embedder,
                   reranker, lecteur.content_store,
                   own_embedder=own_embedder, own_reranker=own_reranker,
                   portee=lecteur.portee, lecteur=lecteur)

    @property
    def portee(self):
        """La portée précalculée (`registry.Portee`) de la génération —
        construite ici depuis le registre si elle n'est pas venue du cache."""
        if self._portee is None:
            self._portee = self.registry.portee()
        return self._portee

    def masque_portee(self, families=None, path_prefixes=None,
                      allowed_ids=None, allowed_paths=None):
        """Masque numpy (aligné sur `portee.faiss_ids`) des fragments
        interrogeables, ou None = tout le corpus (audit perf 2.1, L-1 /
        L-11 : plus de `set` Python ni de requête SQL par question).
        `path_prefixes` prime sur `families` ; `allowed_paths` (documents
        d'une base de recherche, par chemin) et `allowed_ids` (identifiants,
        API historique) s'intersectent avec la portée. Un ensemble vide
        donné explicitement donne un masque vide (aucun résultat, jamais
        « tout le corpus »)."""
        masque = None
        if path_prefixes:
            masque = self.portee.masque_prefixes(path_prefixes)
        elif families:
            masque = self.portee.masque_familles(families)
        if allowed_paths is not None:
            m = self.portee.masque_chemins(allowed_paths)
            masque = m if masque is None else (masque & m)
        if allowed_ids is not None:
            m = self.portee.masque_ids(allowed_ids)
            masque = m if masque is None else (masque & m)
        return masque

    def retrieve(
        self,
        question: str,
        families: Optional[List[str]] = None,
        path_prefixes: Optional[List[str]] = None,
        retrieve_k: Optional[int] = None,
        rerank_k: Optional[int] = None,
        exclude_ids: Optional[set] = None,
        allowed_ids=None,
        log=None,
        qvec=None,
        allowed_paths=None,
    ) -> RetrievalResult:
        """
        Exécute la chaîne de requête (§6) : hybride -> RRF -> rerank -> groupe.

        Portée : `path_prefixes` (répertoires ciblés, à toute profondeur) prime
        sur `families` (thèmes de 1er niveau). Sans l'un ni l'autre = tout le
        corpus. `allowed_paths` : restriction supplémentaire aux fragments des
        documents donnés par CHEMIN — les documents d'une base de recherche
        (mode auto-RAG, §6.7), résolus par la portée précalculée (audit perf
        2.1, L-1) ; `allowed_ids` : la même restriction par identifiants de
        fragments (set, liste ou tableau — API historique). Chacune se
        combine (intersection) avec la portée.

        `retrieve_k` / `rerank_k` à None : résolus par RIVAGE_RETRIEVE_K /
        RIVAGE_RERANK_K, sinon défauts de la spec (50 / 20).
        `exclude_ids` : identifiants d'extraits déjà connus (panier) retirés des
        candidats AVANT le reranking — c'est le moteur d'« Approfondir » (§6.5).
        `qvec` : vecteur de la question DÉJÀ calculé (audit perf 2.1, L-8 ;
        None = embedding ici).

        Deux étapes réutilisables séparément (« Nourrir plus profond » les
        enchaîne sur des tranches croissantes d'un même vivier) :
        `candidats` (recherche hybride, fusion, hydratation) puis `reclasser`
        (exclusion, reranking, quota, signal de complétude).
        """
        _log = log or logger
        retrieve_k, rerank_k = _resolve_ks(retrieve_k, rerank_k)
        candidates = self.candidats(
            question, families=families, path_prefixes=path_prefixes,
            retrieve_k=retrieve_k, allowed_ids=allowed_ids, log=_log, qvec=qvec,
            allowed_paths=allowed_paths)
        if not candidates:
            return RetrievalResult(question=question, chunks=[])
        return self.reclasser(question, candidates, retrieve_k=retrieve_k,
                              rerank_k=rerank_k, exclude_ids=exclude_ids, log=_log)

    def candidats(
        self,
        question: str,
        families: Optional[List[str]] = None,
        path_prefixes: Optional[List[str]] = None,
        retrieve_k: Optional[int] = None,
        allowed_ids=None,
        log=None,
        qvec=None,
        allowed_paths=None,
    ) -> List[RetrievedChunk]:
        """Étapes 7a-7c : portée, recherche hybride (FAISS top-k, BM25 top-k),
        fusion RRF, hydratation (métadonnées du registre, texte du magasin).
        Retourne les candidats dans l'ordre RRF (score = score RRF), sans les
        extraits au texte introuvable ; liste vide si la portée est vide."""
        _log = log or logger
        retrieve_k, _ = _resolve_ks(retrieve_k, None)

        # Pré-filtre de portée appliqué avant le retrieval (§10.2) : un MASQUE
        # numpy de la portée précalculée (audit perf 2.1, L-1 / L-11), injecté
        # DANS chaque index (recall exact) — FAISS reçoit les identifiants
        # triés (sélecteur natif, bitmap sur une portée large), BM25 le masque
        # transposé sur ses propres `faiss_ids` (aucun `np.isin`). Plus de
        # requête SQL ni de `set` Python par question.
        masque = self.masque_portee(families=families, path_prefixes=path_prefixes,
                                    allowed_ids=allowed_ids, allowed_paths=allowed_paths)
        if masque is not None and not masque.any():
            return []
        allowed_faiss = None if masque is None else self.portee.ids(masque)

        # 7a. Vectoriel (FAISS). Si l'embedding de la question échoue (API
        # interne indisponible), on dégrade sur BM25 seul plutôt que d'échouer :
        # le lexical attrape déjà les termes exacts (§5.2). Un vecteur déjà
        # calculé (`qvec`) évite de ré-embedder la même question (L-8).
        faiss_ranked: List[int] = []
        if self.vindex is not None:
            try:
                if qvec is None:
                    qvec = self.embedder.embed_query(question, log=_log)
                faiss_ranked = [
                    fid for fid, _ in self.vindex.search(qvec, retrieve_k,
                                                         allowed_ids=allowed_faiss)
                ]
            except Exception as e:
                _log.warning(
                    f"[retrieval] embedding de la requête indisponible, "
                    f"repli sur BM25 seul : {e}"
                )

        # 7b. Lexical (BM25)
        bm25_ranked: List[int] = []
        if self.lindex is not None:
            allowed_bm25 = (None if masque is None
                            else self.portee.masque_pour(self.lindex.faiss_ids, masque))
            bm25_ranked = [
                fid for fid, _ in self.lindex.search(question, retrieve_k,
                                                     allowed_ids=allowed_bm25)
            ]

        # 7c. Fusion RRF (§6.1)
        fused = reciprocal_rank_fusion([bm25_ranked, faiss_ranked])
        if not fused:
            return []

        # Hydrater : métadonnées depuis le registre, texte depuis le magasin de
        # contenu (le registre ne stocke plus le texte, S1).
        fused_ids = [fid for fid, _ in fused]
        try:
            records = self.registry.get_chunks_by_faiss_ids(fused_ids)
            texts = self.content_store.get_texts(records.keys()) if self.content_store else {}
        except sqlite3.Error:
            # Connexion empruntée au pool devenue inutilisable (hoquet réseau,
            # handle invalidé) : à JETER à la restitution, pas à resservir
            # (revue 2.5, RB-1).
            self._casse = True
            raise
        candidates: List[RetrievedChunk] = []
        empty = 0
        for fid, score in fused:
            rec = records.get(fid)
            if not rec:
                continue
            texte = texts.get(fid, "")
            if not texte.strip():
                # Texte introuvable (segment manquant / incohérence) : écarter le
                # candidat plutôt que d'envoyer un extrait vide au reranking et à
                # la synthèse (M2).
                empty += 1
                continue
            candidates.append(RetrievedChunk(
                faiss_id=fid, texte=texte, chemin=rec["chemin"],
                famille=rec["famille"], page=rec["page"], score=score,
            ))
        if empty:
            _log.warning(
                f"[retrieval] {empty} extrait(s) au texte introuvable écarté(s) "
                f"(segment de contenu manquant ?)."
            )
        # Les noyaux FAISS et BM25 ont tourné sans mourir : le témoin de
        # chargement peut être effacé (V2.7, lot 3).
        try:
            import plateforme
            plateforme.marquer_recherche_reussie()
        except Exception:
            pass
        return candidates

    def reclasser(
        self,
        question: str,
        candidates: List[RetrievedChunk],
        retrieve_k: Optional[int] = None,
        rerank_k: Optional[int] = None,
        exclude_ids: Optional[set] = None,
        log=None,
    ) -> RetrievalResult:
        """Étapes 8-9 sur des candidats déjà hydratés (ordre RRF) : exclusion
        des extraits déjà empilés, reranking du vivier `candidates[:retrieve_k]`,
        quota par document, regroupement, signal de complétude. Les objets
        `RetrievedChunk` reçoivent leur `rerank_score` (ré-écrit à chaque
        appel : un même candidat peut être reclassé dans plusieurs viviers)."""
        _log = log or logger
        retrieve_k, rerank_k = _resolve_ks(retrieve_k, rerank_k)
        if exclude_ids:
            avant = len(candidates)
            candidates = [c for c in candidates if c.faiss_id not in exclude_ids]
            _log.info(f"[retrieval] {avant - len(candidates)} candidat(s) déjà "
                      f"empilé(s) exclu(s) avant reranking")
        if not candidates:
            return RetrievalResult(question=question, chunks=[])

        # 8. Reranking cross-encoder (§6.2)
        # Pool plafonné à retrieve_k (audit) : la fusion peut produire jusqu'à
        # ~2×retrieve_k candidats ; la spec prévoit un rerank sur le top 30-50,
        # pas sur le double (coût/latence du cross-encoder).
        rerank_pool = candidates[:max(retrieve_k, rerank_k)]
        reranked = False
        max_doc = _max_per_doc()
        # Le quota par document a besoin de matière : on demande au reranker un
        # classement PLUS LARGE que les `rerank_k` finaux (même appel, même
        # coût — seule la liste renvoyée est plus longue), puis on sélectionne
        # nous-mêmes en répartissant par document.
        demande = min(len(rerank_pool),
                      rerank_k * 3 if max_doc > 0 else rerank_k)
        ordonnes = candidates[:]          # ordre RRF par défaut
        top = _apply_doc_quota(ordonnes, rerank_k, max_doc)
        if self.reranker and self.reranker.available() and rerank_pool:
            try:
                order = self.reranker.rerank(
                    question, [c.texte for c in rerank_pool], demande, log=_log
                )
                # Une réponse 200 mal formée (clé absente, liste vide) donne un
                # `order` vide : on ne doit PAS renvoyer 0 résultat dans ce cas,
                # mais conserver l'ordre RRF (M2).
                reordered = []
                for idx, rscore in order:
                    # Les index renvoyés se réfèrent aux documents ENVOYÉS
                    # (rerank_pool), pas à la liste complète des candidats.
                    if 0 <= idx < len(rerank_pool):
                        c = rerank_pool[idx]
                        c.rerank_score = rscore
                        reordered.append(c)
                # Réponse vide, OU non vide mais dont AUCUN index n'est valide
                # (décalage 1-based/0-based côté serveur) : on ne renvoie pas 0
                # résultat, on conserve l'ordre RRF (M2/n1).
                if reordered:
                    ordonnes = reordered
                    top = _apply_doc_quota(ordonnes, rerank_k, max_doc)
                    reranked = True
                else:
                    _log.warning(
                        "[retrieval] reranking : réponse vide/inexploitable, "
                        "ordre RRF conservé"
                    )
            except Exception as e:
                _log.warning(f"[retrieval] reranking indisponible, ordre RRF conservé: {e}")

        # 9. Regroupement par thème / source (§6.3)
        groups = _group_by_source(top)

        # Signal de complétude (§6.4) : y avait-il d'autres passages aussi bons
        # que le dernier retenu ? Calculé sur le classement du reranker. Les
        # écartés sont conservés pour l'empilement (§6.5).
        ecartes = _near_miss_chunks(ordonnes, top)
        nm_extraits, nm_documents = len(ecartes), len({c.chemin for c in ecartes})
        if nm_extraits:
            _log.info(f"[retrieval] {nm_extraits} passage(s) écarté(s) de score "
                      f"comparable, dans {nm_documents} document(s) — réponse "
                      f"possiblement partielle")

        return RetrievalResult(question=question, chunks=top, groups=groups,
                               reranked=reranked,
                               near_miss_extraits=nm_extraits,
                               near_miss_documents=nm_documents,
                               ecartes=ecartes)

    def close(self):
        if self._lecteur is not None:
            # Registre et magasin EMPRUNTÉS au cache lecteur (L-9) : rendus au
            # pool (ouverts pour la question suivante), jamais fermés ici —
            # sauf si une erreur SQLite les a rendus suspects (RB-1).
            from index_cache import rendre_lecteur
            rendre_lecteur(self._lecteur, jeter=getattr(self, "_casse", False))
        else:
            try:
                self.registry.close()
            except Exception:
                pass
            if self.content_store is not None:
                try:
                    self.content_store.close()
                except Exception:
                    pass
        # Ne fermer que ce que le retriever a créé (audit) : un client injecté
        # appartient à l'appelant et doit survivre à ce retriever.
        if self._own_reranker and self.reranker:
            self.reranker.close()
        if self._own_embedder and hasattr(self.embedder, "close"):
            self.embedder.close()


def _group_by_source(chunks: List[RetrievedChunk]) -> Dict[str, List[RetrievedChunk]]:
    """Regroupe les chunks par document source (§6.3), ordre préservé."""
    groups: Dict[str, List[RetrievedChunk]] = {}
    for c in chunks:
        groups.setdefault(c.chemin, []).append(c)
    return groups
