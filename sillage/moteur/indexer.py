# indexer.py
"""
Orchestrateur d'indexation incrémentale - §8.4

Assemble la phase d'indexation complète (§2.1, étapes 1 à 6) en un flux
incrémental (§8.4) :

    1. Parcours récursif                → liste des fichiers présents
    2. Pré-filtre + hash de contenu     → new / unchanged / moved / modified
    3. Nouveaux / modifiés              → extraction, chunk, dédup, embed, ajout
    4. Modifiés                         → remove_ids des anciens vecteurs d'abord
    5. Disparus                         → remove_ids + purge du registre
    6. Publication ATOMIQUE par génération (manifest) + commit SQLite

La publication passe par `store.Publisher` : chaque run écrit une nouvelle
génération d'artefacts, rendue visible d'un coup par la bascule atomique du
manifest. Un échec en cours de run laisse la génération précédente intacte
(pas de corruption, pas de réutilisation d'ids) : il suffit de relancer, la
reprise repart du dernier état publié (§9.1).

Le seul poste sortant est l'embedding (API interne, §5.1) ; le corpus ne quitte
pas l'organisation. L'`embedder` est injectable (tests hors-ligne / mock).
"""

import os
import sys
import time
import logging
from dataclasses import dataclass
from typing import Optional, List, Callable, Any, Dict

logger = logging.getLogger(__name__)

from extraction import walk_corpus, extract_document, _family_of, is_supported
from registry import Registry, FileRecord
from vector_index import VectorIndex
from lexical_index import LexicalIndex
from store import Publisher, reader_paths, validate_generation
from content_store import ContentSegmentWriter, ContentStore


class _StopDuringExtraction(Exception):
    """Arrêt coopératif survenu PENDANT l'extraction d'un fichier : son texte
    peut être partiel (OCR interrompu page à page) — le fichier ne doit pas
    être enregistré, il sera repris au prochain run."""


class _TransientFailure(Exception):
    """Échec TRANSITOIRE sur un fichier : NON enregistré (il sera retenté au
    prochain run), comptage défait, et le run CONTINUE avec les suivants."""

    def __init__(self, path: str, reason: str = ""):
        super().__init__(path)
        self.path = path
        self.reason = reason


class _TransientOcrFailure(_TransientFailure):
    """Des pages OCR ont échoué sur erreur TRANSITOIRE (504/502/503/429,
    timeout — passerelle Vision saturée) : le texte est incomplet mais
    RE-TENTABLE. Le fichier ne doit pas être enregistré — sinon son hash
    serait figé et les pages manquantes ne seraient JAMAIS re-OCRisées. Le
    run CONTINUE avec les fichiers suivants ; celui-ci sera repris au
    prochain run, où seules les pages manquantes seront repayées (cache)."""


class _FaissLoadError(RuntimeError):
    """Échec du chargement du FAISS publié (mémoire, fichier corrompu). Le
    chargement est PARESSEUX (audit perf 2.1, I-6) et peut donc survenir au
    milieu du traitement d'un fichier : cette erreur doit arrêter le run —
    jamais être prise pour un fichier pathologique à sauter, sinon un index
    NEUF serait créé à côté d'un registre qui référence l'ancien."""


class _TransientExtractionFailure(_TransientFailure):
    """L'extraction a échoué sur une erreur d'ACCÈS (verrou, hoquet SMB,
    fichier en cours de copie) : enregistrer le fichier le figerait avec son
    hash sans aucun contenu, et il ne serait jamais ré-extrait tant que son
    contenu ne change pas (audit V2, A-2). Non enregistré, retenté."""


@dataclass
class IndexStats:
    new: int = 0
    modified: int = 0
    moved: int = 0
    unchanged: int = 0
    removed: int = 0
    deduped: int = 0
    chunks_added: int = 0
    errors: int = 0
    generation: int = 0
    ocr_pages: int = 0      # pages payées à l'API Vision (visibilité du coût)
    ocr_pending_docs: int = 0  # documents en attente de rattrapage OCR
    stopped: bool = False   # arrêt propre demandé (parcours incomplet)

    def summary(self) -> str:
        return (
            f"new={self.new} modified={self.modified} moved={self.moved} "
            f"unchanged={self.unchanged} removed={self.removed} "
            f"deduped={self.deduped} chunks+={self.chunks_added} "
            f"errors={self.errors} gen={self.generation}"
            + (f" pages_OCR={self.ocr_pages}" if self.ocr_pages else "")
            + (f" ocr_en_attente={self.ocr_pending_docs}"
               if self.ocr_pending_docs else "")
            + (" [ARRÊT ANTICIPÉ — index partiel]" if self.stopped else "")
        )


