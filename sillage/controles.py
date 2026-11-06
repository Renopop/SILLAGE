"""Contrôles par identifiant et grille des écarts.

Tout ici est déterministe : jointure entre le référentiel et la NCM, trous,
orphelins, doublons, incohérences, statuts inconnus, clôture des écarts, diff
entre deux révisions. Deux campagnes sur les mêmes fichiers donnent la même
grille.

Les lots suivants s'y reportent par des objets optionnels, sans changer la
jointure : appuis et affaiblissements (lot 3 : alertes `c_sans_appui`,
`c_faible`, `valeurs_absentes`, `affaiblissement`), traçabilité vers la norme
(lot 4 : champ `norme` et alerte `impact_norme`), fiches de revue (lot 5 :
texte de la fiche sur la ligne), mémoire des projets (lot 7 : historique de
l'exigence, reporté par `memoire.Memoire.annoter`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .identifiants import trier
from .modele import GRAVITE, LigneNCM, NCM, Statut
from .referentiel import Referentiel

# types d'alertes
TROU = "trou"                   # exigence sans ligne de NCM
ORPHELIN = "orphelin"           # ligne de NCM citant un identifiant absent du référentiel
DOUBLON = "doublon"             # plusieurs lignes, même statut
INCOHERENCE = "incoherence"     # plusieurs lignes, statuts différents
STATUT_INCONNU = "statut_inconnu"
ECART_OUVERT = "ecart_ouvert"   # NC ou PC sans action ni dérogation
# lot 3
C_SANS_APPUI = "c_sans_appui"           # « conforme » sans passage comparable dans la spécification fournisseur
C_FAIBLE = "c_faible"                   # « conforme » avec un appui de score faible
VALEURS_ABSENTES = "valeurs_absentes"   # exigence chiffrée dont aucune valeur n'est citée par les passages
AFFAIBLISSEMENT = "affaiblissement"     # expression d'affaiblissement à côté de l'identifiant
# lot 4
IMPACT_NORME = "impact_norme"           # écart (NC, PC, trou, statut inconnu) sur une exigence tracée vers la norme

# clôture d'un écart
CLOTURE_DEROGATION = "dérogation"
CLOTURE_ACTION = "action"
CLOTURE_OUVERT = "ouvert"


@dataclass
class Alerte:
    type: str
    id: str
    message: str
    origine: str = ""


@dataclass
class LigneGrille:
    id: str
    texte: str = ""
    statut: Statut = Statut.INCONNU
    statut_brut: str = ""
    justification: str = ""
    cloture: str = ""                  # pour NC / PC : dérogation, action, ouvert
    champs: Dict[str, str] = field(default_factory=dict)
    chemin: str = ""
    origine: str = ""
    norme: str = ""
    alertes: List[str] = field(default_factory=list)
    dans_referentiel: bool = True
    nb_lignes_ncm: int = 0
    # lot 3 : appui dans la spécification fournisseur (moteur embarqué) et affaiblissements (lexical)
    appui_niveau: str = ""
    appui_score: Optional[float] = None
    appui_passage: str = ""
    appui_document: str = ""
    valeurs_absentes: List[str] = field(default_factory=list)
    affaiblissements: List[str] = field(default_factory=list)
    # lot 5 : fiche de revue rédigée (texte du modèle), vide sinon
    fiche: str = ""
    fiche_erreur: str = ""
    # lot 7 : historique de l'exigence dans la mémoire des projets
    historique: str = ""
    contestations: int = 0
    ecart_accepte_avant: bool = False


@dataclass
class Grille:
    fournisseur: str = ""
    revision: str = ""
    referentiel: str = ""
    ncm: str = ""
    lignes: List[LigneGrille] = field(default_factory=list)
    alertes: List[Alerte] = field(default_factory=list)

    def resume(self) -> Dict[str, int]:
        r: Dict[str, int] = {"exigences": sum(1 for l in self.lignes if l.dans_referentiel)}
        for s in Statut:
            r[s.value] = sum(1 for l in self.lignes if l.dans_referentiel and l.statut is s)
        for t in (TROU, ORPHELIN, DOUBLON, INCOHERENCE, STATUT_INCONNU, ECART_OUVERT,
                  C_SANS_APPUI, C_FAIBLE, VALEURS_ABSENTES, AFFAIBLISSEMENT, IMPACT_NORME):
            r[t] = sum(1 for a in self.alertes if a.type == t)
        r["tracees"] = sum(1 for l in self.lignes if l.dans_referentiel and l.norme)
        r["deja_contestees"] = sum(1 for l in self.lignes if l.dans_referentiel and l.contestations)
        r["deja_acceptees"] = sum(1 for l in self.lignes if l.dans_referentiel and l.ecart_accepte_avant)
        r["ecarts"] = sum(1 for l in self.lignes if l.dans_referentiel and l.statut in (Statut.PARTIEL, Statut.NON_CONFORME))
        r["couverts"] = r["exigences"] - r[TROU]
        return r

    def texte_resume(self) -> str:
        r = self.resume()
        n = r["exigences"] or 1
        lignes = [
            f"Exigences : {r['exigences']} — couvertes : {r['couverts']} ({r['couverts'] / n:.0%})",
            f"Conformes : {r['C']}, partielles : {r['PC']}, non conformes : {r['NC']}, "
            f"non applicables : {r['NA']}, statut inconnu : {r['?']}",
            f"Trous : {r[TROU]}, orphelins : {r[ORPHELIN]}, doublons : {r[DOUBLON]}, "
            f"incohérences : {r[INCOHERENCE]}, écarts ouverts : {r[ECART_OUVERT]}",
        ]
        if any(l.appui_niveau for l in self.lignes):
            lignes.append(f"« Conformes » sans appui : {r[C_SANS_APPUI]}, appui faible : {r[C_FAIBLE]}, "
                          f"valeurs chiffrées absentes : {r[VALEURS_ABSENTES]}")
        if r[AFFAIBLISSEMENT]:
            lignes.append(f"Exigences avec affaiblissement à proximité : {r[AFFAIBLISSEMENT]}")
        if r["tracees"]:
            lignes.append(f"Exigences tracées vers la norme : {r['tracees']} — écarts à impact norme : {r[IMPACT_NORME]}")
        if r["deja_contestees"] or r["deja_acceptees"]:
            lignes.append(f"Mémoire des projets : {r['deja_contestees']} exigence(s) déjà contestée(s) ailleurs, "
                          f"{r['deja_acceptees']} avec un écart déjà accepté")
        return "\n".join(lignes)

    def impacts_norme(self) -> List["LigneGrille"]:
        return [l for l in self.lignes if IMPACT_NORME in l.alertes]


def cloture_de(ligne: LigneNCM) -> str:
    if ligne.statut not in (Statut.PARTIEL, Statut.NON_CONFORME):
        return ""
    if ligne.champs.get("derogation"):
        return CLOTURE_DEROGATION
    if ligne.champs.get("action"):
        return CLOTURE_ACTION
    return CLOTURE_OUVERT


def controler(ref: Referentiel, ncm: NCM, campagne=None, releve=None, tracabilite=None, fiches=None) -> Grille:
    """Grille des écarts. `campagne` (appui.Campagne) et `releve`
    (affaiblissements.Releve) sont les résultats optionnels du lot 3 ;
    `tracabilite` (norme.Tracabilite) celui du lot 4, reporté dans le champ
    `norme` des exigences avant les contrôles ; `fiches` (fiches.Fiches)
    celui du lot 5 (texte de la fiche reporté sur la ligne)."""
    if tracabilite is not None:
        from .norme import appliquer
        appliquer(ref, tracabilite)
    g = Grille(fournisseur=ncm.fournisseur, revision=ncm.revision, referentiel=ref.source, ncm=ncm.fichier)
    par_id = ncm.par_id()
    exigences = ref.par_id()
    appuis = campagne.appuis if campagne is not None else {}
    affaibl = releve.par_id() if releve is not None else {}
    fiches_par_id = fiches.fiches if fiches is not None else {}

    for ident in ref.ids:
        e = exigences[ident]
        lignes = par_id.get(ident, [])
        lg = LigneGrille(id=ident, texte=e.texte, norme=e.norme, nb_lignes_ncm=len(lignes))
        if not lignes:
            lg.statut = Statut.INCONNU
            lg.alertes.append(TROU)
            g.alertes.append(Alerte(TROU, ident, "exigence sans ligne dans la NCM"))
        else:
            _remplir(lg, lignes, g)
        if ident in appuis:
            _remplir_appui(lg, appuis[ident], g)
        if ident in affaibl:
            lg.affaiblissements = sorted({o.expression for o in affaibl[ident]})
            lg.alertes.append(AFFAIBLISSEMENT)
            g.alertes.append(Alerte(AFFAIBLISSEMENT, ident, "près de l'identifiant : " + ", ".join(f"« {x} »" for x in lg.affaiblissements),
                                    "; ".join(dict.fromkeys(f"{Path(o.fichier).name}, {o.reference}" for o in affaibl[ident]))))
        if lg.norme and lg.statut in (Statut.PARTIEL, Statut.NON_CONFORME, Statut.INCONNU):
            lg.alertes.append(IMPACT_NORME)
            g.alertes.append(Alerte(IMPACT_NORME, ident, f"{lg.statut.libelle} sur une exigence tracée vers {lg.norme}", lg.origine))
        if ident in fiches_par_id:
            lg.fiche = fiches_par_id[ident].texte
            lg.fiche_erreur = fiches_par_id[ident].erreur
        g.lignes.append(lg)

    for ident in trier([i for i in par_id if i not in exigences]):
        lignes = par_id[ident]
        lg = LigneGrille(id=ident, dans_referentiel=False, nb_lignes_ncm=len(lignes))
        _remplir(lg, lignes, g)
        lg.alertes.insert(0, ORPHELIN)
        g.alertes.append(Alerte(ORPHELIN, ident, "identifiant absent du référentiel", str(lignes[0].origine or "")))
        g.lignes.append(lg)
    return g


def _remplir_appui(lg: LigneGrille, a, g: Grille) -> None:
    from .appui import ABSENT, FAIBLE
    lg.appui_niveau = a.niveau
    lg.appui_score = a.score
    if a.meilleur is not None:
        lg.appui_passage = a.meilleur.texte
        lg.appui_document = Path(a.meilleur.chemin).name + (f", page {a.meilleur.page}" if a.meilleur.page else "")
    lg.valeurs_absentes = list(a.valeurs_absentes)
    if a.erreur:
        return
    if lg.statut is Statut.CONFORME:
        if a.niveau == ABSENT:
            lg.alertes.append(C_SANS_APPUI)
            g.alertes.append(Alerte(C_SANS_APPUI, lg.id, "déclaré conforme, aucun passage comparable dans la spécification fournisseur",
                                    lg.appui_document))
        elif a.niveau == FAIBLE:
            lg.alertes.append(C_FAIBLE)
            g.alertes.append(Alerte(C_FAIBLE, lg.id, f"déclaré conforme, appui de score faible ({a.score:.2f})", lg.appui_document))
    if a.valeurs_absentes and a.niveau != ABSENT:
        lg.alertes.append(VALEURS_ABSENTES)
        g.alertes.append(Alerte(VALEURS_ABSENTES, lg.id, "valeurs de l'exigence non citées par les passages : " +
                                ", ".join(a.valeurs_absentes), lg.appui_document))


def _remplir(lg: LigneGrille, lignes: List[LigneNCM], g: Grille) -> None:
    statuts = {l.statut for l in lignes}
    premiere = lignes[0]
    lg.chemin = premiere.chemin
    lg.origine = "; ".join(str(l.origine) for l in lignes if l.origine)
    lg.statut_brut = " | ".join(dict.fromkeys(l.statut_brut for l in lignes if l.statut_brut))
    lg.justification = "\n".join(dict.fromkeys(l.justification for l in lignes if l.justification))
    for l in lignes:
        for k, v in l.champs.items():
            if v and v not in lg.champs.get(k, ""):
                lg.champs[k] = (lg.champs[k] + " | " + v) if lg.champs.get(k) else v

    if len(statuts) > 1:
        lg.statut = Statut.INCONNU
        lg.alertes.append(INCOHERENCE)
        g.alertes.append(Alerte(INCOHERENCE, lg.id,
                                "statuts différents sur plusieurs lignes : " + ", ".join(s.value for s in statuts),
                                lg.origine))
    else:
        lg.statut = premiere.statut
        if len(lignes) > 1:
            lg.alertes.append(DOUBLON)
            g.alertes.append(Alerte(DOUBLON, lg.id, f"{len(lignes)} lignes pour la même exigence", lg.origine))
    if lg.statut is Statut.INCONNU and len(statuts) == 1:
        lg.alertes.append(STATUT_INCONNU)
        g.alertes.append(Alerte(STATUT_INCONNU, lg.id, f"statut non reconnu : « {lg.statut_brut} »", lg.origine))

    if lg.statut in (Statut.PARTIEL, Statut.NON_CONFORME):
        clotures = {cloture_de(l) for l in lignes if l.statut in (Statut.PARTIEL, Statut.NON_CONFORME)}
        if CLOTURE_DEROGATION in clotures:
            lg.cloture = CLOTURE_DEROGATION
        elif CLOTURE_ACTION in clotures:
            lg.cloture = CLOTURE_ACTION
        else:
            lg.cloture = CLOTURE_OUVERT
            lg.alertes.append(ECART_OUVERT)
            g.alertes.append(Alerte(ECART_OUVERT, lg.id, f"{lg.statut.libelle} sans action ni dérogation", lg.origine))


# --- diff entre deux révisions -----------------------------------------------------
@dataclass
class Changement:
    id: str
    nature: str            # nouvelle, disparue, statut, justification
    avant: str = ""
    apres: str = ""
    sens: str = ""         # pour « statut » : amélioration, régression, neutre
    origine: str = ""


@dataclass
class Diff:
    avant: str = ""
    apres: str = ""
    changements: List[Changement] = field(default_factory=list)

    def resume(self) -> Dict[str, int]:
        r = {"nouvelle": 0, "disparue": 0, "statut": 0, "justification": 0, "regression": 0, "amelioration": 0}
        for c in self.changements:
            r[c.nature] += 1
            if c.sens == "régression":
                r["regression"] += 1
            elif c.sens == "amélioration":
                r["amelioration"] += 1
        return r

    def texte_resume(self) -> str:
        r = self.resume()
        return (f"Nouvelles : {r['nouvelle']}, disparues : {r['disparue']}, statuts changés : {r['statut']} "
                f"(régressions : {r['regression']}, améliorations : {r['amelioration']}), "
                f"justifications réécrites : {r['justification']}")


def _statut_effectif(lignes: List[LigneNCM]) -> Statut:
    statuts = {l.statut for l in lignes}
    return lignes[0].statut if len(statuts) == 1 else Statut.INCONNU


def comparer(avant: NCM, apres: NCM) -> Diff:
    d = Diff(avant=f"{avant.fournisseur} {avant.revision}".strip() or avant.fichier,
             apres=f"{apres.fournisseur} {apres.revision}".strip() or apres.fichier)
    a, b = avant.par_id(), apres.par_id()
    for ident in trier(list(set(a) | set(b))):
        if ident not in a:
            d.changements.append(Changement(ident, "nouvelle", apres=_statut_effectif(b[ident]).value,
                                            origine=str(b[ident][0].origine or "")))
            continue
        if ident not in b:
            d.changements.append(Changement(ident, "disparue", avant=_statut_effectif(a[ident]).value))
            continue
        sa, sb = _statut_effectif(a[ident]), _statut_effectif(b[ident])
        if sa is not sb:
            ga, gb = GRAVITE[sa], GRAVITE[sb]
            sens = "régression" if gb > ga else ("amélioration" if gb < ga else "neutre")
            d.changements.append(Changement(ident, "statut", sa.value, sb.value, sens, str(b[ident][0].origine or "")))
        ja = "\n".join(l.justification for l in a[ident]).strip()
        jb = "\n".join(l.justification for l in b[ident]).strip()
        if ja != jb and (ja or jb):
            d.changements.append(Changement(ident, "justification", ja, jb, origine=str(b[ident][0].origine or "")))
    return d
