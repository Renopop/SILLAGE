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
donnent la même grille. Pour vérifier qu'un « conforme » est étayé par la
spécification du fournisseur, SILLAGE s'appuie sur le moteur de **RIVAGE**
(index, portée, recherche hybride, reranking) sans en réécrire une ligne.
Voir `docs/PLAN_SILLAGE.md`.

Auteur, créateur et développeur : Renaud Loison.

## Installation

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
:: moteur de RIVAGE (lot 3) : installé comme dépendance dans le même environnement
.venv\Scripts\python -m pip install chemin\vers\RIVAGE
```

À défaut d'installation, SILLAGE trouve RIVAGE dans le dossier désigné par
`SILLAGE_RIVAGE_DIR`, ou dans un dossier `RIVAGE` voisin du dépôt. La
passerelle d'IA se configure comme pour RIVAGE (`RIVAGE.env`,
`RIVAGE_keys.env`). Sans RIVAGE ni passerelle, les commandes `lire`,
`grille` (sans `--verifier`), `diff` et `affaiblissements` fonctionnent
entièrement.

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

:: lot 3 — indexer les documents du fournisseur (moteur de RIVAGE), puis vérifier que les « C » sont étayés
python -m sillage indexer D:\Fournisseurs\A\Documents --fournisseur "Fournisseur A"
python -m sillage appui --spec SPEC_exigences.xlsx --ncm NCM_fournisseur_revB.xlsx --fournisseur "Fournisseur A" --json appuis_revB.json

:: grille complète : contrôles, appuis rejoués depuis le JSON, affaiblissements dans la spécification fournisseur
python -m sillage grille --spec SPEC_exigences.xlsx --ncm NCM_fournisseur_revB.xlsx --fournisseur "Fournisseur A" ^
    --appuis appuis_revB.json --affaiblissements D:\Fournisseurs\A\Documents\Spec_fournisseur.docx --sortie grille_revB.xlsx

:: ou tout en une fois (--verifier interroge RIVAGE ; --index et --dossier pour un index RIVAGE existant)
python -m sillage grille --spec SPEC_exigences.xlsx --ncm NCM_fournisseur_revB.xlsx --fournisseur "Fournisseur A" --verifier

:: expressions d'affaiblissement (TBD, TBC, where practicable, sous réserve…) rattachées aux exigences
python -m sillage affaiblissements D:\Fournisseurs\A\Documents\Spec_fournisseur.docx --spec SPEC_exigences.xlsx
```

Les seuils d'appui (`--seuil-etaye 0.5 --seuil-faible 0.25`) dépendent du
reranker servi par la passerelle : à calibrer sur quelques exigences dont on
connaît la réponse, en s'aidant de la distribution des scores affichée.

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
