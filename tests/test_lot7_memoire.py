"""Lot 7 : mémoire des projets (SQLite local) — capitalisation entre campagnes."""
from conftest import NCM_A, SPEC, ecrire_xlsx

from sillage.controles import controler
from sillage.lecture_ncm import lire_ncm
from sillage.memoire import Memoire
from sillage.referentiel import lire_referentiel
from sillage.statuts import DictionnaireStatuts

NCM_B = [["Req ID", "Compliance", "Justification", "Action plan", "Waiver ref"],
         ["REQ-001", "C", "1,9 kg.", "", ""],
         ["REQ-002", "C", "Qualifié -40/+70.", "", ""],
         ["REQ-003", "NC", "24 V seulement.", "Convertisseur à ajouter", ""],
         ["REQ-004", "NC", "DAL C également.", "", "RFD-0101"],
         ["REQ-005", "C", "BITE.", "", ""],
         ["REQ-006", "PC", "Documentation en français, traduction partielle.", "", ""]]


def _grille(tmp_path, lignes, nom, fournisseur, revision=""):
    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC))
    ncm = lire_ncm(ecrire_xlsx(tmp_path / nom, lignes), ref.motif, DictionnaireStatuts(), fournisseur=fournisseur, revision=revision)
    return ref, ncm, controler(ref, ncm)


def test_memoire(tmp_path):
    chemin = tmp_path / "memoire.sqlite"
    _, _, gA = _grille(tmp_path, NCM_A, "ncmA.xlsx", "Fournisseur A", "B")
    _, _, gB = _grille(tmp_path, NCM_B, "ncmB.xlsx", "Fournisseur B", "1")
    with Memoire(chemin) as mem:
        assert mem.campagnes() == []
        mem.enregistrer(gA, "PROJET-1", signee=True)
        mem.enregistrer(gB, "PROJET-1")
        mem.enregistrer(gB, "PROJET-2", signee=False, fournisseur="Fournisseur B", revision="2")
        cs = mem.campagnes()
        assert [(c.projet, c.fournisseur, c.revision, c.signee, c.exigences, c.ecarts) for c in cs] == [
            ("PROJET-1", "Fournisseur A", "B", True, 6, 2), ("PROJET-1", "Fournisseur B", "1", False, 6, 3),
            ("PROJET-2", "Fournisseur B", "2", False, 6, 3)]
        # remplacement d'une campagne identique (projet, fournisseur, révision)
        mem.enregistrer(gA, "PROJET-1", signee=True)
        assert len(mem.campagnes()) == 3
        # exigences contestées : REQ-4 NC partout (3 campagnes, 2 couples projet/fournisseur… A et B)
        rows = {d["exigence"]: d for d in mem.exigences_contestees()}
        assert rows["REQ-4"]["contestations"] == 3 and rows["REQ-4"]["acceptations"] == 3 and rows["REQ-4"]["norme"] == "CS 25.1309"
        assert rows["REQ-3"]["contestations"] == 2 and rows["REQ-2"]["contestations"] == 1
        assert "REQ-1" not in rows and "REQ-9" not in rows          # conforme / orphelin
        assert [d["exigence"] for d in mem.exigences_contestees(minimum=2)] == ["REQ-4", "REQ-3", "REQ-6"]
        # écarts acceptés
        acc = mem.ecarts_acceptes(exigence="REQ-4")
        assert len(acc) == 3 and {a["derogation"] for a in acc} == {"RFD-0007", "RFD-0101"}
        assert mem.ecarts_acceptes(norme="25.1309")[0]["exigence"] == "REQ-4"
        assert mem.ecarts_acceptes(exigence="REQ-3") == []
        # par norme et par fournisseur
        normes = {d["norme"]: d for d in mem.par_norme()}
        assert normes["CS 25.1309"]["ecarts"] == 3 and normes["CS 25.1353"]["ecarts"] == 2
        four = {d["fournisseur"]: d for d in mem.par_fournisseur()}
        assert four["Fournisseur B"]["campagnes"] == 2 and four["Fournisseur B"]["projets"] == 2
        assert 0 < four["Fournisseur A"]["taux_conformite"] < 1 and four["Fournisseur B"]["ouverts"] == 2
        # recherche
        assert {d["exigence"] for d in mem.chercher("DAL")} == {"REQ-4"}
        assert mem.chercher("inexistant") == []
        # historique d'une exigence, hors campagne courante
        h = mem.historique("REQ-4", exclure=("PROJET-1", "Fournisseur A", "B"))
        assert h.campagnes == 2 and h.contestations == 2 and len(h.acceptations) == 2
        assert "contestée 2 fois" in h.texte() and "écart déjà accepté" in h.texte()
        assert mem.historique("REQ-1").campagnes == 3 and mem.historique("REQ-1").texte().startswith("vue sur 3")
        assert mem.historique("REQ-42").texte() == ""
        assert mem.historique("REQ-4", signees_seulement=True).campagnes == 1
        # annotation d'une grille courante : nouvelle NCM du fournisseur A sur PROJET-3
        _, _, gC = _grille(tmp_path, NCM_A, "ncmC.xlsx", "Fournisseur A", "A")
        n = mem.annoter(gC, projet="PROJET-3")
        par = {l.id: l for l in gC.lignes}
        # trois couples (projet, fournisseur) dans la mémoire, tous NC sur REQ-4 avec dérogation
        assert n >= 6 and par["REQ-4"].contestations == 3 and par["REQ-4"].ecart_accepte_avant
        assert par["REQ-1"].contestations == 0 and not par["REQ-1"].ecart_accepte_avant and par["REQ-1"].historique
        assert par["REQ-9"].contestations == 0 and "contestée 0 fois" in par["REQ-9"].historique   # orphelin déjà vu chez A
        r = gC.resume()
        assert r["deja_contestees"] >= 3 and r["deja_acceptees"] == 1 and "Mémoire des projets" in gC.texte_resume()
        # la campagne courante elle-même n'est pas comptée
        mem.enregistrer(gC, "PROJET-3")
        mem.annoter(gC, projet="PROJET-3")
        assert par["REQ-4"].contestations == 3
        # export
        from openpyxl import load_workbook
        p = mem.exporter(tmp_path / "capitalisation.xlsx")
        wb = load_workbook(str(p))
        assert wb.sheetnames == ["Campagnes", "Exigences contestées", "Écarts acceptés", "Par norme", "Par fournisseur"]
        assert wb["Exigences contestées"].cell(2, 1).value == "REQ-4"
        assert "4 campagne(s)" in mem.texte_resume()
        assert mem.supprimer("PROJET-2", "Fournisseur B", "2") == 1 and len(mem.campagnes()) == 3
    # la grille exportée porte l'historique
    from sillage.grille import exporter_grille
    x = exporter_grille(gC, tmp_path / "grille.xlsx")
    ws = load_workbook(str(x))["Grille"]
    assert ws["Y1"].value == "Historique (mémoire)"
    lignes = {ws.cell(r, 1).value: ws.cell(r, 25).value for r in range(2, ws.max_row + 1)}
    assert "écart déjà accepté" in lignes["REQ-4"]


