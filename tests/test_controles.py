from conftest import NCM_A, SPEC, ecrire_xlsx

from sillage.controles import (DOUBLON, ECART_OUVERT, IMPACT_NORME, INCOHERENCE, ORPHELIN, STATUT_INCONNU, TROU,
                               CLOTURE_ACTION, CLOTURE_DEROGATION, CLOTURE_OUVERT, comparer, controler)
from sillage.lecture_ncm import lire_ncm
from sillage.modele import Statut
from sillage.referentiel import lire_referentiel
from sillage.statuts import DictionnaireStatuts


def _grille(tmp_path, lignes_ncm, nom="ncm.xlsx"):
    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC))
    ncm = lire_ncm(ecrire_xlsx(tmp_path / nom, lignes_ncm), ref.motif, DictionnaireStatuts(),
                   fournisseur="A", ids_referentiel=ref.ids)
    return ref, ncm, controler(ref, ncm)


def test_grille_de_base(tmp_path):
    ref, ncm, g = _grille(tmp_path, NCM_A)
    par = {l.id: l for l in g.lignes}
    assert [l.id for l in g.lignes] == ["REQ-1", "REQ-2", "REQ-3", "REQ-4", "REQ-5", "REQ-6", "REQ-9"]
    assert par["REQ-1"].statut is Statut.CONFORME and par["REQ-1"].norme == "CS 25.1309"
    assert par["REQ-1"].texte.startswith("Le calculateur doit peser")
    assert par["REQ-2"].statut is Statut.PARTIEL and par["REQ-2"].cloture == CLOTURE_ACTION
    assert par["REQ-4"].statut is Statut.NON_CONFORME and par["REQ-4"].cloture == CLOTURE_DEROGATION
    assert par["REQ-6"].alertes == [TROU] and par["REQ-6"].statut is Statut.INCONNU
    assert par["REQ-9"].alertes == [ORPHELIN] and not par["REQ-9"].dans_referentiel
    r = g.resume()
    assert r["exigences"] == 6 and r["couverts"] == 5 and r["C"] == 3 and r["PC"] == 1 and r["NC"] == 1
    assert r[TROU] == 1 and r[ORPHELIN] == 1 and r[ECART_OUVERT] == 0 and r["ecarts"] == 2
    assert "Trous : 1" in g.texte_resume()


def test_doublons_incoherences_inconnus(tmp_path):
    lignes = [
        ["ID", "Statut", "Justification", "Action"],
        ["REQ-001", "C", "a", ""],
        ["REQ-001", "C", "b", ""],
        ["REQ-002", "C", "", ""],
        ["REQ-002", "NC", "contradiction", ""],
        ["REQ-003", "Peut-être", "", ""],
        ["REQ-004", "NC", "pas d'action", ""],
        ["REQ-005", "PC", "action prévue", "corriger"],
        ["REQ-006", "NA", "", ""],
    ]
    ref, ncm, g = _grille(tmp_path, lignes)
    par = {l.id: l for l in g.lignes}
    assert par["REQ-1"].statut is Statut.CONFORME and par["REQ-1"].alertes == [DOUBLON]
    assert par["REQ-1"].justification == "a\nb" and par["REQ-1"].nb_lignes_ncm == 2
    assert par["REQ-2"].statut is Statut.INCONNU and par["REQ-2"].alertes == [INCOHERENCE]
    # REQ-3 et REQ-4 sont tracées vers la norme dans SPEC : l'écart porte aussi l'alerte « impact norme »
    assert par["REQ-3"].alertes == [STATUT_INCONNU, IMPACT_NORME] and par["REQ-3"].statut_brut == "Peut-être"
    assert par["REQ-4"].cloture == CLOTURE_OUVERT and par["REQ-4"].alertes == [ECART_OUVERT, IMPACT_NORME]
    assert par["REQ-5"].cloture == CLOTURE_ACTION and par["REQ-5"].alertes == [IMPACT_NORME]   # PC tracée CS 25.1309
    assert par["REQ-6"].statut is Statut.NON_APPLICABLE and par["REQ-6"].cloture == ""
    types = sorted(a.type for a in g.alertes)
    assert types == sorted([DOUBLON, INCOHERENCE, STATUT_INCONNU, ECART_OUVERT, IMPACT_NORME, IMPACT_NORME, IMPACT_NORME])
    assert [l.id for l in g.impacts_norme()] == ["REQ-3", "REQ-4", "REQ-5"]


def test_diff_revisions(tmp_path):
    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC))
    a = lire_ncm(ecrire_xlsx(tmp_path / "revA.xlsx", NCM_A), ref.motif, DictionnaireStatuts(), fournisseur="A")
    lignes_b = [
        ["Req ID", "Compliance", "Justification", "Action plan", "Waiver ref"],
        ["REQ-001", "PC", "Masse mesurée 2,1 kg.", "", ""],            # régression C -> PC
        ["REQ-002", "C", "Qualifié jusqu'à +70 °C (essai T-15).", "", ""],   # amélioration PC -> C
        ["REQ-003", "C", "Alimentation 28 V DC conforme DO-160 §16.", "", ""],  # inchangé
        ["REQ-004", "NC", "Logiciel développé au niveau DAL C, RFD acceptée.", "", "RFD-0007"],  # justification
        ["REQ-006", "C", "Documentation en anglais.", "", ""],             # nouvelle
    ]                                                                      # REQ-005 et REQ-009 disparues
    b = lire_ncm(ecrire_xlsx(tmp_path / "revB.xlsx", lignes_b), ref.motif, DictionnaireStatuts(), fournisseur="A")
    d = comparer(a, b)
    nat = {(c.id, c.nature): c for c in d.changements}
    assert nat[("REQ-1", "statut")].sens == "régression" and nat[("REQ-1", "statut")].avant == "C"
    assert nat[("REQ-2", "statut")].sens == "amélioration"
    assert ("REQ-3", "statut") not in nat and ("REQ-3", "justification") not in nat
    assert nat[("REQ-4", "justification")].apres.endswith("acceptée.")
    assert nat[("REQ-6", "nouvelle")].apres == "C"
    assert ("REQ-5", "disparue") in nat and ("REQ-9", "disparue") in nat
    r = d.resume()
    assert r == {"nouvelle": 1, "disparue": 2, "statut": 2, "justification": 3, "regression": 1, "amelioration": 1}
    assert "régressions : 1" in d.texte_resume()
