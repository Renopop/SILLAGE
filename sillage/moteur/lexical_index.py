# lexical_index.py
"""
Index lexical BM25 - §5.2

BM25 capture les correspondances exactes de termes, là où le vectoriel échoue :
il attrape les références et codes techniques mot pour mot (P/N, ATA, normes,
unités, acronymes).

Note bilingue (§5.2) : BM25 est lexical et ne franchit pas la barrière de
langue — c'est le rôle de l'embedding crosslingue (§5.1). La fusion hybride
(§6.1) combine les deux forces.

FORMAT PUBLIÉ (audit V2, C-3 / C-12). L'index publié ne contient plus le corpus
tokenisé (que chaque lecteur devait re-parser, re-compter et re-pondérer en
Python pur : ~10 s par 100 000 fragments au chargement, ~4 s par million par
question, ~4,5 Go par million en mémoire). Les STATISTIQUES sont calculées UNE
FOIS à l'indexation et publiées sous forme de tableaux numpy :

  - matrice creuse des fréquences de termes, orientée TERME (une ligne par
    terme du vocabulaire, ses « postings » = fragments où il apparaît) :
    `indptr` / `indices` (n° de fragment) / `tf` (fréquence) ;
  - `doc_len` (longueur de chaque fragment), `idf` (par terme, formule Lucene
    lissée non négative), `vocab` (terme -> ligne), `faiss_ids`.

Le lecteur charge des tableaux (quelques secondes pour un million de
fragments, aucune boucle Python par document ni par token), calcule les scores
par produit creux numpy sur les seuls postings des termes de la question, et
prend le top-k par `argpartition`. Les scores sont IDENTIQUES à ceux de
l'implémentation historique (BM25 Okapi, k1 = 1,5, b = 0,75, idf lissé), à
l'élagage près des termes omniprésents (audit perf 2.1, L-6 : écart < 1e-3
par terme, voir DEFAULT_PRUNE_IDF).

COMPATIBILITÉ : `load` lit encore l'ancien format pickle {"faiss_ids",
"tokenized"} (générations antérieures rencontrées par les lecteurs) en
reconstruisant les statistiques — puis libère le corpus tokenisé. L'index est
indexé par `faiss_id` pour être fusionnable avec l'index vectoriel au moment de
la requête, et se sérialise par pickle avec **publication atomique**
(os.replace, même principe qu'en §5.1).
"""

import os
import re
import pickle
import logging
from typing import List, Dict, Tuple, Optional, Iterable

import numpy as np

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# Constantes BM25 Okapi (celles du repli intégré historique).
BM25_K1 = 1.5
BM25_B = 0.75

# Version du format publié (clé "format" du pickle). Absente = ancien format.
FORMAT = 2

# Termes OMNIPRÉSENTS (audit perf 2.1, L-6) : un terme présent dans presque
# tous les fragments a un idf ≈ 0 (1e-5 pour df = n) ; sa contribution
# maximale à un score vaut idf × (k1 + 1), soit quelques 1e-5 — un passage
# complet sur TOUS les fragments (2,5 ms par 200 000, 62 ms à 5 M) pour un
# effet nul sur le classement. Au-delà de ce seuil de contribution, le terme
# est sauté à la requête, et ses postings sont élagués à l'indexation
# (2 à 6 % de la mémoire du BM25). RIVAGE_BM25_PRUNE_IDF : 0 = désactivé.
DEFAULT_PRUNE_IDF = 1e-3


def prune_idf_threshold() -> float:
    """Seuil de contribution idf × (k1 + 1) sous lequel un terme est ignoré
    (RIVAGE_BM25_PRUNE_IDF, défaut 1e-3, 0 = jamais)."""
    v = (os.environ.get("RIVAGE_BM25_PRUNE_IDF") or "").strip()
    if not v:
        return DEFAULT_PRUNE_IDF
    try:
        return max(0.0, float(v))
    except ValueError:
        logger.warning(f"[lexical_index] RIVAGE_BM25_PRUNE_IDF={v!r} non numérique — "
                       f"défaut {DEFAULT_PRUNE_IDF} utilisé.")
        return DEFAULT_PRUNE_IDF