def index_corpus(
    corpus_root: str,
    index_dir: str,
    embedder: Any = None,
    use_ocr: bool = True,
    ocr_quality_threshold: Optional[float] = None,
    dedup_threshold: float = 0.85,
    exclude_themes: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    checkpoint_files: Optional[int] = None,
    checkpoint_min_s: Optional[float] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    log=None,
) -> IndexStats:
    """
    Indexe (ou met à jour) le corpus de façon incrémentale.

    Args:
        corpus_root: racine du corpus à parcourir
        index_dir: répertoire où publier les artefacts (réseau partagé)
        embedder: objet exposant embed_passages(list[str]) -> np.ndarray (N,dim)
                  et .dim. Si None, un EmbeddingClient (API interne) est créé.
        use_ocr: autorise l'OCR de secours (PDF sous seuil, images PPTX)
        ocr_quality_threshold: seuil qualité OCR (§3.1). None (défaut) =
                  RIVAGE_OCR_QUALITY_THRESHOLD ou 0.5 — l'ABAISSER (ex. 0.35)
                  réduit le coût : seules les pages vraiment illisibles
                  partent en OCR Vision.
        dedup_threshold: seuil de déduplication MinHash/LSH (§4.2)
        exclude_themes: thèmes (répertoires de 1er niveau) à NE PAS indexer.
                  Fusionnés avec la config appli et le .rivageignore du corpus.
        progress_cb: callback (ratio, message)
        checkpoint_files: publier un point de reprise intermédiaire tous les N
                  fichiers NOUVEAUX/MODIFIÉS traités (0 = jamais). Met le travail
                  déjà encodé à l'abri d'un plantage : la reprise repart du
                  dernier point publié au lieu de tout recommencer. Défaut :
                  variable d'environnement RIVAGE_CHECKPOINT_FILES ou 200.
        should_stop: callable optionnel renvoyant True pour demander un ARRÊT
                  PROPRE. Vérifié avant chaque fichier : le parcours s'arrête,
                  et une génération finale contenant TOUT ce qui a déjà été
                  traité est publiée (index cohérent et consultable). La purge
                  des fichiers disparus est alors sautée (parcours incomplet).
                  La reprise ultérieure repart de là (incrémental par hash).
        checkpoint_min_s: délai MINIMAL entre deux points de reprise (secondes).
                  Chaque point réécrit le FAISS cumulé : sans plancher temporel,
                  une petite cadence (ex. 5 fichiers) rendrait l'indexation
                  quadratique sur un gros corpus (audit). Défaut :
                  RIVAGE_CHECKPOINT_MIN_S ou 300 s. Le plancher effectif est
                  ADAPTATIF (audit perf 2.1, I-4) : max(checkpoint_min_s,
                  20 × durée du point précédent), journalisé à chaque point.
                  0 (explicite) désactive tout plancher, adaptatif compris.
                  Le BM25 n'est reconstruit qu'à la publication FINALE (les
                  générations intermédiaires réutilisent le BM25 précédent,
                  éventuellement en retard).
        log: logger optionnel

    Returns:
        IndexStats du run.
    """
    _log = log or logger
    os.makedirs(index_dir, exist_ok=True)
    stats = IndexStats()

    # Thèmes exclus : union de l'argument, de la config appli et du .rivageignore
    # du corpus. Un thème exclu n'est jamais parcouru ni encodé, et ses contenus
    # déjà indexés sont retirés (voir boucle des « disparus »).
    # Lecture STRICTE (audit V2, A-8) : une config ou un .rivageignore présent
    # mais illisible (hoquet SMB au démarrage, JSON corrompu) REFUSE le run.
    # Continuer avec les seules exclusions passées en argument ferait parcourir,
    # encoder (coût API) et PUBLIER aux lecteurs des thèmes censés être
    # protégés, purgés seulement au run suivant. Un fichier simplement absent
    # reste un cas normal (aucune exclusion).
    try:
        import config
        excluded = set(exclude_themes or []) | config.get_excluded_themes(
            corpus_root, strict=True)
    except Exception as e:
        raise RuntimeError(
            f"Lecture des exclusions de thèmes impossible ({e}) — indexation "
            f"REFUSÉE pour ne pas encoder ni publier des thèmes protégés. "
            f"Vérifiez rivage.config.json et .rivageignore à la racine du "
            f"corpus, puis relancez.") from e
    # Normalisation (m8) : sans / final ; comparaison insensible à la casse via
    # normcase (répertoires Windows insensibles à la casse, exacts sous POSIX).
    excluded = {str(t).strip().rstrip("/\\") for t in excluded if str(t).strip()}
    excluded_nc = {os.path.normcase(t) for t in excluded}
    if excluded:
        _log.info(f"[indexer] Thèmes exclus : {sorted(excluded)}")

    # Embedder (API interne par défaut) ---------------------------------------
    own_embedder = embedder is None
    if embedder is None:
        from embeddings import EmbeddingClient
        embedder = EmbeddingClient()

    # R1 : refuser de continuer si la génération publiée est incohérente
    # (fichiers référencés introuvables) — sinon on repartirait d'un index
    # vide en croyant faire de l'incrémental, perdant tout le corpus.
    validate_generation(index_dir)

    # Point de reprise : publier une génération intermédiaire tous les N
    # fichiers nouveaux/modifiés traités, pour ne pas tout perdre en cas de
    # plantage. 0 désactive (comportement historique : une seule publication).
    if checkpoint_files is None:
        checkpoint_files = _env_int("RIVAGE_CHECKPOINT_FILES", 200)
    checkpoint_files = max(0, int(checkpoint_files))
    if checkpoint_min_s is None:
        checkpoint_min_s = _env_float("RIVAGE_CHECKPOINT_MIN_S", 300.0)
    checkpoint_min_s = max(0.0, float(checkpoint_min_s))

    # Seuil de déclenchement de l'OCR Vision — configurable sans CLI
    # (RIVAGE_OCR_QUALITY_THRESHOLD) : l'abaisser réduit le COÛT (moins de
    # pages jugées « mauvaises »), l'augmenter améliore le rappel sur scans.
    if ocr_quality_threshold is None:
        ocr_quality_threshold = _env_float("RIVAGE_OCR_QUALITY_THRESHOLD", 0.5)
    ocr_quality_threshold = min(1.0, max(0.0, float(ocr_quality_threshold)))

    # Récapitulatif de la configuration OCR dans le JOURNAL (logging) : les
    # bannières console (print) sont invisibles en exe fenêtré, et le nombre
    # de workers n'apparaît qu'au moment d'un dispatch — cette ligne donne la
    # visibilité dès le départ, quel que soit le mode de lancement.
    # Concurrence OCR TOTALE visée (répartie entre les processus d'extraction
    # quand il y en a plusieurs, I-5) ; 0 = inconnue (llm_ocr absent).
    ocr_eff = 0
    if use_ocr:
        try:
            from llm_ocr import OCR_PARALLEL_WORKERS as _ocr_workers_auto
        except ImportError:
            _ocr_workers_auto = None
        if _ocr_workers_auto is not None:
            _cap = _env_int("RIVAGE_OCR_WORKERS", 0)
            _eff = min(_cap, _ocr_workers_auto) if _cap else _ocr_workers_auto
            ocr_eff = int(_eff or 0)
            _src = (f"plafond RIVAGE_OCR_WORKERS={_cap}, auto={_ocr_workers_auto}"
                    if _cap else "auto RAM/CPU")
            _rot = (os.environ.get("RIVAGE_OCR_SMART_ROTATE") or "0").strip() == "1"
            _cap_doc = _env_int("RIVAGE_OCR_MAX_PAGES_DOC", 0)
            _log.info(
                f"[indexer] OCR Vision : {_eff} worker(s) en parallèle ({_src}) "
                f"— la concurrence s'adapte ensuite aux 504 ; seuil qualité "
                f"{ocr_quality_threshold:.2f} ; orientation LLM "
                f"{'activée' if _rot else 'désactivée'} ; plafond pages/doc : "
                f"{_cap_doc if _cap_doc else 'illimité'}.")
    if checkpoint_files:
        _log.info(f"[indexer] Points de reprise : tous les {checkpoint_files} "
                  f"fichiers nouveaux/modifiés, au plus un toutes les "
                  f"{checkpoint_min_s:.0f} s (plancher relevé à "
                  f"{_CHECKPOINT_FLOOR_FACTOR} × la durée du point précédent).")

    # Nouvelle génération : registre semé depuis la génération publiée ---------
    pub = Publisher(index_dir, log=_log)
    reg = Registry(pub.seed_registry())
    # UN SEUL instantané de l'état AU DÉMARRAGE du run (pas d'un checkpoint) :
    # {hash: (chemin, famille)}, pour la purge des disparus (famille lue au
    # registre, M2) qui n'a lieu qu'à la publication FINALE. Un second dict
    # {hash: chemin} doublait la mémoire pour rien (audit V2, A-12).
    initial_families = reg.all_files()
    # Doublons de contenu connus au démarrage {chemin: hash} (audit V2, A-4) :
    # la purge finale retire ceux dont le chemin a disparu.
    initial_aliases = reg.all_aliases()

    # Index vectoriel : chargement PARESSEUX de la génération publiée (audit
    # perf 2.1, I-6). Il n'est chargé qu'au premier besoin : avant le premier
    # ajout de vecteurs (_process_file), avant un retrait groupé (ids en
    # attente) et avant une publication qui réécrit le FAISS. Un run « aucun
    # changement » — le cas courant d'une indexation planifiée — ne lit donc
    # plus les gigaoctets du FAISS (~2 min et 10 Go à 5 M de fragments) pour
    # les jeter. Une fois chargé, il reste EN MÉMOIRE d'un point de reprise à
    # l'autre (cumulatif) : les points réécrivent le FAISS complet sans jamais
    # le recharger du disque.
    # Le registre, lui, est toujours copié (seed_registry) : le pré-filtre
    # (classify) ÉCRIT dans la base de la nouvelle génération dès le parcours
    # (rafraîchissement des fichiers « touchés », alias — A-3/A-4), et le
    # différer exigerait un registre ouvrable en lecture seule puis basculé en
    # écriture à la première modification, hors du périmètre de ce lot.
    vindex: Optional[VectorIndex] = None
    faiss_loaded = [False]   # [bool] : chargement tenté (même si aucun FAISS)

    def _ensure_vindex():
        """Charge le FAISS publié à la PREMIÈRE demande (une seule fois par
        run) et retourne l'index courant (None si rien n'est publié : il sera
        créé au premier ajout). Après un point de reprise, `pub` est renouvelé
        mais l'index reste celui en mémoire."""
        nonlocal vindex
        if faiss_loaded[0]:
            return vindex
        faiss_loaded[0] = True
        prev_faiss = pub.prev_faiss_path()
        if not prev_faiss:
            return vindex
        # Diagnostic mémoire : un std::bad_alloc DÈS la reprise vient soit du
        # chargement de cet index (fichier corrompu/tronqué => FAISS lit une
        # taille aberrante et tente une allocation géante ; ou RAM réellement
        # insuffisante), soit du premier fichier retraité. Contextualiser pour
        # que l'utilisateur sache immédiatement lequel.
        try:
            size_mb = os.path.getsize(prev_faiss) / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        try:
            import psutil
            avail_mb = psutil.virtual_memory().available / (1024 * 1024)
            _log.info(f"[indexer] Chargement FAISS gén. {pub.prev_gen} : "
                      f"{size_mb:.0f} Mo (RAM disponible : {avail_mb:.0f} Mo).")
        except ImportError:
            _log.info(f"[indexer] Chargement FAISS gén. {pub.prev_gen} : "
                      f"{size_mb:.0f} Mo.")
        # Deltas de points de reprise (audit perf 2.1, I-4) : la génération
        # publiée peut être « base + deltas » (run précédent tué entre deux
        # points, ou après un point en delta) — la base est chargée puis les
        # deltas fusionnés, dans l'ordre.
        prev_deltas = pub.prev_faiss_delta_paths()
        try:
            if prev_deltas:
                vindex = VectorIndex.load_avec_deltas(prev_faiss, prev_deltas)
            else:
                vindex = VectorIndex.load(prev_faiss)
        except (RuntimeError, MemoryError) as e:
            if "bad_alloc" in str(e).lower() or isinstance(e, MemoryError):
                raise _FaissLoadError(
                    f"Échec MÉMOIRE au chargement de l'index FAISS "
                    f"({os.path.basename(prev_faiss)}, {size_mb:.0f} Mo) : "
                    f"{e}. Deux causes possibles : (1) mémoire réellement "
                    f"insuffisante — fermez des applications, vérifiez que "
                    f"Python est en 64 bits ; (2) fichier d'index corrompu — "
                    f"dans ce cas, supprimez le manifest.json et les fichiers "
                    f"de génération du répertoire d'index pour repartir d'une "
                    f"indexation complète (le corpus source n'est jamais "
                    f"touché)."
                ) from e
            raise
        if prev_deltas:
            # Les suppressions survenues depuis l'écriture de la base (fichiers
            # modifiés ou disparus, retirées de l'index en mémoire du run
            # précédent) ne sont pas dans les deltas : le registre fait foi —
            # tout identifiant qu'il ne connaît plus est retiré ici, et la
            # publication finale (fusion en un seul FAISS) sera exacte.
            morts = vindex.retirer_absents(reg.live_faiss_ids())
            _log.info(f"[indexer] Index FAISS (gén. {pub.prev_gen}) chargé : base + "
                      f"{len(prev_deltas)} delta(s) de points de reprise, "
                      f"{vindex.size} vecteurs ({morts} identifiant(s) absent(s) "
                      f"du registre retiré(s)).")
        else:
            _log.info(f"[indexer] Index FAISS (gén. {pub.prev_gen}) chargé ({vindex.size} vecteurs)")
        return vindex

    # Segment de contenu du segment courant (créé à la 1re écriture) + borne d'ids
    run_lo = reg.peek_next_faiss_id()
    writer_holder: List[Any] = [None]  # [ContentSegmentWriter | None]

    published = False
    seg_removed = False  # des vecteurs ont-ils été retirés DEPUIS le dernier checkpoint ?
    # Ids FAISS à retirer (fichiers modifiés et disparus), ACCUMULÉS puis
    # retirés en UN appel avant chaque finalisation (audit V2, A-5) : chaque
    # `remove_ids` parcourt tout l'index (O(ntotal)) — fichier par fichier,
    # 20 000 suppressions sur 5 M de vecteurs coûtaient ≈ 2 000 s.
    pending_removals: List[int] = []
    # Bases pour mesurer l'activité et le contenu ajouté DEPUIS le dernier
    # checkpoint : la publication (finale ou intermédiaire) ne réécrit FAISS/BM25
    # que si ce segment a réellement changé, et n'est pas republiée à vide.
    seg_chunks_start = 0
    base_new = base_modified = base_moved = 0
    files_since_ckpt = 0
    last_ckpt_ts = time.monotonic()
    # Plancher ADAPTATIF entre deux points de reprise (audit perf 2.1, I-4) :
    # chaque point réécrit le FAISS cumulé et recopie le registre — un coût
    # qui croît avec l'index. Le délai minimal est donc relevé à
    # max(checkpoint_min_s, 20 × durée du point précédent) : le temps passé en
    # points de reprise reste borné à ~5 % du run, quelle que soit la cadence
    # demandée (un lanceur réglé à 5 fichiers écrivait ~12 To pour un run de
    # 200 h sur un gros partage). checkpoint_min_s = 0 (choix EXPLICITE,
    # réservé aux diagnostics) désactive tout plancher, adaptatif compris :
    # un point exactement tous les N fichiers.
    ckpt_floor_s = checkpoint_min_s
    ckpt_adaptive = checkpoint_min_s > 0
    # Points de reprise EN DELTA (audit perf 2.1, I-4, second volet) : un
    # point n'écrit plus que les vecteurs ajoutés depuis le point précédent
    # (`corpus.<gen>.delta.faiss`, voir store.py) par-dessus le FAISS de
    # base ; la chaîne de deltas est bornée (au-delà, un point réécrit le
    # FAISS complet) et la publication FINALE fusionne tout en un seul
    # fichier. RIVAGE_CHECKPOINT_DELTA=0 : réécriture complète à chaque
    # point (lecteurs antérieurs à cette version).
    ckpt_delta = _env_int("RIVAGE_CHECKPOINT_DELTA", 1) != 0
    delta_max = max(1, _env_int("RIVAGE_CHECKPOINT_DELTA_MAX", _DELTA_CHAIN_MAX))
    # Les checkpoints ne reconstruisent PAS le BM25 (coût O(corpus) à chaque
    # point) : dès qu'un checkpoint publie du contenu nouveau, le BM25 publié
    # est « en retard » et la publication FINALE doit le reconstruire même si
    # plus rien ne change ensuite. SEMÉ depuis le manifest (audit v2) : un run
    # précédent tué pendant la reconstruction finale laisse une génération
    # intermédiaire active — sans ce ressemis, les runs no-op suivants
    # conserveraient un BM25 absent/périmé définitivement.
    bm25_stale = pub.prev_bm25_stale()
    if bm25_stale:
        _log.info("[indexer] BM25 de la génération publiée en retard : une "
                  "publication réparatrice aura lieu en fin de run.")
    stopped = False  # arrêt propre demandé (should_stop) : parcours incomplet

    # Tampon d'embedding INTER-fichiers (audit perf 2.1, I-3) : un appel
    # d'embedding par fichier (7 textes en moyenne) laissait les lots de 32 et
    # le parallélisme de la passerelle inemployés — 401 requêtes pour 400
    # fichiers. Les fichiers prêts (extraits, découpés, dédoublonnés et
    # ENREGISTRÉS au registre, contrat A-1) s'accumulent jusqu'à
    # lot × lots en vol textes (RIVAGE_EMBED_BATCH × RIVAGE_EMBED_PARALLEL,
    # 64 par défaut), puis UN appel groupé les encode : 88 requêtes pour les
    # mêmes 400 fichiers, attente réseau ÷5. Le tampon est vidé avant tout
    # point de reprise, à l'arrêt demandé et en fin de parcours.
    try:
        from embeddings import taille_tampon_indexeur
        embed_cap = taille_tampon_indexeur(embedder)
    except Exception:
        embed_cap = 64
    tampon: List[_EntreeTampon] = []
    tampon_textes = [0]            # [int] : textes en attente
    # Hashs des fichiers ENREGISTRÉS mais pas encore encodés (au tampon) : un
    # second fichier de même contenu arrivant pendant ce temps ne doit pas
    # être enregistré à son tour (revue 2.5, RA-1 : ses fragments auraient été
    # ajoutés une seconde fois sous le même hash — vecteurs doublés à jamais,
    # premier chemin perdu). Il devient un alias, comme un doublon classique.
    tampon_hashes: set = set()

    # Extraction PARALLÈLE (audit perf 2.1, I-5) : décidée après le parcours
    # (voir _ExtractionPool) ; `extract_document` est transmis pour le mode
    # en ligne afin de rester substituable (tests, instrumentation).
    extract_workers = _extract_workers_count(_log)
    pool: Optional[_ExtractionPool] = None

    def _progress(ratio, msg):
        # Un rappel de progression qui échoue (console Windows redirigée en
        # cp1252 refusant un caractère du message, audit V2 E-2) ne doit
        # JAMAIS faire avorter le run : il n'est pas dans le try par fichier.
        if progress_cb is None:
            return
        try:
            progress_cb(ratio, msg)
        except Exception as e:
            _log.debug(f"[indexer] progress_cb en échec (ignoré) : {e}")

    def _flush_removals():
        # Retrait GROUPÉ des vecteurs en attente (A-5). Appelé avant chaque
        # publication (point de reprise, finale) et avant le test « aucun
        # changement » : le FAISS écrit ne contient jamais un vecteur mort.
        if pending_removals:
            # Chargement paresseux (I-6) : le FAISS publié n'est lu qu'ici
            # s'il ne l'a pas déjà été pour un ajout.
            _ensure_vindex()
            if vindex is not None:
                vindex.remove(list(pending_removals))
            pending_removals.clear()

    def _defaire_comptage(status):
        if status == "modified":
            stats.modified -= 1
        elif status == "new":
            stats.new -= 1

    def _est_erreur_memoire(e) -> bool:
        return (not isinstance(e, _FaissLoadError)
                and (isinstance(e, MemoryError)
                     or (isinstance(e, RuntimeError) and "bad_alloc" in str(e).lower())))

    def _encoder_entrees(entrees: List["_EntreeTampon"]):
        """Encode et intègre un lot de fichiers ENREGISTRÉS (I-3) : un seul
        `embed_passages` pour tous leurs fragments, puis par fichier ids,
        FAISS, registre, segment de contenu. En cas d'échec, l'enregistrement
        de TOUS les fichiers du lot est défait (A-1) ; une erreur MÉMOIRE sur
        un lot de plusieurs fichiers est rejouée fichier par fichier pour
        isoler le coupable, qui est sauté (retenté au prochain run) pendant
        que le run continue ; les autres erreurs remontent (API : le run
        s'arrête, comme auparavant)."""
        nonlocal vindex
        if not entrees:
            return
        textes = [c.text for e in entrees for c in e.chunks]
        try:
            # Embedding sur le texte ENRICHI (préfixe de contexte, §4.1)
            vecs = embedder.embed_passages(textes, log=_log)
            # Un vecteur par texte, sinon le dernier fichier du lot recevrait
            # une tranche vide et serait enregistré SANS vecteur (revue 2.5,
            # RA-3) : traité comme un échec d'API (lot défait, run arrêté).
            if getattr(vecs, "shape", (len(vecs),))[0] != len(textes):
                raise RuntimeError(
                    f"embedding : {getattr(vecs, 'shape', (len(vecs),))[0]} vecteur(s) "
                    f"pour {len(textes)} texte(s)")
            # Premier ajout de ce run : charger le FAISS publié (I-6) — ou en
            # créer un si rien n'est publié.
            if vindex is None:
                vindex = _ensure_vindex()
            if vindex is None:
                vindex = VectorIndex(vecs.shape[1])
                _log.info(f"[indexer] Nouvel index FAISS créé (dim={vecs.shape[1]})")
            pos = 0
            for e in entrees:
                n = len(e.chunks)
                e.faiss_ids = reg.allocate_faiss_ids(n)
                vindex.add(vecs[pos:pos + n], e.faiss_ids)
                e.dans_vindex = True
                pos += n
                # Métadonnées des chunks dans le registre (léger) ...
                reg.add_chunks(e.file_hash,
                               [(c.page, fid) for c, fid in zip(e.chunks, e.faiss_ids)])
                # ... et le CONTENU BRUT (c.content) dans le segment de contenu de
                # ce run (M8 : BM25/synthèse s'appuient sur le contenu, sans le
                # préfixe ; S1 : le texte ne transite plus par la base recopiée à
                # chaque run). Insertion GROUPÉE et SANS commit par fichier (audit
                # V2, A-6) : commit unique à la fermeture du segment.
                if writer_holder[0] is None:
                    writer_holder[0] = ContentSegmentWriter(pub.content_path)
                writer_holder[0].add_many(
                    (fid, c.content) for c, fid in zip(e.chunks, e.faiss_ids))
                stats.chunks_added += n
                e.integre = True
        except BaseException as exc:
            # Tout ce qui suit l'enregistrement est DÉFAIT (audit V2, A-1) :
            # un fichier enregistré SANS fragment serait commité au point de
            # reprise puis classé « unchanged » à jamais. Pour tout le lot.
            for e in entrees:
                try:
                    reg.delete_file(e.file_hash)
                    if e.dans_vindex and vindex is not None and e.faiss_ids:
                        vindex.remove(list(e.faiss_ids))
                    if e.integre:
                        stats.chunks_added -= len(e.chunks)
                    e.faiss_ids, e.dans_vindex, e.integre = None, False, False
                except Exception as e2:
                    _log.error(f"[indexer] {e.path} : impossible de défaire "
                               f"l'enregistrement partiel : {e2}")
            if not _est_erreur_memoire(exc):
                _log.warning(f"[indexer] Échec de l'embedding d'un lot de "
                             f"{len(entrees)} fichier(s) — enregistrements DÉFAITS "
                             f"(ils seront retentés).")
                raise
            import gc as _gc
            _gc.collect()
            if len(entrees) > 1:
                # RÉSILIENCE MÉMOIRE sur un lot : isoler le coupable en
                # rejouant fichier par fichier (chacun ré-enregistré puis
                # encodé seul ; le pathologique est sauté ci-dessous).
                _log.warning(f"[indexer] Mémoire épuisée sur un lot de "
                             f"{len(entrees)} fichiers ({exc}) — reprise fichier "
                             f"par fichier pour isoler le coupable.")
                for e in entrees:
                    if _register_file(reg, e.prep, e.file_hash, e.path,
                                      ocr_pending=e.ocr_pending):
                        _encoder_entrees([e])
                return
            import traceback as _tb
            e = entrees[0]
            _defaire_comptage(e.status)
            stats.errors += 1
            _log.error(
                f"[indexer] MÉMOIRE épuisée sur : {e.path}\n"
                f"Fichier SAUTÉ — l'indexation CONTINUE. C'est très "
                f"probablement LUI le coupable des plantages : examinez-le "
                f"(python rivage.py diag \"{e.path}\"), ou écartez-le "
                f"(RIVAGE_MAX_FILE_MB, ou déplacement hors corpus).\n"
                f"Trace complète :\n{_tb.format_exc()}")

    def _vider_tampon():
        """UN appel d'embedding groupé pour tous les fichiers en attente."""
        if not tampon:
            return
        entrees = list(tampon)
        tampon.clear()
        tampon_textes[0] = 0
        tampon_hashes.clear()
        _encoder_entrees(entrees)

    def _doublon_de_contenu(job) -> bool:
        """Contenu déjà indexé sous ce hash (fichier strictement identique
        déjà vu) : ne pas ré-extraire ni ré-encoder. True si traité."""
        if not reg.get_faiss_ids_for_hash(job.file_hash):
            return False
        existing = reg.get_file_by_hash(job.file_hash)
        if (existing and existing.chemin != job.path
                and not _is_gone_strict(existing.chemin, _log, stats)):
            # L'autre fichier de même contenu existe toujours (vérification
            # stricte, A-4) : ne pas écraser sa métadonnée de chemin (n2). Ce
            # chemin est mémorisé comme ALIAS pour ne plus être re-haché.
            st = os.stat(job.path)
            reg.upsert_alias(job.path, job.file_hash, st.st_size, st.st_mtime)
            return True
        st = os.stat(job.path)
        # Doublon de contenu : hériter l'état de rattrapage OCR de l'original
        # (même contenu => même besoin d'OCR).
        reg.upsert_file(FileRecord(
            hash=job.file_hash, chemin=job.path, taille=st.st_size,
            mtime=st.st_mtime, famille=_family_of(job.path, corpus_root),
            ocr_pending=getattr(existing, "ocr_pending", 0) if existing else 0,
        ))
        return True

    def _admettre(job, prep):
        """Suite PRINCIPALE du traitement d'un fichier préparé (extrait,
        découpé, dédoublonné — dans ce processus ou un processus de travail) :
        décision d'enregistrement, puis mise au tampon d'embedding."""
        if _doublon_de_contenu(job):
            return
        # Visibilité du coût : pages payées à l'API Vision (comptées AVANT les
        # abandons éventuels — l'argent est dépensé même si le doc n'est pas
        # enregistré ; les pages réussies restent au cache).
        stats.ocr_pages += int(prep.pages_ocr or 0)
        if job.file_hash in tampon_hashes:
            # Même contenu qu'un fichier déjà au tampon (enregistré, fragments
            # pas encore au registre) : alias, comme pour un contenu déjà
            # indexé (A-4) — jamais un second enregistrement (RA-1).
            st = os.stat(job.path)
            reg.upsert_alias(job.path, job.file_hash, st.st_size, st.st_mtime)
            _log.info(f"[indexer] {job.path} : même contenu qu'un fichier en cours "
                      f"d'encodage — mémorisé comme doublon (alias).")
            return
        # Arrêt demandé PENDANT l'extraction (constaté juste après elle, dans
        # ce processus ou le processus de travail) : texte possiblement
        # partiel — ne pas l'enregistrer. La boucle appelante défait le
        # comptage et finalise proprement.
        if prep.interrupted:
            raise _StopDuringExtraction(job.path)
        # Pages OCR en échec transitoire (passerelle saturée) : document
        # incomplet mais re-tentable — même principe, sauf que le run CONTINUE.
        if prep.ocr_transient_failures:
            raise _TransientOcrFailure(job.path, "pages OCR en échec transitoire")
        # Erreur d'ACCÈS transitoire à l'extraction (verrou, hoquet SMB) : ne
        # pas figer le fichier (audit V2, A-2) — ré-extrait au prochain run.
        if prep.error and prep.transient:
            raise _TransientExtractionFailure(job.path, prep.error)
        # Rattrapage OCR : marquer un document enregistré SANS OCR alors qu'il
        # en avait besoin — un futur run avec OCR coché le retraitera (voir la
        # boucle). Un enregistrement AVEC OCR efface naturellement la marque.
        ocr_pending = _ocr_pending_flag(prep, use_ocr, ocr_quality_threshold)
        if ocr_pending:
            _log.info(f"[indexer] {job.path} : indexé sans OCR alors qu'il en "
                      f"aurait besoin — marqué pour rattrapage au prochain run "
                      f"avec OCR.")
        if prep.error or not prep.has_content:
            if prep.error:
                _log.warning(f"[indexer] extraction: {job.path}: {prep.error}")
                stats.errors += 1
            # Enregistrer quand même pour ne pas retraiter en boucle
            _register_file(reg, prep, job.file_hash, job.path, ocr_pending=ocr_pending)
            return
        stats.deduped += int(prep.deduped or 0)
        if not prep.chunks:
            _register_file(reg, prep, job.file_hash, job.path, ocr_pending=ocr_pending)
            return
        # Enregistrer le fichier AVANT d'allouer/encoder (audit v2) : si cet
        # enregistrement échoue (fichier disparu, hoquet SMB), on s'arrête là —
        # sinon des chunks seraient rattachés à un fichier ABSENT du registre,
        # invisibles de toute purge (fuite définitive dans FAISS/BM25/segments).
        if not _register_file(reg, prep, job.file_hash, job.path, ocr_pending=ocr_pending):
            return
        # Un appel groupé ne dépasse pas la capacité (lots en vol pleins,
        # jamais un lot résiduel de quelques textes) : le tampon est vidé
        # AVANT d'y mettre un fichier qui le ferait déborder ; un fichier à
        # lui seul plus gros que la capacité part aussitôt.
        n = len(prep.chunks)
        if tampon and tampon_textes[0] + n > embed_cap:
            _vider_tampon()
        tampon.append(_EntreeTampon(job.path, job.file_hash, job.status,
                                    prep, prep.chunks, ocr_pending))
        tampon_hashes.add(job.file_hash)
        tampon_textes[0] += n
        if tampon_textes[0] >= embed_cap:
            _vider_tampon()

    try:
        # Parcours du corpus (audit V2, E-12) : INTERRUPTIBLE (should_stop
        # transmis à walk_corpus — des minutes sur 500 000 fichiers) et VISIBLE
        # (progression périodique « Parcours du corpus… N fichiers » pour que
        # l'interface n'affiche pas une sauvegarde qui n'a pas lieu).
        files = []
        for path in walk_corpus(corpus_root, exclude_families=excluded, log=_log,
                                should_stop=should_stop):
            files.append(path)
            if len(files) % _WALK_PROGRESS_EVERY == 0:
                _progress(0.0, f"Parcours du corpus… {len(files)} fichiers")
        total = len(files)
        _progress(0.0, f"Parcours du corpus terminé : {total} fichiers")
        seen_hashes = set()
        # Chemins reconnus comme doublons de contenu (alias, A-4) pendant ce
        # run : candidats à la promotion si leur original a disparu.
        seen_alias_paths = set()

        # Pool d'extraction (I-5) : des processus seulement si le corpus en
        # vaut la peine (au moins 2 × workers fichiers — en dessous, le
        # démarrage des processus coûterait plus que le gain) ; sinon
        # extraction en ligne, dans ce processus, comportement historique.
        workers_eff = (extract_workers
                       if total >= _POOL_MIN_FILES_PAR_WORKER * extract_workers else 1)
        ocr_par_proc = 0
        if workers_eff > 1 and use_ocr:
            ocr_par_proc = max(1, (ocr_eff or workers_eff) // workers_eff)
        pool = _ExtractionPool(
            workers_eff, corpus_root, use_ocr, ocr_quality_threshold,
            dedup_threshold, log=_log, ocr_workers_par_proc=ocr_par_proc,
            should_stop=should_stop, extract_fn=extract_document)
        if workers_eff > 1:
            _log.info(f"[indexer] Extraction parallèle : {workers_eff} processus "
                      f"(RIVAGE_EXTRACT_WORKERS, {2 * workers_eff} fichiers en vol)"
                      + (f" ; OCR : {ocr_par_proc} page(s) en parallèle par "
                         f"processus ({ocr_par_proc * workers_eff} au total)"
                         if use_ocr else "") + ".")

        def _consommer() -> bool:
            """Traite le PLUS ANCIEN fichier préparé (ordre de parcours
            préservé) : enregistrement, tampon d'embedding, point de reprise.
            Retourne True si la boucle doit s'arrêter (arrêt demandé)."""
            nonlocal stopped, pub, reg, run_lo, seg_removed, seg_chunks_start
            nonlocal base_new, base_modified, base_moved, files_since_ckpt
            nonlocal last_ckpt_ts, ckpt_floor_s, bm25_stale, initial_aliases
            job, fut = pool.pop()
            path, file_hash, status, i = job.path, job.file_hash, job.status, job.index
            # Corps protégé (audit) : un fichier supprimé/verrouillé ENTRE
            # classify et son traitement (corpus vivant, hoquet SMB) ne doit
            # pas faire avorter tout le run — sauté, revu au run suivant. Les
            # erreurs API (RuntimeError) continuent d'arrêter le run : les
            # points de reprise protègent le travail accompli.
            try:
                try:
                    prep = pool.resultat(job, fut)
                    _admettre(job, prep)
                except _TransientFailure as tf:
                    # Échec transitoire sur ce fichier (passerelle Vision
                    # saturée malgré retries, ou erreur d'accès au fichier) :
                    # NON enregistré (re-tentable), comptage défait, et le run
                    # CONTINUE — les autres fichiers ne doivent pas payer pour
                    # celui-ci.
                    _defaire_comptage(status)
                    stats.errors += 1
                    seen_hashes.add(file_hash)
                    if isinstance(tf, _TransientOcrFailure):
                        _log.warning(
                            f"[indexer] {path} : pages OCR en échec transitoire "
                            f"(passerelle Vision saturée) — document NON enregistré, "
                            f"repris au prochain run (pages réussies déjà au cache).")
                    else:
                        _log.warning(
                            f"[indexer] {path} : extraction en échec transitoire "
                            f"({tf.reason}) — document NON enregistré, repris au "
                            f"prochain run.")
                    return False
                except _StopDuringExtraction:
                    # Arrêt demandé PENDANT l'extraction de ce fichier : texte
                    # possiblement partiel (OCR interrompu page à page), donc
                    # NON enregistré — sinon son hash serait mémorisé et le
                    # texte partiel resterait indexé pour toujours. Défaire le
                    # comptage (le registre doit refléter les stats), puis
                    # sortir : la finalisation publie ce qui est acquis. Les
                    # pages OCR déjà payées sont au cache de reprise.
                    _defaire_comptage(status)
                    stopped = True
                    _log.info(f"[indexer] Arrêt pendant l'extraction de {path} "
                              f"— document non enregistré, repris au prochain "
                              f"run. Finalisation des documents déjà indexés.")
                    return True
                seen_hashes.add(file_hash)
            except (MemoryError, RuntimeError) as e:
                # RÉSILIENCE MÉMOIRE : un fichier pathologique (std::bad_alloc
                # natif — FAISS/PyMuPDF — ou MemoryError Python, y compris un
                # processus de travail mort) ne doit plus tuer TOUTE
                # l'indexation. Il est sauté avec sa trace complète, la mémoire
                # est rendue, et le run CONTINUE. Non enregistré, il sera
                # retenté au prochain run (l'écarter durablement :
                # RIVAGE_MAX_FILE_MB, ou déplacer le fichier). Les autres
                # RuntimeError (API…) continuent d'arrêter le run comme avant.
                # Le FAISS publié qui ne se charge pas (chargement paresseux,
                # I-6) n'est PAS un fichier pathologique : le run s'arrête.
                if not _est_erreur_memoire(e):
                    raise
                import gc as _gc
                import traceback as _tb
                _defaire_comptage(status)
                stats.errors += 1
                seen_hashes.add(file_hash)
                _log.error(
                    f"[indexer] MÉMOIRE épuisée sur : {path}\n"
                    f"Fichier SAUTÉ — l'indexation CONTINUE. C'est très "
                    f"probablement LUI le coupable des plantages : examinez-le "
                    f"(python rivage.py diag \"{path}\"), ou écartez-le "
                    f"(RIVAGE_MAX_FILE_MB, ou déplacement hors corpus).\n"
                    f"Trace complète :\n{_tb.format_exc()}")
                _gc.collect()
                return False
            except OSError as e:
                _log.warning(f"[indexer] Fichier sauté (erreur d'accès) : {path}: {e}")
                stats.errors += 1
                # Le fichier vient d'être VU par walk/classify : le marquer vu
                # (audit v2) — sinon un « moved » sauté sur erreur transitoire
                # verrait son ANCIEN chemin purgé à la fin (ENOENT avéré) et
                # tout son contenu ré-embeddé au run suivant.
                seen_hashes.add(file_hash)
                return False

            # --- Point de reprise périodique ---------------------------------
            # Publie une génération intermédiaire (FAISS+registre+segment ; le
            # BM25 précédent est RÉUTILISÉ — reconstruit à la finale seulement),
            # puis repart sur une nouvelle génération en gardant le vindex en
            # mémoire. La purge des disparus n'a PAS lieu ici : des fichiers pas
            # encore parcourus seraient à tort considérés comme disparus.
            # Double condition : N fichiers ET plancher temporel (audit : chaque
            # point réécrit le FAISS cumulé ; sans plancher, une petite cadence
            # rend l'indexation quadratique). Le plancher est ADAPTATIF
            # (I-4) : relevé à 20 fois la durée du point précédent. Jamais
            # pendant la vidange qui suit un arrêt demandé (la finale suit).
            files_since_ckpt += 1
            if (checkpoint_files and not stopped and files_since_ckpt >= checkpoint_files
                    and time.monotonic() - last_ckpt_ts >= ckpt_floor_s):
                _vider_tampon()                # tout ce qui est enregistré est encodé (I-3)
                run_hi = reg.peek_next_faiss_id()
                content_written = writer_holder[0] is not None
                if content_written:
                    writer_holder[0].close()   # commit UNIQUE du segment (A-6)
                _flush_removals()              # retrait groupé (A-5)
                ckpt_changed = (stats.chunks_added > seg_chunks_start
                                or seg_removed or pub.prev_gen == 0)
                if ckpt_changed:
                    _ensure_vindex()           # réécriture du FAISS (I-6)
                ckpt_t0 = time.monotonic()
                gen = _finalize_generation(
                    pub, reg, vindex, index_dir, run_lo, run_hi,
                    content_written, rebuild_faiss=ckpt_changed,
                    rebuild_bm25=False, progress_cb=_progress, _log=_log,
                    ratio=(i + 1) / max(total, 1),
                    note=f"Point de reprise publié ({i+1}/{total})",
                    mark_bm25_stale=(bm25_stale or ckpt_changed),
                    faiss_delta_lo=(run_lo if ckpt_delta else None),
                    faiss_delta_max=delta_max,
                )
                ckpt_duration = time.monotonic() - ckpt_t0
                if ckpt_adaptive:
                    ckpt_floor_s = max(checkpoint_min_s,
                                       _CHECKPOINT_FLOOR_FACTOR * ckpt_duration)
                stats.generation = gen
                bm25_stale = bm25_stale or ckpt_changed
                _log.info(f"[indexer] Point de reprise publié (génération {gen}) "
                          f"après {i+1}/{total} fichiers, en {ckpt_duration:.1f} s "
                          f"— prochain point au plus tôt dans {ckpt_floor_s:.0f} s "
                          f"(plancher adaptatif : {_CHECKPOINT_FLOOR_FACTOR} × "
                          f"la durée de celui-ci, minimum {checkpoint_min_s:.0f} s).")
                # Repartir sur une nouvelle génération. seed_registry recopie le
                # registre (léger) qu'on vient de publier ; le vindex reste en
                # mémoire ; un nouveau segment de contenu démarre à run_lo.
                pub = Publisher(index_dir, log=_log)
                if pub.prev_gen != gen:
                    # Défense en profondeur (audit) : si le manifest relu ne
                    # reflète pas la génération qu'on vient de publier (partage
                    # incohérent), continuer écraserait l'index des lecteurs.
                    raise RuntimeError(
                        f"Incohérence après point de reprise : manifest à la "
                        f"génération {pub.prev_gen}, attendu {gen}. Partage "
                        f"réseau suspect — indexation interrompue.")
                reg = Registry(pub.seed_registry())
                run_lo = reg.peek_next_faiss_id()
                writer_holder[0] = None
                seg_removed = False
                seg_chunks_start = stats.chunks_added
                base_new, base_modified, base_moved = (
                    stats.new, stats.modified, stats.moved)
                files_since_ckpt = 0
                last_ckpt_ts = time.monotonic()
                # Les alias écrits avant ce point sont publiés : rafraîchir
                # l'instantané pour que le pré-filtre les reconnaisse.
                initial_aliases = reg.all_aliases()
            return False

        for i, path in enumerate(files):
            # Arrêt PROPRE demandé (bouton Quitter) : on sort de la boucle et on
            # publie une génération finale de tout ce qui a déjà été traité.
            if should_stop is not None and should_stop():
                stopped = True
                _log.info(f"[indexer] Arrêt demandé après {i}/{total} fichiers — "
                          f"finalisation des documents déjà indexés.")
                _progress(i / max(total, 1), "Arrêt demandé — sauvegarde en cours…")
                break

            _progress(i / max(total, 1), f"[{i+1}/{total}] {os.path.basename(path)}")

            try:
                status, file_hash = reg.classify(path)
            except Exception as e:
                _log.warning(f"[indexer] classify a échoué sur {path}: {e}")
                stats.errors += 1
                continue

            if status == "unchanged":
                if path in initial_aliases:
                    # Doublon reconnu par le pré-filtre des alias (A-4) : son
                    # ORIGINAL n'a pas été vu ici — ne pas marquer le hash vu,
                    # la purge finale décidera (original présent : rien ;
                    # disparu : cet alias est promu à sa place). Testé AVANT
                    # le rattrapage OCR : c'est l'original qui est retraité,
                    # jamais le doublon (sinon le chemin enregistré basculerait).
                    stats.unchanged += 1
                    seen_alias_paths.add(path)
                    continue
                # RATTRAPAGE OCR : fichier indexé SANS OCR alors qu'il en
                # avait besoin (ocr_pending, voir _register_file) — l'OCR est
                # de nouveau actif : le retraiter comme un fichier modifié
                # (anciens chunks retirés, ré-extraction AVEC OCR, marque
                # effacée à l'enregistrement). Seuls les fichiers marqués
                # sont repris : le coût du recochage reste borné.
                if use_ocr and file_hash and reg.is_ocr_pending(file_hash):
                    _log.info(f"[indexer] Rattrapage OCR : {path} — "
                              f"ré-extraction avec OCR de secours.")
                    status = "modified"
                else:
                    stats.unchanged += 1
                    seen_hashes.add(file_hash)
                    continue

            # Corps d'itération protégé (audit) : un fichier supprimé/verrouillé
            # ENTRE classify et son traitement (corpus vivant, hoquet SMB) ne
            # doit pas faire avorter tout le run — on le saute, il sera revu au
            # run suivant.
            try:
                if status == "moved":
                    existing = reg.get_file_by_hash(file_hash)
                    # Présence de l'original vérifiée STRICTEMENT (ENOENT avéré,
                    # audit V2, A-4) : `os.path.exists` répond False sur
                    # n'importe quelle OSError, et un hoquet SMB faisait
                    # basculer le chemin enregistré d'un doublon à l'autre à
                    # chaque run (flip-flop).
                    if (existing and existing.chemin != path
                            and not _is_gone_strict(existing.chemin, _log, stats)):
                        # Doublon de contenu : l'autre fichier de même contenu
                        # existe toujours. Ne pas réétiqueter sa métadonnée
                        # (n2)... SAUF si ce fichier vit dans un thème exclu (m7)
                        # OU si son extension n'est plus prise en charge (audit :
                        # un .csv désindexé dupliqué en .txt doit basculer sur le
                        # chemin encore indexable).
                        if (os.path.normcase(existing.famille or "") not in excluded_nc
                                and is_supported(existing.chemin)):
                            # Mémorisé comme ALIAS (A-4) : au run suivant, le
                            # pré-filtre le reconnaît sans le re-hacher.
                            st = os.stat(path)
                            reg.upsert_alias(path, file_hash, st.st_size, st.st_mtime)
                            stats.unchanged += 1
                            seen_hashes.add(file_hash)
                            continue
                    mtime = os.stat(path).st_mtime
                    reg.update_path(file_hash, path, mtime, _family_of(path, corpus_root))
                    stats.moved += 1
                    seen_hashes.add(file_hash)
                    continue

                if status == "modified":
                    old = reg.get_file_by_path(path)
                    if old:
                        # L'ANCIEN contenu survit peut-être dans un doublon
                        # (A-4) : le promouvoir plutôt que de retirer des
                        # vecteurs encore utiles — sinon le doublon serait
                        # ré-encodé (ou absent de l'index jusqu'au run suivant).
                        promoted = _promote_alias(reg, old.hash, seen_alias_paths,
                                                  corpus_root, excluded_nc, _log, stats)
                        if promoted:
                            stats.moved += 1
                            seen_hashes.add(old.hash)
                        else:
                            old_ids = reg.delete_file(old.hash)
                            if old_ids:
                                pending_removals.extend(old_ids)
                                seg_removed = True
                    stats.modified += 1
                elif status == "new":
                    stats.new += 1
                else:  # missing (course entre walk et classify)
                    continue

                job = _Job(i, path, file_hash, status)
                # Contenu déjà indexé sous ce hash : traité tout de suite,
                # sans extraction (ni soumission au pool).
                if _doublon_de_contenu(job):
                    seen_hashes.add(file_hash)
                    continue
                pool.submit(job)
            except OSError as e:
                _log.warning(f"[indexer] Fichier sauté (erreur d'accès) : {path}: {e}")
                stats.errors += 1
                seen_hashes.add(file_hash)
                continue

            # Fenêtre BORNÉE de fichiers en vol (I-5) : on ne consomme le plus
            # ancien résultat que lorsque la fenêtre est pleine — jamais tout
            # le corpus pré-extrait en mémoire. En mode en ligne (fenêtre 1),
            # le fichier est traité immédiatement, comme auparavant.
            while pool.plein():
                if _consommer():
                    break
            if stopped:
                break

        # Fin du parcours (ou arrêt) : vider la fenêtre d'extraction, dans
        # l'ordre. Sur arrêt demandé, les fichiers pas encore commencés sont
        # annulés et les processus en cours priés de s'arrêter (événement
        # partagé : la boucle OCR page à page s'interrompt) ; chaque résultat
        # est ensuite consommé comme d'habitude — un fichier dont l'extraction
        # s'est ACHEVÉE avant la demande est enregistré (texte complet), un
        # fichier annulé ou extrait pendant l'arrêt ne l'est pas (comptage
        # défait, repris au prochain run).
        if stopped:
            pool.arreter()
        while len(pool):
            _consommer()
        # Tout fichier enregistré est encodé AVANT la finale (I-3) — y compris
        # sur arrêt demandé : les fichiers du tampon sont complets, on les
        # embarque plutôt que de les défaire.
        _vider_tampon()

        stats.stopped = stopped  # figé ici : les retours anticipés le préservent
        # Visibilité : combien de documents attendent un rattrapage OCR (un
        # futur run avec OCR coché les retraitera) — affiché au résumé.
        try:
            stats.ocr_pending_docs = reg.count_ocr_pending()
        except Exception:
            pass

        # --- Disparus OU appartenant à un thème désormais exclu (FINAL) ------
        # Un fichier du registre est retiré s'il a disparu du disque (§8.4) OU
        # si son thème vient d'être exclu de l'indexation (il ne doit plus être
        # encodé, donc plus présent dans l'index). La famille est lue AU
        # REGISTRE (M2) : recalculer _family_of(chemin, corpus_root) échoue si
        # le chemin enregistré vient d'une autre racine (UNC vs lecteur mappé).
        # NE S'EXÉCUTE QU'À LA PUBLICATION FINALE (jamais aux checkpoints) :
        # `seen_hashes` couvre alors tout le corpus parcouru. SAUTÉE aussi sur
        # arrêt anticipé (parcours incomplet : des fichiers non encore vus
        # seraient pris à tort pour disparus).
        for h, (chemin, famille) in ({} if stopped else initial_families).items():
            if h in seen_hashes:
                continue
            # `gone` STRICT (audit) : os.path.exists retourne False sur
            # N'IMPORTE QUELLE OSError — un hoquet SMB pendant cette boucle
            # purgerait en masse des fichiers présents (à ré-embedder ensuite).
            # On ne purge que sur ENOENT avéré ; en cas d'erreur d'accès
            # persistante, on considère le fichier PRÉSENT (revu au run suivant).
            gone = _is_gone_strict(chemin, _log, stats)
            now_excluded = os.path.normcase(famille or "") in excluded_nc
            # Extension retirée des formats supportés (ex. .csv désormais exclus) :
            # le fichier existe toujours mais ne doit plus être indexé. walk_corpus
            # ne l'a pas parcouru, donc il n'est pas dans seen_hashes — on le purge
            # ici pour retirer ses vecteurs de l'index existant.
            now_unsupported = not is_supported(chemin)
            if gone or now_excluded or now_unsupported:
                # Original disparu (ou désormais hors périmètre) mais un
                # DOUBLON vu ce run porte encore ce contenu : le promouvoir
                # (update_path) au lieu de jeter des vecteurs valides (A-4).
                if _promote_alias(reg, h, seen_alias_paths, corpus_root,
                                  excluded_nc, _log, stats):
                    stats.moved += 1
                    continue
                old_ids = reg.delete_file(h)
                if old_ids:
                    pending_removals.extend(old_ids)   # retrait groupé (A-5)
                    seg_removed = True
                stats.removed += 1

        # --- Alias disparus (A-4, FINAL uniquement) -------------------------
        # Un alias dont le chemin n'a pas été revu ce run et qui est avéré
        # absent est retiré ; ceux dont le contenu n'est plus indexé aussi.
        if not stopped:
            for chemin in initial_aliases:
                if chemin in seen_alias_paths or reg.get_alias(chemin) is None:
                    continue
                if _is_gone_strict(chemin, _log, stats):
                    reg.delete_alias(chemin)
            reg.purge_orphan_aliases()

        # Fermer le segment de contenu de ce run (le texte est écrit dedans) :
        # c'est SON unique commit (A-6), avant toute publication.
        run_hi = reg.peek_next_faiss_id()
        content_written = writer_holder[0] is not None
        if content_written:
            writer_holder[0].close()
        _flush_removals()   # retrait groupé (A-5), avant le test « aucun changement »

        # --- Rien n'a changé DEPUIS le dernier point publié : ne pas republier
        # (évite de churner le share). L'activité est mesurée relativement au
        # dernier checkpoint : sans checkpoint, les bases valent 0 et c'est le
        # test historique sur tout le run. EXCEPTION : si un checkpoint a publié
        # du contenu sans reconstruire le BM25 (bm25_stale), la finale doit
        # quand même publier pour remettre le BM25 à niveau. Les écritures de
        # MÉTADONNÉES seules (fichiers « touchés » rafraîchis, alias — A-3/A-4)
        # comptent : jeter la génération les perdrait et le re-hachage
        # reprendrait au run suivant ; leur publication réutilise FAISS et BM25
        # (comme un simple « moved »).
        seg_activity = ((stats.new - base_new) + (stats.modified - base_modified)
                        + (stats.moved - base_moved) + stats.removed
                        + reg.meta_changes)
        if seg_activity == 0 and not bm25_stale:
            reg.close()
            pub.discard()
            stats.generation = pub.prev_gen  # dernier checkpoint, ou gén. d'origine
            published = True  # la génération courante reste valide
            _progress(1.0, "Aucun changement — index inchangé")
            _log.info(f"[indexer] Aucun changement depuis le dernier point publié : "
                      f"génération {pub.prev_gen} conservée.")
            return stats

        # Le contenu (donc FAISS) n'a changé que si des chunks ont été ajoutés
        # OU des vecteurs retirés (suppression de fichier OU modification ne
        # ré-ajoutant rien) DEPUIS le dernier point publié. Un simple `moved`
        # ne change QUE des métadonnées : on réutilise alors FAISS (S2). Sur le
        # tout premier run, il n'y a rien à réutiliser → on construit. Le BM25
        # est en plus reconstruit si un checkpoint l'a laissé en retard.
        # Des deltas de points de reprise publiés (I-4) sont FUSIONNÉS en un
        # seul FAISS à la finale, même si plus rien n'a changé depuis.
        content_changed = (stats.chunks_added > seg_chunks_start
                           or seg_removed or pub.prev_gen == 0
                           or bool(pub.prev_faiss_deltas()))
        if content_changed:
            _ensure_vindex()   # réécriture du FAISS : il doit être chargé (I-6)

        stats.generation = _finalize_generation(
            pub, reg, vindex, index_dir, run_lo, run_hi,
            content_written, rebuild_faiss=content_changed,
            rebuild_bm25=(content_changed or bm25_stale),
            progress_cb=_progress, _log=_log,
            ratio=0.99, note="Bascule atomique du manifest",
        )
        published = True

    finally:
        # Fermeture garantie des ressources, même en cas d'échec (M5)
        if pool is not None:
            try:
                # Sur un chemin d'échec, les processus en cours d'extraction
                # sont priés de s'arrêter (événement partagé) puis attendus :
                # aucun processus orphelin après le retour de l'indexeur.
                pool.fermer(arret=not published)
            except Exception as e:
                _log.debug(f"[indexer] Fermeture du pool d'extraction : {e}")
        if writer_holder[0] is not None:
            try:
                writer_holder[0].close()
            except Exception:
                pass
        try:
            # Sans commit : sur un chemin d'échec, ne pas figer un état partiel
            # (la base non publiée sera discardée de toute façon).
            reg.close(commit=False)
        except Exception:
            pass
        if own_embedder and hasattr(embedder, "close"):
            embedder.close()
        if not published:
            # Nettoyer les artefacts de la génération non publiée (n5) : sans ça
            # ils resteraient orphelins sur le partage jusqu'au prochain run.
            try:
                pub.discard()
            except Exception:
                pass
            _log.warning(
                "[indexer] Run interrompu avant publication : la génération "
                f"{pub.prev_gen} reste active. Relancez pour reprendre."
            )

    tag = "Arrêt anticipé (index sauvegardé)" if stopped else "Indexation terminée"
    _progress(1.0, f"{tag} — {stats.summary()}")
    _log.info(f"[indexer] {tag} : {stats.summary()}")
    return stats


# Progression émise pendant le parcours du corpus, tous les N fichiers trouvés
# (E-12) — assez rare pour ne pas inonder la file de l'interface.
_WALK_PROGRESS_EVERY = 1000

# Plancher adaptatif des points de reprise (audit perf 2.1, I-4) : délai
# minimal avant le point suivant = ce facteur × la durée du point précédent
# (jamais moins que RIVAGE_CHECKPOINT_MIN_S). 20 borne le temps passé en
# points de reprise à ~5 % du run.
_CHECKPOINT_FLOOR_FACTOR = 20


def _promote_alias(reg, file_hash: str, seen_alias_paths, corpus_root,
                   excluded_nc, log, stats) -> bool:
    """
    Promotion d'un doublon (audit V2, A-4) : l'original de `file_hash` a
    disparu (ou doit quitter l'index, ou change de contenu) mais un alias de
    même contenu existe encore — il prend sa place (update_path, sans
    ré-encodage). Candidats : un alias VU ce run (le parcours garantit qu'il
    est présent et indexable), ou, s'il n'a pas encore été parcouru, un alias
    hors thème exclu, d'extension prise en charge et avéré PRÉSENT
    (vérification stricte). Retourne True si promu.
    """
    for alias in reg.aliases_for_hash(file_hash):
        if alias.chemin not in seen_alias_paths:
            famille = _family_of(alias.chemin, corpus_root)
            if (os.path.normcase(famille or "") in excluded_nc
                    or not is_supported(alias.chemin)
                    or _is_gone_strict(alias.chemin, log, stats)):
                continue
        try:
            mtime = os.stat(alias.chemin).st_mtime
        except OSError:
            continue
        reg.update_path(file_hash, alias.chemin, mtime,
                        _family_of(alias.chemin, corpus_root))
        seen_alias_paths.discard(alias.chemin)
        log.info(f"[indexer] Doublon promu à la place de l'original disparu : "
                 f"{alias.chemin}")
        return True
    return False


def _env_int(name: str, default: int) -> int:
    """Lit un entier depuis l'environnement, ou `default` si absent/invalide."""
    try:
        v = os.environ.get(name)
        return int(v) if v is not None and v.strip() else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """Flottant depuis l'environnement (audit v3 : '0.5' ne doit pas retomber
    silencieusement au défaut), ou `default` si absent/invalide."""
    try:
        v = os.environ.get(name)
        return float(v) if v is not None and v.strip() else default
    except (TypeError, ValueError):
        logger.warning(f"[indexer] {name} invalide — défaut {default} utilisé.")
        return default


def _is_gone_strict(chemin: str, log, stats) -> bool:
    """
    True si le fichier est AVÉRÉ absent (ENOENT/ENOTDIR). Une erreur d'accès
    transitoire (partage réseau) est retentée puis, si elle persiste, le
    fichier est considéré PRÉSENT — on ne purge jamais sur un doute (audit).
    """
    for attempt in range(3):
        try:
            os.stat(chemin)
            return False
        except (FileNotFoundError, NotADirectoryError):
            return True
        except OSError as e:
            if attempt == 2:
                log.warning(f"[indexer] État inconnu (erreur d'accès), purge "
                            f"différée : {chemin}: {e}")
                stats.errors += 1
                return False
            time.sleep(0.5 * (attempt + 1))
    return False


def _compact_segments(pub, reg, index_dir, active_segments, run_hi, _log):
    """
    Fusionne les segments de contenu actifs en UN segment (celui de la
    génération en cours) quand ils sont trop nombreux — les checkpoints en
    créent un par point et rien ne les fusionnait (audit : manifest interminable,
    latence lecteur). Écrit le contenu VIVANT des anciens segments dans
    `pub.content_path` (INSERT OR REPLACE : les ids du run y sont déjà), puis
    retourne la nouvelle liste de segments [un seul, couvrant [0, run_hi)].
    """
    import sqlite3
    from registry import _sqlite_ro_uri
    old_files = [s["file"] for s in active_segments if s["file"] != pub.content_name]
    if pub.content_name not in {s["file"] for s in active_segments} \
            and os.path.exists(pub.content_path):
        # Aucun contenu écrit ce run : le fichier présent est un RÉSIDU d'un
        # run tué portant le même numéro (possiblement inconsistant depuis
        # que le segment non publié s'écrit sans journal, A-6) — repartir
        # d'un fichier neuf, comme le fait ContentSegmentWriter.
        os.remove(pub.content_path)
    # Compaction en SQL PUR (audit V2, A-11) : plus d'ensemble Python de tous
    # les ids vivants ni d'INSERT ligne à ligne — le registre (ids vivants) et
    # chaque ancien segment sont ATTACHés et copiés par INSERT … SELECT. Le
    # segment compacté n'est PAS encore publié : journal et fsync inutiles
    # (recréé à neuf en cas d'abandon, voir ci-dessus et store.discard).
    # Ouverture en URI (uri=True) : indispensable pour que les ATTACH ci-dessous
    # acceptent des URI `file:` en lecture seule.
    conn = sqlite3.connect(_sqlite_ro_uri(pub.content_path, params="mode=rwc"),
                           uri=True)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("CREATE TABLE IF NOT EXISTS contenu ("
                     " faiss_id INTEGER PRIMARY KEY, texte TEXT)")
        # Purger un éventuel RÉSIDU d'un run tué portant le même numéro de
        # génération (audit v2) : ses lignes ont des ids >= run_hi (jamais
        # commit au registre) et ne doivent pas être embarquées dans le
        # segment canonique.
        conn.execute("DELETE FROM contenu WHERE faiss_id >= ?", (run_hi,))
        # Registre de la génération en cours (commité juste avant par
        # _finalize_generation) : en lecture seule, sans `immutable` (c'est
        # notre propre écrivain, le verrou partagé est légitime).
        conn.execute("ATTACH DATABASE ? AS reg",
                     (_sqlite_ro_uri(reg.db_path, params="mode=ro"),))
        for name in old_files:
            seg_path = name if os.path.isabs(name) else os.path.join(index_dir, name)
            if not os.path.exists(seg_path):
                # Même diagnostic que ContentStore : incohérence signalée,
                # jamais muette (ATTACH créerait sinon une base vide).
                _log.warning(f"[indexer] Segment de contenu introuvable à la "
                             f"compaction : {seg_path}")
                continue
            conn.execute("ATTACH DATABASE ? AS src", (_sqlite_ro_uri(seg_path),))
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO contenu(faiss_id, texte) "
                    "SELECT s.faiss_id, s.texte FROM src.contenu s "
                    "WHERE s.faiss_id IN (SELECT faiss_id FROM reg.chunks)")
            finally:
                # DETACH refuse une base engagée dans la transaction implicite
                # ouverte par l'INSERT : commit (sans journal ni fsync, peu
                # coûteux) avant de détacher.
                conn.commit()
                conn.execute("DETACH DATABASE src")
        conn.commit()
        conn.execute("DETACH DATABASE reg")
    finally:
        conn.close()
    _log.info(f"[indexer] {len(active_segments)} segments de contenu compactés "
              f"en un seul ({pub.content_name}).")
    return [{"file": pub.content_name, "lo": 0, "hi": run_hi}]


