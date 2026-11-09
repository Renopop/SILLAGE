"""Cas « terrain » : une spécification PDF de texte (identifiants entre crochets,
seuls sur leur ligne, préfixe composé « REQ_B2-3 », titres de section, en-tête
et pied courants, exigence à cheval sur deux pages) et une NCM PDF en tableau
pré-filtré (RR ID, DA ID, texte de l'exigence répété, Compliance, Comments,
lignes de titre intercalées). Les fichiers sont fabriqués ici : aucune donnée
fournisseur dans le dépôt."""
import pytest

from sillage.controles import CONFORME_IMPLICITE, TROU, controler
from sillage.identifiants import MotifIdentifiant, deduire_motif
from sillage.lecture_ncm import lire_ncm
from sillage.modele import Statut
from sillage.referentiel import lire_referentiel
from sillage.statuts import DictionnaireStatuts

fitz = pytest.importorskip("fitz")   # PyMuPDF, seulement pour fabriquer les PDF d'essai

EN_TETE = "Appendix B2-3 - Control System High Level Requirements - DGT 165737 A"

PAGES = [
    [EN_TETE,
     "5.1.2 Provide thrust and controlled primary power",
     "**",
     "This chapter describes the requirements for the sub-functions of the function",
     "Provide thrust and energy :",
     "- Control thrust (cf. §5.1.2.1)",
     "[REQ_B2-3_15]",
     "The engine control system must maintain normal engine operation free from",
     "instability and/or loss of power, for any combination of accessory power",
     "extraction and airframe bleed air within the engine acceptable limits.",
     "[REQ_B2-3_16]",
     "In case of abnormal condition leading to engine surge, the system shall provide",
     "automatic recovery without pilot action.",
     "5.1.2.1 Control thrust",
     "5.1.2.1.1 Ratings",
     "**",
     "The ratings are defined within B2-1 Engine Technical Requirements.",
     "[REQ_B2-3_18]",
     "No ITT limiter shall be implemented in the FADEC logic (except during starting",
     "Page 12 / 80"],
    [EN_TETE,
     "if needed).",
     "[REQ_B2-3_128]",
     "Control system accuracy, including transient behavior, software digitalization,",
     "and control parameter sensors accuracy, shall be taken into account.",
     "[REQ_B2-3_129] A minimum of four idle ratings shall be provisioned: ground idle,",
     "flight idle based on WOW, approach idle and anti-ice idle.",
     "5.1.2.2 Protect from thrust control malfunction",
     "See DO-178C and CS 25.1309 for the applicable standards.",
     "Page 13 / 80"],
    [EN_TETE,
     "[REQ_B2-3_34]",
     "Last data set shall be delivered no later than 6 months before first flight.",
     "Page 14 / 80"],
]


def _pdf_texte(chemin, pages):
    doc = fitz.open()
    for lignes in pages:
        page = doc.new_page(width=595, height=842)
        y = 50
        for l in lignes:
            page.insert_text((40, y), l, fontsize=9)
            y += 14
    doc.save(str(chemin))
    doc.close()
    return chemin


NCM_LIGNES = [
    ["RR ID", "DA ID", "DGT 165737 A", "Rolls-Royce Compliance", "Comments"],
    ["127", "", "5.1.2.1.4.1 Auto-throttle and Auto thrust architecture", "", ""],
    ["135", "[REQ_B2-3_34]", "Last data set shall be delivered no later than 6 months before first flight.",
     "Compliant", "Should be transferred to SOW."],
    ["150", "", "5.1.2.2.1 Inadvertent Shut Down", "", ""],
    ["151", "[REQ_B2-3_16]", "In case of abnormal condition leading to engine surge, the system shall provide automatic recovery without pilot action.",
     "Partially compliant", "Recovery logic to be agreed during PDP."],
    ["194", "[REQ_B2-3_128]", "Control system accuracy, including transient behavior, software digitalization, and control parameter sensors accuracy, shall be taken into account.",
     "N/A", "Requirement not applicable to RR."],
    ["265", "[REQ_B2-3_129]", "A minimum of four idle ratings shall be provisioned: ground idle, flight idle based on WOW, approach idle and anti-ice idle.",
     "", "Under discussion."],
]


