"""Ligne de commande de SILLAGE.

    sillage lire   NCM [--spec SPEC] [--fournisseur F] [--revision R] [--json SORTIE]
    sillage grille --spec SPEC --ncm NCM [--sortie grille.xlsx] [--fournisseur F] [--revision R]
    sillage diff   --avant NCM_A --apres NCM_B [--sortie diff.xlsx]
    sillage statuts [--ajouter "valeur=C"]
    sillage profils [--proposer NCM --fournisseur F]
    sillage gui
    sillage version
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .modele import Statut
from .version import VERSION_LABEL, __version__


def _motif_et_ref(spec: Optional[str]):
    from .referentiel import lire_referentiel
    if not spec:
        return None, None
    ref = lire_referentiel(spec)
    return ref.motif, ref


def _lire(chemin: str, spec: Optional[str], fournisseur: str, revision: str, sans_profil: bool = False):
    from .lecture_ncm import lire_ncm
    from .profils import Profil
    from .statuts import DictionnaireStatuts
    motif, ref = _motif_et_ref(spec)
    profil = None if (sans_profil or not fournisseur) else Profil.charger(fournisseur)
    ncm = lire_ncm(chemin, motif=motif, dico=DictionnaireStatuts.charger(), profil=profil,
                   fournisseur=fournisseur, revision=revision, ids_referentiel=ref.ids if ref else None)
    return ncm, ref


def cmd_lire(a) -> int:
    ncm, _ = _lire(a.ncm, a.spec, a.fournisseur, a.revision, a.sans_profil)
    print(f"{VERSION_LABEL} — lecture de {Path(a.ncm).name}" + (f" (révision {ncm.revision})" if ncm.revision else ""))
    print(ncm.diagnostic.texte())
    if a.json:
        ncm.enregistrer(a.json)
        print(f"NCM normalisée écrite : {a.json}")
    if a.aperçu:
        for l in ncm.lignes[: a.aperçu]:
            print(f"  {l.id:<14} {l.statut.value:<3} {l.statut_brut[:12]:<12} {l.justification[:70]}")
    return 0 if ncm.lignes else 2


def _index_dir(a) -> str:
    from .config import dossier_index
    return a.index or str(dossier_index(a.fournisseur))


def _campagne(a, ref, ncm):
    """Vérification des « C » par RIVAGE (lot 3), si demandée ou si un fichier d'appuis est donné."""
    from .appui import Campagne, verifier
    if getattr(a, "appuis", None):
        return Campagne.charger(a.appuis)
    if not getattr(a, "verifier", False):
        return None
    from .rivage import Moteur
    statuts = [s.strip().upper() for s in (a.statuts or "C").split(",") if s.strip()]
    def progression(k, n, ident):
        print(f"  [{k}/{n}] {ident}", end="\r", flush=True)
    with Moteur(_index_dir(a)) as moteur:
        c = verifier(ref, ncm, moteur, prefixes=a.dossier or None, fichiers=a.documents or None, statuts=statuts,
                     seuil_etaye=a.seuil_etaye, seuil_faible=a.seuil_faible, n_passages=a.n_passages,
                     progression=progression)
    print()
    return c


def _releve(a, ref):
    from .affaiblissements import relever
    fichiers = list(getattr(a, "affaiblissements", None) or [])
    if not fichiers:
        return None
    return relever(fichiers, motif=ref.motif, ids_connus=ref.ids)


def cmd_grille(a) -> int:
    from .controles import controler
    from .grille import exporter_grille
    ncm, ref = _lire(a.ncm, a.spec, a.fournisseur, a.revision, a.sans_profil)
    print(f"{VERSION_LABEL} — référentiel : {len(ref)} exigences ({Path(a.spec).name})")
    print(ncm.diagnostic.texte())
    campagne = _campagne(a, ref, ncm)
    if campagne is not None:
        print(campagne.texte_resume())
    releve = _releve(a, ref)
    if releve is not None:
        print(releve.texte_resume())
    g = controler(ref, ncm, campagne=campagne, releve=releve)
    print(g.texte_resume())
    sortie = a.sortie or str(Path(a.ncm).with_suffix("")) + "_grille.xlsx"
    exporter_grille(g, sortie)
    print(f"Grille écrite : {sortie}")
    return 0


