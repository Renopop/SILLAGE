# embeddings.py
"""
Client d'embedding - passerelle LiteLLM, snowflake-arctic-embed-l-v2-ft (§5.1, §12)

Chaque chunk est transformé en vecteur par le modèle d'embedding, servi par
la passerelle interne (le corpus ne sort pas de l'organisation, §5.1). Les
vecteurs sont ensuite indexés localement dans FAISS.

Robustesse réseau à l'indexation (§5.1, §12.2) : une indexation massive
enchaîne des milliers d'appels et peut déclencher des 504 Gateway Timeout.
Trois protections implémentées ici :
  - batching de plusieurs chunks par requête,
  - retry avec backoff exponentiel sur les échecs,
  - (la reprise incrémentale est portée par le registre §8).

Conventions de préfixe SELON LE MODÈLE (résolues dans __init__) :
  - snowflake-arctic-embed : requêtes « query: {query} », passages sans préfixe ;
  - e5-instruct : gabarit « Instruct: ... Query: ... », passages sans préfixe.
La longueur d'entrée est bornée (limite servie : 1024 tokens pour
snowflake-arctic-embed-l-v2-ft) — voir _DEFAULT_MAX_CHARS / RIVAGE_EMBED_MAXCHARS.
"""

import os
import random
import time
import logging
import threading
from typing import List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)


def _body_snippet(resp, limit: int = 600) -> str:
    """Extrait un extrait lisible du corps d'une réponse HTTP (diagnostic)."""
    try:
        text = (resp.text or "").strip()
    except Exception:
        return "(corps illisible)"
    text = " ".join(text.split())
    return text[:limit] if text else "(corps vide)"


def _raise_if_tls(e: Exception) -> None:
    """Lève un message actionnable si l'exception est une erreur de certificat TLS."""
    msg = str(e).lower()
    if "certificate" in msg or "certificat" in msg or "ssl" in msg:
        raise RuntimeError(
            "Certificat TLS de l'API interne non validé (CERTIFICATE_VERIFY_FAILED). "
            "Renseignez DALLEM_CA_BUNDLE (chemin d'un bundle CA interne .pem) ou, en "
            "repli, DALLEM_VERIFY_SSL=false — dans l'environnement qui lance "
            f"l'application. Détail : {e}"
        )


# Formats de requête candidats : (champ, inclure_model, par_lot).
# Ordre = du plus standard au plus spécifique ; le calibrage retient le premier
# accepté par le serveur (200 + corps exploitable).
_CANDIDATE_FORMATS = [
    ("input",  True,  True),    # {"model": M, "input": [textes]}      OpenAI (lot)
    ("input",  False, True),    # {"input": [textes]}
    ("input",  True,  False),   # {"model": M, "input": "texte"}       OpenAI (1 texte)
    ("input",  False, False),   # {"input": "texte"}
    ("inputs", False, True),    # {"inputs": [textes]}                 TEI (lot)
    ("inputs", False, False),   # {"inputs": "texte"}                  TEI (1 texte)
]

_FORMAT_PRESETS = {
    "openai": ("input", True, True),
    "openai-str": ("input", True, False),
    "input": ("input", False, True),
    "input-str": ("input", False, False),
    "tei": ("inputs", False, True),
    "tei-str": ("inputs", False, False),
}


def _fmt_from_env():
    """Format forcé via RIVAGE_EMBED_FORMAT (sinon None -> calibrage auto)."""
    name = os.getenv("RIVAGE_EMBED_FORMAT", "").strip().lower()
    if name and name not in _FORMAT_PRESETS:
        # Visible (audit) : une faute de frappe basculerait silencieusement en
        # calibrage auto, et l'utilisateur croirait son réglage appliqué.
        logger.warning(
            f"[embeddings] RIVAGE_EMBED_FORMAT={name!r} inconnu — calibrage "
            f"automatique. Valeurs valides : {', '.join(sorted(_FORMAT_PRESETS))}.")
    return _FORMAT_PRESETS.get(name)