def _pdf_ncm(chemin, lignes, par_page=4):
    doc = fitz.open()
    en_tete, donnees = lignes[0], lignes[1:]
    largeurs = [40, 85, 220, 90, 120]
    for debut in range(0, len(donnees), par_page):
        page = doc.new_page(width=595, height=842)
        page.insert_text((30, 30), "Rolls-Royce Non-Compliance Matrix - Rev4", fontsize=8)
        page.insert_text((30, 42), "This document comes pre-filtered. Unless listed below, all other items can be assumed compliant.",
                         fontsize=7)
        y = 60
        for row in [en_tete] + donnees[debut:debut + par_page]:
            x = 30
            for cell, w in zip(row, largeurs):
                page.draw_rect(fitz.Rect(x, y, x + w, y + 60), color=(0, 0, 0), width=0.8)
                page.insert_textbox(fitz.Rect(x + 2, y + 2, x + w - 2, y + 60), str(cell), fontsize=6)
                x += w
            y += 60
    doc.save(str(chemin))
    doc.close()
    return chemin


def test_identifiants_a_prefixe_compose():
    g = MotifIdentifiant()
    assert g.extraire("[REQ_B2-3_15]") == ["REQ_B2-3-15"]          # générique : séparateur normalisé
    m = deduire_motif(["[REQ_B2-3_15]", "[REQ_B2-3_128]"])
    assert m.prefixes == {"REQ_B2-3"}
    assert m.extraire("[REQ_B2-3_15]") == ["REQ_B2-3_15"]          # écriture du référentiel
    assert m.extraire("REQ-B2-3-015 ; req_b2_3_16") == ["REQ_B2-3_15", "REQ_B2-3_16"]
    assert m.extraire("REQ_B2-3_15 à REQ_B2-3_17") == ["REQ_B2-3_15", "REQ_B2-3_16", "REQ_B2-3_17"]
    assert m.extraire("see DO-178C, CS 25.1309 and B2-1 ratings") == []
    assert m.commence_par_id("[REQ_B2-3_15]") == "REQ_B2-3_15"
    assert m.commence_par_id("(REQ_B2-3_15) texte") == "REQ_B2-3_15"
    assert m.canoniser("REQ_B2-3-15") == "REQ_B2-3_15"
    # un préfixe simple garde ses plages abrégées avec tiret
    s = MotifIdentifiant({"REQ"})
    assert s.extraire("REQ-100-103") == ["REQ-100", "REQ-101", "REQ-102", "REQ-103"]


def test_spec_pdf_texte(tmp_path):
    ref = lire_referentiel(_pdf_texte(tmp_path / "spec.pdf", PAGES))
    assert ref.ids == ["REQ_B2-3_15", "REQ_B2-3_16", "REQ_B2-3_18", "REQ_B2-3_34", "REQ_B2-3_128", "REQ_B2-3_129"]
    e = ref.par_id()
    assert e["REQ_B2-3_15"].texte.startswith("The engine control system must maintain")
    assert e["REQ_B2-3_15"].texte.endswith("acceptable limits.")
    assert "\n" not in e["REQ_B2-3_15"].texte                      # lignes recollées par un espace
    # l'exigence 18 est à cheval sur deux pages : ni l'en-tête courant ni le numéro de page ne s'y glissent
    assert e["REQ_B2-3_18"].texte == "No ITT limiter shall be implemented in the FADEC logic (except during starting if needed)."
    # le titre de section ferme l'exigence : le texte d'introduction du chapitre n'est pas une exigence
    assert "Protect from thrust" not in e["REQ_B2-3_129"].texte
    assert "DO-178C" not in e["REQ_B2-3_129"].texte
    # identifiant suivi du texte sur la même ligne
    assert e["REQ_B2-3_129"].texte.startswith("A minimum of four idle ratings")
    assert e["REQ_B2-3_18"].reference == "page 1"
    assert ref.motif.prefixes == {"REQ_B2-3"}


