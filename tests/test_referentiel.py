from conftest import SPEC, ecrire_docx, ecrire_xlsx

from sillage.referentiel import lire_referentiel


def test_tableur(tmp_path):
    p = ecrire_xlsx(tmp_path / "spec.xlsx", SPEC, feuille="Exigences")
    ref = lire_referentiel(p)
    assert ref.ids == ["REQ-1", "REQ-2", "REQ-3", "REQ-4", "REQ-5", "REQ-6"]
    e = ref.par_id()["REQ-1"]
    assert e.texte.startswith("Le calculateur doit peser")
    assert e.norme == "CS 25.1309" and e.id_brut == "REQ-001"
    assert e.reference == "feuille « Exigences », ligne 2"
    assert ref.motif.prefixes == {"REQ"}
    assert ref.motif.extraire("CS 25.1309 et REQ-002") == ["REQ-2"]


def test_csv_sans_en_tete_texte(tmp_path):
    p = tmp_path / "spec.csv"
    p.write_text("REQ-1;Première exigence\nREQ-2;Deuxième exigence\n", encoding="utf-8")
    ref = lire_referentiel(p)
    assert ref.ids == ["REQ-1", "REQ-2"]
    assert ref.par_id()["REQ-2"].texte == "Deuxième exigence"


def test_document(tmp_path):
    p = ecrire_docx(tmp_path / "spec.docx", paragraphes=[
        ("1. Généralités", "Heading 1"),
        "Ce document spécifie le calculateur.",
        "REQ-001 : Le calculateur doit peser au plus 2 kg.",
        "La masse s'entend équipé de ses connecteurs.",
        ("2. Environnement", "Heading 1"),
        "Texte d'introduction sans identifiant.",
        "REQ-002 Le calculateur doit fonctionner de -40 °C à +70 °C.",
        "REQ-003 — Alimentation 28 V DC.",
    ])
    ref = lire_referentiel(p)
    assert ref.ids == ["REQ-1", "REQ-2", "REQ-3"]
    e1 = ref.par_id()["REQ-1"]
    assert e1.texte == "Le calculateur doit peser au plus 2 kg.\nLa masse s'entend équipé de ses connecteurs."
    assert ref.par_id()["REQ-3"].texte == "Alimentation 28 V DC."
    assert ref.par_id()["REQ-2"].reference.startswith("paragraphe")


def test_texte_et_doublons(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("# Spec\n\nREQ-1 Première exigence.\n\nREQ-2 Deuxième.\n\nREQ-1 Première exigence, répétée.\n", encoding="utf-8")
    ref = lire_referentiel(p)
    assert ref.ids == ["REQ-1", "REQ-2"]
    assert "répétée" in ref.par_id()["REQ-1"].texte
