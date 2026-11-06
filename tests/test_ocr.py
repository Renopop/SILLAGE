"""OCR de secours : PDF scannés (pages sans couche texte) lus par le modèle
Vision du moteur embarqué. Ici le modèle est remplacé par un faux qui rend
le texte des pages ; aucun appel réseau."""
import pytest

from sillage import ocr
from sillage.identifiants import MotifIdentifiant
from sillage.lecture_ncm import lire_ncm
from sillage.modele import Statut
from sillage.referentiel import lire_referentiel
from sillage.statuts import DictionnaireStatuts
from sillage.tableaux import lire_tableaux

fitz = pytest.importorskip("fitz")

SPEC_OCR = {
    1: "Appendix B2-3 - Control System\n5.1.2 Provide thrust\n[REQ_B2-3_15]\nThe engine control system must maintain normal\nengine operation.\n[REQ_B2-3_16]\nIn case of engine surge, the system shall provide\nautomatic recovery.\n",
    2: "Appendix B2-3 - Control System\n[REQ_B2-3_18]\nNo ITT limiter shall be implemented.\n",
}
NCM_OCR = {
    1: ("Rolls-Royce Non-Compliance Matrix\n"
        "This document comes pre-filtered. Unless listed below, all other items can be assumed compliant.\n"
        "| RR ID | DA ID | DGT 165737 A | Compliance | Comments |\n"
        "|---|---|---|---|---|\n"
        "| 127 | | 5.1.2.1 Auto-throttle | | |\n"
        "| 151 | [REQ_B2-3_16] | In case of engine surge, the system shall provide automatic recovery. | Partially compliant | Recovery logic to be agreed. |\n"
        "| 158 | [REQ_B2-3_18] | No ITT limiter shall be implemented. | Compliant | Agreed. |\n"),
}


def _pdf_blanc(chemin, pages=1):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(chemin))
    doc.close()
    return chemin


@pytest.fixture
def faux_ocr(monkeypatch):
    appels = []

    def pages_ocr(pdf, pages=None, progression=None, log=None):
        appels.append(list(pages or []))
        nom = str(pdf)
        textes = SPEC_OCR if "spec" in nom else ({2: NCM_OCR[1]} if "mixte" in nom else NCM_OCR)
        return {n: textes.get(n, "") for n in (pages or textes)}

    monkeypatch.setattr(ocr, "pages_ocr", pages_ocr)
    ocr.oublier()
    return appels


