"""Lot 3 : vérification des « C » par le moteur de RIVAGE (faux embedder et faux
reranker, hors ligne), campagne des affaiblissements, grille enrichie."""
import hashlib

import numpy as np
import pytest
from conftest import SPEC, ecrire_docx, ecrire_xlsx

from sillage import rivage

pytestmark = pytest.mark.skipif(not rivage.disponible(), reason="RIVAGE introuvable (SILLAGE_RIVAGE_DIR)")


class FakeEmbedder:
    dim = 128

    def _vec(self, text):
        v = np.zeros(self.dim, dtype="float32")
        for w in text.lower().split():
            v[int(hashlib.sha1(w.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_passages(self, texts, log=None, progress_cb=None):
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")
        return np.array([self._vec(t) for t in texts], dtype="float32")

    def embed_query(self, text, log=None):
        return self._vec(text)

    def close(self):
        pass


class FakeReranker:
    def available(self):
        return True

    def rerank(self, query, documents, top_k, log=None):
        # mots « vides » du corpus d'essai ignorés, pour que seul le fond compte
        vides = {"calculateur", "doit", "être"}
        q = {w for w in query.lower().split() if len(w) > 3} - vides
        scores = []
        for i, d in enumerate(documents):
            mots = {w for w in d.lower().split() if len(w) > 3} - vides
            scores.append((i, 0.1 + 0.9 * len(q & mots) / max(1, len(q))))
        scores.sort(key=lambda t: -t[1])
        return scores[:top_k]

    def close(self):
        pass


@pytest.fixture
def corpus_fournisseur(tmp_path):
    root = tmp_path / "fournisseur"
    (root / "Spec").mkdir(parents=True)
    (root / "Spec" / "spec_fournisseur.txt").write_text(
        "1. Masse\n\nLe calculateur ne doit pas peser plus de 2,5 kg ; masse mesurée 1,8 kg (rapport de pesée T-12).\n\n"
        "2. Alimentation\n\nLe calculateur est alimenté en 28 V DC conformément à DO-160 section 16.\n\n"
        "3. Panne\n\nLe calculateur signale toute panne interne par un BITE intégré et un message au bus.\n\n"
        "4. Documentation\n\nLa documentation sera fournie ultérieurement, where practicable, pour REQ-006.\n",
        encoding="utf-8")
    (root / "Spec" / "divers.txt").write_text("Procédure de livraison et emballage du calculateur.\n", encoding="utf-8")
    return root


@pytest.fixture
def index_fournisseur(tmp_path, corpus_fournisseur):
    import index_cache
    index_cache.clear_cache()
    d = tmp_path / "index"
    rivage.indexer(str(corpus_fournisseur), str(d), embedder=FakeEmbedder(), use_ocr=False)
    yield d
    index_cache.clear_cache()


NCM_C = [
    ["Req ID", "Compliance", "Justification"],
    ["REQ-001", "C", "Masse mesurée 1,8 kg."],      # étayé, valeur citée
    ["REQ-002", "C", "Qualifié en température."],   # rien dans la spec fournisseur : absent
    ["REQ-003", "C", "28 V DC."],                   # étayé
    ["REQ-004", "NC", "DAL C."],                    # non vérifié (NC)
    ["REQ-005", "C", "BITE."],                      # étayé
    ["REQ-006", "C", "Documentation."],             # étayé par un passage affaibli
]


def test_valeurs():
    from sillage.appui import valeurs
    assert valeurs("peser au plus 2 kg et fonctionner de -40 °C à +70 °C en 28 V DC") == ["2 kg", "-40 °C", "70 °C", "28 V"]
    assert valeurs("1,8 kg") == ["1.8 kg"]
    assert valeurs("aucun nombre") == []
    assert valeurs("1. Masse\n\nmasse 1,8 kg (rapport T-12).\n\n2. Alimentation\n\n1.2 Titre\n3) Autre\n28 V") == ["1.8 kg", "28 V"]
    assert valeurs("tolérance 5 % sur 12 mesures") == ["5 %", "12"]


def test_campagne_appui(tmp_path, index_fournisseur, corpus_fournisseur):
    from sillage.appui import ABSENT, ETAYE, Campagne, verifier
    from sillage.lecture_ncm import lire_ncm
    from sillage.referentiel import lire_referentiel
    from sillage.statuts import DictionnaireStatuts
    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC))
    ncm = lire_ncm(ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_C), ref.motif, DictionnaireStatuts(), fournisseur="A")
    with rivage.Moteur(str(index_fournisseur), embedder=FakeEmbedder(), reranker=FakeReranker()) as moteur:
        assert len(moteur.fichiers()) == 2
        assert len(moteur.fichiers([str(corpus_fournisseur / "Spec")])) == 2
        assert moteur.fichiers([str(corpus_fournisseur / "Autre")]) == []
        c = verifier(ref, ncm, moteur, prefixes=[str(corpus_fournisseur / "Spec")], seuil_etaye=0.5, seuil_faible=0.2)
    assert sorted(c.appuis) == ["REQ-1", "REQ-2", "REQ-3", "REQ-5", "REQ-6"]     # les « C » seulement
    a1 = c.appuis["REQ-1"]
    assert a1.niveau == ETAYE and a1.reranked and a1.score >= 0.5
    assert "1,8 kg" in a1.meilleur.texte and a1.meilleur.chemin.endswith("spec_fournisseur.txt")
    assert a1.valeurs_exigence == ["2 kg"] and a1.valeurs_absentes == ["2 kg"]   # 2 kg n'est pas cité (2,5 et 1,8 le sont)
    assert c.appuis["REQ-2"].niveau == ABSENT                                   # rien sur la température
    assert c.appuis["REQ-3"].niveau == ETAYE and c.appuis["REQ-3"].valeurs_absentes == []
    assert c.appuis["REQ-5"].niveau == ETAYE
    r = c.resume()
    assert r["total"] == 5 and r[ETAYE] >= 3 and "Scores" in c.texte_resume()
    # le PC aussi, et REQ-002 (température) ne trouve rien de comparable
    with rivage.Moteur(str(index_fournisseur), embedder=FakeEmbedder(), reranker=FakeReranker()) as moteur:
        c2 = verifier(ref, ncm, moteur, statuts=["C", "NC"], seuil_etaye=0.5, seuil_faible=0.2)
    assert "REQ-4" in c2.appuis
    # aller-retour JSON
    j = tmp_path / "appuis.json"
    c.enregistrer(j)
    c3 = Campagne.charger(j)
    assert c3.appuis["REQ-1"].meilleur.texte == a1.meilleur.texte and c3.seuil_etaye == 0.5


