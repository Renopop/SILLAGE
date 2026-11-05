"""Recopie le moteur de RIVAGE dans `sillage/moteur/` (copie embarquée).

    python outils/synchroniser_rivage.py chemin/vers/RIVAGE

SILLAGE est autonome : il embarque sa propre copie des modules de RIVAGE qui
font l'indexation et la recherche (extraction, découpage, déduplication,
embeddings, index vectoriel et lexical, registre, stockage, indexeur,
retrieval, cache lecteur, clients de la passerelle, diagnostic et sonde de
plateforme). Les fichiers sont copiés TELS QUELS (mêmes noms, imports plats
inchangés) : ce script permet de reprendre une version plus récente de RIVAGE
en une commande. La version et le commit de RIVAGE copiés sont notés dans
`sillage/moteur/VERSION_RIVAGE.txt`.

Ne sont pas copiés : les interfaces de RIVAGE, la base de recherche auto-RAG,
l'historique, le panier, le lanceur (`rivage_app.py` de SILLAGE est un
minimal propre à SILLAGE, conservé).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

MODULES = [
    # extraction
    "extraction", "chunking", "dedup", "pdf_processing", "docx_processing", "pptx_processing",
    "excel_processing", "llm_ocr",
    # index et stockage
    "embeddings", "vector_index", "lexical_index", "registry", "store", "content_store", "indexer",
    # lecture
    "retrieval", "index_cache", "synthesis",
    # configuration, passerelle, robustesse
    "models_utils", "config", "diagnostic", "plateforme", "version",
]
HOOKS = ["hooks/rth_plateforme.py"]
PROPRES_A_SILLAGE = {"__init__.py", "rivage_app.py", "VERSION_RIVAGE.txt"}


def _commit(rivage: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(rivage), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _version(rivage: Path) -> str:
    src = (rivage / "version.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"\'')
    return "?"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def synchroniser(rivage: Path, moteur: Path) -> list:
    rivage, moteur = Path(rivage), Path(moteur)
    manquants = [m for m in MODULES if not (rivage / f"{m}.py").exists()]
    if manquants:
        raise SystemExit(f"modules absents de {rivage} : {', '.join(manquants)}")
    moteur.mkdir(parents=True, exist_ok=True)
    (moteur / "hooks").mkdir(exist_ok=True)
    changes = []
    for m in MODULES:
        src, dst = rivage / f"{m}.py", moteur / f"{m}.py"
        if not dst.exists() or _sha(src) != _sha(dst):
            shutil.copyfile(src, dst)
            changes.append(f"{m}.py")
    for h in HOOKS:
        src, dst = rivage / h, moteur / h
        if src.exists() and (not dst.exists() or _sha(src) != _sha(dst)):
            shutil.copyfile(src, dst)
            changes.append(h)
    # fichiers copiés jadis mais plus dans la liste : signalés, pas supprimés
    for p in moteur.glob("*.py"):
        if p.stem not in MODULES and p.name not in PROPRES_A_SILLAGE:
            print(f"  (fichier hors liste conservé : {p.name})")
    (moteur / "VERSION_RIVAGE.txt").write_text(
        f"RIVAGE {_version(rivage)}\ncommit {_commit(rivage) or 'inconnu'}\n"
        f"copié le {date.today().isoformat()} depuis {rivage}\n", encoding="utf-8")
    return changes


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    rivage = Path(argv[0]).resolve()
    moteur = Path(__file__).resolve().parents[1] / "sillage" / "moteur"
    changes = synchroniser(rivage, moteur)
    print(f"RIVAGE {_version(rivage)} ({_commit(rivage) or 'commit inconnu'}) → {moteur}")
    print(f"{len(changes)} fichier(s) mis à jour" + (" : " + ", ".join(changes) if changes else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
