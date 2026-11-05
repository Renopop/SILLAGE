"""Lecture d'une NCM : du tableau brut aux lignes pivot.

Les colonnes se reconnaissent par leur **contenu** : la colonne des
identifiants est celle où le plus de cellules en contiennent ; celle des
statuts, celle où le plus de cellules sont reconnues par le dictionnaire ; la
justification est la colonne de texte le plus long parmi les autres. Les
colonnes optionnelles se reconnaissent par mots-clés d'en-tête. Un profil
fournisseur, s'il correspond aux en-têtes, l'emporte sur la reconnaissance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .identifiants import MotifIdentifiant
from .modele import (CHAMPS_OPTIONNELS, CHEMIN_PROFIL, CHEMIN_TABLEAU, Diagnostic, LigneNCM, NCM,
                     Origine, Statut)
from .profils import Profil, normaliser_en_tete
from .statuts import DictionnaireStatuts
from .tableaux import Tableau, lire_tableaux

SEUIL_ID = 0.5        # part minimale de lignes avec identifiant pour retenir une colonne (et un tableau)
SEUIL_STATUT = 0.4    # part minimale de valeurs reconnues pour retenir une colonne de statuts
LONGUEUR_ID_MAX = 80  # au-delà, une cellule qui cite un identifiant est du texte, pas une colonne d'identifiants

_MOTS_STATUT = ("statut", "status", "compliance", "conformite", "verdict", "result", "resultat", "reponse",
                "response", "answer", "position", "assessment")
_MOTS_JUSTIFICATION = ("justif", "rationale", "explanation", "explication", "argument", "evidence", "preuve")


@dataclass
class Colonnes:
    id: int
    statut: Optional[int] = None
    justification: Optional[int] = None
    autres: Dict[str, int] = field(default_factory=dict)     # action, echeance, reference, derogation, commentaire
    en_tetes: List[str] = field(default_factory=list)
    ligne_en_tete: Optional[int] = None                        # index dans tableau.lignes
    chemin: str = CHEMIN_TABLEAU

    def roles(self) -> Dict[str, str]:
        """Rôle -> en-tête retenu (pour le diagnostic et les profils)."""
        def h(i: Optional[int]) -> str:
            if i is None:
                return ""
            if i < len(self.en_tetes) and self.en_tetes[i]:
                return self.en_tetes[i]
            return f"colonne {i + 1}"
        out = {"id": h(self.id)}
        if self.statut is not None:
            out["statut"] = h(self.statut)
        if self.justification is not None:
            out["justification"] = h(self.justification)
        for role, i in self.autres.items():
            out[role] = h(i)
        return out


def _cellule(ligne: Sequence[str], i: Optional[int]) -> str:
    if i is None or i >= len(ligne):
        return ""
    return ligne[i]


def reconnaitre_colonnes(tableau: Tableau, motif: MotifIdentifiant, dico: DictionnaireStatuts,
                         profil: Optional[Profil] = None) -> Optional[Colonnes]:
    """Colonnes du tableau, ou None s'il ne ressemble pas à une NCM."""
    lignes = tableau.lignes
    if not lignes:
        return None
    nb = tableau.nb_colonnes

    # colonne des identifiants
    scores_id = []
    for c in range(nb):
        n = sum(1 for l in lignes if len(_cellule(l, c)) <= LONGUEUR_ID_MAX and motif.trouver(_cellule(l, c)))
        scores_id.append(n)
    col_id = max(range(nb), key=lambda c: scores_id[c])
    if scores_id[col_id] == 0:
        return None

    # ligne d'en-tête : première ligne dont la cellule d'identifiant n'en contient pas
    ligne_en_tete = None
    for k, l in enumerate(lignes[:3]):
        if not motif.trouver(_cellule(l, col_id)):
            ligne_en_tete = k
            break
    en_tetes = [_cellule(lignes[ligne_en_tete], c) for c in range(nb)] if ligne_en_tete is not None else [""] * nb
    donnees = [l for k, l in enumerate(lignes) if ligne_en_tete is None or k > ligne_en_tete]
    if not donnees:
        return None
    if scores_id[col_id] < SEUIL_ID * len(donnees):
        return None

    # profil fournisseur
    if profil is not None and profil.correspond(en_tetes):
        idx = profil.indices(en_tetes)
        autres = {r: i for r, i in idx.items() if r not in ("id", "statut", "justification")}
        return Colonnes(id=idx["id"], statut=idx.get("statut"), justification=idx.get("justification"),
                        autres=autres, en_tetes=en_tetes, ligne_en_tete=ligne_en_tete, chemin=CHEMIN_PROFIL)

    # colonne des statuts
    col_statut = None
    meilleur = 0
    for c in range(nb):
        if c == col_id:
            continue
        n = sum(1 for l in donnees if dico.reconnaitre(_cellule(l, c)) is not None and len(_cellule(l, c)) <= 60)
        if n > meilleur:
            meilleur, col_statut = n, c
    if col_statut is not None and meilleur < SEUIL_STATUT * len(donnees):
        col_statut = None
    if col_statut is None:
        # repli : un en-tête de statut explicite avec des valeurs courtes (vocabulaire inconnu du dictionnaire)
        for c, h in enumerate(en_tetes):
            nh = normaliser_en_tete(h)
            if c == col_id or not nh or not any(m in nh for m in _MOTS_STATUT):
                continue
            longueurs = [len(_cellule(l, c)) for l in donnees]
            if sum(longueurs) / len(longueurs) <= 25:
                col_statut = c
                break

    # colonnes optionnelles par en-tête
    autres: Dict[str, int] = {}
    pris = {col_id, col_statut}
    for role, mots in CHAMPS_OPTIONNELS.items():
        for c, h in enumerate(en_tetes):
            nh = normaliser_en_tete(h)
            if c in pris or not nh:
                continue
            if any(re.search(r"(?<![a-z])" + re.escape(normaliser_en_tete(m)) + r"(?![a-z])", nh) for m in mots):
                autres[role] = c
                pris.add(c)
                break

    # justification : colonne de texte le plus long parmi les autres (commentaire inclus si rien d'autre)
    col_just = None
    meilleure_longueur = 0.0
    for c in range(nb):
        if c in pris:
            continue
        longueurs = [len(_cellule(l, c)) for l in donnees]
        moy = sum(longueurs) / len(longueurs)
        if moy > meilleure_longueur:
            meilleure_longueur, col_just = moy, c
    if col_just is None and "commentaire" in autres:
        col_just = autres.pop("commentaire")
    elif col_just is not None and meilleure_longueur < 3:
        col_just = None
    # l'en-tête peut lever le doute : une colonne « justification/rationale » explicite prime
    for c, h in enumerate(en_tetes):
        nh = normaliser_en_tete(h)
        if c not in pris and any(m in nh for m in _MOTS_JUSTIFICATION):
            col_just = c
            break

    return Colonnes(id=col_id, statut=col_statut, justification=col_just, autres=autres,
                    en_tetes=en_tetes, ligne_en_tete=ligne_en_tete, chemin=CHEMIN_TABLEAU)


