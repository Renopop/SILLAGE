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
# La NCM se déclare pré-filtrée : « unless listed below, all other items can be assumed compliant ».
_PREFILTREE = re.compile(r"pre-?\s?filtered|assumed (?:to be )?compliant|deemed compliant|r[ée]put[ée]e?s? conformes?"
                         r"|pr[ée]-?\s?filtr[ée]e?|consid[ée]r[ée]e?s? (?:comme )?conformes?", re.IGNORECASE)
SEUIL_EXIGENCE = 0.5  # part des lignes dont la cellule répète le texte de l'exigence pour y voir la colonne « exigence »


def _debut(texte: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]", "", (texte or "").lower())[:n]


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
                         profil: Optional[Profil] = None,
                         textes_ref: Optional[Dict[str, str]] = None) -> Optional[Colonnes]:
    """Colonnes du tableau, ou None s'il ne ressemble pas à une NCM.

    `textes_ref` (identifiant → texte de l'exigence du référentiel) permet de
    reconnaître la colonne qui **répète l'exigence** (fréquente dans les NCM
    fournisseurs) : elle n'est jamais prise pour la justification."""
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
    # lignes logiques : une ligne vide jusqu'à la colonne des identifiants incluse est une
    # ligne de suite (cellule haute d'un PDF lu ligne par ligne), pas une ligne sans identifiant
    logiques = [l for l in donnees if any(_cellule(l, c) for c in range(col_id + 1))]
    if not logiques or scores_id[col_id] < SEUIL_ID * len(logiques):
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
    if col_statut is not None and meilleur < SEUIL_STATUT * len(logiques):
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

    # colonne qui répète le texte de l'exigence : reconnue par comparaison avec le référentiel
    if textes_ref:
        for c in range(nb):
            if c in pris:
                continue
            comparables = concordants = 0
            for l in donnees:
                ids = motif.extraire(_cellule(l, col_id)) if len(_cellule(l, col_id)) <= LONGUEUR_ID_MAX else []
                attendu = _debut(textes_ref.get(ids[0], "")) if ids else ""
                if not attendu or len(attendu) < 12:
                    continue
                comparables += 1
                trouve = _debut(_cellule(l, c))
                # une cellule haute lue ligne par ligne ne porte que le début du texte
                if len(trouve) >= 12 and (trouve.startswith(attendu) or attendu.startswith(trouve)):
                    concordants += 1
            if comparables and concordants >= SEUIL_EXIGENCE * comparables:
                autres["exigence"] = c
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
    # le mot-clé doit commencer un « mot » du nom (pas « div » ni « valve ») ; « v » seul exige un séparateur ou un chiffre juste après
    m = re.search(r"(?<![A-Za-z])(?:rev|r[ée]v(?:ision)?|issue|iss|ed(?:ition)?|ind(?:ice)?|v(?=[ _.\-]*\d))[ _.\-]*"
                  r"([A-Z]{1,2}\d{0,2}|\d{1,3}(?:\.\d+)?)(?![A-Za-z0-9])", Path(nom).stem, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def lire_ncm(chemin, motif: Optional[MotifIdentifiant] = None, dico: Optional[DictionnaireStatuts] = None,
             profil: Optional[Profil] = None, fournisseur: str = "", revision: str = "",
             ids_referentiel: Optional[Sequence[str]] = None,
             textes_referentiel: Optional[Dict[str, str]] = None, ocr: bool = False) -> NCM:
    """Lit un fichier de NCM et renvoie ses lignes pivot avec leur diagnostic.

    `textes_referentiel` (identifiant → texte) sert à reconnaître la colonne
    qui répète l'exigence. Si le document se déclare pré-filtré (« unless
    listed below, all other items can be assumed compliant »), le diagnostic
    le note et `ncm.prefiltree` est posé : les contrôles traitent alors les
    exigences absentes comme conformes implicites. `ocr` : OCR de secours
    des pages scannées d'une NCM PDF (appels payants) ; un OCR indisponible
    ou incomplet est signalé dans le diagnostic, pas levé."""
    motif = motif or MotifIdentifiant()
    dico = dico or DictionnaireStatuts.charger()
    chemin = Path(chemin)
    diag = Diagnostic()
    ncm = NCM(fournisseur=fournisseur, revision=revision or revision_depuis_nom(chemin.name),
              fichier=str(chemin), diagnostic=diag)

    try:
        tableaux = lire_tableaux(chemin, ocr=ocr)
    except RuntimeError as exc:   # OCR indisponible ou incomplet : on lit ce qui se lit sans lui
        diag.avertissements.append(str(exc))
        tableaux = lire_tableaux(chemin)
        ocr = False
    diag.tableaux_lus = len(tableaux)
    diag.tableaux_ocr = sum(1 for t in tableaux if t.ocr)
    colonnes_retenues: Optional[Colonnes] = None
    for t in tableaux:
        cols = reconnaitre_colonnes(t, motif, dico, profil, textes_referentiel)
        if cols is None:
            continue
        diag.tableaux_retenus += 1
        if colonnes_retenues is None:
            colonnes_retenues = cols
            diag.chemin = cols.chemin
            diag.colonnes = cols.roles()
        _lire_lignes(t, cols, motif, dico, ncm, diag)

    diag.prefiltree = ncm.prefiltree = _declaree_prefiltree(chemin, tableaux, ocr)
    if diag.tableaux_retenus == 0:
        if chemin.suffix.lower() == ".pdf":
            from .textes import pdf_sans_texte
            if pdf_sans_texte(chemin):
                diag.avertissements.append(
                    "PDF sans couche texte (document scanné) : " +
                    ("l'OCR de secours n'y a reconstruit aucun tableau exploitable" if ocr else
                     "rien n'est lisible sans OCR — activer l'OCR de secours (--ocr, case « OCR de secours ») "
                     "ou demander la NCM sous forme de tableur ou de PDF texte"))
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


def _declaree_prefiltree(chemin: Path, tableaux: Sequence[Tableau], ocr: bool = False) -> bool:
    """Vrai si le document annonce que les exigences non listées sont conformes
    (phrase cherchée dans les cellules des tableaux et, pour un PDF ou un
    Word, dans le texte hors tableaux)."""
    textes = [c for t in tableaux for l in t.lignes for c in l if c]
    if chemin.suffix.lower() in (".pdf", ".docx"):
        try:
            from .textes import paragraphes
            textes.extend(p.texte for p in paragraphes(chemin, ocr=ocr))
        except Exception:   # un document illisible hors tableaux n'empêche pas la lecture
            pass
    return any(_PREFILTREE.search(x) for x in textes)


def _continuation(l: Sequence[str], cols: Colonnes) -> bool:
    """Ligne de tableau qui prolonge la précédente : rien jusqu'à la colonne
    des identifiants incluse, au moins une cellule remplie, et aucune cellule
    qui soit un titre de section."""
    from .textes import est_titre
    if any(_cellule(l, c) for c in range(cols.id + 1)):   # cellule d'identifiant remplie sans identifiant : note, pas suite
        return False
    if not any(l):
        return False
    return not any(est_titre(c) for c in l if c)


def _regrouper(t: Tableau, cols: Colonnes, motif: MotifIdentifiant) -> List[tuple]:
    """Lignes logiques (cellules, numéro de la première ligne) : une ligne sans
    identifiant qui suit une ligne avec identifiant, et qui en a l'allure
    (`_continuation`), lui est recollée cellule à cellule. C'est le cas des
    cellules hautes des PDF sans quadrillage (« Partially » sur une ligne,
    « compliant » sur la suivante) et des tableaux reconstruits par l'OCR."""
    debut = 0 if cols.ligne_en_tete is None else cols.ligne_en_tete + 1
    nb = t.nb_colonnes
    groupes: List[tuple] = []          # [cellules, numéro, a_id]
    for k in range(debut, len(t.lignes)):
        l = list(t.lignes[k]) + [""] * (nb - len(t.lignes[k]))
        num = t.numeros[k] if k < len(t.numeros) else k + 1
        cid = _cellule(l, cols.id)
        a_id = len(cid) <= LONGUEUR_ID_MAX and bool(motif.trouver(cid))
        if not a_id and groupes and groupes[-1][2] and _continuation(l, cols):
            prec = groupes[-1][0]
            for c in range(nb):
                if l[c]:
                    prec[c] = (prec[c] + " " + l[c]).strip() if prec[c] else l[c]
            continue
        groupes.append([l, num, a_id])
    return [(g[0], g[1]) for g in groupes]


def _lire_lignes(t: Tableau, cols: Colonnes, motif: MotifIdentifiant, dico: DictionnaireStatuts,
                 ncm: NCM, diag: Diagnostic) -> None:
    for l, numero in _regrouper(t, cols, motif):
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
        origine = Origine(t.fichier, t.emplacement, numero)
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