def _env_int(name: str) -> Optional[int]:
    val = os.getenv(name)
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None


# Cache PROCESSUS du calibrage (audit) : un EmbeddingClient est créé à CHAQUE
# question côté lecteur — sans cache, chaque question re-sonderait les formats
# candidats (jusqu'à 6 POST, et plusieurs timeouts si l'endpoint est injoignable).
# Clé (api_base, model) ; valeur {"fmt": tuple|None, "max_chars": int|None,
# "failed_ts": float|None}. Un échec total est mémorisé et re-sondé après
# _EMBED_FAILED_RETRY_S (le service a pu revenir).
_EMBED_CAL_LOCK = threading.Lock()
_EMBED_CAL_CACHE: dict = {}
_EMBED_FAILED_RETRY_S = 300.0

# Plafond PROACTIF de caractères par entrée quand ni RIVAGE_EMBED_MAXCHARS ni
# le calibrage n'ont fixé de valeur. Le modèle servi par la passerelle
# (snowflake-arctic-embed-l-v2-ft) accepte 1024 tokens max ; en français,
# ~3 caractères/token => 3000 caractères laissent une marge sûre. Les chunks
# RIVAGE (800 caractères, §4.1) passent très en dessous — seuls les textes
# hors norme sont tronqués (vecteur seulement : BM25 et l'affichage gardent
# le texte complet).
_DEFAULT_MAX_CHARS = 3000


def _is_context_error(body: str) -> bool:
    """Vrai si le corps d'erreur indique un dépassement de la longueur de contexte.
    (« max_tokens » retiré — audit v2 : il matchait des 400 de paramétrage sans
    rapport et déclenchait divisions/réductions au lieu d'un échec clair.)"""
    b = (body or "").lower()
    return ("context length" in b or "maximum context" in b
            or "input_tokens" in b or "input tokens" in b
            or "too long" in b)


def _char_cap_from_error(sent_texts, body: str, current_cap: Optional[int]) -> Optional[int]:
    """
    Calcule un plafond de caractères par entrée à partir du message d'erreur
    (« maximum context length is 512 tokens … has 513 input tokens »), avec une
    marge de sécurité. Repli : réduction de 20 % du plus long.
    """
    import re
    longest = max((len(t) for t in sent_texts), default=0)
    if not longest:
        return None
    m_max = re.search(r"context length is\s*(\d+)", body, re.I) \
        or re.search(r"max(?:imum)?[^0-9]{0,20}(\d{2,6})\s*token", body, re.I)
    m_got = re.search(r"has\s*(\d+)\s*input token", body, re.I) \
        or re.search(r"value\s*=\s*(\d+)", body, re.I) \
        or re.search(r"(\d+)\s*token", body, re.I)
    if m_max and m_got:
        maxtok, gottok = int(m_max.group(1)), int(m_got.group(1))
        if gottok > 0 and maxtok > 0:
            chars_per_token = longest / gottok
            return max(200, int(maxtok * chars_per_token * 0.85))
    # Repli : on ne sait pas parser -> on réduit prudemment.
    base = current_cap or longest
    return max(200, int(base * 0.8))

try:
    from models_utils import (
        EMBED_API_BASE, EMBED_MODEL, EMBED_API_KEY, create_http_client,
        is_transport_error,
    )
    _CONFIG_OK = True
except ImportError:
    _CONFIG_OK = False
    EMBED_API_BASE = None
    EMBED_MODEL = None
    EMBED_API_KEY = None
    create_http_client = None

    def is_transport_error(e: BaseException) -> bool:   # repli sans models_utils
        import socket
        return isinstance(e, (socket.timeout, TimeoutError, ConnectionError))


