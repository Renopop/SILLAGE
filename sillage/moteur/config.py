# config.py
"""
Configuration locale de RIVAGE (confort d'utilisation).

Mémorise le **chemin d'index par défaut** choisi au moment de l'indexation,
pour que les lecteurs (interface de consultation) n'aient plus à le saisir.

Emplacement : un fichier `rivage.config.json` **à côté de l'application**
(dossier des scripts). Si l'application est lancée depuis le partage réseau,
l'indexeur l'écrit et tous les lecteurs le voient. Si ce dossier n'est pas
inscriptible, on se replie sur le dossier personnel de l'utilisateur
(`~/.rivage/rivage.config.json`), lu en second recours.
"""

import os
import sys
import json
import time
import logging

logger = logging.getLogger(__name__)

CONFIG_NAME = "rivage.config.json"


def _app_dir() -> str:
    """
    Dossier de l'application. Sous PyInstaller (`sys.frozen`), `__file__`
    pointe DANS l'archive/_internal de l'exécutable : le dossier de référence
    est alors celui de l'exe lui-même — c'est lui qui vit sur le partage.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _app_config_path() -> str:
    """Chemin du fichier de config à côté des scripts (dossier de l'appli)."""
    return os.path.join(_app_dir(), CONFIG_NAME)


def _home_config_path() -> str:
    """Chemin de repli dans le dossier personnel."""
    return os.path.join(os.path.expanduser("~"), ".rivage", CONFIG_NAME)


class ConfigUnreadable(RuntimeError):
    """Fichier de configuration PRÉSENT mais illisible (accès, JSON, encodage)
    — à distinguer d'un fichier absent, cas normal."""


def _read(path: str, strict: bool = False) -> dict:
    """Lit un fichier de config JSON. Absent → {}. Présent mais illisible :
    {} en mode tolérant (lecteurs : l'absence de défaut n'est pas grave),
    ConfigUnreadable en mode strict (indexeur : les exclusions de thèmes ne
    doivent jamais être perdues en silence — audit V2, A-8)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        if strict:
            raise ConfigUnreadable(f"{path}: {e}") from e
        return {}


def load_config(strict: bool = False) -> dict:
    """
    Lit la configuration en fusionnant le fichier appli et le fichier
    personnel : le plus récemment modifié gagne sur les clés en conflit (M1 :
    une sauvegarde repliée sur ~/.rivage ne doit pas être masquée par un
    fichier appli périmé). `strict` : voir _read.
    """
    entries = []
    for path in (_app_config_path(), _home_config_path()):
        data = _read(path, strict=strict)
        if data:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            entries.append((mtime, data))
    # Appliquer du plus ancien au plus récent : le plus récent écrase.
    merged: dict = {}
    for _, data in sorted(entries, key=lambda e: e[0]):
        merged.update(data)
    return merged


def save_config(data: dict) -> str:
    """
    Écrit la configuration (fusionnée avec l'existante). Tente d'abord le
    dossier de l'appli — avec retries sur PermissionError, cas SMB où un
    lecteur tient le fichier ouvert (M1) — puis se replie sur le dossier
    personnel avec un avertissement visible. Retourne le chemin écrit.
    """
    merged = load_config()
    # Écriture AUTORITAIRE : l'appelant (set_excluded_themes) a déjà lu l'état
    # courant via load_config et fournit le dict `exclude_themes` complet, y
    # compris les suppressions. On NE fusionne PAS sous-clé par sous-clé, sinon
    # retirer l'exclusion d'un corpus (pop de sa clé) serait annulé par la
    # fusion — un thème recoché resterait exclu à jamais. Le modèle « un seul
    # indexeur » garantit l'absence d'écritures concurrentes sur ce fichier.
    merged.update(data)

    for i, path in enumerate((_app_config_path(), _home_config_path())):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Nom de temporaire unique par processus (m5) : deux écrivains ne
            # doivent pas entrelacer leurs octets dans le même .tmp partagé.
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            _replace_with_retry(tmp, path)
            if i == 1:
                logger.warning(
                    "[config] Dossier de l'application non inscriptible : "
                    f"configuration écrite dans {path}. Les autres postes ne la "
                    "verront pas tant que le fichier appli n'est pas mis à jour."
                )
            return path
        except OSError as e:
            logger.warning(f"[config] Écriture impossible dans {path}: {e}")
            continue
    raise RuntimeError("Impossible d'écrire la configuration RIVAGE.")


def _replace_with_retry(tmp: str, path: str, retries: int = 4,
                        base_delay: float = 0.25) -> None:
    """os.replace robuste aux verrous de lecture SMB (PermissionError)."""
    last = None
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last = e
            time.sleep(base_delay * (2 ** attempt))
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last if last else OSError(f"os.replace a échoué: {path}")


def get_default_index_dir() -> str:
    """Retourne le chemin d'index par défaut mémorisé, ou une chaîne vide."""
    return load_config().get("index_dir", "") or ""


def set_default_index_dir(index_dir: str) -> str:
    """Mémorise le chemin d'index par défaut. Retourne le fichier de config
    écrit — ou "" si la mémorisation est désactivée (RIVAGE_SET_DEFAULT_INDEX=0) :
    pendant une RÉINDEXATION dans un répertoire vide, chaque point de reprise
    publie une génération partielle, et ce défaut, rafraîchi à chaque run,
    renverrait les consultateurs vers l'index incomplet au lieu de l'ancien
    (revue 2.5.2). Les lecteurs restent alors sur RIVAGE_INDEX_DIR ou sur le
    dernier défaut mémorisé."""
    if (os.environ.get("RIVAGE_SET_DEFAULT_INDEX", "1").strip() == "0"):
        logger.info("[config] Défaut d'index NON mémorisé (RIVAGE_SET_DEFAULT_INDEX=0).")
        return ""
    return save_config({"index_dir": index_dir})


# =============================================================================
#  EXCLUSIONS D'INDEXATION (thèmes entiers)
# =============================================================================
#
#  Un « thème » est un répertoire de 1er niveau du corpus (§10.2). Les thèmes
#  exclus ne sont jamais parcourus ni encodés. La liste effective fusionne :
#    - la config appli (rivage.config.json, par corpus),
#    - un fichier `.rivageignore` à la racine du corpus (portable, une entrée
#      par ligne ; `#` = commentaire).

RIVAGEIGNORE = ".rivageignore"


def _corpus_key(corpus_root: str) -> str:
    # realpath résout les lecteurs mappés / liens (M3) : le même corpus vu via
    # \\serveur\part et Z:\ doit donner la même clé d'exclusions.
    return os.path.normcase(os.path.realpath(os.path.abspath(corpus_root)))


def _corpus_keys(corpus_root: str) -> list:
    """
    Clés candidates pour ce corpus : chemin résolu (realpath) ET chemin brut.
    La résolution d'un lecteur réseau peut échouer TRANSITOIREMENT (realpath
    rend alors le chemin non résolu) : écrire/relire sous les deux orthographes
    évite qu'une exclusion enregistrée sous l'une soit invisible sous l'autre
    (audit). Lecture = union ; écriture = les deux clés.
    """
    resolved = _corpus_key(corpus_root)
    raw = os.path.normcase(os.path.abspath(corpus_root))
    return [resolved] if resolved == raw else [resolved, raw]


def norm_theme(name: str) -> str:
    """Nom de thème normalisé pour stockage/comparaison : sans / ni \\ final (m8)."""
    return str(name).strip().rstrip("/\\")


def _read_rivageignore(corpus_root: str, strict: bool = False) -> list:
    """Thèmes du .rivageignore. Absent → []. Présent mais illisible (accès,
    encodage) : [] avec avertissement en mode tolérant, ConfigUnreadable en
    mode strict (indexeur — audit V2, A-8)."""
    path = os.path.join(corpus_root, RIVAGEIGNORE)
    themes = []
    try:
        # utf-8-sig : tolère le BOM ajouté par Notepad (m6).
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                themes.append(norm_theme(line))
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as e:
        if strict:
            raise ConfigUnreadable(f"{path}: {e}") from e
        logger.warning(f"[config] .rivageignore illisible ({path}): {e} — ignoré.")
    return themes


def _write_rivageignore(corpus_root: str, themes) -> None:
    """Écrit (au mieux) le .rivageignore à la racine du corpus."""
    path = os.path.join(corpus_root, RIVAGEIGNORE)
    # Ne pas réécrire si l'ensemble effectif est inchangé (C6) : préserve un
    # fichier maintenu à la main (commentaires) tant que rien ne change.
    if set(themes) == set(_read_rivageignore(corpus_root)):
        return
    lines = [
        "# RIVAGE — thèmes (répertoires de 1er niveau) exclus de l'indexation.",
        "# Une entrée par ligne. Portable avec le corpus.",
        "",
    ]
    lines.extend(sorted(set(themes)))
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        # Échec visible (M3) : sinon les exclusions n'existent que sous une
        # orthographe de chemin, sans que personne ne le sache.
        logger.warning(f"[config] .rivageignore non écrit ({path}): {e}")


def get_excluded_themes(corpus_root: str, strict: bool = False) -> set:
    """
    Thèmes exclus pour ce corpus : union de la config appli (toutes les
    orthographes du chemin) et du .rivageignore. `strict=True` (indexeur) :
    un fichier présent mais illisible lève ConfigUnreadable au lieu d'être
    silencieusement compté pour vide.
    """
    cfg = load_config(strict=strict)
    ex = cfg.get("exclude_themes", {})
    from_cfg: set = set()
    for key in _corpus_keys(corpus_root):
        from_cfg |= {norm_theme(t) for t in ex.get(key, [])}
    return from_cfg | set(_read_rivageignore(corpus_root, strict=strict))


def set_excluded_themes(corpus_root: str, themes) -> None:
    """
    Mémorise les thèmes exclus pour ce corpus, dans la config (sous TOUTES les
    orthographes du chemin — voir _corpus_keys) ET dans le .rivageignore à la
    racine du corpus.

    Toute clé ALIAS existante résolvant vers le même corpus est d'abord purgée
    (audit v2) : sans cela, une exclusion posée jadis sous une autre orthographe
    (lecteur mappé, lien) survivait à la ré-inclusion et était ressuscitée par
    l'union de lecture — la case se « re-décochait toute seule ».
    """
    import re as _re
    themes = sorted({norm_theme(t) for t in themes if norm_theme(t)})
    cfg = load_config()
    ex = cfg.get("exclude_themes", {})
    # Carte d'alias PERSISTÉE (audit v3) : {clé_brute -> clé_résolue}, remplie à
    # l'écriture (moment où la résolution realpath a réussi). La purge des alias
    # se fait ensuite par simple consultation — ZÉRO realpath réseau sur les
    # clés des autres corpus (le gel sur partage lent/mort disparaît), et un
    # alias de n'importe quelle orthographe (symlink, lettre montée à la racine)
    # est retrouvé, pas seulement ceux partageant le nom de base.
    amap = dict(cfg.get("exclude_aliases", {}))
    resolved = _corpus_key(corpus_root)
    own_keys = set(_corpus_keys(corpus_root))

    # Purge des alias connus de CE corpus (consultation de la carte, sans I/O).
    for k in list(ex.keys()):
        if k not in own_keys and amap.get(k) == resolved:
            ex.pop(k)
            if not themes:
                amap.pop(k, None)
    # GC des LETTRES DE LECTEUR mortes référencées par la carte (cible connue =
    # sûre) : isdir sur une lettre non mappée répond localement et vite.
    for k in list(amap.keys()):
        if (amap[k] == resolved and _re.match(r"^[a-zA-Z]:[\\/]?", str(k))
                and not os.path.isdir(k)):
            ex.pop(k, None)
            amap.pop(k, None)

    for key in own_keys:
        if themes:
            ex[key] = themes
        else:
            ex.pop(key, None)
    # Mémoriser l'alias courant (brut -> résolu) tant que des exclusions vivent.
    if themes:
        for key in own_keys:
            if key != resolved:
                amap[key] = resolved
    save_config({"exclude_themes": ex, "exclude_aliases": amap})
    _write_rivageignore(corpus_root, themes)


def effective_exclusions_after_edit(corpus_root: str, current_themes, unchecked) -> list:
    """
    Calcule la liste d'exclusions à persister après une édition dans la GUI :
    les thèmes décochés PLUS les exclusions mémorisées pour des thèmes
    actuellement absents du disque (C6 : un thème momentanément archivé ne doit
    pas perdre son exclusion parce qu'il n'apparaît plus dans la liste).

    Comparaison INSENSIBLE À LA CASSE (audit v2) : l'indexeur compare via
    normcase (répertoires Windows) — sans l'alignement ici, une exclusion
    « ARCHIVES » face à un dossier « Archives » était classée « absente »,
    conservée à jamais, et le thème restait exclu avec sa case affichée cochée.
    """
    current_nc = {os.path.normcase(norm_theme(t)) for t in current_themes}
    kept_absent = {t for t in get_excluded_themes(corpus_root)
                   if os.path.normcase(t) not in current_nc}
    return sorted({norm_theme(t) for t in unchecked} | kept_absent)
