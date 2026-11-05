"""Le moteur de RIVAGE embarqué : SILLAGE fonctionne sans aucun RIVAGE extérieur."""
import importlib
import subprocess
import sys
from pathlib import Path

from sillage import moteur, rivage

MOTEUR = Path(moteur.DOSSIER)


def test_copie_complete():
    from outils.synchroniser_rivage import HOOKS, MODULES
    for m in MODULES:
        assert (MOTEUR / f"{m}.py").exists(), m
    for h in HOOKS:
        assert (MOTEUR / h).exists(), h
    assert (MOTEUR / "VERSION_RIVAGE.txt").read_text(encoding="utf-8").startswith("RIVAGE ")
    assert moteur.version_rivage()[0].isdigit()


def test_localisation_embarquee(monkeypatch):
    monkeypatch.delenv("SILLAGE_RIVAGE_DIR", raising=False)
    assert rivage.disponible(), rivage.erreur()
    assert Path(rivage.localiser()) == MOTEUR
    import retrieval, indexer, index_cache, plateforme, diagnostic  # noqa: F401
    assert Path(retrieval.__file__).resolve().parent == MOTEUR
    assert rivage.version_rivage() == moteur.version_rivage()


def test_modules_importables():
    from outils.synchroniser_rivage import MODULES
    rivage.localiser()
    for m in MODULES:
        mod = importlib.import_module(m)
        assert Path(mod.__file__).resolve().parent == MOTEUR, m


def test_fichiers_env_du_dossier_application(tmp_path, monkeypatch):
    """Les fichiers RIVAGE.env / RIVAGE_keys.env du dossier de SILLAGE sont désignés au moteur."""
    monkeypatch.delenv("RIVAGE_ENV_FILE", raising=False)
    monkeypatch.delenv("RIVAGE_KEYS_FILE", raising=False)
    monkeypatch.setattr(moteur, "_ACTIVE", False)
    monkeypatch.setattr(moteur, "dossier_application", lambda: tmp_path)
    (tmp_path / "RIVAGE.env").write_text("RIVAGE_RERANK_K=5\n", encoding="utf-8")
    moteur.activer()
    import os
    assert os.environ["RIVAGE_ENV_FILE"] == str(tmp_path / "RIVAGE.env")
    assert "RIVAGE_KEYS_FILE" not in os.environ


def test_sonde_fils_en_script():
    """`rivage_app.py` de SILLAGE répond à la sonde de plateforme comme celui de RIVAGE."""
    r = subprocess.run([sys.executable, str(MOTEUR / "rivage_app.py"), "--sonde-plateforme", "{}"],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode in (0, 3), r.stderr[-500:]
    r2 = subprocess.run([sys.executable, "-m", "sillage", "--sonde-plateforme", "{}"],
                        capture_output=True, text=True, timeout=180, cwd=str(MOTEUR.parents[1]))
    assert r2.returncode in (0, 3), r2.stderr[-500:]