DEFAULT_BATCH_SIZE = 32
# Taille de lot et lots en vol RÉGLABLES (audit V2, C-13) : les lots étaient
# strictement séquentiels (taille 32 figée) — la passerelle attend pendant
# que le client prépare le lot suivant. RIVAGE_EMBED_PARALLEL lots sont
# envoyés en parallèle (défaut 2, plafond 8) ; l'ORDRE des vecteurs rendus
# est celui des textes. RIVAGE_EMBED_BATCH règle la taille (défaut 32).
DEFAULT_PARALLEL = 2
_MAX_PARALLEL = 8
# Retries alignés sur ceux de l'OCR (504 = passerelle SATURÉE : il faut des
# minutes de patience, pas des secondes) : 8 tentatives par défaut
# (RIVAGE_EMBED_RETRIES), base 2 s (RIVAGE_EMBED_RETRY_BASE_S) ×3 sur
# congestion HTTP, délais PLAFONNÉS à 60 s, jitter ±30 %. Une réindexation
# complète ne doit plus s'arrêter pour un épisode de congestion passager.
# `max_retries` = nombre TOTAL de tentatives, identique pour les erreurs
# réseau et les statuts HTTP re-tentables (audit V2, C-11 : les HTTP avaient
# une tentative de moins que le journal n'annonçait).
DEFAULT_MAX_RETRIES = 8
DEFAULT_BASE_DELAY = 2.0
DEFAULT_TIMEOUT_S = 60.0
RETRIABLE_STATUS = (502, 503, 504, 429)
_RETRY_CAP_S = 60.0
_CONGESTION_FACTOR = 3.0

# Profil INTERACTIF (audit V2, C-1) : le lecteur (GUI, CLI query) n'a pas la
# patience de l'indexeur. Avec le profil patient, une passerelle saturée
# faisait attendre jusqu'à 12 minutes (8 tentatives, délais plafonnés à 60 s,
# timeout 60 s) avant le repli BM25 — pourtant disponible immédiatement. En
# interactif : 2 tentatives, base ~1 s, timeout 20 s, soit moins d'une minute
# dans le pire cas. Les variables RIVAGE_EMBED_RETRIES / RETRY_BASE_S (pensées
# pour l'indexeur) ne s'appliquent pas à ce profil ; un argument explicite
# garde la priorité.
INTERACTIVE_MAX_RETRIES = 2
INTERACTIVE_BASE_DELAY = 1.0
INTERACTIVE_TIMEOUT_S = 20.0


def _retry_delay(base_delay: float, attempt: int, congestion: bool) -> float:
    """Délai exponentiel plafonné, base ×3 sur congestion, jitter ±30 %."""
    base = base_delay * (_CONGESTION_FACTOR if congestion else 1.0)
    return min(_RETRY_CAP_S, base * (2 ** (attempt - 1))) * random.uniform(0.7, 1.3)


def _retry_after_s(resp) -> Optional[float]:
    """Valeur de l'en-tête `Retry-After` (secondes ou date HTTP) d'une réponse
    429/503, en secondes — None si absent ou illisible (audit V2, C-11 : la
    passerelle dit QUAND revenir, l'ignorer entretient la congestion)."""
    try:
        headers = getattr(resp, "headers", None)
        raw = headers.get("Retry-After") if headers is not None else None
    except Exception:
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
    except Exception:
        return None


def _env_parallel() -> int:
    """Lots d'embedding en vol (RIVAGE_EMBED_PARALLEL, 1..8, défaut 2)."""
    v = _env_int("RIVAGE_EMBED_PARALLEL")
    if v is None:
        return DEFAULT_PARALLEL
    return max(1, min(_MAX_PARALLEL, v))