def _finalize_generation(pub, reg, vindex, index_dir, run_lo, run_hi,
                         content_written, rebuild_faiss, rebuild_bm25,
                         progress_cb, _log, ratio, note,
                         mark_bm25_stale: bool = False,
                         faiss_delta_lo: Optional[int] = None,
                         faiss_delta_max: int = 16):
    """
    Finalise et publie ATOMIQUEMENT une génération (checkpoint OU publication
    finale) : commit du registre, (re)construction ou réutilisation de FAISS et
    de BM25 (découplés : un checkpoint sauve le FAISS mais réutilise le BM25),
    bascule du manifest. Retourne le numéro de génération publié.

    `faiss_delta_lo` (audit perf 2.1, I-4) : point de reprise EN DELTA —
    premier identifiant FAISS alloué depuis le dernier point ; seuls les
    vecteurs d'identifiant >= cette borne sont écrits (`corpus.<gen>.delta
    .faiss`), par-dessus le FAISS de base et les deltas déjà publiés. None =
    FAISS complet (publication finale, ou RIVAGE_CHECKPOINT_DELTA=0). Au-delà
    de `faiss_delta_max` deltas, le point réécrit le FAISS complet (chaîne
    bornée pour les lecteurs).

    Ne réalise PAS la purge des disparus : l'appelant décide (elle n'a lieu
    qu'à la publication finale, quand tout le corpus a été parcouru).
    """
    # --- Commit du registre (léger) AVANT publication (M1) ------------------
    reg.commit()

    # --- Segments de contenu actifs de la nouvelle génération ---------------
    # On conserve les segments précédents qui portent encore des ids vivants
    # (GC des segments entièrement morts) + éventuellement le segment courant.
    active_segments = []
    for seg in pub.prev_content_segments():
        if reg.count_faiss_in_range(seg["lo"], seg["hi"]) > 0:
            active_segments.append(seg)
    if content_written:
        active_segments.append({"file": pub.content_name, "lo": run_lo, "hi": run_hi})

    # --- Compaction (publication finale uniquement, via rebuild_bm25) -------
    # Les checkpoints accumulent un segment par point ; au-delà d'un seuil, la
    # finale les fusionne en un seul (le coût O(contenu) est déjà payé par la
    # reconstruction BM25 qui suit).
    compact_threshold = _env_int("RIVAGE_COMPACT_SEGMENTS", 12)
    if (rebuild_bm25 and compact_threshold > 0
            and len(active_segments) > compact_threshold):
        active_segments = _compact_segments(
            pub, reg, index_dir, active_segments, run_hi, _log)

    # --- FAISS : reconstruire si le contenu a changé, sinon réutiliser ------
    faiss_deltas = pub.prev_faiss_deltas()
    if rebuild_faiss:
        faiss_ref = None
        ecrit_en_delta = False
        if (faiss_delta_lo is not None and pub.prev_faiss_ref()
                and vindex is not None and vindex.size > 0
                and len(faiss_deltas) < max(1, int(faiss_delta_max))):
            # Point de reprise en delta (I-4) : seuls les vecteurs ajoutés
            # depuis le dernier point partent sur le partage ; la base et
            # les deltas précédents restent référencés tels quels. Les
            # retraits éventuels du segment ne sont pas persistés ici (le
            # registre fait foi, voir VectorIndex.retirer_absents) ; sans
            # vecteur nouveau, rien n'est écrit.
            delta = vindex.extraire_delta(int(faiss_delta_lo))
            if delta is not None:
                if delta.size > 0:
                    if progress_cb:
                        progress_cb(ratio, "Écriture du delta FAISS")
                    delta.save(pub.delta_path)   # écriture atomique
                    faiss_deltas = faiss_deltas + [pub.delta_name]
                    _log.info(f"[indexer] Point de reprise en delta : "
                              f"{delta.size} vecteur(s) écrits ({pub.delta_name}), "
                              f"base {pub.prev_faiss_ref()} + "
                              f"{len(faiss_deltas)} delta(s).")
                faiss_ref = pub.prev_faiss_ref()
                ecrit_en_delta = True
        if not ecrit_en_delta:
            if progress_cb:
                progress_cb(ratio, "Écriture de l'index FAISS")
            faiss_deltas = []
            if vindex is not None and vindex.size > 0:
                vindex.save(pub.faiss_path)          # écriture atomique
                faiss_ref = pub.faiss_name
    else:
        faiss_ref = pub.prev_faiss_ref()

    # --- BM25 : reconstruit à la publication finale seulement ---------------
    if rebuild_bm25:
        if progress_cb:
            progress_cb(ratio, "Reconstruction de l'index BM25")
        live = set(reg.live_faiss_ids())
        cstore = ContentStore(index_dir, [s["file"] for s in active_segments],
                              ranges=active_segments)
        try:
            bm25_chunks = (
                {"faiss_id": fid, "texte": txt}
                for fid, txt in cstore.iter_all() if fid in live
            )
            lindex = LexicalIndex.build(bm25_chunks)
        finally:
            cstore.close()
        lindex.save(pub.bm25_path)
        bm25_ref = pub.bm25_name
        from lexical_index import FORMAT as bm25_format
    else:
        # Checkpoint (BM25 en retard toléré) ou métadonnées seules (moved) :
        # réutiliser la référence précédente (None possible au tout 1er run —
        # recherche vectorielle seule jusqu'à la publication finale).
        bm25_ref = pub.prev_bm25_ref()
        bm25_format = None                 # format reporté du manifest précédent

    reg.close()

    if progress_cb:
        progress_cb(ratio, note)
    return pub.publish(faiss_ref, bm25_ref, active_segments,
                       bm25_stale=mark_bm25_stale, bm25_format=bm25_format,
                       faiss_deltas=faiss_deltas)