def cmd_indexer(a) -> int:
    from .rivage import disponible, indexer, message_absent, version_rivage
    if not disponible():
        print(message_absent(), file=sys.stderr)
        return 1
    index_dir = _index_dir(a)
    print(f"{VERSION_LABEL} — indexation par RIVAGE {version_rivage()} : {a.dossier} → {index_dir}")
    def progression(ratio, message):
        print(f"  {ratio:5.0%} {message}", flush=True)
    stats = indexer(a.dossier, index_dir, use_ocr=not a.sans_ocr, progress_cb=progression)
    print(f"Terminé : {stats}")
    return 0


def cmd_appui(a) -> int:
    from .appui import verifier
    from .rivage import Moteur, disponible, message_absent
    if not disponible():
        print(message_absent(), file=sys.stderr)
        return 1
    ncm, ref = _lire(a.ncm, a.spec, a.fournisseur, a.revision, a.sans_profil)
    statuts = [s.strip().upper() for s in (a.statuts or "C").split(",") if s.strip()]
    print(f"{VERSION_LABEL} — vérification des statuts {', '.join(statuts)} de {Path(a.ncm).name} "
          f"dans l'index {_index_dir(a)}")
    def progression(k, n, ident):
        print(f"  [{k}/{n}] {ident}", end="\r", flush=True)
    with Moteur(_index_dir(a)) as moteur:
        c = verifier(ref, ncm, moteur, prefixes=a.dossier or None, fichiers=a.documents or None, statuts=statuts,
                     seuil_etaye=a.seuil_etaye, seuil_faible=a.seuil_faible, n_passages=a.n_passages,
                     progression=progression)
    print()
    print(c.texte_resume())
    for ident, ap in c.appuis.items():
        if ap.niveau in ("absent", "faible") or ap.valeurs_absentes:
            doc = Path(ap.meilleur.chemin).name if ap.meilleur else "—"
            print(f"  {ident:<14} {ap.niveau:<12} {'' if ap.score is None else f'{ap.score:.2f}':<6} {doc}"
                  + (f"  valeurs absentes : {', '.join(ap.valeurs_absentes)}" if ap.valeurs_absentes else ""))
    if a.json:
        c.enregistrer(a.json)
        print(f"Appuis écrits : {a.json}")
    return 0


def cmd_affaiblissements(a) -> int:
    from .affaiblissements import relever
    motif, ref = _motif_et_ref(a.spec)
    r = relever(a.fichiers, motif=motif, ids_connus=ref.ids if ref else None)
    print(f"{VERSION_LABEL} — {r.texte_resume()}")
    for o in r.occurrences[: a.limite]:
        ids = ", ".join(o.ids) if o.ids else "—"
        print(f"  {Path(o.fichier).name}, {o.reference} [{ids}] « {o.expression} » : {o.contexte}")
    if len(r.occurrences) > a.limite:
        print(f"  … {len(r.occurrences) - a.limite} autre(s)")
    return 0


def cmd_diff(a) -> int:
    from .controles import comparer
    from .grille import exporter_diff
    avant, _ = _lire(a.avant, a.spec, a.fournisseur, a.revision_avant, a.sans_profil)
    apres, _ = _lire(a.apres, a.spec, a.fournisseur, a.revision_apres, a.sans_profil)
    d = comparer(avant, apres)
    print(f"{VERSION_LABEL} — diff {d.avant} → {d.apres}")
    print(d.texte_resume())
    for c in d.changements:
        if c.nature == "statut":
            print(f"  {c.id:<14} {c.avant} → {c.apres}  ({c.sens})")
        elif c.nature in ("nouvelle", "disparue"):
            print(f"  {c.id:<14} {c.nature} {c.apres or c.avant}")
    if a.sortie:
        exporter_diff(d, a.sortie)
        print(f"Diff écrit : {a.sortie}")
    return 0


