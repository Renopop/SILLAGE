from conftest import NCM_A, ecrire_docx, ecrire_xlsx

from sillage.identifiants import MotifIdentifiant
from sillage.lecture_ncm import lire_ncm, profil_depuis_colonnes, reconnaitre_colonnes, revision_depuis_nom
from sillage.modele import CHEMIN_PROFIL, CHEMIN_TABLEAU, NCM, Statut
from sillage.profils import Profil
from sillage.statuts import DictionnaireStatuts
from sillage.tableaux import lire_tableaux


def test_colonnes_par_contenu(tmp_path):
    p = ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_A)
    t = lire_tableaux(p)[0]
    cols = reconnaitre_colonnes(t, MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    assert cols.id == 0 and cols.statut == 1 and cols.justification == 2
    assert cols.autres == {"action": 3, "derogation": 4}
    assert cols.ligne_en_tete == 0
    assert cols.roles()["statut"] == "Compliance"


def test_lecture_complete(tmp_path):
    p = ecrire_xlsx(tmp_path / "NCM_fournisseurA_revB.xlsx", NCM_A)
    ncm = lire_ncm(p, MotifIdentifiant({"REQ"}), DictionnaireStatuts(), fournisseur="A",
                   ids_referentiel=["REQ-1", "REQ-2", "REQ-3", "REQ-4", "REQ-5", "REQ-6"])
    assert ncm.revision == "B"
    assert ncm.ids == ["REQ-1", "REQ-2", "REQ-3", "REQ-4", "REQ-5", "REQ-9"]
    par = ncm.par_id()
    assert par["REQ-2"][0].statut is Statut.PARTIEL
    assert par["REQ-2"][0].champs["action"].startswith("Essai")
    assert par["REQ-4"][0].champs["derogation"] == "RFD-0007"
    assert par["REQ-5"][0].statut is Statut.CONFORME and par["REQ-5"][0].statut_brut == "Compliant (see note 2)"
    assert par["REQ-1"][0].origine.ligne == 2 and "NCM" in par["REQ-1"][0].origine.emplacement
    assert par["REQ-1"][0].brut["Req ID"] == "REQ-001"
    d = ncm.diagnostic
    assert d.tableaux_retenus == 1 and d.lignes_total == 6 and d.lignes_avec_id == 6 and d.lignes_pivot == 6
    assert d.statuts_reconnus == 6 and d.taux_statuts == 1.0
    assert d.ids_referentiel == 6 and d.ids_retrouves == 5
    assert d.chemin == CHEMIN_TABLEAU
    assert "Compliance" in d.texte()


def test_plages_et_statuts_inconnus(tmp_path):
    lignes = [
        ["Exigence", "Réponse", "Commentaire"],
        ["REQ-10 à REQ-12", "Conforme", "groupe"],
        ["REQ-13, REQ-14", "Vert", ""],
        ["REQ-15", "Rouge", "x"],
        ["Note de bas de tableau sans identifiant", "", ""],
    ]
    p = ecrire_xlsx(tmp_path / "ncm.xlsx", lignes)
    ncm = lire_ncm(p, MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    assert ncm.ids == ["REQ-10", "REQ-11", "REQ-12", "REQ-13", "REQ-14", "REQ-15"]
    par = ncm.par_id()
    assert par["REQ-11"][0].statut is Statut.CONFORME and par["REQ-11"][0].id_brut == "REQ-10 à REQ-12"
    assert par["REQ-13"][0].statut is Statut.INCONNU
    assert ncm.diagnostic.statuts_inconnus == {"Vert": 1, "Rouge": 1}   # une occurrence par cellule, pas par ligne pivot
    assert ncm.diagnostic.lignes_total == 4 and ncm.diagnostic.lignes_avec_id == 3 and ncm.diagnostic.lignes_pivot == 6
    # le commentaire sert de justification faute de colonne plus longue
    assert par["REQ-10"][0].justification == "groupe"


def test_sans_colonne_statut(tmp_path):
    p = ecrire_xlsx(tmp_path / "ncm.xlsx", [["ID", "Texte"], ["REQ-1", "blabla"], ["REQ-2", "blibli"]])
    ncm = lire_ncm(p, MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    assert all(l.statut is Statut.INCONNU for l in ncm.lignes)
    assert any("aucune colonne de statuts" in a for a in ncm.diagnostic.avertissements)


def test_tableau_sans_identifiants(tmp_path):
    p = ecrire_xlsx(tmp_path / "ncm.xlsx", [["a", "b"], ["1", "2"]])
    ncm = lire_ncm(p, MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    assert ncm.lignes == [] and ncm.diagnostic.tableaux_retenus == 0
    assert ncm.diagnostic.avertissements


def test_profil_prioritaire(tmp_path, sillage_home):
    lignes = [
        ["Req ID", "Compliance", "Col A", "Col B"],
        ["REQ-1", "C", "court", "Un long texte qui serait pris pour la justification par contenu."],
    ]
    p = ecrire_xlsx(tmp_path / "ncm.xlsx", lignes)
    dico = DictionnaireStatuts()
    sans = lire_ncm(p, MotifIdentifiant({"REQ"}), dico)
    assert sans.lignes[0].justification.startswith("Un long")
    profil = Profil("A", {"id": "Req ID", "statut": "Compliance", "justification": "Col A", "commentaire": "Col B"})
    profil.enregistrer()
    assert Profil.charger("A").colonnes["justification"] == "Col A"
    avec = lire_ncm(p, MotifIdentifiant({"REQ"}), dico, profil=Profil.charger("A"))
    assert avec.lignes[0].justification == "court"
    assert avec.lignes[0].champs["commentaire"].startswith("Un long")
    assert avec.diagnostic.chemin == CHEMIN_PROFIL
    # profil qui ne correspond plus aux en-têtes : retour à la reconnaissance par contenu
    autre = Profil("A", {"id": "Identifiant", "statut": "Compliance"})
    assert lire_ncm(p, MotifIdentifiant({"REQ"}), dico, profil=autre).diagnostic.chemin == CHEMIN_TABLEAU


def test_profil_propose(tmp_path):
    p = ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_A)
    cols = reconnaitre_colonnes(lire_tableaux(p)[0], MotifIdentifiant({"REQ"}), DictionnaireStatuts())
    prof = profil_depuis_colonnes("A", cols)
    assert prof.colonnes == {"id": "Req ID", "statut": "Compliance", "justification": "Justification",
                             "action": "Action plan", "derogation": "Waiver ref"}
    assert prof.correspond(["Req ID", "Compliance", "Justification", "Action plan", "Waiver ref"])


def test_docx_et_json(tmp_path):
    p = ecrire_docx(tmp_path / "ncm.docx", tables=[NCM_A])
    ncm = lire_ncm(p, MotifIdentifiant({"REQ"}), DictionnaireStatuts(), fournisseur="A", revision="C")
    assert len(ncm.lignes) == 6
    j = tmp_path / "ncm.json"
    ncm.enregistrer(j)
    ncm2 = NCM.charger(j)
    assert ncm2.revision == "C" and ncm2.ids == ncm.ids
    assert ncm2.par_id()["REQ-4"][0].statut is Statut.NON_CONFORME
    assert ncm2.par_id()["REQ-4"][0].origine.emplacement == "table 1"


def test_revision_depuis_nom():
    assert revision_depuis_nom("NCM_fournisseur_revB.xlsx") == "B"
    assert revision_depuis_nom("NCM Rev 3.docx") == "3"
    assert revision_depuis_nom("ncm-issue-A2.pdf") == "A2"
    assert revision_depuis_nom("ncm_v1.2.xlsx") == "1.2"
    assert revision_depuis_nom("ncm.xlsx") == ""
