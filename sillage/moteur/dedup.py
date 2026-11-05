# dedup.py
"""
Déduplication (MinHash / LSH) - §4.2

Les répertoires mal rangés contiennent forcément des doublons et
quasi-doublons (versions, copies). On les élimine avant l'indexation :
MinHash estime la similarité de Jaccard entre fragments ; LSH regroupe les
candidats proches sans comparer toutes les paires. Cela réduit le bruit et le
volume à indexer.

S'appuie sur `datasketch` (pip install datasketch) si disponible, avec un repli
intégré (MinHash + banding LSH) sinon, afin de rester fonctionnel et testable
sans dépendance.

Paramètre par défaut de la spec : threshold=0.85, num_perm=128.
"""

import logging
import hashlib
from typing import List, Iterable, Set, Callable, Any, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.85
DEFAULT_NUM_PERM = 128


# =============================================================================
#  SIGNATURE (shingles de mots)
# =============================================================================

def _tokens(text: str) -> Set[str]:
    """Ensemble de mots normalisés — base de la similarité de Jaccard."""
    return {w for w in text.lower().split() if w}


# =============================================================================
#  BACKEND datasketch (préféré)
# =============================================================================

# Mémoires AU NIVEAU DU MODULE (audit perf 2.1, I-2). Un dédupeur est construit
# PAR FICHIER (la déduplication est intra-document, voir indexer._process_file) :
#  - `MinHashLSH(threshold, num_perm)` recalcule à chaque construction les
#    paramètres (bandes, lignes) par une intégration numérique sur toutes les
#    combinaisons (b, r) — 16 ms mesurés, soit 66 % du temps CPU de l'indexeur
#    sur le banc (2,2 h extrapolées sur 500 000 fichiers) pour retrouver
#    toujours le même couple. Les paramètres sont calculés UNE fois par
#    (threshold, num_perm) puis transmis via `params=` : même (b, r), donc
#    mêmes bandes, donc mêmes décisions de doublon.
#  - `MinHash(num_perm)` régénère à chaque signature les permutations (tirage
#    pseudo-aléatoire à graine fixe : 0,15 ms, ×16 par rapport au reste de la
#    signature). Un MinHash DE RÉFÉRENCE par num_perm est conservé et ses
#    permutations (déterministes, graine 1) sont partagées par toutes les
#    signatures — valeurs de hachage strictement identiques.
_LSH_PARAMS: dict = {}       # (threshold, num_perm) -> (b, r)
_REF_MINHASH: dict = {}      # num_perm -> MinHash de référence (jamais mis à jour)


def _lsh_params(MinHashLSH, threshold: float, num_perm: int):
    """(b, r) optimaux pour ce couple, calculés une seule fois par processus."""
    key = (threshold, num_perm)
    params = _LSH_PARAMS.get(key)
    if params is None:
        try:
            probe = MinHashLSH(threshold=threshold, num_perm=num_perm)
            params = (probe.b, probe.r)
        except Exception as e:
            # Couple refusé par datasketch (« bands too small ») : mémorisé
            # aussi, sinon chaque fichier repaierait l'intégration numérique
            # avant de retomber sur le repli intégré (make_deduper).
            params = e
        _LSH_PARAMS[key] = params
    if isinstance(params, Exception):
        raise params
    return params


def _reference_minhash(MinHash, num_perm: int):
    """MinHash de référence (permutations partagées) pour ce num_perm."""
    ref = _REF_MINHASH.get(num_perm)
    if ref is None:
        ref = MinHash(num_perm=num_perm)
        _REF_MINHASH[num_perm] = ref
    return ref


class _DatasketchDeduper:
    def __init__(self, threshold: float, num_perm: int):
        from datasketch import MinHash, MinHashLSH
        self._MinHash = MinHash
        self._num_perm = num_perm
        self._lsh = MinHashLSH(threshold=threshold, num_perm=num_perm,
                               params=_lsh_params(MinHashLSH, threshold, num_perm))
        ref = _reference_minhash(MinHash, num_perm)
        # Arguments d'instanciation d'une signature : permutations et fonction
        # de hachage de la référence. `scheme` n'existe qu'à partir de
        # datasketch 2.0 (où il est OBLIGATOIRE dès qu'on fournit des
        # permutations) ; les versions 1.x n'en ont pas.
        self._sig_kwargs = {"num_perm": num_perm, "seed": ref.seed,
                            "hashfunc": ref.hashfunc,
                            "permutations": ref.permutations}
        if hasattr(ref, "scheme"):
            self._sig_kwargs["scheme"] = ref.scheme
        self._count = 0

    def _signature(self, text: str):
        m = self._MinHash(**self._sig_kwargs)
        # update_batch : une seule passe numpy sur tous les jetons (0,56 s/Mo
        # contre 1,44 s/Mo en `update` jeton par jeton — audit perf 2.1, I-2).
        m.update_batch([tok.encode("utf-8") for tok in _tokens(text)])
        return m

    def is_duplicate(self, text: str) -> bool:
        sig = self._signature(text)
        if self._lsh.query(sig):
            return True
        self._lsh.insert(f"c{self._count}", sig)
        self._count += 1
        return False