# =============================================================================
#  FICHIERS EN VOL, TAMPON D'EMBEDDING ET POOL D'EXTRACTION
#  (audit perf 2.1, I-3 et I-5)
# =============================================================================

@dataclass
class _Job:
    """Un fichier nouveau/modifié à préparer, dans l'ordre du parcours."""
    index: int          # position dans le parcours (progression, ratio)
    path: str
    file_hash: str
    status: str         # "new" | "modified" (pour défaire le comptage)


@dataclass
class _EntreeTampon:
    """Un fichier ENREGISTRÉ au registre, en attente d'embedding (I-3)."""
    path: str
    file_hash: str
    status: str
    prep: Any                       # extraction.PreparedDocument
    chunks: list
    ocr_pending: int = 0
    faiss_ids: Optional[List[int]] = None
    dans_vindex: bool = False       # vecteurs ajoutés à l'index (à retirer si défait)
    integre: bool = False           # registre + segment écrits (comptage à défaire)


class _WorkerDied(MemoryError):
    """Un processus d'extraction est mort pendant ce fichier (mémoire épuisée
    tuée par le système, plantage natif d'un extracteur) : traité comme un
    fichier pathologique — sauté, non enregistré, retenté au prochain run —
    sans arrêter le run (même sémantique que std::bad_alloc en ligne)."""