def test_campagne_sans_reranker(tmp_path, index_fournisseur):
    from sillage.appui import NON_VERIFIE, verifier
    from sillage.lecture_ncm import lire_ncm
    from sillage.referentiel import lire_referentiel
    from sillage.statuts import DictionnaireStatuts

    class SansReranker(FakeReranker):
        def available(self):
            return False

        def rerank(self, *a, **k):
            raise RuntimeError("indisponible")

    ref = lire_referentiel(ecrire_xlsx(tmp_path / "spec.xlsx", SPEC))
    ncm = lire_ncm(ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_C), ref.motif, DictionnaireStatuts())
    with rivage.Moteur(str(index_fournisseur), embedder=FakeEmbedder(), reranker=SansReranker()) as moteur:
        c = verifier(ref, ncm, moteur)
    assert all(a.niveau == NON_VERIFIE and a.score is None for a in c.appuis.values() if a.passages)


def test_affaiblissements(tmp_path):
    from sillage.affaiblissements import relever
    from sillage.identifiants import MotifIdentifiant
    doc = ecrire_docx(tmp_path / "reponse.docx", paragraphes=[
        "REQ-002 : la qualification en température sera confirmée ultérieurement (TBC).",
        "REQ-006 : documentation fournie where practicable.",
        "Le fournisseur may deliver early.",                 # « may » sans identifiant : ignoré
        "Livraison sous réserve de disponibilité (REQ-003).",
        "Point ouvert sur l'emballage.",                     # expression forte sans identifiant : conservée
    ])
    r = relever([str(doc)], MotifIdentifiant({"REQ"}))
    par = r.par_id()
    assert sorted(par) == ["REQ-2", "REQ-3", "REQ-6"]
    assert {o.expression for o in par["REQ-2"]} == {"ultérieurement", "TBC"}
    assert {o.expression for o in par["REQ-6"]} == {"where practicable"}
    assert {o.expression for o in par["REQ-3"]} == {"sous réserve"}
    assert [o.expression for o in r.sans_id()] == ["point ouvert"]
    assert "ultérieurement" in r.resume() and "occurrence(s)" in r.texte_resume()
    assert r.occurrences[0].reference.startswith("paragraphe") and "…" not in r.occurrences[0].contexte[:1]