def tokenize(text: str) -> List[str]:
    """Tokenisation simple, minuscule, unicode (mots alphanumériques)."""
    return _TOKEN_RE.findall(text.lower())


# =============================================================================
#  STATISTIQUES BM25 PRÉCALCULÉES
# =============================================================================

class _StatsAccumulator:
    """
    Accumulateur de statistiques BM25 EN FLUX (audit perf 2.1, I-1).

    L'ancienne construction matérialisait trois représentations complètes du
    corpus (listes de tokens, copie internée, puis six tableaux int64 pour le
    tri) : 13 Ko et 355 µs par fragment — 67 Go pour 5 M de fragments, soit
    une publication impossible au-delà de 1 à 2 M de fragments sur un poste.

    Ici, chaque fragment est tokenisé puis AUSSITÔT converti en une colonne
    d'identifiants de termes int32 (le texte et ses tokens sont jetés) ; les
    colonnes s'accumulent dans un tampon unique qui double de taille (pas de
    liste de tableaux à concaténer), avec la longueur de chaque fragment. À
    la fin, une seule clé combinée int64 (terme << 32 | n° de fragment) est
    triée EN PLACE (quicksort, sans tableau auxiliaire) : les frontières de la
    clé triée donnent les postings (fragment, tf) de chaque terme, exactement
    comme le tri lexicographique (terme, fragment) de l'implémentation
    précédente. Statistiques IDENTIQUES (ordre du vocabulaire, df, idf, tf,
    doc_len, élagage), mémoire au pic ≈ 2,5 Ko par fragment (÷5), temps ÷2.

    Limites du codage : 2^31 termes distincts et 2^32 fragments par index.
    """

    _INITIAL = 1 << 16

    def __init__(self):
        self.vocab: Dict[str, int] = {}
        self._cols = np.empty(self._INITIAL, dtype=np.int32)
        self._total = 0
        self._doc_len: List[int] = []

    def add(self, tokens: List[str]) -> None:
        """Ajoute un fragment (liste de tokens) ; les tokens sont oubliés."""
        n = len(tokens)
        vocab = self.vocab
        if self._total + n > self._cols.shape[0]:
            # Croissance géométrique : amortie, une seule recopie à chaque
            # doublement (jamais une liste de 100 000 petits tableaux).
            new_size = max(self._cols.shape[0] * 2, self._total + n)
            grown = np.empty(new_size, dtype=np.int32)
            grown[:self._total] = self._cols[:self._total]
            self._cols = grown
        # `setdefault(t, len(vocab))` : len évalué AVANT l'insertion -> colonne
        # suivante pour un terme nouveau, colonne existante sinon.
        self._cols[self._total:self._total + n] = np.fromiter(
            (vocab.setdefault(t, len(vocab)) for t in tokens),
            dtype=np.int32, count=n)
        self._total += n
        self._doc_len.append(n)

    def finish(self, prune_idf: Optional[float] = None) -> Dict[str, object]:
        """Calcule et retourne les tableaux du format 2 (voir _build_stats)."""
        n = len(self._doc_len)
        total = self._total
        doc_len = np.asarray(self._doc_len, dtype=np.int64)
        self._doc_len = []
        n_terms = len(self.vocab)
        if total:
            # Clé combinée : terme dans les 32 bits hauts, fragment dans les
            # 32 bits bas. Un seul tableau int64 vivant (le tampon int32 est
            # libéré dès la conversion), rempli fragment par fragment sans
            # `np.repeat` intermédiaire de 8 octets par token.
            key = self._cols[:total].astype(np.int64)
            self._cols = np.empty(0, dtype=np.int32)
            key <<= 32
            pos = 0
            for d, ln in enumerate(doc_len.tolist()):
                if ln:
                    key[pos:pos + ln] |= d
                    pos += ln
            key.sort()                            # en place : (terme, fragment)
            change = np.empty(total, dtype=bool)
            change[0] = True
            np.not_equal(key[1:], key[:-1], out=change[1:])
            starts = np.flatnonzero(change)
            del change
            tf = np.diff(np.append(starts, total))
            heads = key[starts]
            del key
            indices = (heads & 0xFFFFFFFF).astype(np.int32)
            term_of_posting = heads >> 32
            del heads
            df = np.bincount(term_of_posting, minlength=n_terms)
        else:
            self._cols = np.empty(0, dtype=np.int32)
            tf = np.zeros(0, dtype=np.int64)
            indices = np.zeros(0, dtype=np.int32)
            term_of_posting = np.zeros(0, dtype=np.int64)
            df = np.zeros(n_terms, dtype=np.int64)
        return _stats_from_postings(n, total, doc_len, tf, indices,
                                    term_of_posting, df, self.vocab, prune_idf)


