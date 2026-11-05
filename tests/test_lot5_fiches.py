"""Lot 5 : fiches de revue — faux client de synthèse (aucune passerelle)."""
from types import SimpleNamespace

from conftest import NCM_A, SPEC, ecrire_xlsx

from sillage.fiches import CONSIGNE, Fiches, construire_contexte, exporter_fiches, rediger
from sillage.lecture_ncm import lire_ncm
from sillage.modele import Exigence, LigneNCM, Statut
from sillage.referentiel import lire_referentiel
from sillage.rivage import Passage
from sillage.statuts import DictionnaireStatuts


class FauxSynth:
    """Imite synthesis.Synthesizer.synthesize_raw : renvoie une fiche structurée qui cite les sources reçues."""
    def __init__(self, disponible=True, tronque=False):
        self.appels = []
        self.disponible = disponible
        self.tronque = tronque

    def available(self):
        return self.disponible

    def synthesize_raw(self, question, user_content, sources, log=None, temperature=0.1, max_tokens=1024, system_prompt=None):
        self.appels.append((question, user_content, system_prompt))
        if not self.disponible:
            return SimpleNamespace(answer="", sources=sources, model="", error="API de synthèse indisponible", fallback_used=False, tronquee=False)
        cite = f" [{sources[0]['index']}]" if sources else " (NCM)"
        return SimpleNamespace(answer=f"ÉCART :\nLe fournisseur ne tient pas l'exigence{cite}.\nRISQUE :\nRisque technique (NCM).\n"
                                      f"QUESTIONS POUR LA REVUE :\n1. Quand ?{cite}",
                               sources=sources, model="faux-modele", error=None, fallback_used=False, tronquee=self.tronque)

    def close(self):
        pass


def test_contexte():
    e = Exigence(id="REQ-4", texte="Le logiciel doit être développé au niveau DAL B.", id_brut="REQ-004", norme="CS 25.1309")
    l = LigneNCM(id="REQ-4", statut=Statut.NON_CONFORME, statut_brut="NC", justification="DAL C.", champs={"derogation": "RFD-0007"})
    p = Passage("D:/spec_fournisseur.docx", 12, "Le logiciel est développé en DAL C.", 0.8, 0.0, 3)
    c = construire_contexte(e, [l], [p])
    assert "EXIGENCE REQ-004" in c and "PARAGRAPHE DE NORME TRACÉ : CS 25.1309" in c
    assert "statut « NC » (non conforme)" in c and "Justification du fournisseur : DAL C." in c and "Dérogation : RFD-0007" in c
    assert "[1] (source : spec_fournisseur.docx, page 12)" in c
    assert "aucun passage disponible" in construire_contexte(e, [l], [])
    assert "verdict" in CONSIGNE and "ÉCART" in CONSIGNE


