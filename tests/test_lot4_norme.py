"""Lot 4 : traçabilité vers la norme (moteur embarqué, faux embedder et faux reranker)."""
import pytest
from conftest import SPEC, ecrire_xlsx
from test_lot3_rivage import FakeEmbedder, FakeReranker

from sillage import rivage
from sillage.norme import (Tracabilite, appliquer, couverture, exporter_tracabilite, paragraphes_cites,
                           references_norme, tracer)
from sillage.referentiel import lire_referentiel

pytestmark = pytest.mark.skipif(not rivage.disponible(), reason="moteur embarqué inutilisable : " + rivage.erreur())


def test_references_norme():
    assert references_norme("voir CS 25.1309 et CS-25.1309(b), puis AMC 25.1309 ; FAR 25.1353 ; 14 CFR 25.1301") == \
        ["CS 25.1309", "AMC 25.1309", "FAR 25.1353", "FAR 25.1301"]
    assert references_norme("cs25.1322 et JAR 25.1322") == ["CS 25.1322", "JAR 25.1322"]
    assert references_norme("REQ-001 pèse 2,5 kg, version 1.2") == []


@pytest.fixture
def index_norme(tmp_path):
    import index_cache
    index_cache.clear_cache()
    root = tmp_path / "norme"
    root.mkdir()
    # un fichier par paragraphe (extraits traduits, vocabulaire commun avec les exigences d'essai)
    (root / "CS-25_1309.txt").write_text(
        "CS 25.1309 Équipements, systèmes et installations\n\n"
        "L'équipement doit être conçu pour que toute condition de panne soit signalée à l'équipage ; "
        "une alarme doit indiquer toute panne interne du calculateur (internal failure, warning, crew).\n",
        encoding="utf-8")
    (root / "CS-25_1353.txt").write_text(
        "CS 25.1353 Équipements et installations électriques\n\n"
        "L'équipement électrique doit être alimenté en 28 V DC et protégé contre les surcharges.\n",
        encoding="utf-8")
    (root / "CS-25_1301.txt").write_text(
        "CS 25.1301 Fonction et installation\n\n"
        "Chaque équipement installé doit fonctionner en température ambiante de -40 °C à +70 °C et résister aux vibrations.\n",
        encoding="utf-8")
    (root / "AMC-25_1309.txt").write_text(
        "AMC 25.1309 Conception et analyse des systèmes\n\nGuide sur les conditions de panne et l'alarme à l'équipage.\n",
        encoding="utf-8")
    (root / "CS-25_extraits.txt").write_text(
        "Table des matières : CS 25.1301, CS 25.1309, CS 25.1353, AMC 25.1309.\n", encoding="utf-8")
    d = tmp_path / "index_norme"
    rivage.indexer(str(root), str(d), embedder=FakeEmbedder(), use_ocr=False)
    yield d, root
    index_cache.clear_cache()


SPEC_SANS_NORME = [["ID", "Exigence"],
                   ["REQ-001", "Le calculateur doit peser au plus 2 kg."],
                   ["REQ-002", "Le calculateur doit fonctionner de -40 °C à +70 °C en température ambiante."],
                   ["REQ-003", "Le calculateur doit être alimenté en 28 V DC et protégé contre les surcharges."],
                   ["REQ-005", "Le calculateur doit signaler toute panne interne (internal failure) par un warning au crew."]]


def test_tracer_et_relire(tmp_path, index_norme):
    d, root = index_norme
    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC_SANS_NORME))
    with rivage.Moteur(str(d), embedder=FakeEmbedder(), reranker=FakeReranker()) as moteur:
        t = tracer(ref, moteur, seuil_retenue=0.3)
    assert sorted(t.traces) == ["REQ-1", "REQ-2", "REQ-3", "REQ-5"]
    assert t.traces["REQ-3"].retenue == "CS 25.1353"
    assert t.traces["REQ-5"].retenue in ("CS 25.1309", "AMC 25.1309")
    assert t.traces["REQ-2"].retenue == "CS 25.1301"
    assert t.traces["REQ-1"].retenue == "" or t.traces["REQ-1"].candidats   # rien de spécifique à la masse
    assert all(tr.origine == "proposée" and not tr.validee for tr in t.traces.values())
    r = t.resume()
    assert r["total"] == 4 and r["retenues"] >= 3 and "paragraphe retenu" in t.texte_resume()
    # JSON aller-retour
    j = tmp_path / "trac.json"
    t.enregistrer(j)
    t2 = Tracabilite.charger(j)
    assert t2.traces["REQ-3"].retenue == "CS 25.1353" and t2.traces["REQ-3"].candidats[0].passage.chemin.endswith(".txt")
    # classeur de relecture, corrigé par l'ingénieur, réimporté
    x = exporter_tracabilite(ref, t, tmp_path / "trac.xlsx")
    from openpyxl import load_workbook
    wb = load_workbook(str(x))
    ws = wb["Traçabilité"]
    lignes = {ws.cell(r, 1).value: r for r in range(2, ws.max_row + 1)}
    ws.cell(lignes["REQ-1"], 3).value = "CS 25.1301"     # l'ingénieur tranche
    for ident in ("REQ-1", "REQ-3"):
        ws.cell(lignes[ident], 4).value = "oui"
    wb.save(str(x))
    t3 = Tracabilite.charger(x)
    assert t3.traces["REQ-1"].retenue == "CS 25.1301" and t3.traces["REQ-1"].validee
    assert t3.traces["REQ-3"].validee and not t3.traces["REQ-2"].validee
    assert t3.traces["REQ-2"].origine == "relue"
    # application : validées seulement, puis toutes
    assert appliquer(ref, t3, validees_seulement=True) == 2
    assert ref.par_id()["REQ-1"].norme == "CS 25.1301" and ref.par_id()["REQ-2"].norme == ""
    assert appliquer(ref, t3) >= 3
    # exigences déjà tracées dans la spec : conservées, pas re-cherchées
    ref2 = lire_referentiel(ecrire_xlsx(tmp_path / "spec2.xlsx", SPEC))
    with rivage.Moteur(str(d), embedder=FakeEmbedder(), reranker=FakeReranker()) as moteur:
        t4 = tracer(ref2, moteur)
    assert t4.traces["REQ-1"].origine == "référentiel" and t4.traces["REQ-1"].retenue == "CS 25.1309"
    assert t4.traces["REQ-2"].origine == "proposée"