def taille_tampon_indexeur(embedder=None) -> int:
    """Textes accumulés par l'indexeur avant un appel d'embedding GROUPÉ
    (audit perf 2.1, I-3 : un appel par fichier, 7 textes en moyenne,
    laissait les lots et le parallélisme inemployés). Par défaut lot × lots
    en vol — `batch_size` × `parallel` du client s'il les expose, sinon
    RIVAGE_EMBED_BATCH × RIVAGE_EMBED_PARALLEL (32 × 2 = 64) — pour qu'un
    appel groupé remplisse exactement les lots en vol. RIVAGE_EMBED_BUFFER
    impose une valeur (1 = un appel par fichier, comportement antérieur)."""
    v = _env_int("RIVAGE_EMBED_BUFFER")
    if v is not None:
        return max(1, v)
    batch = getattr(embedder, "batch_size", None)
    if not batch:
        batch = _env_int("RIVAGE_EMBED_BATCH") or DEFAULT_BATCH_SIZE
    parallel = getattr(embedder, "parallel", None)
    if not parallel:
        parallel = _env_parallel()
    return max(1, int(batch) * int(parallel))


def _normalize(vecs: np.ndarray) -> np.ndarray:
    """Normalise L2 chaque vecteur (produit scalaire = cosinus, §5.1)."""
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).astype("float32")