def test_cli(tmp_path, capsys):
    from sillage.cli import main
    spec = ecrire_xlsx(tmp_path / "spec.xlsx", SPEC)
    ncmA = ecrire_xlsx(tmp_path / "ncmA.xlsx", NCM_A)
    ncmB = ecrire_xlsx(tmp_path / "ncmB.xlsx", NCM_B)
    mem = str(tmp_path / "m.sqlite")
    assert main(["grille", "--spec", str(spec), "--ncm", str(ncmA), "--fournisseur", "A", "--projet", "P1", "--capitaliser",
                 "--signee", "--memoire-fichier", mem, "--sortie", str(tmp_path / "g1.xlsx")]) == 0
    out = capsys.readouterr().out
    assert "Campagne enregistrée" in out and "(NCM signée)" in out
    assert main(["grille", "--spec", str(spec), "--ncm", str(ncmB), "--fournisseur", "B", "--projet", "P2", "--capitaliser",
                 "--memoire", "--memoire-fichier", mem, "--sortie", str(tmp_path / "g2.xlsx")]) == 0
    out = capsys.readouterr().out
    assert "Historique reporté" in out and "Mémoire des projets" in out
    assert main(["grille", "--spec", str(spec), "--ncm", str(ncmA), "--projet", "P3", "--capitaliser", "--memoire-fichier", mem,
                 "--sortie", str(tmp_path / "g3.xlsx")]) == 0
    capsys.readouterr()
    assert main(["grille", "--spec", str(spec), "--ncm", str(ncmA), "--capitaliser", "--memoire-fichier", mem]) == 2
    capsys.readouterr()
    for action, attendu in (("campagnes", "3 campagne(s)"), ("contestees", "REQ-4"), ("acceptes", "RFD-0007"),
                            ("norme", "CS 25.1309"), ("fournisseurs", "conformité")):
        assert main(["memoire", action, "--fichier", mem]) == 0
        assert attendu in capsys.readouterr().out, action
    assert main(["memoire", "chercher", "DAL", "--fichier", mem]) == 0
    assert "REQ-4" in capsys.readouterr().out
    assert main(["memoire", "chercher", "--fichier", mem]) == 2
    assert main(["memoire", "exporter", "--fichier", mem, "--sortie", str(tmp_path / "cap.xlsx")]) == 0
    assert (tmp_path / "cap.xlsx").exists()
    assert main(["memoire", "supprimer", "--fichier", mem, "--projet", "P3", "--fournisseur", "(sans fournisseur)"]) == 0
    assert "1 campagne(s) supprimée(s)" in capsys.readouterr().out