def test_ncm_pdf_prefiltree(tmp_path):
    ref = lire_referentiel(_pdf_texte(tmp_path / "spec.pdf", PAGES))
    ncm = lire_ncm(_pdf_ncm(tmp_path / "ncm.pdf", NCM_LIGNES), ref.motif, DictionnaireStatuts(),
                   ids_referentiel=ref.ids, textes_referentiel={e.id: e.texte for e in ref.exigences})
    assert ncm.ids == ["REQ_B2-3_34", "REQ_B2-3_16", "REQ_B2-3_128", "REQ_B2-3_129"]
    par = ncm.par_id()
    assert par["REQ_B2-3_34"][0].statut is Statut.CONFORME
    assert par["REQ_B2-3_16"][0].statut is Statut.PARTIEL
    assert par["REQ_B2-3_128"][0].statut is Statut.NON_APPLICABLE
    assert par["REQ_B2-3_129"][0].statut is Statut.INCONNU          # cellule de statut vide
    # la colonne qui répète l'exigence n'est pas la justification : ce sont les commentaires
    assert ncm.diagnostic.colonnes["exigence"] == "DGT 165737 A"
    assert par["REQ_B2-3_34"][0].justification == "Should be transferred to SOW."
    assert ncm.diagnostic.colonnes["statut"] == "Rolls-Royce Compliance"
    assert ncm.diagnostic.prefiltree and ncm.prefiltree
    assert ncm.diagnostic.taux_couverture == pytest.approx(4 / 6)
    assert not ncm.diagnostic.statuts_inconnus                       # une cellule vide n'est pas une valeur inconnue

    g = controler(ref, ncm)
    par_g = {l.id: l for l in g.lignes}
    assert par_g["REQ_B2-3_15"].statut is Statut.CONFORME and CONFORME_IMPLICITE in par_g["REQ_B2-3_15"].alertes
    assert par_g["REQ_B2-3_18"].statut is Statut.CONFORME
    assert g.resume()[TROU] == 0 and g.resume()[CONFORME_IMPLICITE] == 2
    g2 = controler(ref, ncm, prefiltree=False)
    assert g2.resume()[TROU] == 2 and g2.resume()[CONFORME_IMPLICITE] == 0


