from sillage.modele import Statut
from sillage.statuts import DictionnaireStatuts


def test_vocabulaires():
    d = DictionnaireStatuts()
    assert d.reconnaitre("C") is Statut.CONFORME
    assert d.reconnaitre("Compliant") is Statut.CONFORME
    assert d.reconnaitre("Fully compliant") is Statut.CONFORME
    assert d.reconnaitre("Oui") is Statut.CONFORME
    assert d.reconnaitre("PC") is Statut.PARTIEL
    assert d.reconnaitre("Partially compliant") is Statut.PARTIEL
    assert d.reconnaitre("partiellement conforme") is Statut.PARTIEL
    assert d.reconnaitre("NC") is Statut.NON_CONFORME
    assert d.reconnaitre("Non-compliant") is Statut.NON_CONFORME
    assert d.reconnaitre("Deviation") is Statut.NON_CONFORME
    assert d.reconnaitre("N/A") is Statut.NON_APPLICABLE
    assert d.reconnaitre("Not applicable") is Statut.NON_APPLICABLE
    assert d.reconnaitre("Sans objet") is Statut.NON_APPLICABLE


def test_tolerance():
    d = DictionnaireStatuts()
    assert d.reconnaitre("C (see note 3)") is Statut.CONFORME
    assert d.reconnaitre("PC - voir §4.2") is Statut.PARTIEL
    assert d.reconnaitre("NC: deviation requested") is Statut.NON_CONFORME
    assert d.reconnaitre("  nc  ") is Statut.NON_CONFORME
    assert d.reconnaitre("Partially compliant, see justification") is Statut.PARTIEL
    assert d.reconnaitre("") is None
    assert d.reconnaitre(None) is None
    assert d.reconnaitre("Masse mesurée 1,8 kg") is None
    assert d.reconnaitre("Compliant by design") is Statut.CONFORME


def test_enrichissement_persistant(sillage_home):
    d = DictionnaireStatuts.charger()
    assert d.reconnaitre("Vert") is None
    d.ajouter("Vert", Statut.CONFORME)
    assert (sillage_home / "statuts.json").exists()
    d2 = DictionnaireStatuts.charger()
    assert d2.reconnaitre("vert") is Statut.CONFORME
    assert d2.ajouts() == {"vert": "C"}