# =============================================================================
#  BACKEND intégré (repli : MinHash + banding LSH)
# =============================================================================

class _FallbackDeduper:
    """
    MinHash maison avec LSH par banding. num_perm hashes répartis en bandes ;
    deux textes partageant une bande identique sont candidats, puis on vérifie
    la similarité estimée contre le seuil.
    """

    def __init__(self, threshold: float, num_perm: int):
        self.threshold = threshold
        self.num_perm = num_perm
        self.bands, self.rows = self._optimal_bands(num_perm, threshold)
        self._buckets: List[dict] = [dict() for _ in range(self.bands)]
        self._signatures: List[Tuple[int, ...]] = []
        # Coefficients (a, b) pour num_perm fonctions de hachage universelles
        self._a = [self._stable_int(f"a{i}") | 1 for i in range(num_perm)]
        self._b = [self._stable_int(f"b{i}") for i in range(num_perm)]
        self._mersenne = (1 << 61) - 1

    @staticmethod
    def _stable_int(seed: str) -> int:
        h = hashlib.sha1(seed.encode("utf-8")).digest()
        return int.from_bytes(h[:8], "big")

    @staticmethod
    def _optimal_bands(num_perm: int, threshold: float) -> Tuple[int, int]:
        # Choisit (bandes, lignes) dont le seuil (1/b)^(1/r) approche threshold
        best = (num_perm, 1)
        best_err = float("inf")
        for b in range(1, num_perm + 1):
            if num_perm % b:
                continue
            r = num_perm // b
            t = (1.0 / b) ** (1.0 / r)
            err = abs(t - threshold)
            if err < best_err:
                best_err = err
                best = (b, r)
        return best

    def _minhash(self, text: str) -> Tuple[int, ...]:
        tokens = _tokens(text)
        if not tokens:
            return tuple([0] * self.num_perm)
        token_hashes = [self._stable_int(t) for t in tokens]
        sig = []
        for a, b in zip(self._a, self._b):
            sig.append(min((a * th + b) % self._mersenne for th in token_hashes))
        return tuple(sig)

    def _estimate_jaccard(self, s1: Tuple[int, ...], s2: Tuple[int, ...]) -> float:
        equal = sum(1 for x, y in zip(s1, s2) if x == y)
        return equal / self.num_perm

    def is_duplicate(self, text: str) -> bool:
        sig = self._minhash(text)
        candidates: Set[int] = set()
        band_keys: List[Any] = []
        for band in range(self.bands):
            start = band * self.rows
            key = sig[start:start + self.rows]
            band_keys.append(key)
            bucket = self._buckets[band].get(key)
            if bucket:
                candidates.update(bucket)

        for cand in candidates:
            if self._estimate_jaccard(sig, self._signatures[cand]) >= self.threshold:
                return True

        # Nouveau : on l'enregistre
        idx = len(self._signatures)
        self._signatures.append(sig)
        for band, key in enumerate(band_keys):
            self._buckets[band].setdefault(key, []).append(idx)
        return False


# =============================================================================
#  API PUBLIQUE
# =============================================================================

def make_deduper(threshold: float = DEFAULT_THRESHOLD,
                 num_perm: int = DEFAULT_NUM_PERM):
    """
    Construit un dédupeur incrémental exposant `.is_duplicate(text) -> bool`.

    Utilise datasketch si installé, sinon un repli intégré équivalent.
    """
    try:
        import datasketch  # noqa: F401
        return _DatasketchDeduper(threshold, num_perm)
    except ImportError:
        logger.debug("[dedup] datasketch indisponible, repli MinHash/LSH intégré")
        return _FallbackDeduper(threshold, num_perm)
    except Exception as e:
        # datasketch 2.x refuse certains couples (threshold, num_perm) que 1.x
        # acceptait (« number of bands too small ») : plutôt que d'imposer un
        # paramétrage, on bascule sur le repli intégré — mêmes garanties.
        logger.debug(f"[dedup] datasketch inutilisable ici ({e}) — repli intégré")
        return _FallbackDeduper(threshold, num_perm)


def deduplicate(
    items: Iterable[Any],
    key: Callable[[Any], str] = lambda x: x,
    threshold: float = DEFAULT_THRESHOLD,
    num_perm: int = DEFAULT_NUM_PERM,
    log=None,
) -> List[Any]:
    """
    Filtre une séquence en retirant les quasi-doublons.

    Args:
        items: éléments à dédupliquer (chunks, textes…)
        key: fonction extrayant le texte comparé (défaut: identité)
        threshold: seuil de similarité de Jaccard (défaut 0.85)
        num_perm: nombre de permutations MinHash (défaut 128)
        log: logger optionnel

    Returns:
        Liste des éléments conservés (premier vu de chaque groupe de doublons).
    """
    _log = log or logger
    deduper = make_deduper(threshold, num_perm)
    kept: List[Any] = []
    removed = 0

    for item in items:
        text = key(item)
        if text and text.strip() and deduper.is_duplicate(text):
            removed += 1
            continue
        kept.append(item)

    if removed:
        _log.info(f"[dedup] {removed} quasi-doublon(s) retiré(s), {len(kept)} conservé(s)")
    return kept