class _RelaisJournal(logging.Handler):
    """Rejoue dans les loggers du processus PRINCIPAL les enregistrements
    reçus des processus de travail (QueueListener) : les messages des
    extracteurs apparaissent dans le journal de l'indexeur (interface,
    fichier) comme s'ils venaient de lui."""

    def emit(self, record):
        try:
            logging.getLogger(record.name).handle(record)
        except Exception:
            pass


# Chaîne maximale de deltas de points de reprise avant une réécriture
# complète du FAISS (RIVAGE_CHECKPOINT_DELTA_MAX) : borne le nombre de
# fichiers qu'un lecteur fusionne au chargement.
_DELTA_CHAIN_MAX = 16

# Un pool de processus ne vaut que pour un lot de fichiers : en dessous de ce
# multiple du nombre de processus (fichiers parcourus), l'extraction reste en
# ligne — le démarrage des processus coûterait plus que le gain.
_POOL_MIN_FILES_PAR_WORKER = 2


def _extract_workers_count(log=None) -> int:
    """Nombre de processus d'extraction (audit perf 2.1, I-5) :
    RIVAGE_EXTRACT_WORKERS, défaut max(1, cœurs - 1) ; 1 = extraction en ligne
    (comportement antérieur). Forcé à 1 dans un exécutable gelé : le
    démarrage d'un processus de travail y relancerait le programme principal
    (le lanceur n'appelle pas `multiprocessing.freeze_support`)."""
    v = _env_int("RIVAGE_EXTRACT_WORKERS", 0)
    if v <= 0:
        v = max(1, (os.cpu_count() or 1) - 1)
    if v > 1 and getattr(sys, "frozen", False):
        (log or logger).info("[indexer] Extraction parallèle indisponible dans "
                             "l'exécutable gelé : extraction en ligne.")
        return 1
    return min(int(v), 61)       # plafond Windows des handles d'attente


