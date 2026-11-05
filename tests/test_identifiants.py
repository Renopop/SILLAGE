from sillage.identifiants import MotifIdentifiant, deduire_motif, trier


def test_formes_normalisees():
    m = MotifIdentifiant()
    assert m.extraire("REQ-001") == ["REQ-1"]
    assert m.extraire("req_0042") == ["REQ-42"]
    assert m.extraire("REQ 7") == ["REQ-7"]
    assert m.extraire("SYS_REQ-12") == ["SYS_REQ-12"]
    assert m.extraire("REQ-100a") == ["REQ-100a"]
    assert m.extraire("REQ-100.2") == ["REQ-100.2"]


def test_plusieurs_et_plages():
    m = MotifIdentifiant()
    assert m.extraire("REQ-1, REQ-3 et REQ-5") == ["REQ-1", "REQ-3", "REQ-5"]
    assert m.extraire("REQ-100 à REQ-103") == ["REQ-100", "REQ-101", "REQ-102", "REQ-103"]
    assert m.extraire("REQ-100 to REQ-102") == ["REQ-100", "REQ-101", "REQ-102"]
    assert m.extraire("REQ-100 – REQ-102") == ["REQ-100", "REQ-101", "REQ-102"]
    assert m.extraire("REQ-100 à 102") == ["REQ-100", "REQ-101", "REQ-102"]
    # plage inversée ou préfixes différents : pas de dépliage
    assert m.extraire("REQ-105 à REQ-100") == ["REQ-105", "REQ-100"]
    assert m.extraire("REQ-100 à SRD-103") == ["REQ-100", "SRD-103"]
    assert m.extraire("REQ-100 à REQ-103", deplier_plages=False) == ["REQ-100", "REQ-103"]


def test_motif_restreint_aux_prefixes():
    m = deduire_motif(["REQ-001", "REQ-002", "SRD-10"])
    assert m.prefixes == {"REQ", "SRD"}
    assert m.extraire("voir CS 25.1309 et REQ-2, ISO 9001") == ["REQ-2"]
    assert m.extraire("srd-0010") == ["SRD-10"]
    generique = MotifIdentifiant()
    assert "CS-25" in generique.extraire("voir CS 25.1309")


def test_commence_par_id():
    m = MotifIdentifiant({"REQ"})
    assert m.commence_par_id("REQ-12 : le calculateur doit…") == "REQ-12"
    assert m.commence_par_id("- REQ-12 le calculateur") == "REQ-12"
    assert m.commence_par_id("Le calculateur REQ-12 doit") is None


def test_tri_naturel():
    assert trier(["REQ-10", "REQ-2", "REQ-2a", "ABC-1"]) == ["ABC-1", "REQ-2", "REQ-2a", "REQ-10"]
