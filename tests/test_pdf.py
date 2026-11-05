import pytest

from conftest import NCM_A

from sillage.identifiants import MotifIdentifiant
from sillage.lecture_ncm import lire_ncm
from sillage.modele import Statut
from sillage.statuts import DictionnaireStatuts

fitz = pytest.importorskip("fitz")   # PyMuPDF, seulement pour fabriquer un PDF d'essai


def _pdf_tableau(chemin, lignes, par_page=4):
    """Tableau à grille tracée, coupé sur plusieurs pages avec en-tête répété."""
    doc = fitz.open()
    en_tete, donnees = lignes[0], lignes[1:]
    largeurs = [70, 90, 220, 110, 70]
    for debut in range(0, len(donnees), par_page):
        page = doc.new_page(width=595, height=842)
        y = 60
        for row in [en_tete] + donnees[debut:debut + par_page]:
            x = 30
            for cell, w in zip(row, largeurs):
                page.draw_rect(fitz.Rect(x, y, x + w, y + 40), color=(0, 0, 0), width=0.8)
                page.insert_textbox(fitz.Rect(x + 3, y + 3, x + w - 3, y + 40), str(cell), fontsize=7)
                x += w
            y += 40
    doc.save(str(chemin))
    doc.close()
    return chemin


def test_pdf_coupe_sur_deux_pages(tmp_path):
    p = _pdf_tableau(tmp_path / "ncm.pdf", NCM_A)
    ncm = lire_ncm(p, MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    assert ncm.ids == ["REQ-1", "REQ-2", "REQ-3", "REQ-4", "REQ-5", "REQ-9"]
    par = ncm.par_id()
    assert par["REQ-4"][0].statut is Statut.NON_CONFORME
    assert par["REQ-4"][0].champs.get("derogation") == "RFD-0007"
    assert par["REQ-9"][0].origine.emplacement.startswith("page 1, tableau 1 à page 2")
    assert ncm.diagnostic.tableaux_retenus == 1