def test_rediger_avec_campagne_et_sans(tmp_path):
    from sillage.appui import Appui, Campagne
    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC))
    ncm = lire_ncm(ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_A), ref.motif, DictionnaireStatuts(), fournisseur="A", revision="B")
    c = Campagne()
    c.appuis["REQ-4"] = Appui(id="REQ-4", niveau="faible", score=0.3,
                              passages=[Passage("D:/spec.docx", 3, "Le logiciel est en DAL C.", 0.3, 0.0, 1)])
    synth = FauxSynth()
    f = rediger(ref, ncm, synth, campagne=c)
    assert sorted(f.fiches) == ["REQ-2", "REQ-4"]            # PC et NC seulement
    assert f.fournisseur == "A" and f.revision == "B" and f.statuts == ["NC", "PC"] or set(f.statuts) == {"NC", "PC"}
    f4 = f.fiches["REQ-4"]
    assert f4.statut == "NC" and "ÉCART" in f4.texte and "[1]" in f4.texte and f4.modele == "faux-modele"
    assert f4.sources and f4.sources[0]["chemin"] == "D:/spec.docx"
    assert "(NCM)" in f.fiches["REQ-2"].texte and f.fiches["REQ-2"].sources == []
    q, contenu, consigne = synth.appels[-1]
    assert consigne == CONSIGNE and "LIGNE DE NCM" in contenu
    r = f.resume()
    assert r == {"total": 2, "redigees": 2, "degradees": 0, "tronquees": 0, "erreurs": 0}
    assert "rédigées : 2" in f.texte_resume()
    # JSON aller-retour
    j = tmp_path / "fiches.json"
    f.enregistrer(j)
    f2 = Fiches.charger(j)
    assert f2.fiches["REQ-4"].texte == f4.texte and f2.fiches["REQ-4"].sources[0]["page"] == 3
    # statuts choisis
    f3 = rediger(ref, ncm, FauxSynth(), statuts=["NC"])
    assert list(f3.fiches) == ["REQ-4"]
    # passerelle indisponible : fiches en erreur, rien d'inventé
    f4b = rediger(ref, ncm, FauxSynth(disponible=False))
    assert all(x.erreur and not x.texte for x in f4b.fiches.values())
    assert "sans réponse du modèle" in f4b.texte_resume()
    # moteur à la demande (sans campagne) : une recherche par exigence
    class FauxMoteur:
        def passages(self, q, path_prefixes=None, allowed_paths=None, rerank_k=None):
            return [Passage("D:/x.txt", None, "passage trouvé", 0.9, 0.0, 9)], True
    f5 = rediger(ref, ncm, FauxSynth(), moteur=FauxMoteur())
    assert f5.fiches["REQ-2"].sources[0]["chemin"] == "D:/x.txt"


def test_export_word_et_grille(tmp_path):
    import docx
    from sillage.controles import controler
    from sillage.grille import exporter_grille
    from openpyxl import load_workbook
    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC))
    ncm = lire_ncm(ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_A), ref.motif, DictionnaireStatuts(), fournisseur="A", revision="B")
    f = rediger(ref, ncm, FauxSynth(tronque=True))
    p = exporter_fiches(f, ref, ncm, tmp_path / "revue" / "fiches.docx")
    d = docx.Document(str(p))
    textes = [para.text for para in d.paragraphs]
    assert any("Fiches de revue — A, NCM révision B" in t for t in textes)
    assert any(t.startswith("REQ-002 — partiellement conforme") for t in textes)
    assert any(t.startswith("REQ-004 — non conforme") for t in textes)
    assert any("tronquée" in t for t in textes)
    assert any(t.startswith("ÉCART") for t in textes)
    g = controler(ref, ncm, fiches=f)
    par = {l.id: l for l in g.lignes}
    assert "ÉCART" in par["REQ-4"].fiche and par["REQ-1"].fiche == ""
    x = exporter_grille(g, tmp_path / "grille.xlsx")
    ws = load_workbook(str(x))["Grille"]
    assert ws["X1"].value == "Fiche de revue"
    lignes = {ws.cell(r, 1).value: ws.cell(r, 24).value for r in range(2, ws.max_row + 1)}
    assert "ÉCART" in lignes["REQ-4"] and not lignes["REQ-1"]


def test_cli(tmp_path, capsys, monkeypatch):
    import sillage.fiches as fiches_mod
    from sillage.cli import main
    monkeypatch.setattr(fiches_mod, "ouvrir_synthesizer", lambda log=None: FauxSynth())
    spec = ecrire_xlsx(tmp_path / "spec.xlsx", SPEC)
    ncm = ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_A)
    j = tmp_path / "fiches.json"
    assert main(["fiches", "--spec", str(spec), "--ncm", str(ncm), "--json", str(j), "--docx", str(tmp_path / "f.docx")]) == 0
    out = capsys.readouterr().out
    assert "Fiches de revue : 2" in out and j.exists() and (tmp_path / "f.docx").exists()
    assert main(["grille", "--spec", str(spec), "--ncm", str(ncm), "--fiches", str(j), "--sortie", str(tmp_path / "g.xlsx")]) == 0
    assert "rédigées : 2" in capsys.readouterr().out
