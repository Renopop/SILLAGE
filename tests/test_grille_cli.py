from openpyxl import load_workbook

from conftest import NCM_A, SPEC, ecrire_xlsx

from sillage.cli import main
from sillage.controles import comparer, controler
from sillage.grille import exporter_diff, exporter_grille
from sillage.lecture_ncm import lire_ncm
from sillage.referentiel import lire_referentiel
from sillage.statuts import DictionnaireStatuts


def test_export_grille(tmp_path):
    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC))
    ncm = lire_ncm(ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_A), ref.motif, DictionnaireStatuts(), fournisseur="A")
    g = controler(ref, ncm)
    p = exporter_grille(g, tmp_path / "out" / "grille.xlsx")
    wb = load_workbook(str(p))
    assert wb.sheetnames == ["Grille", "Alertes", "Impact norme", "Résumé"]   # SPEC trace REQ-1/3/4/5 vers la norme
    ws = wb["Grille"]
    assert ws["A1"].value == "Identifiant" and ws["C1"].value == "Statut"
    lignes = {ws.cell(r, 1).value: [ws.cell(r, c).value for c in range(1, 18)] for r in range(2, ws.max_row + 1)}
    assert lignes["REQ-2"][2] == "PC" and lignes["REQ-2"][6] == "action"
    assert lignes["REQ-4"][10] == "RFD-0007"
    assert lignes["REQ-6"][13] == "trou"
    assert lignes["REQ-9"][13] == "orphelin"
    assert ws.freeze_panes == "A2"
    wa = wb["Alertes"]
    assert wa.max_row == 4  # en-tête + trou + orphelin + impact norme (REQ-4 NC tracée)
    assert wb["Impact norme"].cell(2, 1).value == "REQ-4"
    d = exporter_diff(comparer(ncm, ncm), tmp_path / "diff.xlsx")
    assert load_workbook(str(d))["Diff"].max_row == 1


def test_cli(tmp_path, capsys):
    spec = ecrire_xlsx(tmp_path / "spec.xlsx", SPEC)
    ncm = ecrire_xlsx(tmp_path / "ncm_revB.xlsx", NCM_A)
    assert main(["lire", str(ncm), "--spec", str(spec), "--json", str(tmp_path / "n.json"), "--aperçu", "2"]) == 0
    out = capsys.readouterr().out
    assert "révision B" in out and "retrouvés : 5/6" in out and (tmp_path / "n.json").exists()

    sortie = tmp_path / "grille.xlsx"
    assert main(["grille", "--spec", str(spec), "--ncm", str(ncm), "--fournisseur", "A", "--sortie", str(sortie)]) == 0
    out = capsys.readouterr().out
    assert "6 exigences" in out and "Trous : 1" in out and sortie.exists()

    assert main(["diff", "--spec", str(spec), "--avant", str(ncm), "--apres", str(ncm), "--sortie", str(tmp_path / "d.xlsx")]) == 0
    assert "statuts changés : 0" in capsys.readouterr().out

    assert main(["statuts", "--ajouter", "Vert=C"]) == 0
    assert main(["statuts"]) == 0
    assert "vert → C" in capsys.readouterr().out
    assert main(["statuts", "--ajouter", "Bleu=XX"]) == 2

    assert main(["profils", "--proposer", str(ncm), "--fournisseur", "A", "--spec", str(spec), "--enregistrer"]) == 0
    assert main(["profils"]) == 0
    out = capsys.readouterr().out
    assert "Profils enregistrés : 1" in out and "Compliance" in out

    assert main(["version"]) == 0
    assert main([]) == 0
    assert main(["lire", str(tmp_path / "absent.xlsx")]) == 1