def cmd_statuts(a) -> int:
    from .statuts import DictionnaireStatuts
    dico = DictionnaireStatuts.charger()
    if a.ajouter:
        for item in a.ajouter:
            if "=" not in item:
                print(f"forme attendue : valeur=CODE (codes : C, PC, NC, NA) — reçu « {item} »", file=sys.stderr)
                return 2
            valeur, code = item.rsplit("=", 1)
            statut = Statut.depuis_code(code.strip().upper())
            if statut is Statut.INCONNU:
                print(f"code inconnu « {code} » (attendus : C, PC, NC, NA)", file=sys.stderr)
                return 2
            dico.ajouter(valeur.strip(), statut)
            print(f"ajouté : « {valeur.strip()} » → {statut.value}")
        return 0
    ajouts = dico.ajouts()
    print(f"Ajouts utilisateur : {len(ajouts)}")
    for k, v in sorted(ajouts.items()):
        print(f"  {k} → {v}")
    if a.tout:
        print("Dictionnaire complet :")
        for k, v in dico.valeurs().items():
            print(f"  {k} → {v}")
    return 0


def cmd_profils(a) -> int:
    from .profils import lister, Profil
    if a.proposer:
        if not a.fournisseur:
            print("--fournisseur est requis avec --proposer", file=sys.stderr)
            return 2
        from .lecture_ncm import lire_ncm, profil_depuis_colonnes, reconnaitre_colonnes
        from .statuts import DictionnaireStatuts
        from .tableaux import lire_tableaux
        motif, _ = _motif_et_ref(a.spec)
        from .identifiants import MotifIdentifiant
        motif = motif or MotifIdentifiant()
        dico = DictionnaireStatuts.charger()
        for t in lire_tableaux(a.proposer):
            cols = reconnaitre_colonnes(t, motif, dico)
            if cols is None:
                continue
            p = profil_depuis_colonnes(a.fournisseur, cols)
            print(f"Profil proposé pour « {a.fournisseur} » depuis {t.emplacement} :")
            for role, h in p.colonnes.items():
                print(f"  {role:<14} ← « {h} »")
            if a.enregistrer:
                chemin = p.enregistrer()
                print(f"Profil enregistré : {chemin}")
            return 0
        print("aucun tableau de NCM reconnu dans ce fichier", file=sys.stderr)
        return 2
    profils = lister()
    print(f"Profils enregistrés : {len(profils)}")
    for p in profils:
        print(f"  {p.fournisseur} : " + ", ".join(f"{r} = « {h} »" for r, h in p.colonnes.items()))
    return 0


def cmd_gui(a) -> int:
    from .gui import main as gui
    return gui()


