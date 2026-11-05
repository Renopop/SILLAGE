"""
models_utils.py - Configuration des API internes et client HTTP partagé.

Ce module centralise l'accès aux modèles servis par la passerelle interne
LiteLLM Dassault Aviation (voir §12 de la note de conception RIVAGE) :

    Étape RIVAGE   Modèle interne                    Endpoint
    ------------   -------------------------------   -----------------
    Embedding      snowflake-arctic-embed-l-v2-ft    /v1/embeddings
    Reranking      bge-reranker-v2-m3                /v1/rerank (auto)
    Synthèse       mistral-small-4                   /v1/chat/completions
    OCR Vision     mistral-small-4 (entrées image)   /v1/chat/completions

Passerelle UNIQUE : tous les modèles sont servis derrière
https://litellm.dassault-aviation.pro/v1 — le modèle est choisi par le champ
« model » du corps de requête, plus par le chemin de l'URL.

Les modules d'extraction (`llm_ocr.py`, `pptx_processing.py`) importent
d'ici la configuration de l'OCR par LLM Vision (« DALLEM ») :

    DALLEM_API_KEY, DALLEM_API_BASE, LLM_MODEL, create_http_client

CLÉS API — fichier dédié `RIVAGE_keys.env`, UNE CLÉ PAR MODÈLE
--------------------------------------------------------------
La passerelle délivre une clé DISTINCTE par modèle — pas de clé commune :
RIVAGE_EMBED_KEY (snowflake), RIVAGE_RERANK_KEY (bge-reranker),
RIVAGE_SYNTH_KEY (mistral-small — synthèse ET OCR Vision). Les trois vivent
dans un fichier À PART, jamais versionné (modèle : `RIVAGE_keys.env.example`),
chargé ici à l'import, donc pour TOUS les modes de lancement (CLI, GUIs,
.bat, exe autonome). Recherche : chemin forcé par RIVAGE_KEYS_FILE, sinon à
côté de l'application, sinon dossier parent, sinon répertoire courant. Les
variables déjà définies dans l'environnement (.bat, Windows) restent
PRIORITAIRES. Aucune clé n'est codée en dur : une clé absente ne désactive
QUE son service (sans clé d'embedding : BM25 seul ; sans clé mistral-small :
ni synthèse ni OCR ; sans clé reranker : tri par score hybride seul).

Variables d'environnement reconnues
------------------------------------
    RIVAGE_EMBED_KEY      Clé du modèle d'embedding (snowflake-arctic…)
    RIVAGE_RERANK_KEY     Clé du modèle de reranking (bge-reranker-v2-m3)
    RIVAGE_SYNTH_KEY      Clé de mistral-small-4 (synthèse ET OCR Vision)
    RIVAGE_KEYS_FILE      Chemin explicite du fichier de clés (optionnel)
    RIVAGE_API_BASE       Base de la passerelle (défaut:
                          https://litellm.dassault-aviation.pro/v1)
    DALLEM_API_BASE       URL de base du endpoint vision (défaut: la passerelle)
    DALLEM_MODEL          Nom du modèle vision (défaut: mistral-small-4)
    DALLEM_CA_BUNDLE      Chemin d'un bundle CA interne (.pem) — prioritaire
                          sur DALLEM_VERIFY_SSL pour la vérification TLS
    DALLEM_VERIFY_SSL     "true"/"false" — vérification TLS (défaut: true)
    DALLEM_TIMEOUT        Timeout HTTP par défaut en secondes (défaut: 120)

    RIVAGE_EMBED_BASE     Endpoint embeddings (défaut: la passerelle)
    RIVAGE_EMBED_MODEL    Modèle d'embedding (défaut: snowflake-arctic-embed-l-v2-ft)
    RIVAGE_RERANK_BASE    Endpoint reranking (défaut: la passerelle)
    RIVAGE_RERANK_MODEL   Modèle de reranking (défaut: bge-reranker-v2-m3)
    RIVAGE_SYNTH_BASE     Endpoint synthèse (défaut: la passerelle)
    RIVAGE_SYNTH_MODEL    Modèle de synthèse (défaut: mistral-small-4)
"""

import os
import sys
import logging

logger = logging.getLogger(__name__)

# =============================================================================
#  FICHIER DE CLÉS DÉDIÉ (RIVAGE_keys.env)
# =============================================================================

