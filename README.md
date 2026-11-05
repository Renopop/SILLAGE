# SILLAGE

**Spécifications, Incomplétudes, Livrables : Lecture, Analyse et Grille des Écarts.**

*RIVAGE cherche, SILLAGE garde la trace.*

SILLAGE lit les matrices de non-conformité (NCM) que les fournisseurs
renvoient en réponse à une spécification, les ramène à un modèle pivot
commun quel que soit leur format, et produit la **grille des écarts** :
pour chaque exigence, le statut effectif, sa justification, l'état de
clôture de l'écart, et les anomalies (exigences sans réponse, identifiants
inconnus, doublons, incohérences, statuts non reconnus). Il compare aussi
deux révisions de NCM.

Tout le socle est déterministe : deux campagnes sur les mêmes fichiers
donnent la même grille. Les lots suivants (vérification des « conformes »
contre la spécification fournisseur, traçabilité vers la norme, fiches de
revue) s'appuieront sur RIVAGE. Voir `docs/PLAN_SILLAGE.md`.

Auteur, créateur et développeur : Renaud Loison.

## Installation

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Utilisation

```bat
:: lire une NCM et mesurer son exploitabilité
python -m sillage lire NCM_fournisseur_revB.xlsx --spec SPEC_exigences.xlsx --fournisseur "Fournisseur A"

:: grille des écarts (classeur Excel : Grille, Alertes, Résumé)
python -m sillage grille --spec SPEC_exigences.xlsx --ncm NCM_fournisseur_revB.xlsx --fournisseur "Fournisseur A" --sortie grille_revB.xlsx

:: comparer deux révisions
python -m sillage diff --spec SPEC_exigences.xlsx --avant NCM_revA.xlsx --apres NCM_revB.xlsx --sortie diff_A_B.xlsx

:: enrichir le dictionnaire des statuts
python -m sillage statuts --ajouter "Full compliance=C" --ajouter "Deviation requested=NC"

:: proposer puis enregistrer le profil d'un fournisseur
python -m sillage profils --proposer NCM_fournisseur_revB.xlsx --fournisseur "Fournisseur A" --spec SPEC_exigences.xlsx --enregistrer
```

Le référentiel (`--spec`) est la spécification : un tableur avec une colonne
d'identifiants (et, si possible, le texte de l'exigence et le paragraphe de
norme), ou un document dont les paragraphes commencent par l'identifiant.

Données utilisateur (dictionnaire des statuts, profils) :
`%LOCALAPPDATA%\sillage` sous Windows, `~/.sillage` ailleurs, ou le dossier
donné par `SILLAGE_HOME`.

## Tests

```bat
python -m pytest
```