def test_pdf_scanne_signale(tmp_path):
    doc = fitz.open()
    doc.new_page()
    doc.save(str(tmp_path / "scan.pdf"))
    doc.close()
    with pytest.raises(ValueError, match="sans couche texte"):
        lire_referentiel(tmp_path / "scan.pdf")
    ncm = lire_ncm(tmp_path / "scan.pdf", MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    assert any("scanné" in a for a in ncm.diagnostic.avertissements)


def _pdf_sans_traits(chemin, lignes, hauteur_ligne=11):
    """Tableau sans quadrillage : du texte aligné en colonnes, les cellules
    hautes écrites sur plusieurs lignes (le statut centré verticalement)."""
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    xs = [30, 70, 170, 520, 640]
    y = 40
    for row in lignes:
        cellules = [str(c).split("\n") for c in row]
        h = max(len(c) for c in cellules)
        for col, (x, lignes_cell) in enumerate(zip(xs, cellules)):
            decal = (h - len(lignes_cell)) // 2 if col == 3 else 0     # statut centré dans la hauteur
            for i, l in enumerate(lignes_cell):
                if l:
                    page.insert_text((x, y + (decal + i) * hauteur_ligne), l, fontsize=7)
        y += (h + 1) * hauteur_ligne
    doc.save(str(chemin))
    doc.close()
    return chemin


NCM_SANS_TRAITS = [
    ["RR ID", "DA ID", "DGT 165737 A", "Compliance", "Comments"],
    ["135", "[REQ_B2-3_34]", "Last data set shall be delivered\nno later than 6 months before\nfirst flight.", "Compliant", "Should be transferred\nto SOW."],
    ["150", "", "5.1.2.2.1 Inadvertent Shut Down", "", ""],
    ["196", "[REQ_B2-3_16]", "In case of abnormal condition\nleading to engine surge, the\nsystem shall provide automatic\nrecovery without pilot action.", "Partially\ncompliant", "Based on discussions 22nd June\nRolls-Royce will study solutions\nwith DA for bleed temperature\nmeasurement."],
    ["237", "[REQ_B2-3_128]", "Control system accuracy shall\nbe taken into account.", "N/A", "Requirement not applicable\nto RR."],
]


def test_ncm_pdf_sans_traits_lignes_recollees(tmp_path):
    """Sans quadrillage, pdfplumber découpe chaque ligne de texte : le statut
    « Partially compliant », centré dans une ligne haute, n'est pas sur la
    ligne de l'identifiant. Les lignes de suite sont recollées."""
    ref = lire_referentiel(_pdf_texte(tmp_path / "spec.pdf", PAGES))
    ncm = lire_ncm(_pdf_sans_traits(tmp_path / "ncm_sans_traits.pdf", NCM_SANS_TRAITS), ref.motif, DictionnaireStatuts(),
                   ids_referentiel=ref.ids, textes_referentiel={e.id: e.texte for e in ref.exigences})
    assert ncm.ids == ["REQ_B2-3_34", "REQ_B2-3_16", "REQ_B2-3_128"]
    par = ncm.par_id()
    assert par["REQ_B2-3_16"][0].statut is Statut.PARTIEL
    assert par["REQ_B2-3_16"][0].statut_brut == "Partially compliant"
    assert par["REQ_B2-3_16"][0].justification.startswith("Based on discussions 22nd June")
    assert par["REQ_B2-3_16"][0].justification.endswith("measurement.")
    assert par["REQ_B2-3_34"][0].statut is Statut.CONFORME
    assert par["REQ_B2-3_128"][0].statut is Statut.NON_APPLICABLE
    assert ncm.diagnostic.lignes_total == 4           # trois exigences et la ligne de titre, pas les lignes de suite
    assert not ncm.diagnostic.statuts_inconnus


def test_lignes_de_suite_dans_un_tableur(tmp_path):
    """Même recollage pour un tableur où une réponse s'étale sur plusieurs lignes."""
    from conftest import ecrire_xlsx
    ncm = lire_ncm(ecrire_xlsx(tmp_path / "ncm.xlsx", [
        ["N°", "Req ID", "Compliance", "Justification"],
        ["1", "REQ-1", "Partially", "Qualified to +55 °C"],
        ["", "", "compliant", "only ; test at +70 °C planned."],
        ["2", "", "5.2 Environment", ""],
        ["3", "REQ-2", "C", "OK"],
    ]), MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    par = ncm.par_id()
    assert par["REQ-1"][0].statut is Statut.PARTIEL
    assert par["REQ-1"][0].justification == "Qualified to +55 °C only ; test at +70 °C planned."
    assert par["REQ-2"][0].statut is Statut.CONFORME


def _pdf_quadrillage_faux(chemin, lignes):
    """PDF scanné puis OCRisé : des traits de colonnes et un cadre, mais aucune
    ligne horizontale entre les rangées — pdfplumber y voit une seule rangée
    de cellules géantes. Les statuts sont centrés dans leur colonne."""
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    xs = [30, 70, 170, 520, 640, 820]
    y0, y, h = 30, 40, 11
    page.insert_text((300, 22), "This document comes pre-filtered. Unless listed below, all other items can be assumed compliant.", fontsize=6)
    for row in lignes:
        cellules = [str(c).split("\n") for c in row]
        haut = max(len(c) for c in cellules)
        for col, (x, lc) in enumerate(zip(xs, cellules)):
            for i, l in enumerate(lc):
                if not l:
                    continue
                if col == 3:   # statut centré dans la largeur et la hauteur de la cellule
                    largeur = fitz.get_text_length(l, fontsize=7)
                    xx = x + (xs[4] - x - largeur) / 2
                    yy = y + ((haut - len(lc)) // 2 + i) * h
                else:
                    xx, yy = x, y + i * h
                page.insert_text((xx, yy), l, fontsize=7)
        y += (haut + 1) * h
    for x in xs:
        page.draw_line((x - 4, y0), (x - 4, y), width=0.8)
    page.draw_rect(fitz.Rect(xs[0] - 4, y0, xs[-1] - 4, y), width=0.8)
    doc.save(str(chemin))
    doc.close()
    return chemin


def test_ncm_pdf_quadrillage_faux_statuts_centres(tmp_path):
    ref = lire_referentiel(_pdf_texte(tmp_path / "spec.pdf", PAGES))
    p = _pdf_quadrillage_faux(tmp_path / "ncm_scan.pdf", NCM_SANS_TRAITS)
    import pdfplumber
    with pdfplumber.open(str(p)) as pdf:
        assert pdf.pages[0].extract_tables()                       # un faux tableau est bien détecté
    ncm = lire_ncm(p, ref.motif, DictionnaireStatuts(), ids_referentiel=ref.ids,
                   textes_referentiel={e.id: e.texte for e in ref.exigences})
    assert ncm.ids == ["REQ_B2-3_34", "REQ_B2-3_16", "REQ_B2-3_128"]
    par = ncm.par_id()
    assert par["REQ_B2-3_34"][0].statut is Statut.CONFORME
    assert par["REQ_B2-3_16"][0].statut is Statut.PARTIEL
    assert par["REQ_B2-3_128"][0].statut is Statut.NON_APPLICABLE
    assert par["REQ_B2-3_16"][0].justification.endswith("measurement.")
    assert ncm.diagnostic.prefiltree
    assert any("ligne par ligne" in a for a in ncm.diagnostic.avertissements)


def test_ncm_excel_cellules_hautes_fusionnees(tmp_path):
    """NCM Excel réelle : l'identifiant ouvre une cellule haute qui contient aussi
    le texte de l'exigence et ses métadonnées ; chaque exigence est fusionnée
    sur plusieurs lignes ; la réponse du fournisseur n'est pas fusionnée et
    continue sur les lignes suivantes ; des feuilles annexes existent."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "NCM B2 VC"
    ws.append(["", "Applicable documents are of 2 natures:\n- specific documents of the system", "", ""])
    ws.append(["", "Requirement", "Compliance", "Supplier rationale"])
    exigences = [
        ("HLTR_APU_10", "The APU fuel system shall meet the fuel icing requirements of TSO-C77b and potential A/C level CRMP.",
         "Partially\nCompliant", ["The APU fuel system is compliant with the fuel icing requirements of TSO-C77b.", "Ice accumulation at A/C level to be assessed."]),
        ("HLTR_APU_11", "No fuel shall be drained outside of the APU under normal operation circumstances.", "Compliant", ["Ecology tank incorporated."]),
        ("HLTR_APU_12", "The APU shall operate satisfactorily when using fuels in accordance with the following specifications.", "Compliant", [""]),
    ]
    rang = 3
    for ident, texte, statut, reponses in exigences:
        cellule = f"[{ident}]\n\n{texte}\nPriority: MANDATORY\nCategory: Derived\nOrigin: Functional\nType: Product\nRev: 21"
        haut = 4
        ws.cell(rang, 2, cellule)
        ws.cell(rang, 3, statut)
        for i, rep in enumerate(reponses):
            ws.cell(rang + i, 4, rep)
        ws.merge_cells(start_row=rang, start_column=2, end_row=rang + haut - 1, end_column=2)
        ws.merge_cells(start_row=rang, start_column=3, end_row=rang + haut - 1, end_column=3)
        rang += haut
    w2 = wb.create_sheet("Deleted lines")
    w2.append(["", "Requirement", "Compliance", "Supplier rationale"])
    w2.append(["", "[HLTR_APU_7]\n\nDeleted requirement.", "Compliant", "n/a"])
    wb.create_sheet("VC vs VB").append(["[HLTR_APU_10]", "changed"])
    wb.create_sheet("Feuil1")
    p = tmp_path / "NCM_B2_VC.xlsx"
    wb.save(str(p))

    from sillage.identifiants import deduire_motif
    motif = deduire_motif(["[HLTR_APU_2]"])
    ncm = lire_ncm(p, motif, DictionnaireStatuts(), ids_referentiel=[f"HLTR_APU_{n}" for n in range(2, 60)],
                   textes_referentiel={"HLTR_APU_10": exigences[0][1], "HLTR_APU_11": exigences[1][1], "HLTR_APU_12": exigences[2][1]})
    assert ncm.ids == ["HLTR_APU_10", "HLTR_APU_11", "HLTR_APU_12"]
    par = ncm.par_id()
    assert par["HLTR_APU_10"][0].statut is Statut.PARTIEL
    assert par["HLTR_APU_11"][0].statut is Statut.CONFORME
    assert len(par["HLTR_APU_10"]) == 1                      # les lignes fusionnées ne font pas de doublon
    assert par["HLTR_APU_10"][0].justification == ("The APU fuel system is compliant with the fuel icing requirements of TSO-C77b. "
                                                   "Ice accumulation at A/C level to be assessed.")
    assert ncm.diagnostic.colonnes["id"] == "Requirement" and ncm.diagnostic.colonnes["statut"] == "Compliance"
    assert ncm.diagnostic.lignes_total == 3
    assert any("Deleted lines" in a for a in ncm.diagnostic.avertissements)
    assert not ncm.diagnostic.statuts_inconnus