class EmbeddingClient:
    """
    Client d'embedding via l'API interne, avec batching et retry/backoff.
    """

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        http_client=None,
        # None = RIVAGE_EMBED_BATCH (défaut 32) ; lots en vol : voir `parallel`.
        batch_size: Optional[int] = None,
        max_retries: Optional[int] = None,
        base_delay: Optional[float] = None,
        normalize: bool = True,
        # Les modèles d'embedding retrieval sont ASYMÉTRIQUES : les requêtes
        # portent un préfixe/gabarit, les passages sont bruts (AUCUN préfixe).
        # Se tromper de convention dégrade le recall (M9). Surchargeable si la
        # gateway applique elle-même le formatage. query_template=None =>
        # résolu selon le MODÈLE : les snowflake-arctic attendent
        # « query: {query} », les e5-instruct « Instruct: ... Query: ... ».
        passage_prefix: str = "",
        query_instruction: str = (
            "Étant donné une question technique, retrouver les passages de "
            "documentation pertinents qui y répondent."
        ),
        query_template: Optional[str] = None,
        # Profil lecteur (audit V2, C-1) : retries courts, timeout 20 s.
        interactive: bool = False,
        timeout: Optional[float] = None,
        # Lots en vol (audit V2, C-13) : None = RIVAGE_EMBED_PARALLEL (défaut 2).
        parallel: Optional[int] = None,
    ):
        self.api_base = (api_base or EMBED_API_BASE)
        # Clé DÉDIÉE au modèle d'embedding (RIVAGE_EMBED_KEY) — une clé par
        # modèle sur la passerelle, pas de clé commune.
        self.api_key = api_key or EMBED_API_KEY
        self.model = model or EMBED_MODEL
        self._own_client = http_client is None
        if http_client is None:
            if not _CONFIG_OK or create_http_client is None:
                raise RuntimeError(
                    "models_utils indisponible : impossible de créer le client HTTP."
                )
            http_client = create_http_client()
        self.http = http_client
        if batch_size is None:
            batch_size = _env_int("RIVAGE_EMBED_BATCH") or DEFAULT_BATCH_SIZE
        self.batch_size = max(1, int(batch_size))
        self.parallel = (max(1, min(_MAX_PARALLEL, int(parallel)))
                         if parallel is not None else _env_parallel())
        self.interactive = bool(interactive)
        if interactive:
            if max_retries is None:
                max_retries = INTERACTIVE_MAX_RETRIES
            if base_delay is None:
                base_delay = INTERACTIVE_BASE_DELAY
            if timeout is None:
                timeout = INTERACTIVE_TIMEOUT_S
        if max_retries is None:
            max_retries = _env_int("RIVAGE_EMBED_RETRIES") or DEFAULT_MAX_RETRIES
        if base_delay is None:
            _bd = os.getenv("RIVAGE_EMBED_RETRY_BASE_S")
            try:
                base_delay = float(_bd) if _bd and _bd.strip() else DEFAULT_BASE_DELAY
            except ValueError:
                base_delay = DEFAULT_BASE_DELAY
        self.max_retries = max(1, int(max_retries))
        self.base_delay = max(0.0, float(base_delay))
        self.timeout = float(timeout) if timeout is not None else DEFAULT_TIMEOUT_S
        self.normalize = normalize
        self.passage_prefix = passage_prefix
        self.query_instruction = query_instruction
        if query_template is None:
            model_lc = (self.model or "").lower()
            if "arctic" in model_lc or "snowflake" in model_lc:
                # Convention snowflake-arctic-embed : requêtes préfixées
                # « query: », passages SANS préfixe.
                query_template = "query: {query}"
            else:
                query_template = "Instruct: {instruction}\nQuery: {query}"
        self.query_template = query_template
        self._dim: Optional[int] = None
        # Format de requête (field, avec_model, par_lot) — None = à calibrer.
        self._fmt: Optional[tuple] = _fmt_from_env()
        # Plafond de caractères par entrée (contexte max du modèle).
        # Priorité : RIVAGE_EMBED_MAXCHARS > plafond appris (calibrage) >
        # défaut PROACTIF _DEFAULT_MAX_CHARS — le modèle servi
        # (snowflake-arctic-embed-l-v2-ft) est limité à 1024 tokens : tronquer
        # d'emblée évite un aller-retour d'erreur 400 par lot ; la réduction
        # automatique sur erreur de contexte reste active en dessous.
        self._max_chars: Optional[int] = _env_int("RIVAGE_EMBED_MAXCHARS")
        # Réutiliser le calibrage d'une instance précédente du même processus
        # (audit) : format ET plafond appris ne sont pas re-découverts à chaque
        # question. Les forçages env (ci-dessus) gardent la priorité.
        cached = _EMBED_CAL_CACHE.get((self.api_base, self.model))
        if cached:
            if self._fmt is None:
                self._fmt = cached.get("fmt")
            if self._max_chars is None:
                self._max_chars = cached.get("max_chars")
        if self._max_chars is None:
            self._max_chars = _DEFAULT_MAX_CHARS

    # --- Endpoint & format -----------------------------------------------------

    def _endpoint(self):
        url = self.api_base.rstrip("/") + "/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return url, headers

    def _body(self, field, with_model, batched, texts):
        # Appliquer le plafond de longueur (troncature de sécurité) au moment
        # d'émettre — la troncature ne concerne QUE le vecteur ; le texte complet
        # reste dans BM25 et dans les sources affichées.
        if self._max_chars:
            texts = [t[:self._max_chars] for t in texts]
        body = {"model": self.model} if with_model else {}
        body[field] = list(texts) if batched else texts[0]
        return body

    @staticmethod
    def _parse(resp) -> Optional[List[List[float]]]:
        """
        Extrait les vecteurs, en tolérant les formats de réponse courants :
        - OpenAI  : {"data": [{"embedding": [...], "index": 0}, ...]}
        - TEI/HF  : [[...], [...]]  (ou [{"embedding": [...]}, ...])
        - simple  : {"embedding": [...]} / {"embeddings": [[...]]}
        Retourne None si le format est inattendu.
        """
        try:
            j = resp.json()
        except Exception:
            return None
        if isinstance(j, list):
            if j and isinstance(j[0], dict) and "embedding" in j[0]:
                return [d["embedding"] for d in j]
            if j and isinstance(j[0], (int, float)):
                return [j]  # un seul vecteur renvoyé « à plat »
            return j
        if isinstance(j, dict):
            if isinstance(j.get("data"), list):
                data = j["data"]
                if data and isinstance(data[0], dict) and "index" in data[0]:
                    data = sorted(data, key=lambda d: d["index"])
                return [d["embedding"] if isinstance(d, dict) else d for d in data]
            if isinstance(j.get("embeddings"), list):
                return j["embeddings"]
            if isinstance(j.get("embedding"), list):
                return [j["embedding"]]
        return None

    def _ensure_format(self, log):
        """
        Calibrage : trouve un format de requête accepté par le serveur (une
        fois PAR PROCESSUS — résultat mémorisé, succès comme échec, pour ne pas
        re-sonder à chaque question ; audit).
        """
        if self._fmt is not None:
            return
        key = (self.api_base, self.model)
        with _EMBED_CAL_LOCK:
            cached = _EMBED_CAL_CACHE.get(key)
            if cached:
                if cached.get("fmt") is not None:
                    self._fmt = cached["fmt"]
                    return
                failed_ts = cached.get("failed_ts")
                if (failed_ts is not None
                        and time.monotonic() - failed_ts < _EMBED_FAILED_RETRY_S):
                    raise RuntimeError(
                        "Endpoint d'embedding marqué indisponible (échec récent "
                        f"du calibrage) — nouvel essai dans "
                        f"{_EMBED_FAILED_RETRY_S:.0f} s au plus.")
            url, headers = self._endpoint()
            last = ""
            for field, with_model, batched in _CANDIDATE_FORMATS:
                body = self._body(field, with_model, batched, ["calibration"])
                try:
                    # Timeout de SONDE court (audit v2) : 6 candidats × 30 s
                    # bloquaient trop longtemps sur un endpoint en trou noir.
                    resp = self.http.post(url, headers=headers, json=body, timeout=10.0)
                except Exception as e:
                    _raise_if_tls(e)
                    last = str(e)
                    continue
                if resp.status_code < 400 and self._parse(resp) is not None:
                    self._fmt = (field, with_model, batched)
                    entry = _EMBED_CAL_CACHE.setdefault(key, {})
                    entry["fmt"] = self._fmt
                    entry["failed_ts"] = None
                    log.info("[embeddings] format API calibré : champ=%r model=%s lot=%s"
                             % (field, with_model, batched))
                    return
                last = _body_snippet(resp)
            _EMBED_CAL_CACHE.setdefault(key, {})["failed_ts"] = time.monotonic()
            raise RuntimeError(
                "Aucun format de requête d'embedding accepté par le serveur. "
                f"Dernière réponse : {last}"
            )

    def _request(self, texts, log, budget=None) -> List[List[float]]:
        """
        POST d'un lot, avec retries, troncature auto (contexte max) et messages
        clairs. `budget` = compteurs {attempt, reductions} PARTAGÉS entre les
        sous-lots issus d'une division (audit v2) : sans partage, chaque moitié
        récursive repartait avec 5 tentatives + backoff — un serveur
        pathologique multipliait les retries par le nombre de feuilles (~315
        requêtes et des minutes de sommeil pour UN lot de 32).
        """
        url, headers = self._endpoint()
        field, with_model, batched = self._fmt
        if budget is None:
            budget = {"attempt": 0, "reductions": 0}

        def _retry_possible() -> bool:
            # Même borne pour le réseau et les statuts HTTP (audit V2, C-11) :
            # `attempt` compte les échecs ; max_retries tentatives en tout.
            return budget["attempt"] < self.max_retries

        while True:
            body = self._body(field, with_model, batched, texts)
            try:
                resp = self.http.post(url, headers=headers, json=body,
                                      timeout=self.timeout)
            except Exception as e:
                _raise_if_tls(e)
                # Seules les erreurs de TRANSPORT sont transitoires (audit V2,
                # C-9) : protocole d'URL non supporté, requête mal formée,
                # erreur de programmation → immédiatement, sans 7 retries.
                if not is_transport_error(e):
                    raise RuntimeError(
                        f"Échec embedding (non re-tentable, {type(e).__name__}) : {e}")
                budget["attempt"] += 1
                if _retry_possible():
                    delay = _retry_delay(self.base_delay, budget["attempt"],
                                         congestion=False)
                    log.warning(f"[embeddings] réseau {type(e).__name__} — "
                                f"tentative {budget['attempt'] + 1}/{self.max_retries} "
                                f"dans {delay:.1f}s")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Échec réseau embedding: {e}")

            if resp.status_code >= 400:
                rb = _body_snippet(resp)
                detail = f"HTTP {resp.status_code} sur {url} — {rb}"
                # Dépassement de contexte. Deux cas distincts (audit) :
                #  1) Le serveur compte les tokens du LOT ENTIER : calculer un
                #     plafond par entrée à partir de ce message donnerait un
                #     ratio faux (caractères d'UNE entrée / tokens de TOUT le
                #     lot) → troncature massive à ~200 caractères MÉMORISÉE
                #     pour tout le run. On DIVISE donc d'abord le lot en deux
                #     (récursivement) tant qu'il compte plusieurs textes.
                #  2) Lot de UN texte : là seulement, l'entrée elle-même est
                #     trop longue → plafond de caractères calculé du message,
                #     mémorisé pour les lots suivants.
                # 413 Payload Too Large : lot trop gros, même remède que le
                # dépassement de contexte (audit v2), sans exiger de motif texte.
                is_ctx = (resp.status_code == 413
                          or (resp.status_code in (400, 422) and _is_context_error(rb)))
                if is_ctx:
                    if len(texts) > 1:
                        mid = len(texts) // 2
                        log.warning(f"[embeddings] contexte/taille max dépassé sur "
                                    f"un lot de {len(texts)} — division du lot")
                        return (self._request(texts[:mid], log, budget)
                                + self._request(texts[mid:], log, budget))
                    if budget["reductions"] < 6:
                        sent = ([t[:self._max_chars] for t in texts]
                                if self._max_chars else list(texts))
                        cap = _char_cap_from_error(sent, rb, self._max_chars)
                        if cap and (self._max_chars is None or cap < self._max_chars):
                            # Sous le verrou (audit V2, C-13) : plusieurs lots
                            # en vol peuvent apprendre un plafond en même temps.
                            with _EMBED_CAL_LOCK:
                                if self._max_chars is None or cap < self._max_chars:
                                    self._max_chars = cap
                                # Plafond appris partagé avec les instances
                                # suivantes du processus (lecteur : un client
                                # par question).
                                _EMBED_CAL_CACHE.setdefault(
                                    (self.api_base, self.model), {})["max_chars"] = cap
                            budget["reductions"] += 1
                            log.warning(f"[embeddings] entrée > contexte max — "
                                        f"troncature à {cap} caractères et reprise")
                            continue
                if resp.status_code in RETRIABLE_STATUS:
                    budget["attempt"] += 1
                    if _retry_possible():
                        # Congestion passerelle (504/429…) : base ×3, plafond
                        # 60 s, jitter — la passerelle a besoin de minutes, pas
                        # de secondes (même stratégie que l'OCR). `Retry-After`
                        # (429/503) est honoré : max(délai calculé, Retry-After),
                        # plafonné à 60 s (audit V2, C-11).
                        delay = _retry_delay(self.base_delay, budget["attempt"],
                                             congestion=True)
                        retry_after = (_retry_after_s(resp)
                                       if resp.status_code in (429, 503) else None)
                        if retry_after:
                            delay = min(_RETRY_CAP_S, max(delay, retry_after))
                        log.warning(f"[embeddings] {detail} — tentative "
                                    f"{budget['attempt'] + 1}/{self.max_retries} "
                                    f"dans {delay:.1f}s"
                                    + (f" (Retry-After {retry_after:.0f}s)"
                                       if retry_after else ""))
                        time.sleep(delay)
                        continue
                raise RuntimeError(detail)

            vecs = self._parse(resp)
            if vecs is None:
                raise RuntimeError(f"Réponse embedding inattendue depuis {url} : "
                                   f"{_body_snippet(resp)}")
            # Contrôle immédiat (audit v2) : un décalage vecteurs/textes ne doit
            # pas attendre VectorIndex.add pour éclater loin de sa cause.
            if len(vecs) != len(texts):
                raise RuntimeError(
                    f"{len(vecs)} vecteur(s) reçu(s) pour {len(texts)} texte(s) "
                    f"depuis {url} — réponse serveur incohérente.")
            return vecs

    # --- API publique ---------------------------------------------------------

    def embed_passages(self, texts: Sequence[str], log=None,
                       progress_cb=None) -> np.ndarray:
        """Encode des passages (indexation). Retourne un array (N, dim) float32."""
        return self._embed(texts, self.passage_prefix, log, progress_cb)

    def embed_query(self, text: str, log=None) -> np.ndarray:
        """Encode une question (requête) au format instruct e5. Retourne (dim,) float32."""
        formatted = self.query_template.format(
            instruction=self.query_instruction, query=text
        )
        vec = self._embed([formatted], "", log, None)
        return vec[0]

    def _embed(self, texts: Sequence[str], prefix: str, log, progress_cb) -> np.ndarray:
        _log = log or logger
        texts = [prefix + t for t in texts]
        if not texts:
            return np.zeros((0, self._dim or 0), dtype="float32")

        self._ensure_format(_log)
        batched = self._fmt[2]
        step = self.batch_size if batched else 1  # certains serveurs = 1 texte/appel

        total = len(texts)
        lots = [texts[start:start + step] for start in range(0, total, step)]
        par = min(self.parallel, len(lots))
        results: List[Optional[List[List[float]]]] = [None] * len(lots)
        done = 0

        def _avancer(n_textes: int):
            nonlocal done
            done += n_textes
            if progress_cb:
                progress_cb(done / total, f"Embedding {done}/{total}")

        if par <= 1:
            # Séquentiel (un seul lot, ou RIVAGE_EMBED_PARALLEL=1) : chemin
            # historique, sans thread.
            for i, lot in enumerate(lots):
                results[i] = self._request(lot, _log)
                _avancer(len(lot))
        else:
            # Lots EN VOL (audit V2, C-13) : fenêtre glissante de `par` lots
            # soumis à un petit pool ; les résultats sont rangés à l'indice du
            # lot, l'ordre des vecteurs est donc celui des textes. Le premier
            # lot en échec fait échouer l'ensemble (comme avant) : les lots non
            # encore soumis ne le sont pas, ceux en vol finissent seuls. Le
            # plafond de caractères appris par un lot vaut pour les suivants
            # (verrou _EMBED_CAL_LOCK dans _request) ; chaque lot garde son
            # propre budget de retries.
            from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
            with ThreadPoolExecutor(max_workers=par,
                                    thread_name_prefix="rivage-embed") as pool:
                en_vol = {}
                prochain = 0
                while prochain < len(lots) and len(en_vol) < par:
                    en_vol[pool.submit(self._request, lots[prochain], _log)] = prochain
                    prochain += 1
                try:
                    while en_vol:
                        finis, _ = wait(list(en_vol), return_when=FIRST_COMPLETED)
                        for fut in finis:
                            i = en_vol.pop(fut)
                            results[i] = fut.result()      # relève l'exception du lot
                            _avancer(len(lots[i]))
                            if prochain < len(lots):
                                en_vol[pool.submit(self._request, lots[prochain], _log)] = prochain
                                prochain += 1
                except BaseException:
                    for fut in en_vol:
                        fut.cancel()
                    raise

        all_vecs: List[List[float]] = []
        for r in results:
            all_vecs.extend(r or [])

        arr = np.asarray(all_vecs, dtype="float32")
        if self._dim is None and arr.size:
            self._dim = arr.shape[1]
        return _normalize(arr) if self.normalize else arr

    @property
    def dim(self) -> Optional[int]:
        return self._dim

    def close(self) -> None:
        if self._own_client:
            try:
                self.http.close()
            except Exception:
                pass