KEYS_FILE = "RIVAGE_keys.env"


def _app_dir() -> str:
    """Dossier de l'application — exécutable gelé (PyInstaller) inclus."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _load_keys_file():
    """Charge le fichier de clés (lignes KEY=VALUE, `#` = commentaire) dans
    l'environnement, SANS écraser les variables déjà définies. Retourne le
    chemin chargé, ou None si aucun fichier n'est trouvé."""
    candidates = []
    forced = os.getenv("RIVAGE_KEYS_FILE")
    if forced:
        candidates.append(forced)
    base = _app_dir()
    candidates += [
        os.path.join(base, KEYS_FILE),
        os.path.join(os.path.dirname(base), KEYS_FILE),
        os.path.join(os.getcwd(), KEYS_FILE),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            key, value = _parse_env_line(line)
            if key:
                os.environ.setdefault(key, value)
        logger.info(f"[models_utils] Fichier de clés chargé : {path}")
        return path
    return None


def _parse_env_line(line: str):
    """Lit une ligne `KEY=VALUE` d'un fichier de clés. Retourne (key, value),
    ou (None, None) pour une ligne vide, un commentaire ou une ligne invalide.

    Sémantique dotenv (audit V2, C-10) : un préfixe `export ` est toléré
    (fichier copié d'un shell) ; une valeur NON quotée s'arrête au premier
    ` #` (commentaire de fin de ligne — « sk-x # clé snowflake » donnait une
    clé fausse et un 401 silencieux) ; une valeur quotée est prise telle
    quelle entre ses guillemets. Une valeur qui contient encore un espace est
    signalée (sans l'afficher) : clé mal collée, probablement."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None, None
    if line.startswith("export ") or line.startswith("export\t"):
        line = line[len("export"):].strip()
    key, _, value = line.partition("=")
    key = key.strip()
    if not key or any(c.isspace() for c in key):
        return None, None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    else:
        hash_at = value.find(" #")
        if hash_at >= 0:
            value = value[:hash_at].rstrip()
        value = value.strip('"').strip("'")
    if any(c.isspace() for c in value) and _ressemble_a_un_secret(key, value):
        logger.warning(
            f"[models_utils] La valeur de {key} contient un espace — clé mal "
            f"collée ou guillemets manquants ? Vérifiez le fichier de clés.")
    return key, value


def _ressemble_a_un_secret(key: str, value: str = "") -> bool:
    """Vrai pour une variable qui porte une clé ou un jeton (par son nom :
    KEY, TOKEN, SECRET, PASSWORD ; ou par sa valeur : préfixe `sk-` des
    clés d'API) : seule une telle valeur contenant un espace mérite
    l'avertissement « clé mal collée ». Un chemin
    (`RIVAGE_INDEX_DIR=L:/test 2`), un libellé ou un motif d'exclusion
    contiennent légitimement des espaces (2.6 : faux positif affiché dans
    le journal de l'indexeur)."""
    k = (key or "").upper()
    if any(m in k for m in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")):
        return True
    return (value or "").lstrip().lower().startswith("sk-")


_KEYS_FILE_LOADED = _load_keys_file()


# =============================================================================
#  FICHIER D'OPTIONS (RIVAGE.env) — lu dans TOUS les modes de lancement
# =============================================================================
#
#  Jusqu'en 2.5.1, RIVAGE.env n'était lu que par l'exécutable RIVAGE.exe : en
#  lancement par script (launch.bat → python rivage.py gui), un
#  RIVAGE_INDEX_DIR posé dans RIVAGE.env restait lettre morte et le lecteur
#  ouvrait l'index mémorisé par l'indexeur — « le changement de base ne
#  fonctionne pas ». Désormais le fichier est lu ici, au chargement du
#  catalogue des modèles (donc avant que les constantes ci-dessous ne figent
#  l'environnement), avec la même priorité partout :
#      .bat du mode  >  variables Windows  >  RIVAGE_keys.env  >  RIVAGE.env
#  (setdefault : une variable déjà définie n'est jamais écrasée).

ENV_FILE = "RIVAGE.env"


def _read_text_any(path: str):
    """Texte d'un fichier Windows : UTF-8 (BOM ou non), sinon cp1252."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _load_env_file():
    """Charge RIVAGE.env (KEY=VALUE, `#` = commentaire) dans l'environnement
    SANS écraser l'existant. Cherché à côté de l'application (.py ou .exe),
    dans son dossier parent, puis dans le répertoire courant ; chemin forcé
    par RIVAGE_ENV_FILE. Retourne le chemin chargé, ou None."""
    candidates = []
    forced = os.getenv("RIVAGE_ENV_FILE")
    if forced:
        candidates.append(forced)
    base = _app_dir()
    candidates += [
        os.path.join(base, ENV_FILE),
        os.path.join(os.path.dirname(base), ENV_FILE),
        os.path.join(os.getcwd(), ENV_FILE),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        text = _read_text_any(path)
        if text is None:
            continue
        n = 0
        for line in text.splitlines():
            key, value = _parse_env_line(line)
            if key:
                if key not in os.environ:
                    n += 1
                os.environ.setdefault(key, value)
        logger.info(f"[models_utils] Fichier d'options chargé : {path} "
                    f"({n} variable(s) posée(s))")
        return path
    return None


_ENV_FILE_LOADED = _load_env_file()

# =============================================================================
#  CATALOGUE DES API INTERNES (passerelle LiteLLM, note de conception §12)
# =============================================================================
#
#  Passerelle UNIQUE : /v1/embeddings, /v1/chat/completions, etc. — le modèle
#  est sélectionné par le champ « model » du corps de requête. RIVAGE_API_ROOT
#  reste accepté (la base devient <root>/v1) ; pour un endpoint vraiment
#  spécifique par service, utiliser RIVAGE_EMBED_BASE / RIVAGE_RERANK_BASE /
#  RIVAGE_SYNTH_BASE / DALLEM_API_BASE.

_API_ROOT = os.getenv("RIVAGE_API_ROOT", "https://litellm.dassault-aviation.pro")
_API_BASE = os.getenv("RIVAGE_API_BASE", _API_ROOT.rstrip("/") + "/v1")

# --- Clés API : UNE CLÉ DISTINCTE PAR MODÈLE ---------------------------------
# La passerelle LiteLLM délivre une clé PAR MODÈLE. Chaque service utilise
# STRICTEMENT la sienne — il n'y a PAS de clé commune :
#   RIVAGE_EMBED_KEY   -> snowflake-arctic-embed-l-v2-ft  (embedding)
#   RIVAGE_RERANK_KEY  -> bge-reranker-v2-m3              (reranking)
#   RIVAGE_SYNTH_KEY   -> mistral-small-4                 (synthèse ET OCR Vision)
# Les trois se renseignent dans le fichier dédié RIVAGE_keys.env (modèle :
# RIVAGE_keys.env.example). Une clé absente ne désactive QUE son service.
EMBED_API_KEY = os.getenv("RIVAGE_EMBED_KEY")
RERANK_API_KEY = os.getenv("RIVAGE_RERANK_KEY")
SYNTH_API_KEY = os.getenv("RIVAGE_SYNTH_KEY")

# --- OCR Vision (DALLEM) -----------------------------------------------------
DALLEM_MODEL = os.getenv("DALLEM_MODEL", "mistral-small-4")
DALLEM_API_BASE = os.getenv("DALLEM_API_BASE", _API_BASE)
# L'OCR Vision utilise mistral-small-4, comme la synthèse : MÊME clé. L'alias
# historique DALLEM_API_KEY (importé par llm_ocr / pptx_processing) désigne
# donc la clé mistral-small.
DALLEM_API_KEY = SYNTH_API_KEY

# Alias attendu par les modules d'extraction (llm_ocr, pptx_processing)
LLM_MODEL = DALLEM_MODEL

# --- Embedding (snowflake-arctic-embed-l-v2-ft, §5.1 / §12.1) ----------------
# Longueur d'entrée servie : 1024 tokens max — le plafond proactif côté client
# est appliqué dans embeddings.py (RIVAGE_EMBED_MAXCHARS, défaut ~3000 chars).
EMBED_MODEL = os.getenv("RIVAGE_EMBED_MODEL", "snowflake-arctic-embed-l-v2-ft")
EMBED_API_BASE = os.getenv("RIVAGE_EMBED_BASE", _API_BASE)

# --- Reranking (bge-reranker-v2-m3, §6.2 / §12) ------------------------------
RERANK_MODEL = os.getenv("RIVAGE_RERANK_MODEL", "bge-reranker-v2-m3")
RERANK_API_BASE = os.getenv("RIVAGE_RERANK_BASE", _API_BASE)

# --- Synthèse (mistral-small-4, §7 / §12) ------------------------------------
SYNTH_MODEL = os.getenv("RIVAGE_SYNTH_MODEL", "mistral-small-4")
SYNTH_API_BASE = os.getenv("RIVAGE_SYNTH_BASE", _API_BASE)

# --- Repli de synthèse : modèle « dallem » -----------------------------------
# Si mistral-small échoue (saturation 5xx épuisée, timeout, erreur), la
# synthèse peut se replier sur un second modèle de la passerelle — « dallem » —
# qui porte SA PROPRE clé, comme chaque modèle (RIVAGE_DALLEM_KEY).
# Le repli est INACTIF tant que cette clé n'est pas renseignée.
# NB : ne pas confondre avec l'alias historique DALLEM_API_KEY ci-dessus
# (OCR Vision = clé mistral-small) — ici il s'agit d'un modèle distinct.
SYNTH_FALLBACK_MODEL = os.getenv("RIVAGE_DALLEM_MODEL", "dallem")
SYNTH_FALLBACK_KEY = os.getenv("RIVAGE_DALLEM_KEY")

# --- Paramètres transport ----------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "oui", "on")


def _env_float(name: str, default: float) -> float:
    """Parse une variable numérique sans faire échouer l'import si mal formée (m3)."""
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        logger.warning(
            f"[models_utils] {name}={val!r} non numérique — valeur par défaut {default} utilisée."
        )
        return default


def _resolve_verify():
    """
    Détermine la vérification TLS pour les API internes.

    Les gateways internes exposent souvent un certificat signé par une autorité
    interne (CA d'entreprise) absente du magasin de confiance de Python, d'où un
    « CERTIFICATE_VERIFY_FAILED ». Ordre de résolution :

      1. DALLEM_CA_BUNDLE = chemin d'un bundle CA (.pem) -> vérification AVEC ce
         bundle (recommandé : sécurisé).
      2. DALLEM_VERIFY_SSL = false -> vérification désactivée (repli rapide,
         acceptable sur un réseau interne maîtrisé, moins sûr).
      3. sinon -> vérification standard (magasin système).

    Retourne soit un chemin (str) vers le bundle, soit un booléen.
    """
    ca = os.getenv("DALLEM_CA_BUNDLE")
    if ca:
        if os.path.isfile(ca):
            return ca
        logger.warning(
            f"[models_utils] DALLEM_CA_BUNDLE={ca!r} introuvable — ignoré."
        )
    return _env_bool("DALLEM_VERIFY_SSL", True)


VERIFY_SSL = _resolve_verify()
DEFAULT_TIMEOUT = _env_float("DALLEM_TIMEOUT", 120.0)


# =============================================================================
#  CLIENT HTTP PARTAGÉ
# =============================================================================

def create_http_client(timeout: float = DEFAULT_TIMEOUT, verify=VERIFY_SSL):
    """
    Crée un client HTTP réutilisable pour les appels aux API internes.

    Le client est conçu pour être PARTAGÉ : l'OCR en crée un par document,
    partagé par ses workers de pages et fermé en `finally` (audit V2, B-13 ;
    httpx.Client est sûr entre threads) ; le lecteur graphique garde un client
    par service pour toute la session (D-6). Le client doit exposer :
        .post(url, headers=..., json=..., timeout=...) -> réponse
        réponse.raise_for_status(), réponse.json(), réponse.status_code
        .close()

    On privilégie httpx (HTTP/2, timeouts fins) avec repli sur requests si
    httpx n'est pas installé.

    Args:
        timeout: timeout par défaut (surchargeable par appel)
        verify: vérification du certificat TLS. Les gateways internes peuvent
                exposer un certificat privé ; dans ce cas, préférer fournir le
                bundle CA plutôt que de désactiver la vérification.

    Returns:
        Un client HTTP prêt à l'emploi.
    """
    try:
        import httpx
        return httpx.Client(timeout=timeout, verify=verify)
    except ImportError:
        logger.debug("[models_utils] httpx indisponible, repli sur requests")

    try:
        import requests

        class _RequestsClient:
            """Adaptateur minimal requests -> interface httpx utilisée par le code."""

            def __init__(self, timeout, verify):
                self._session = requests.Session()
                self._timeout = timeout
                self._verify = verify

            def post(self, url, headers=None, json=None, timeout=None):
                return self._session.post(
                    url,
                    headers=headers,
                    json=json,
                    timeout=timeout or self._timeout,
                    verify=self._verify,
                )

            def close(self):
                self._session.close()

        return _RequestsClient(timeout, verify)
    except ImportError:
        raise RuntimeError(
            "Aucun client HTTP disponible. Installez httpx (recommandé) ou requests :\n"
            "    pip install httpx"
        )


def is_transport_error(e: BaseException) -> bool:
    """Timeout, connexion coupée/refusée, DNS, proxy, serveur qui raccroche :
    erreur de TRANSPORT, transitoire par nature, donc à RE-TENTER. Reconnue
    par son TYPE et non par son texte (audit V2, B-1 : `str(httpx.ReadTimeout)`
    vaut « timed out », `str(RemoteProtocolError)` « Server disconnected »).

    Partagée par l'OCR et les embeddings (audit V2, C-9) : le client
    d'embedding re-tentait TOUTE exception 7 fois (145 s de sommeil) — y
    compris un protocole d'URL non supporté ou une erreur de programmation,
    que personne ne corrige en attendant. Ces dernières retournent False et
    doivent être levées immédiatement."""
    if isinstance(e, (ValueError, TypeError, KeyError, AttributeError,
                      IndexError)):
        return False                              # erreur locale, jamais transitoire
    try:
        import httpx
        if isinstance(e, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
            return False                          # URL/requête mal formée
        if isinstance(e, (httpx.TimeoutException, httpx.NetworkError,
                          httpx.RemoteProtocolError, httpx.ProxyError)):
            return True
        if isinstance(e, httpx.TransportError):
            return True
    except ImportError:
        pass
    try:
        import requests
        if isinstance(e, (requests.exceptions.InvalidSchema,
                          requests.exceptions.MissingSchema,
                          requests.exceptions.InvalidURL)):
            return False
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


# =============================================================================
#  ÉTAT DE LA CONFIGURATION
# =============================================================================

def is_dallem_configured() -> bool:
    """True si la clé mistral-small est présente (OCR Vision activable)."""
    return bool(DALLEM_API_KEY)


def config_summary() -> dict:
    """
    Retourne un récapitulatif de la configuration (sans exposer les clés —
    seulement leur PRÉSENCE, service par service).
    Pratique pour un log de démarrage ou un diagnostic.
    """
    return {
        "version": __import__("version").__version__,
        "ocr_vision_configured": is_dallem_configured(),
        "ocr_vision_api_base": DALLEM_API_BASE,
        "ocr_vision_model": DALLEM_MODEL,
        "embed_api_base": EMBED_API_BASE,
        "embed_model": EMBED_MODEL,
        "embed_key": bool(EMBED_API_KEY),
        "rerank_api_base": RERANK_API_BASE,
        "rerank_model": RERANK_MODEL,
        "rerank_key": bool(RERANK_API_KEY),
        "synth_api_base": SYNTH_API_BASE,
        "synth_model": SYNTH_MODEL,
        "synth_key": bool(SYNTH_API_KEY),
        "synth_fallback_model": SYNTH_FALLBACK_MODEL,
        "synth_fallback_key": bool(SYNTH_FALLBACK_KEY),
        "keys_file": _KEYS_FILE_LOADED,
        "env_file": _ENV_FILE_LOADED,
        "tls_verify": (VERIFY_SSL if isinstance(VERIFY_SSL, bool)
                       else f"bundle CA : {VERIFY_SSL}"),
        "default_timeout": DEFAULT_TIMEOUT,
    }


_MISSING_KEYS = [name for name, key in (
    ("RIVAGE_EMBED_KEY (embedding snowflake)", EMBED_API_KEY),
    ("RIVAGE_RERANK_KEY (reranking bge)", RERANK_API_KEY),
    ("RIVAGE_SYNTH_KEY (mistral-small : synthèse + OCR)", SYNTH_API_KEY),
) if not key]
if _MISSING_KEYS:
    logger.info(
        "[models_utils] Clé(s) API manquante(s) : " + ", ".join(_MISSING_KEYS)
        + " — le(s) service(s) correspondant(s) seront indisponibles. "
        "Renseignez-les dans RIVAGE_keys.env (modèle : RIVAGE_keys.env.example)."
    )
