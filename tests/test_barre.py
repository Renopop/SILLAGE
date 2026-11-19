"""Le texte barré est du texte supprimé : il n'est lu nulle part."""
import pytest

from sillage.identifiants import MotifIdentifiant
from sillage.lecture_ncm import lire_ncm
from sillage.modele import Statut
from sillage.referentiel import lire_referentiel
from sillage.statuts import DictionnaireStatuts
from sillage.tableaux import lire_tableaux
from sillage.textes import paragraphes


def test_excel_cellule_et_portion_barrees(tmp_path):
    from openpyxl import Workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "NCM"
    ws.append(["Req ID", "Compliance", "Justification"])
    # portion barrée dans un texte enrichi : l'ancienne fin de phrase
    ws.cell(2, 1, "[REQ-1]")
    ws.cell(2, 2, CellRichText("Partially ", TextBlock(InlineFont(strike=True), "Non "), "Compliant"))
    ws.cell(2, 3, CellRichText("The APU fuel system is compliant with TSO-C77b ",
                               TextBlock(InlineFont(strike=True), "and potential A/C level CRMP be designed in order to not be subject to ice accumulation.")))
    # cellule entièrement barrée : une réponse supprimée
    ws.cell(3, 1, "[REQ-2]")
    ws.cell(3, 2, "NC")
    c = ws.cell(3, 3, "Ancienne justification à ignorer")
    c.font = Font(strike=True)
    # statut barré : la ligne n'a plus de statut
    ws.cell(4, 1, "[REQ-3]")
    c = ws.cell(4, 2, "Compliant")
    c.font = Font(strike=True)
    ws.cell(4, 3, "Réponse en attente")
    p = tmp_path / "ncm.xlsx"
    wb.save(str(p))
    ncm = lire_ncm(p, MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    par = ncm.par_id()
    assert par["REQ-1"][0].statut is Statut.PARTIEL and par["REQ-1"][0].statut_brut == "Partially Compliant"
    assert par["REQ-1"][0].justification == "The APU fuel system is compliant with TSO-C77b"
    assert par["REQ-2"][0].justification == ""
    assert par["REQ-3"][0].statut is Statut.INCONNU and par["REQ-3"][0].statut_brut == ""


def test_word_runs_barres(tmp_path):
    import docx
    d = docx.Document()
    para = d.add_paragraph()
    para.add_run("[REQ-10] ")
    para.add_run("Le calculateur doit peser au plus 2 kg")
    r = para.add_run(", connecteurs compris")
    r.font.strike = True
    para.add_run(".")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Req"
    t.cell(0, 1).text = "Status"
    t.cell(1, 0).text = "REQ-10"
    pc = t.cell(1, 1).paragraphs[0]
    r = pc.add_run("NC ")
    r.font.strike = True
    pc.add_run("C")
    p = tmp_path / "spec.docx"
    d.save(str(p))
    paras = paragraphes(p)
    assert paras[0].texte == "[REQ-10] Le calculateur doit peser au plus 2 kg."
    ref = lire_referentiel(p)
    texte = ref.par_id()["REQ-10"].texte          # les lignes de la table qui suit s'y ajoutent, c'est voulu ; pas le run barré
    assert texte.startswith("Le calculateur doit peser au plus 2 kg.") and "connecteurs" not in texte and "NC" not in texte
    tables = lire_tableaux(p)
    assert tables[0].lignes[1] == ["REQ-10", "C"]


def test_pdf_caracteres_barres(tmp_path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((40, 60), "[REQ-5]", fontsize=10)
    page.insert_text((40, 80), "The APU shall be designed to not be subject to ice accumulation.", fontsize=10)
    # trait au milieu des caractères de « to not be subject to ice accumulation. »
    debut = 40 + fitz.get_text_length("The APU shall be designed ", fontsize=10)
    fin = 40 + fitz.get_text_length("The APU shall be designed to not be subject to ice accumulation.", fontsize=10)
    page.draw_line((debut, 76.5), (fin, 76.5), width=0.8)
    # un soulignement sous la ligne de base ne barre rien
    page.insert_text((40, 100), "Underlined text stays.", fontsize=10)
    page.draw_line((40, 101.5), (140, 101.5), width=0.8)
    p = tmp_path / "spec.pdf"
    doc.save(str(p))
    doc.close()
    ref = lire_referentiel(p)
    assert ref.par_id()["REQ-5"].texte == "The APU shall be designed Underlined text stays."
