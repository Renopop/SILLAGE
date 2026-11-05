"""Interface graphique : construction, lecture d'une NCM, grille à l'écran,
filtres, détail, dialogues — sous un serveur X virtuel (Xvfb) si disponible."""
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

pytestmark = pytest.mark.skipif(not (shutil.which("Xvfb") or sys.platform.startswith("win")),
                                reason="pas de serveur X (Xvfb) pour l'interface")

SCENARIO = textwrap.dedent('''
    import json, os, sys, time, queue
    sys.path.insert(0, os.environ["SILLAGE_RACINE"])
    sys.path.insert(0, os.path.join(os.environ["SILLAGE_RACINE"], "tests"))
    from conftest import NCM_A, SPEC, ecrire_xlsx
    import tkinter as tk
    from tkinter import messagebox
    from sillage import gui
    from sillage.statuts import DictionnaireStatuts
    from sillage.modele import Statut

    tmp = os.environ["SILLAGE_TMP"]
    spec = ecrire_xlsx(os.path.join(tmp, "spec.xlsx"), SPEC)
    ncm = ecrire_xlsx(os.path.join(tmp, "ncm_revB.xlsx"), NCM_A + [["REQ-006", "Vert", "statut inconnu", "", ""]])
    messagebox.showinfo = lambda *a, **k: None
    messagebox.showwarning = lambda *a, **k: None
    messagebox.showerror = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(str(a)))

    app = gui.Application()
    app.v_spec.set(str(spec)); app.v_ncm.set(str(ncm)); app.v_fournisseur.set("A")
    # le dialogue des statuts inconnus répond « Vert = C » puis relit
    def dialogue(inconnus=None):
        if inconnus:
            DictionnaireStatuts.charger().ajouter("Vert", Statut.CONFORME)
            return True
        return False
    app.dialogue_statuts = dialogue

    def attendre(cond, delai=30.0):
        t0 = time.time()
        while not cond():
            app.update()
            time.sleep(0.02)
            if time.time() - t0 > delai:
                raise SystemExit("délai dépassé : " + app.v_statut_bar.get())

    app.lire_ncm()
    attendre(lambda: app.grille is not None and not app.en_cours)
    items = app.tree.get_children()
    assert len(items) == 7, len(items)
    par = {app.tree.item(i, "text"): app.tree.item(i, "values") for i in items}
    assert par["REQ-6"][0] == "C", par["REQ-6"]            # Vert → C après enrichissement et relecture
    assert par["REQ-9"][3] == "orphelin", par["REQ-9"]
    assert par["REQ-2"][2] == "action", par["REQ-2"]
    # filtres
    app.v_statut_filtre.set("écarts (PC + NC)"); app._rafraichir_grille()
    assert len(app.tree.get_children()) == 2
    app.v_statut_filtre.set("tous"); app.v_alerte_filtre.set("orphelin (identifiant inconnu)"); app._rafraichir_grille()
    assert len(app.tree.get_children()) == 1
    app.v_alerte_filtre.set("toutes"); app.v_recherche.set("DAL"); app._rafraichir_grille()
    assert len(app.tree.get_children()) == 1
    app.v_recherche.set(""); app._rafraichir_grille()
    # détail
    item = [i for i in app.tree.get_children() if app.tree.item(i, "text") == "REQ-4"][0]
    app.tree.selection_set(item); app._afficher_detail()
    detail = app.detail.get("1.0", "end")
    assert "RFD-0007" in detail and "non conforme" in detail, detail
    # résumé et diagnostic
    assert "Trous" in app.t_resume.get("1.0", "end")
    assert "Identifiants du référentiel retrouvés" in app.t_diag.get("1.0", "end")
    # export de la grille
    sortie = os.path.join(tmp, "grille.xlsx")
    from tkinter import filedialog
    filedialog.asksaveasfilename = lambda *a, **k: sortie
    app.exporter_grille()
    assert os.path.exists(sortie)
    # diff avec la même NCM (aucun changement)
    filedialog.askopenfilename = lambda *a, **k: str(ncm)
    app.comparer()
    attendre(lambda: app.diff is not None and not app.en_cours)
    assert app.diff.resume()["statut"] == 0
    # affaiblissements sur un dossier de documents
    docs = os.path.join(tmp, "docs"); os.makedirs(docs, exist_ok=True)
    open(os.path.join(docs, "reponse.txt"), "w", encoding="utf-8").write("REQ-002 : TBC, where practicable.\\n")
    app.v_documents.set(docs)
    app.affaiblissements()
    attendre(lambda: app.releve is not None and not app.en_cours)
    item = [i for i in app.tree.get_children() if app.tree.item(i, "text") == "REQ-2"][0]
    assert "affaiblissement" in app.tree.item(item, "values")[3]
    # profil des colonnes (dialogue confirmé tel quel)
    class Faux:
        def __init__(self, parent, fournisseur, en_tetes, proposition, existant=None):
            from sillage.profils import Profil
            self.profil = Profil(fournisseur, {r: h for r, h in proposition.items() if not h.startswith("colonne ")})
    gui.DialogueProfil = Faux
    app.wait_window = lambda w: None
    app.confirmer_profil()
    from sillage.profils import Profil
    assert Profil.charger("A").colonnes["statut"] == "Compliance"
    # lot 4 : traçabilité chargée depuis un JSON → alerte impact norme sur REQ-4 (NC, tracée CS 25.1309)
    from sillage.norme import Tracabilite, Trace
    t = Tracabilite(); t.traces["REQ-4"] = Trace("REQ-4", "CS 25.1309", True, origine="relue")
    tj = os.path.join(tmp, "trac.json"); t.enregistrer(tj)
    filedialog.askopenfilename = lambda *a, **k: tj
    app.charger_tracabilite()
    item = [i for i in app.tree.get_children() if app.tree.item(i, "text") == "REQ-4"][0]
    assert "impact_norme" in app.tree.item(item, "values")[3], app.tree.item(item, "values")
    app.v_alerte_filtre.set("impact norme (écart sur exigence tracée)"); app._rafraichir_grille()
    assert len(app.tree.get_children()) == 1
    app.v_alerte_filtre.set("toutes"); app._rafraichir_grille()
    gui.DialogueTracer = lambda parent, idx="": type("D", (), {"resultat": None, "destroy": lambda self: None})()
    app.tracer_norme()
    # lot 5 : fiches de revue avec un faux client de synthèse, export Word
    import sillage.fiches as fiches_mod
    from types import SimpleNamespace
    class FauxSynth:
        def available(self): return True
        def synthesize_raw(self, question, user_content, sources, log=None, temperature=0.1, max_tokens=1024, system_prompt=None):
            return SimpleNamespace(answer="ÉCART : écart (NCM). RISQUE : risque (NCM). QUESTIONS POUR LA REVUE : 1. ?", sources=sources,
                                   model="faux", error=None, fallback_used=False, tronquee=False)
        def close(self): pass
    fiches_mod.ouvrir_synthesizer = lambda log=None: FauxSynth()
    messagebox.askyesno = lambda *a, **k: True
    app.lire_ncm()                      # le profil enregistré impose une relecture de la NCM
    attendre(lambda: app.ncm is not None and not app.en_cours)
    app.rediger_fiches()
    attendre(lambda: app.fiches is not None and not app.en_cours)
    assert sorted(app.fiches.fiches) == ["REQ-2", "REQ-4"], list(app.fiches.fiches)
    item = [i for i in app.tree.get_children() if app.tree.item(i, "text") == "REQ-4"][0]
    app.tree.selection_set(item); app._afficher_detail()
    assert "Fiche de revue" in app.detail.get("1.0", "end")
    sortie_docx = os.path.join(tmp, "fiches.docx")
    filedialog.asksaveasfilename = lambda *a, **k: sortie_docx
    app.exporter_fiches()
    assert os.path.exists(sortie_docx)
    # lot 7 : capitaliser la campagne, fenêtre de la mémoire, historique dans le détail
    app.v_projet.set("PROJET-TEST"); app.v_signee.set(True)
    app.capitaliser()
    from sillage.memoire import Memoire
    with Memoire() as mem:
        assert [(c.projet, c.signee) for c in mem.campagnes()] == [("PROJET-TEST", True)], mem.campagnes()
    app.v_projet.set("PROJET-SUIVANT"); app.calculer_grille()
    item = [i for i in app.tree.get_children() if app.tree.item(i, "text") == "REQ-4"][0]
    app.tree.selection_set(item); app._afficher_detail()
    assert "Mémoire des projets" in app.detail.get("1.0", "end") and "écart déjà accepté" in app.detail.get("1.0", "end")
    fen = gui.FenetreMemoire(app, Memoire()); fen.update(); fen.v_texte.set("DAL"); fen._chercher()
    assert len(fen.t_rech.get_children()) == 1; fen._fermer()
    # fiche poste et à propos ne lèvent pas
    gui.FenetreTexte = lambda *a, **k: None
    app.fiche_poste(); app.a_propos()
    app.quitter()
    print("GUI OK")
''')


def test_interface(tmp_path):
    racine = Path(__file__).resolve().parents[1]
    scenario = tmp_path / "scenario.py"
    scenario.write_text(SCENARIO, encoding="utf-8")
    import os
    env = dict(os.environ)
    env.update(SILLAGE_RACINE=str(racine), SILLAGE_TMP=str(tmp_path), SILLAGE_HOME=str(tmp_path / "home"))
    cmd = [sys.executable, str(scenario)]
    if not sys.platform.startswith("win"):
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1400x900x24"] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    assert r.returncode == 0 and "GUI OK" in r.stdout, (r.stdout[-2000:] + "\n" + r.stderr[-3000:])