def _stats_from_postings(n, total, doc_len, tf, indices, term_of_posting, df,
                         vocab, prune_idf):
    """IDF, élagage (L-6) et tableaux finaux à partir des postings triés."""
    n_terms = len(vocab)
    # IDF Okapi lissé, non négatif (formule Lucene) — identique au repli
    # intégré historique et à la surcharge appliquée à rank-bm25.
    idf = np.log1p((n - df + 0.5) / (df + 0.5)).astype(np.float32)
    seuil = prune_idf_threshold() if prune_idf is None else float(prune_idf)
    n_elagues = 0
    if seuil > 0 and total:
        omnipresents = idf * (BM25_K1 + 1.0) < seuil
        n_elagues = int(omnipresents.sum())
        if n_elagues:
            garder = ~omnipresents[term_of_posting]
            tf, indices = tf[garder], indices[garder]
            df = np.where(omnipresents, 0, df)
            logger.info(f"[lexical_index] {n_elagues} terme(s) omniprésent(s) "
                        f"élagué(s) (contribution < {seuil:g}), "
                        f"{int((~garder).sum())} postings retirés.")
    indptr = np.zeros(n_terms + 1, dtype=np.int64)
    np.cumsum(df, out=indptr[1:])
    return {
        "prune_idf": seuil,
        "n_elagues": n_elagues,
        "indptr": indptr,
        "indices": indices,
        # Un fragment de 800 caractères ne dépasse jamais 65 535 occurrences
        # d'un même terme : uint16 divise par deux l'empreinte des postings.
        "tf": np.minimum(tf, 65535).astype(np.uint16),
        "doc_len": doc_len.astype(np.int32),
        "idf": idf,
        "vocab": list(vocab.keys()),             # ordre d'insertion = colonne
    }


def _build_stats(tokenized_corpus: Iterable[List[str]],
                 prune_idf: Optional[float] = None) -> Dict[str, object]:
    """
    Calcule les statistiques BM25 d'un corpus tokenisé (constructeur direct,
    ou lecteur sur une génération à l'ancien format). Retourne les tableaux
    du format 2. Même accumulateur en flux que `LexicalIndex.build` (I-1) :
    les listes de tokens ne sont lues qu'une fois, dans l'ordre.

    `prune_idf` (audit perf 2.1, L-6 ; None = RIVAGE_BM25_PRUNE_IDF) : les
    postings des termes dont idf × (k1 + 1) est sous ce seuil sont ÉLAGUÉS —
    le terme reste au vocabulaire (avec son idf) mais sans fragments, donc
    sauté à la requête ; les longueurs de fragments gardent ses occurrences
    (les scores des autres termes sont inchangés). Effet sur un score :
    inférieur au seuil par terme omniprésent de la question.
    """
    acc = _StatsAccumulator()
    for doc in tokenized_corpus:
        acc.add(doc)
    return acc.finish(prune_idf)


# =============================================================================
#  INDEX LEXICAL
# =============================================================================