def test_couverture(tmp_path, index_norme):
    d, root = index_norme
    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC))
    cites = paragraphes_cites([str(root / "CS-25_extraits.txt")])
    assert cites["CS 25.1309"] >= 1 and "CS 25.1301" in cites
    c = couverture(ref, cites)
    lignes = {p: ids for p, _n, ids in c.lignes()}
    assert lignes["CS 25.1309"] == ["REQ-1", "REQ-4", "REQ-5"] and lignes["CS 25.1353"] == ["REQ-3"]
    assert "CS 25.1301" in c.non_couverts() and "AMC 25.1309" in c.non_couverts()
    assert "sans exigence" in c.texte_resume()


def test_grille_impact_norme_et_cli(tmp_path, index_norme, capsys):
    from conftest import NCM_A
    from openpyxl import load_workbook
    from sillage.cli import main
    from sillage.controles import IMPACT_NORME, controler
    from sillage.grille import exporter_grille
    from sillage.lecture_ncm import lire_ncm
    from sillage.statuts import DictionnaireStatuts
    d, root = index_norme
    spec = ecrire_xlsx(tmp_path / "spec.xlsx", SPEC)
    ncm_f = ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_A)
    ref = lire_referentiel(spec)
    ncm = lire_ncm(ncm_f, ref.motif, DictionnaireStatuts())
    g = controler(ref, ncm)
    par = {l.id: l for l in g.lignes}
    assert IMPACT_NORME in par["REQ-4"].alertes          # NC tracée CS 25.1309
    assert IMPACT_NORME not in par["REQ-2"].alertes      # PC mais sans norme
    assert IMPACT_NORME not in par["REQ-1"].alertes      # C
    assert [l.id for l in g.impacts_norme()] == ["REQ-4"]
    assert g.resume()["tracees"] == 4 and "impact norme : 1" in g.texte_resume()
    c = couverture(ref, paragraphes_cites([str(root / "CS-25_extraits.txt")]))
    p = exporter_grille(g, tmp_path / "grille.xlsx", couverture=c)
    wb = load_workbook(str(p))
    assert wb.sheetnames == ["Grille", "Alertes", "Impact norme", "Couverture norme", "Résumé"]
    assert wb["Impact norme"].cell(2, 1).value == "REQ-4" and wb["Impact norme"].cell(2, 2).value == "CS 25.1309"
    assert wb["Couverture norme"].max_row >= 4
    # ligne de commande : tracer (spec sans norme) → json + xlsx ; grille --tracabilite ; couverture
    spec2 = ecrire_xlsx(tmp_path / "spec2.xlsx", SPEC_SANS_NORME)
    from test_lot3_rivage import main_avec_faux
    j = tmp_path / "trac.json"
    assert main_avec_faux(["tracer", "--spec", str(spec2), "--index", str(d), "--json", str(j), "--xlsx", str(tmp_path / "trac.xlsx")]) == 0
    out = capsys.readouterr().out
    assert "Traçabilité :" in out and j.exists() and (tmp_path / "trac.xlsx").exists()
    ncm2 = ecrire_xlsx(tmp_path / "ncm2.xlsx", [["ID", "Statut", "Justification"], ["REQ-003", "NC", "24 V seulement"], ["REQ-005", "C", "ok"]])
    assert main(["grille", "--spec", str(spec2), "--ncm", str(ncm2), "--tracabilite", str(j),
                 "--norme-documents", str(root / "CS-25_extraits.txt"), "--sortie", str(tmp_path / "g.xlsx")]) == 0
    out = capsys.readouterr().out
    assert "impact norme : " in out and "Couverture de la norme" in out
    assert "Impact norme" in load_workbook(str(tmp_path / "g.xlsx")).sheetnames
    assert main(["couverture", "--spec", str(spec2), "--tracabilite", str(j), "--norme-documents", str(root / "CS-25_extraits.txt")]) == 0
    assert "CS 25.1353" in capsys.readouterr().out
