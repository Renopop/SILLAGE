from conftest import ecrire_docx, ecrire_xlsx

from sillage.tableaux import Tableau, _recoller, lire_tableaux


def test_xlsx_lignes_vides_et_fusions(tmp_path):
    p = ecrire_xlsx(tmp_path / "t.xlsx", [
        ["ID", "Statut", "Justif"],
        ["REQ-1", "C", "ok"],
        [None, None, None],
        ["REQ-2", "PC", "a"],
        [None, None, "b"],
    ], fusions=["A4:A5"])
    tabs = lire_tableaux(p)
    assert len(tabs) == 1
    t = tabs[0]
    assert t.emplacement == "feuille « NCM »"
    assert t.lignes == [["ID", "Statut", "Justif"], ["REQ-1", "C", "ok"], ["REQ-2", "PC", "a"], ["REQ-2", "", "b"]]
    assert t.numeros == [1, 2, 4, 5]


def test_docx_tables(tmp_path):
    p = ecrire_docx(tmp_path / "t.docx", paragraphes=["intro"], tables=[
        [["ID", "Statut"], ["REQ-1", "C"]],
        [["ID", "Statut"], ["REQ-2", "NC"]],
    ])
    tabs = lire_tableaux(p)
    assert [t.emplacement for t in tabs] == ["table 1", "table 2"]
    assert tabs[1].lignes[1] == ["REQ-2", "NC"]


def test_csv(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("ID;Statut;Justif\nREQ-1;C;ok\nREQ-2;NC;ko\n", encoding="utf-8")
    t = lire_tableaux(p)[0]
    assert t.lignes[2] == ["REQ-2", "NC", "ko"]


def test_format_inconnu(tmp_path):
    p = tmp_path / "t.odt"
    p.write_text("x")
    try:
        lire_tableaux(p)
        assert False
    except ValueError as e:
        assert "odt" in str(e)


def test_recollage_pdf():
    a = Tableau("f.pdf", "page 1, tableau 1", [["ID", "S"], ["REQ-1", "C"]], [1, 2])
    b = Tableau("f.pdf", "page 2, tableau 1", [["ID", "S"], ["REQ-2", "NC"]], [1, 2])
    c = Tableau("f.pdf", "page 2, tableau 2", [["x", "y", "z"]], [1])
    out = _recoller([a, b, c])
    assert len(out) == 2
    assert out[0].lignes == [["ID", "S"], ["REQ-1", "C"], ["REQ-2", "NC"]]
    assert out[0].emplacement == "page 1, tableau 1 à page 2, tableau 1"