class _ExtractionPool:
    """
    Fenêtre BORNÉE de fichiers en cours de préparation (extraction + découpage
    + dédup), dans des processus de travail (`workers` > 1) ou en ligne
    (`workers` = 1, fenêtre de 1 : le fichier est préparé à la soumission).

    L'ORDRE du parcours est préservé : les résultats sont consommés dans
    l'ordre de soumission (file), quel que soit le processus qui a fini le
    premier — registre, FAISS, segments et points de reprise restent dans le
    processus principal, dans le même ordre qu'avant. Au plus
    2 × `workers` fichiers sont en vol : jamais tout le corpus pré-extrait en
    mémoire.

    Démarrage des processus en mode `spawn` partout (celui de Windows ; sûr
    avec les threads de l'interface et les bibliothèques natives) : les
    fonctions de travail sont importables (extraction._worker_*), les
    processus importent un module léger, les extracteurs lourds ne se
    chargent qu'au besoin. Journal des workers rejoué dans celui de
    l'indexeur (QueueHandler → QueueListener) ; arrêt demandé propagé par un
    événement partagé (coupe la boucle OCR page à page des workers).

    Un processus mort (mémoire, plantage natif) n'arrête pas le run : le pool
    est recréé et les fichiers en vol resoumis, y compris celui en tête de
    file — le processus mort n'est pas forcément le sien (revue 2.5, RA-4 :
    la mort est souvent constatée au `submit` suivant, pendant qu'un autre
    worker extrait la tête). Un fichier qui voit le pool mourir une seconde
    fois est tenu pour coupable (sauté, retenté au prochain run). Trois morts
    consécutives sans succès rabattent l'extraction en ligne pour le reste du
    run. Un pool trouvé mort au moment d'un `submit` (revue 2.5, RA-2) suit
    le même chemin au lieu d'avorter le run.
    """

    def __init__(self, workers: int, corpus_root, use_ocr, ocr_quality_threshold,
                 dedup_threshold, log=None, ocr_workers_par_proc: int = 0,
                 should_stop=None, extract_fn=None):
        from collections import deque
        self.workers = max(1, int(workers))
        self.parallele = self.workers > 1
        self.fenetre = 2 * self.workers if self.parallele else 1
        self._file = deque()                    # (job, future), ordre de soumission
        self._log = log or logger
        self._args = (corpus_root, bool(use_ocr), float(ocr_quality_threshold),
                      float(dedup_threshold))
        self._should_stop = should_stop
        self._extract_fn = extract_fn
        self._ocr_workers = int(ocr_workers_par_proc or 0)
        self._executor = None
        self._ctx = self._stop_event = self._log_queue = self._listener = None
        self._morts_consecutives = 0
        self._soupcons: Dict[str, int] = {}     # chemin -> morts du pool vues en tête
        if self.parallele:
            import multiprocessing
            self._ctx = multiprocessing.get_context("spawn")
            self._stop_event = self._ctx.Event()
            self._log_queue = self._ctx.Queue()
            from logging.handlers import QueueListener
            self._listener = QueueListener(self._log_queue, _RelaisJournal())
            self._listener.start()

    # --- Soumission / consommation ------------------------------------------

    def __len__(self) -> int:
        return len(self._file)

    def plein(self) -> bool:
        return len(self._file) >= self.fenetre

    def _executeur(self):
        if self._executor is None:
            from concurrent.futures import ProcessPoolExecutor
            from extraction import _worker_init
            niveau = min(logging.getLogger().getEffectiveLevel(),
                         self._log.getEffectiveLevel())
            self._executor = ProcessPoolExecutor(
                max_workers=self.workers, mp_context=self._ctx,
                initializer=_worker_init,
                initargs=(self._log_queue, self._stop_event, niveau,
                          getattr(self._log, "name", None) or "indexer",
                          self._ocr_workers))
        return self._executor

    def submit(self, job: _Job) -> None:
        """Soumet un fichier ; en mode en ligne, le prépare immédiatement
        (le résultat ou l'exception est rendu à `resultat`)."""
        from concurrent.futures import Future
        if self.parallele:
            from concurrent.futures.process import BrokenProcessPool
            from extraction import _worker_prepare
            try:
                fut = self._executeur().submit(_worker_prepare, job.path, *self._args)
            except BrokenProcessPool as e:
                # Pool mort entre deux consommations (RA-2) : BrokenProcessPool
                # est une RuntimeError, qui avortait tout le run. On la range
                # dans la future : `resultat` la traitera comme une mort en vol
                # (pool recréé, fichiers resoumis).
                fut = Future()
                fut.set_exception(e)
        else:
            from extraction import prepare_document
            fut = Future()
            try:
                fut.set_result(prepare_document(
                    job.path, *self._args, log=self._log,
                    should_stop=self._should_stop, extract_fn=self._extract_fn))
            except BaseException as e:          # relevée à la consommation
                fut.set_exception(e)
        self._file.append((job, fut))

    def pop(self):
        """Le plus ancien (job, future) en vol."""
        return self._file.popleft()

    def resultat(self, job: _Job, fut):
        """Résultat de la préparation de `job` (bloque jusqu'à sa fin) ; les
        exceptions du worker sont relevées telles quelles (MemoryError,
        std::bad_alloc…). Un pool cassé est relancé, voir la classe."""
        from concurrent.futures import CancelledError
        from concurrent.futures.process import BrokenProcessPool
        try:
            prep = fut.result()
        except CancelledError:
            # Annulé par `arreter` avant d'avoir commencé : même sort qu'un
            # fichier extrait pendant l'arrêt (non enregistré, repris).
            raise _StopDuringExtraction(job.path)
        except BrokenProcessPool as e:
            self._morts_consecutives += 1
            restants = list(self._file)
            self._file.clear()
            try:
                if self._executor is not None:
                    self._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._executor = None
            if self._morts_consecutives >= 3:
                self._log.error("[indexer] Pool d'extraction mort trois fois de "
                                "suite — extraction EN LIGNE pour le reste du run.")
                self.parallele = False
                self.fenetre = 1
            # Coupable ? Pas à la première mort vue par ce fichier (RA-4) : la
            # mort peut être celle d'un autre worker, constatée pendant que la
            # tête s'extrayait sainement. Il est resoumis EN TÊTE ; s'il voit
            # le pool mourir une seconde fois, il est sauté.
            soupcons = self._soupcons.get(job.path, 0) + 1
            self._soupcons[job.path] = soupcons
            if soupcons >= 2:
                self._log.warning(f"[indexer] Processus d'extraction mort ({e}) une "
                                  f"seconde fois pendant {job.path} : fichier sauté, "
                                  f"pool relancé, {len(restants)} fichier(s) en vol "
                                  f"resoumis.")
                for j, _ in restants:
                    self.submit(j)
                raise _WorkerDied(
                    f"processus d'extraction mort pendant {job.path} (mémoire "
                    f"épuisée ou plantage natif d'un extracteur) : {e}") from e
            self._log.warning(f"[indexer] Processus d'extraction mort ({e}) : pool "
                              f"relancé, {job.path} et {len(restants)} fichier(s) en "
                              f"vol resoumis (le coupable est désigné à la seconde "
                              f"mort).")
            self.submit(job)
            for j, _ in restants:
                self.submit(j)
            _j, fut2 = self._file.popleft()        # la tête : `job` lui-même
            return self.resultat(job, fut2)
        self._morts_consecutives = 0
        self._soupcons.pop(job.path, None)
        return prep

    # --- Arrêt / fermeture --------------------------------------------------

    def arreter(self) -> None:
        """Arrêt demandé : pose l'événement partagé (les extractions en cours
        s'abrègent) et annule les fichiers pas encore commencés. La file est
        conservée : l'appelant consomme chaque résultat dans l'ordre — un
        fichier annulé est relevé comme _StopDuringExtraction."""
        if self._stop_event is not None:
            self._stop_event.set()
        for _job, fut in self._file:
            fut.cancel()

    def fermer(self, arret: bool = False) -> None:
        """Termine le pool : attend les fichiers en cours (après `arret`,
        l'événement partagé abrège leur OCR), ferme la file de journal."""
        if arret and self._stop_event is not None:
            self._stop_event.set()
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                pass
            self._executor = None
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        if self._log_queue is not None:
            try:
                self._log_queue.close()
                self._log_queue.join_thread()
            except Exception:
                pass
            self._log_queue = None