def construire_parseur() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sillage", description="SILLAGE — Spécifications, Incomplétudes, Livrables : "
                                "Lecture, Analyse et Grille des Écarts")
    p.add_argument("--version", action="version", version=VERSION_LABEL)
    sp = p.add_subparsers(dest="commande")

    def commun(q, spec_obligatoire=False):
        q.add_argument("--spec", required=spec_obligatoire, help="référentiel : spécification (xlsx, csv, docx, txt, md, pdf)")
        q.add_argument("--fournisseur", default="", help="nom du fournisseur (profil et sortie)")
        q.add_argument("--sans-profil", action="store_true", help="ignorer le profil fournisseur enregistré")

    q = sp.add_parser("lire", help="lire une NCM et afficher son diagnostic")
    q.add_argument("ncm")
    commun(q)
    q.add_argument("--revision", default="")
    q.add_argument("--json", help="écrire la NCM normalisée (JSON)")
    q.add_argument("--aperçu", "--apercu", dest="aperçu", type=int, default=0, metavar="N", help="afficher les N premières lignes")
    q.set_defaults(fonction=cmd_lire)

    def options_rivage(q):
        q.add_argument("--index", help="index RIVAGE des documents du fournisseur (défaut : dossier de données SILLAGE, par fournisseur)")
        q.add_argument("--dossier", action="append", metavar="PREFIXE", help="portée : préfixe de chemin dans l'index (répétable)")
        q.add_argument("--documents", action="append", metavar="CHEMIN", help="portée : fichier indexé (répétable)")
        q.add_argument("--statuts", default="C", help="statuts NCM à vérifier (défaut : C ; ex. C,PC)")
        q.add_argument("--seuil-etaye", type=float, default=0.5, help="score de reranking minimal d'un appui « étayé »")
        q.add_argument("--seuil-faible", type=float, default=0.25, help="score minimal d'un appui « faible »")
        q.add_argument("--n-passages", type=int, default=3, help="passages conservés par exigence")

    q = sp.add_parser("grille", help="contrôler une NCM contre la spécification et écrire la grille des écarts")
    q.add_argument("--ncm", required=True)
    commun(q, spec_obligatoire=True)
    q.add_argument("--revision", default="")
    q.add_argument("--sortie", help="classeur de sortie (défaut : <ncm>_grille.xlsx)")
    q.add_argument("--verifier", action="store_true", help="lot 3 : vérifier les « C » dans la spécification fournisseur (RIVAGE)")
    q.add_argument("--appuis", metavar="JSON", help="lot 3 : reprendre une campagne d'appuis enregistrée par « appui --json »")
    q.add_argument("--affaiblissements", action="append", metavar="FICHIER", help="lot 3 : documents du fournisseur à passer au crible lexical (répétable)")
    options_rivage(q)
    q.set_defaults(fonction=cmd_grille)

    q = sp.add_parser("indexer", help="indexer les documents d'un fournisseur avec le moteur de RIVAGE")
    q.add_argument("dossier", help="dossier des documents du fournisseur")
    q.add_argument("--index", help="dossier de l'index (défaut : dossier de données SILLAGE, par fournisseur)")
    q.add_argument("--fournisseur", default="")
    q.add_argument("--sans-ocr", action="store_true", help="pas d'OCR de secours")
    q.set_defaults(fonction=cmd_indexer)

    q = sp.add_parser("appui", help="lot 3 : vérifier que les « C » sont étayés par la spécification fournisseur")
    q.add_argument("--ncm", required=True)
    commun(q, spec_obligatoire=True)
    q.add_argument("--revision", default="")
    options_rivage(q)
    q.add_argument("--json", help="enregistrer la campagne d'appuis (réutilisable par « grille --appuis »)")
    q.set_defaults(fonction=cmd_appui)

    q = sp.add_parser("affaiblissements", help="lot 3 : expressions d'affaiblissement dans les documents du fournisseur")
    q.add_argument("fichiers", nargs="+")
    q.add_argument("--spec", help="référentiel, pour rattacher les occurrences aux exigences")
    q.add_argument("--limite", type=int, default=50, help="occurrences affichées")
    q.set_defaults(fonction=cmd_affaiblissements)

    q = sp.add_parser("diff", help="comparer deux révisions de NCM")
    q.add_argument("--avant", required=True)
    q.add_argument("--apres", required=True)
    commun(q)
    q.add_argument("--revision-avant", default="")
    q.add_argument("--revision-apres", default="")
    q.add_argument("--sortie", help="classeur de sortie")
    q.set_defaults(fonction=cmd_diff)

    q = sp.add_parser("statuts", help="dictionnaire des statuts (afficher, enrichir)")
    q.add_argument("--ajouter", action="append", metavar="VALEUR=CODE", help="ex. « Full compliance=C »")
    q.add_argument("--tout", action="store_true", help="afficher le dictionnaire complet")
    q.set_defaults(fonction=cmd_statuts)

    q = sp.add_parser("profils", help="profils fournisseurs (lister, proposer depuis une NCM)")
    q.add_argument("--proposer", metavar="NCM", help="proposer un profil depuis cette NCM")
    q.add_argument("--fournisseur", default="")
    q.add_argument("--spec", help="référentiel, pour le motif d'identifiant")
    q.add_argument("--enregistrer", action="store_true", help="enregistrer le profil proposé")
    q.set_defaults(fonction=cmd_profils)

    q = sp.add_parser("gui", help="interface graphique")
    q.set_defaults(fonction=cmd_gui)

    q = sp.add_parser("version")
    q.set_defaults(fonction=lambda a: (print(VERSION_LABEL), 0)[1])
    return p


def main(argv=None) -> int:
    p = construire_parseur()
    a = p.parse_args(argv)
    if not a.commande:
        p.print_help()
        return 0
    try:
        return a.fonction(a)
    except (ValueError, OSError, ImportError) as e:
        print(f"erreur : {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