def revision_depuis_nom(nom: str) -> str:
    m = re.search(r"(?:rev|r[ée]v(?:ision)?|issue|iss|ed(?:ition)?|ind(?:ice)?|v)[ _.\-]*([A-Z]{1,2}\d{0,2}|\d{1,3}(?:\.\d+)?)(?![A-Za-z0-9])",
                  Path(nom).stem, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def lire_ncm(chemin, motif: Optional[MotifIdentifiant] = None, dico: Optional[DictionnaireStatuts] = None,
             profil: Optional[Profil] = None, fournisseur: str = "", revision: str = "",
             ids_referentiel: Optional[Sequence[str]] = None) -> NCM:
    """Lit un fichier de NCM et renvoie ses lignes pivot avec leur diagnostic."""
    motif = motif or MotifIdentifiant()
    dico = dico or DictionnaireStatuts.charger()
    chemin = Path(chemin)
    diag = Diagnostic()
    ncm = NCM(fournisseur=fournisseur, revision=revision or revision_depuis_nom(chemin.name),
              fichier=str(chemin), diagnostic=diag)

    tableaux = lire_tableaux(chemin)
    diag.tableaux_lus = len(tableaux)
    colonnes_retenues: Optional[Colonnes] = None
    for t in tableaux:
        cols = reconnaitre_colonnes(t, motif, dico, profil)
        if cols is None:
            continue
        diag.tableaux_retenus += 1
        if colonnes_retenues is None:
            colonnes_retenues = cols
            diag.chemin = cols.chemin
            diag.colonnes = cols.roles()
        _lire_lignes(t, cols, motif, dico, ncm, diag)

    if diag.tableaux_retenus == 0:
        diag.avertissements.append("aucun tableau ne contient une colonne d'identifiants d'exigences")
    if colonnes_retenues is not None and colonnes_retenues.statut is None:
        diag.avertissements.append("aucune colonne de statuts reconnue : toutes les lignes sont « ? » "
                                   "(enrichir le dictionnaire des statuts ou donner un profil)")
    if colonnes_retenues is not None and colonnes_retenues.justification is None:
        diag.avertissements.append("aucune colonne de justification reconnue")
    if ids_referentiel:
        ref = set(ids_referentiel)
        diag.ids_referentiel = len(ref)
        diag.ids_retrouves = len(ref & set(ncm.ids))
        if diag.ids_referentiel and diag.taux_couverture < 0.5:
            diag.avertissements.append("moins de la moitié des identifiants du référentiel sont cités : "
                                       "NCM partielle, motif d'identifiant inadapté, ou mauvais document")
    return ncm


def _lire_lignes(t: Tableau, cols: Colonnes, motif: MotifIdentifiant, dico: DictionnaireStatuts,
                 ncm: NCM, diag: Diagnostic) -> None:
    debut = 0 if cols.ligne_en_tete is None else cols.ligne_en_tete + 1
    for k in range(debut, len(t.lignes)):
        l = t.lignes[k]
        diag.lignes_total += 1
        cellule_id = _cellule(l, cols.id)
        ids = motif.extraire(cellule_id) if len(cellule_id) <= LONGUEUR_ID_MAX else []
        if not ids:
            continue
        diag.lignes_avec_id += 1
        brut_statut = _cellule(l, cols.statut)
        statut = dico.reconnaitre(brut_statut) if cols.statut is not None else None
        if statut is None:
            statut = Statut.INCONNU
            if brut_statut:
                diag.statuts_inconnus[brut_statut] = diag.statuts_inconnus.get(brut_statut, 0) + 1
        brut = {}
        for c in range(t.nb_colonnes):
            h = cols.en_tetes[c] if c < len(cols.en_tetes) and cols.en_tetes[c] else f"colonne {c + 1}"
            brut[h] = _cellule(l, c)
        champs = {role: _cellule(l, c) for role, c in cols.autres.items() if _cellule(l, c)}
        origine = Origine(t.fichier, t.emplacement, t.numeros[k] if k < len(t.numeros) else k + 1)
        for ident in ids:
            ncm.lignes.append(LigneNCM(
                id=ident, statut=statut, statut_brut=brut_statut,
                justification=_cellule(l, cols.justification), id_brut=cellule_id,
                champs=dict(champs), brut=brut, origine=origine, chemin=cols.chemin,
            ))
            diag.lignes_pivot += 1
            if statut is not Statut.INCONNU:
                diag.statuts_reconnus += 1


def profil_depuis_colonnes(fournisseur: str, cols: Colonnes) -> Profil:
    """Profil fournisseur proposé à partir des colonnes reconnues (à confirmer)."""
    return Profil(fournisseur=fournisseur, colonnes={r: h for r, h in cols.roles().items() if not h.startswith("colonne ")})