def _ocr_pending_flag(prep, use_ocr: bool, ocr_quality_threshold: float) -> int:
    """1 si le document est enregistré SANS OCR alors qu'il en aurait eu
    besoin — il sera RETRAITÉ automatiquement à un run où l'OCR est coché.

    Marqué UNIQUEMENT quand use_ocr est décoché : les dégradations choisies
    par configuration fine (RIVAGE_OCR_IMAGES=0, plafond de pages…) sont des
    choix assumés, pas des rattrapages en boucle. `prep` : le document
    préparé (extraction.PreparedDocument).
    """
    if use_ocr:
        return 0
    if prep.ext == ".pdf":
        q = prep.quality_score
        if not prep.has_text or (q is not None and q < ocr_quality_threshold):
            return 1
    elif prep.ext == ".pptx" and prep.pptx_image_count:
        return 1
    return 0


def _register_file(reg: Registry, doc, file_hash: str, path: str,
                   ocr_pending: int = 0) -> bool:
    # Protégé (audit) : le fichier peut avoir disparu entre l'extraction et cet
    # os.stat (corpus vivant). On saute l'enregistrement — le fichier sera
    # réexaminé au run suivant — plutôt que d'avorter tout le run. Retourne
    # False dans ce cas : l'appelant NE DOIT alors ni allouer d'ids ni écrire
    # de chunks (audit v2 : chunks orphelins impurgeables sinon).
    try:
        st = os.stat(path)
    except OSError as e:
        logger.warning(f"[indexer] Enregistrement sauté ({path}): {e}")
        return False
    reg.upsert_file(FileRecord(
        hash=file_hash, chemin=path, taille=st.st_size,
        mtime=st.st_mtime, famille=doc.family, ocr_pending=ocr_pending,
    ))
    return True