def test_grille_enrichie_et_cli(tmp_path, index_fournisseur, corpus_fournisseur, capsys):
    from openpyxl import load_workbook
    from sillage.appui import ETAYE, verifier
    from sillage.controles import AFFAIBLISSEMENT, C_SANS_APPUI, VALEURS_ABSENTES, controler
    from sillage.affaiblissements import relever
    from sillage.grille import exporter_grille
    from sillage.lecture_ncm import lire_ncm
    from sillage.referentiel import lire_referentiel
    from sillage.statuts import DictionnaireStatuts
    spec = ecrire_xlsx(tmp_path / "spec.xlsx", SPEC)
    ncm_f = ecrire_xlsx(tmp_path / "ncm.xlsx", NCM_C)
    ref = lire_referentiel(spec)
    ncm = lire_ncm(ncm_f, ref.motif, DictionnaireStatuts(), fournisseur="A")
    with rivage.Moteur(str(index_fournisseur), embedder=FakeEmbedder(), reranker=FakeReranker()) as moteur:
        c = verifier(ref, ncm, moteur, seuil_etaye=0.5, seuil_faible=0.2)
    rel = relever([str(corpus_fournisseur / "Spec" / "spec_fournisseur.txt")], ref.motif, ids_connus=ref.ids)
    g = controler(ref, ncm, campagne=c, releve=rel)
    par = {l.id: l for l in g.lignes}
    assert par["REQ-1"].appui_niveau == ETAYE and par["REQ-1"].appui_document.startswith("spec_fournisseur")
    assert VALEURS_ABSENTES in par["REQ-1"].alertes and par["REQ-1"].valeurs_absentes == ["2 kg"]
    assert par["REQ-6"].affaiblissements == ["ultérieurement", "where practicable"] and AFFAIBLISSEMENT in par["REQ-6"].alertes
    assert par["REQ-4"].appui_niveau == ""            # NC : pas vérifié
    assert C_SANS_APPUI in par["REQ-2"].alertes and par["REQ-2"].appui_niveau == "absent"
    r = g.resume()
    assert r[AFFAIBLISSEMENT] == 1 and r[VALEURS_ABSENTES] >= 1 and r[C_SANS_APPUI] == 1
    assert "sans appui" in g.texte_resume()
    p = exporter_grille(g, tmp_path / "grille.xlsx")
    ws = load_workbook(str(p))["Grille"]
    assert ws["R1"].value == "Appui" and ws["W1"].value == "Affaiblissements"
    lignes = {ws.cell(r, 1).value: [ws.cell(r, c).value for c in range(1, 24)] for r in range(2, ws.max_row + 1)}
    assert lignes["REQ-1"][17] == ETAYE and isinstance(lignes["REQ-1"][18], (int, float))
    assert lignes["REQ-6"][22] == "ultérieurement, where practicable"

    # ligne de commande : appui --json puis grille --appuis, et affaiblissements
    j = tmp_path / "appuis.json"
    assert main_avec_faux(["appui", "--spec", str(spec), "--ncm", str(ncm_f), "--index", str(index_fournisseur),
                           "--seuil-etaye", "0.5", "--seuil-faible", "0.2", "--json", str(j)]) == 0
    out = capsys.readouterr().out
    assert "Exigences vérifiées : 5" in out and j.exists()
    from sillage.cli import main
    assert main(["grille", "--spec", str(spec), "--ncm", str(ncm_f), "--appuis", str(j),
                 "--affaiblissements", str(corpus_fournisseur / "Spec" / "spec_fournisseur.txt"),
                 "--sortie", str(tmp_path / "g2.xlsx")]) == 0
    out = capsys.readouterr().out
    assert "Affaiblissements :" in out and "sans appui" in out and (tmp_path / "g2.xlsx").exists()
    assert main(["affaiblissements", str(corpus_fournisseur / "Spec" / "spec_fournisseur.txt"), "--spec", str(spec)]) == 0
    assert "where practicable" in capsys.readouterr().out


def main_avec_faux(argv):
    """La ligne de commande avec le faux embedder et le faux reranker injectés dans le moteur."""
    from sillage.cli import main
    orig = rivage.Moteur.__init__

    def init(self, index_dir, embedder=None, reranker=None, log=None):
        orig(self, index_dir, embedder=FakeEmbedder(), reranker=FakeReranker(), log=log)
    rivage.Moteur.__init__ = init
    try:
        return main(argv)
    finally:
        rivage.Moteur.__init__ = orig


def test_indexer_cli(tmp_path, corpus_fournisseur, capsys, monkeypatch):
    import index_cache
    index_cache.clear_cache()
    monkeypatch.setattr(rivage, "indexer", lambda corpus, index_dir, **k: "ok")
    from sillage.cli import main
    assert main(["indexer", str(corpus_fournisseur), "--fournisseur", "A"]) == 0
    assert "indexation par RIVAGE" in capsys.readouterr().out