def test_sans_ocr_le_scan_est_signale(tmp_path):
    with pytest.raises(ValueError, match="OCR de secours"):
        lire_referentiel(_pdf_blanc(tmp_path / "spec.pdf"))
    ncm = lire_ncm(_pdf_blanc(tmp_path / "ncm.pdf"), MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    assert any("OCR de secours" in a for a in ncm.diagnostic.avertissements)


def test_ocr_indisponible_est_explique(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "disponible", lambda: (False, "clé du modèle Vision absente"))
    ocr.oublier()
    with pytest.raises(ValueError, match="clé du modèle Vision absente"):
        lire_referentiel(_pdf_blanc(tmp_path / "spec.pdf"), ocr=True)
    ncm = lire_ncm(_pdf_blanc(tmp_path / "ncm.pdf"), MotifIdentifiant({"REQ"}), DictionnaireStatuts(), ocr=True)
    assert any("clé du modèle Vision absente" in a for a in ncm.diagnostic.avertissements)


def test_spec_scannee_par_ocr(tmp_path, faux_ocr):
    ref = lire_referentiel(_pdf_blanc(tmp_path / "spec.pdf", pages=2), ocr=True)
    assert ref.ids == ["REQ_B2-3_15", "REQ_B2-3_16", "REQ_B2-3_18"]
    assert ref.par_id()["REQ_B2-3_15"].texte == "The engine control system must maintain normal engine operation."
    assert ref.par_id()["REQ_B2-3_18"].reference == "page 2"
    assert faux_ocr == [[1, 2]]                      # seules les pages sans texte partent en OCR


def test_ncm_scannee_par_ocr(tmp_path, faux_ocr):
    ref = lire_referentiel(_pdf_blanc(tmp_path / "spec.pdf", pages=2), ocr=True)
    ncm = lire_ncm(_pdf_blanc(tmp_path / "ncm.pdf"), ref.motif, DictionnaireStatuts(), ids_referentiel=ref.ids,
                   textes_referentiel={e.id: e.texte for e in ref.exigences}, ocr=True)
    assert ncm.ids == ["REQ_B2-3_16", "REQ_B2-3_18"]
    par = ncm.par_id()
    assert par["REQ_B2-3_16"][0].statut is Statut.PARTIEL
    assert par["REQ_B2-3_18"][0].statut is Statut.CONFORME
    assert par["REQ_B2-3_18"][0].justification == "Agreed."
    assert ncm.diagnostic.tableaux_ocr == 1 and ncm.diagnostic.prefiltree
    assert par["REQ_B2-3_16"][0].origine.emplacement == "page 1, tableau 1"
    assert "OCR" in ncm.diagnostic.texte()


def test_tableaux_depuis_texte():
    t = ocr.tableaux_depuis_texte("titre\n| a | b | c |\n|---|---|---|\n| 1 | 2 |\n\n| x | y |\n| 3 | 4 |\n| seul |\n",
                                  "f.pdf", 3)
    assert len(t) == 2
    assert t[0].lignes == [["a", "b", "c"], ["1", "2", ""]] and t[0].ocr
    assert t[0].numeros == [2, 4]
    assert t[1].emplacement == "page 3, tableau 2" and t[1].lignes[-1] == ["seul", ""]   # ligne courte complétée
    assert ocr.tableaux_depuis_texte("pas de tableau | ici\n| une | seule | ligne |", "f.pdf", 1) == []


def test_lire_tableaux_pdf_mixte(tmp_path, faux_ocr):
    """Page 1 avec un vrai tableau, page 2 scannée : seule la seconde part en OCR."""
    doc = fitz.open()
    page = doc.new_page()
    y = 50
    for row in (["ID", "Status"], ["[REQ_B2-3_15]", "Compliant"]):
        x = 40
        for cell, w in zip(row, (120, 80)):
            page.draw_rect(fitz.Rect(x, y, x + w, y + 30), color=(0, 0, 0), width=0.8)
            page.insert_textbox(fitz.Rect(x + 2, y + 2, x + w - 2, y + 30), cell, fontsize=8)
            x += w
        y += 30
    doc.new_page()
    doc.save(str(tmp_path / "ncm_mixte.pdf"))
    doc.close()
    tableaux = lire_tableaux(tmp_path / "ncm_mixte.pdf", ocr=True)
    assert faux_ocr == [[2]]
    assert [t.ocr for t in tableaux] == [False, True]


SPEC_MAUVAISE = ["Appendix B2-3", "5.1.2 Provide thrust", "[REQ_B2-3_15]", "The engine control system must maintain normal engine operation."]
NCM_MAUVAISE = ["RR ID DA ID DGT Compliance Comments", "135 [REQ_B2-3_34] Last data set Compliant Should be", "151 [REQ_B2-3_16] In case Partially compliant Recovery"]


def _pdf_texte_degrade(chemin, lignes):
    """Couche texte inexploitable (OCR du scanner) : mots tronqués sans aucun identifiant reconnaissable."""
    doc = fitz.open()
    page = doc.new_page()
    y = 50
    for l in lignes:
        page.insert_text((40, y), l.replace("REQ_B2-3", "R£Q 82 3").replace("[", "{"), fontsize=9)
        y += 14
    doc.save(str(chemin))
    doc.close()
    return chemin


def test_ocr_force_sur_toutes_les_pages(tmp_path, faux_ocr):
    """« forcer » : la couche texte est ignorée, toutes les pages partent en OCR."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((40, 50), "[REQ_B2-3_99] texte de la couche", fontsize=9)
    doc.new_page()
    doc.save(str(tmp_path / "spec_force.pdf"))
    doc.close()
    ref = lire_referentiel(tmp_path / "spec_force.pdf", ocr="forcer")
    assert faux_ocr == [[1, 2]]
    assert ref.ids == ["REQ_B2-3_15", "REQ_B2-3_16", "REQ_B2-3_18"]      # le texte du faux OCR, pas la couche


def test_couche_texte_inexploitable_relue_par_ocr(tmp_path, faux_ocr):
    """OCR de secours (vrai) sur un PDF dont la couche texte existe mais ne
    donne rien : relu par le modèle Vision, tableaux reconstruits."""
    spec = _pdf_texte_degrade(tmp_path / "spec.pdf", SPEC_MAUVAISE)
    ref = lire_referentiel(spec, ocr=True)
    assert ref.ids == ["REQ_B2-3_15", "REQ_B2-3_16"]          # une seule page : le faux OCR de la page 1
    assert faux_ocr[-1] == [1]
    ncm_p = _pdf_texte_degrade(tmp_path / "ncm.pdf", NCM_MAUVAISE)
    sans = lire_ncm(ncm_p, ref.motif, DictionnaireStatuts())
    assert not sans.ids and any("activer l'OCR" in a for a in sans.diagnostic.avertissements)
    ncm = lire_ncm(ncm_p, ref.motif, DictionnaireStatuts(), ocr=True)
    assert ncm.ids == ["REQ_B2-3_16", "REQ_B2-3_18"]
    assert ncm.par_id()["REQ_B2-3_16"][0].statut is Statut.PARTIEL
    assert ncm.diagnostic.tableaux_ocr == 1
    assert any("relues par l'OCR" in a for a in ncm.diagnostic.avertissements)


def test_tableau_ocr_sans_barres():
    texte = "RR ID   DA ID            Compliance           Comments\n135     [REQ_B2-3_34]    Compliant            Should be transferred.\n151     [REQ_B2-3_16]    Partially compliant   To be agreed.\n"
    t = ocr.tableaux_depuis_texte(texte, "f.pdf", 1)
    assert len(t) == 1 and t[0].lignes[2] == ["151", "[REQ_B2-3_16]", "Partially compliant", "To be agreed."]