class LexicalIndex:
    """
    Index BM25 mappé sur les identifiants FAISS stables (§5.2, §8).

    Deux voies de construction : `LexicalIndex(faiss_ids, tokenized_corpus)`
    (indexeur, via `build`) calcule les statistiques ; `load` relit les
    tableaux publiés (ou reconstruit depuis l'ancien format).
    """

    def __init__(self, faiss_ids: List[int], tokenized_corpus: Iterable[List[str]]):
        # Plus aucune copie du corpus conservée (audit perf 2.1, I-1) : les
        # listes de tokens sont consommées en flux par l'accumulateur et
        # jetées ; l'index ne garde que les tableaux du format 2.
        self._tokenized = None
        self._init_arrays(np.asarray(list(faiss_ids), dtype=np.int64),
                          _build_stats(tokenized_corpus))

    def _init_arrays(self, faiss_ids: np.ndarray, stats: Dict[str, object]) -> None:
        self.faiss_ids = faiss_ids
        self._indptr = np.asarray(stats["indptr"], dtype=np.int64)
        self._indices = np.asarray(stats["indices"])
        self._tf = np.asarray(stats["tf"])
        self._doc_len = np.asarray(stats["doc_len"])
        self._idf = np.asarray(stats["idf"], dtype=np.float32)
        # Seuil d'élagage appliqué à la CONSTRUCTION (None : index publié
        # avant L-6, ou inconnu) — informatif, ré-écrit par `save`.
        self._prune_idf_build = stats.get("prune_idf")
        vocab = stats["vocab"]
        self._vocab: Dict[str, int] = (vocab if isinstance(vocab, dict)
                                       else dict(zip(vocab, range(len(vocab)))))
        self.k1 = float(stats.get("k1", BM25_K1))
        self.b = float(stats.get("b", BM25_B))
        # Seuil de saut à la requête (L-6) : celui de l'environnement du
        # lecteur — un index publié sans élagage reste accéléré, et
        # RIVAGE_BM25_PRUNE_IDF=0 côté lecteur rend les scores exacts.
        self._prune_idf = prune_idf_threshold()
        n = len(self.faiss_ids)
        self.avgdl = (float(self._doc_len.sum()) / n) if n else 0.0
        # Dénominateur par fragment, indépendant de la question :
        # k1 * (1 - b + b * dl / avgdl) — précalculé une fois (vectorisé).
        self._K = (self.k1 * (1.0 - self.b + self.b * self._doc_len.astype(np.float64)
                              / (self.avgdl or 1.0)))

    @classmethod
    def _from_stats(cls, faiss_ids, stats: Dict[str, object]) -> "LexicalIndex":
        obj = cls.__new__(cls)
        obj._tokenized = None          # lecteur : jamais le corpus en mémoire
        obj._init_arrays(np.asarray(faiss_ids, dtype=np.int64), stats)
        return obj

    @classmethod
    def build(cls, chunks: Iterable[Dict]) -> "LexicalIndex":
        """
        Construit l'index à partir de chunks {texte, faiss_id, ...}
        (typiquement l'itération du magasin de contenu, filtrée par le
        registre). Construction À MÉMOIRE CONSTANTE par fragment (audit perf
        2.1, I-1) : chaque texte est tokenisé puis immédiatement réduit à une
        colonne d'identifiants de termes int32 — ni corpus tokenisé, ni copie
        internée ne sont jamais matérialisés. Statistiques et classements
        identiques à la construction précédente.
        """
        faiss_ids: List[int] = []
        acc = _StatsAccumulator()
        for c in chunks:
            faiss_ids.append(c["faiss_id"])
            acc.add(tokenize(c["texte"]))
        return cls._from_stats(faiss_ids, acc.finish())

    # --- Scoring ---------------------------------------------------------------

    def scores(self, query_tokens: List[str]) -> np.ndarray:
        """Score BM25 de CHAQUE fragment (float64, 0 si aucun terme commun).

        Pour chaque terme de la question (répétitions comptées, comme
        rank-bm25 et le repli historique), seuls ses postings sont touchés :
        scores[d] += idf(t) * tf * (k1 + 1) / (tf + K[d]).
        """
        n = len(self.faiss_ids)
        scores = np.zeros(n, dtype=np.float64)
        k1 = self.k1
        # Termes omniprésents (audit perf 2.1, L-6) : contribution maximale
        # idf × (k1 + 1) sous le seuil -> aucun effet sur le classement, mais
        # un passage sur tous les fragments ; sautés (seuil 0 = jamais).
        seuil = self._prune_idf
        for tok in query_tokens:
            col = self._vocab.get(tok)
            if col is None:
                continue
            if seuil > 0 and float(self._idf[col]) * (k1 + 1.0) < seuil:
                continue
            lo, hi = int(self._indptr[col]), int(self._indptr[col + 1])
            if hi <= lo:
                continue
            docs = self._indices[lo:hi]
            tf = self._tf[lo:hi].astype(np.float64)
            scores[docs] += float(self._idf[col]) * (tf * (k1 + 1.0)) / (tf + self._K[docs])
        return scores

    def search(self, query: str, k: int,
               allowed_ids=None) -> List[Tuple[int, float]]:
        """
        Retourne les k meilleurs (faiss_id, score) pour la requête.
        Ignore les scores nuls (aucun terme en commun). Si `allowed_ids` est
        fourni (filtre de portée), la restriction est appliquée avant le
        tri top-k (masque numpy) — recall exact sans matérialiser tout le
        corpus. Trois formes (audit perf 2.1, L-1 / L-11) : un masque
        booléen ALIGNÉ sur `faiss_ids` (celui de la portée précalculée :
        aucune recherche d'appartenance), un tableau d'identifiants, ou un
        `set`/itérable (API historique). Les ex æquo gardent l'ordre de
        l'index (tri stable, comme l'implémentation historique).
        """
        n = len(self.faiss_ids)
        if n == 0 or k <= 0:
            return []
        scores = self.scores(tokenize(query))
        mask = scores > 0
        if allowed_ids is not None:
            if isinstance(allowed_ids, np.ndarray) and allowed_ids.dtype == np.bool_:
                if allowed_ids.shape[0] != n:
                    raise ValueError(
                        f"masque de portée de longueur {allowed_ids.shape[0]} "
                        f"pour un index BM25 de {n} fragments")
                mask &= allowed_ids
            else:
                if isinstance(allowed_ids, np.ndarray):
                    allowed_arr = np.asarray(allowed_ids, dtype=np.int64).ravel()
                else:
                    allowed_arr = np.fromiter(allowed_ids, dtype=np.int64,
                                              count=len(allowed_ids))
                mask &= np.isin(self.faiss_ids, allowed_arr)
        cand = np.flatnonzero(mask)
        if cand.size == 0:
            return []
        if cand.size > k:
            cand = cand[np.argpartition(-scores[cand], k - 1)[:k]]
        # Tri final : score décroissant, puis position croissante (stable).
        order = np.lexsort((cand, -scores[cand]))
        cand = cand[order]
        return [(int(self.faiss_ids[i]), float(scores[i])) for i in cand]

    @property
    def size(self) -> int:
        return int(len(self.faiss_ids))

    # --- Persistance atomique -------------------------------------------------

    def save(self, path: str) -> None:
        """Sérialise l'index (pickle, format 2 : tableaux numpy) avec
        publication atomique (os.replace)."""
        payload = {
            "format": FORMAT,
            "k1": self.k1, "b": self.b,
            "faiss_ids": np.ascontiguousarray(self.faiss_ids, dtype=np.int64),
            "indptr": self._indptr, "indices": self._indices, "tf": self._tf,
            "doc_len": self._doc_len, "idf": self._idf,
            "vocab": list(self._vocab.keys()),
            "prune_idf": getattr(self, "_prune_idf_build", None),
        }
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
        logger.info(f"[lexical_index] Index BM25 publié ({self.size} chunks, "
                    f"{len(self._vocab)} termes) -> {path}")

    @classmethod
    def load(cls, path: str) -> "LexicalIndex":
        """Charge un index publié : format 2 (tableaux) ou ancien format
        (corpus tokenisé, reconstruit puis libéré)."""
        with open(path, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict) and data.get("format") == FORMAT:
            return cls._from_stats(data["faiss_ids"], data)
        # Ancien format {"faiss_ids", "tokenized"} : une génération publiée par
        # un indexeur antérieur. Reconstruire les statistiques (sans
        # internement : le corpus est jeté aussitôt après).
        logger.info(f"[lexical_index] Ancien format BM25 ({os.path.basename(path)}) : "
                    f"statistiques reconstruites au chargement — l'indexeur publiera "
                    f"le nouveau format à sa prochaine génération.")
        tokenized = data["tokenized"]
        obj = cls._from_stats(data["faiss_ids"], _build_stats(tokenized))
        del tokenized, data
        return obj
