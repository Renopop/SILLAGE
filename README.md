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
spécification du fournisseur, SILLAGE utilise le moteur de **RIVAGE**
(index, portée, recherche hybride, reranking), **embarqué** dans
`sillage/moteur/` : SILLAGE est autonome, aucune installation de RIVAGE
n'est nécessaire sur le poste. Voir `docs/PLAN_SILLAGE.md`, et la
présentation `docs/SILLAGE_comment_ca_marche_principes.pptx` (principes,
étapes, règles et interface ; générée par `docs/outils/make_deck_sillage.js`).

Auteur, créateur et développeur : Renaud Loison.

## Installation

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

La passerelle d'IA (embedding, reranking, OCR) se configure comme pour
RIVAGE, avec les mêmes fichiers posés à côté de SILLAGE : `RIVAGE.env`
(options) et `RIVAGE_keys.env` (clés, jamais versionné). Les mêmes clés
servent aux deux outils. Sans passerelle, les commandes `lire`, `grille`
(sans `--verifier`), `diff` et `affaiblissements` fonctionnent entièrement ;
`appui` rend des « non vérifié » (recherche lexicale seule).

Le moteur embarqué est une copie de RIVAGE (version et commit dans
`sillage/moteur/VERSION_RIVAGE.txt`). Pour reprendre une version plus
récente : `python outils/synchroniser_rivage.py chemin\vers\RIVAGE`.

## Interface graphique

`launch.bat` (ou `python sillage_app.py`) ouvre l'interface : un projet
(spécification, fournisseur, NCM, dossier des documents du fournisseur),
cinq étapes (lire la NCM, grille des écarts, indexer les documents,
vérifier les « C », affaiblissements), la comparaison avec une révision
antérieure, la grille à l'écran colorée par statut avec filtres et volet de
détail, les onglets diagnostic / résumé / journal, et les exports Excel.
Les valeurs de statut inconnues du dictionnaire sont proposées à
l'enrichissement dès la lecture ; le profil des colonnes d'un fournisseur se
confirme une fois et se mémorise.

Exécutable autonome (aucun Python sur le poste) : `build_exe.bat` produit
`dist\SILLAGE\SILLAGE.exe`, à copier tel quel avec `RIVAGE_keys.env` à côté.

## Ligne de commande

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

:: lot 4 — norme : indexer la CS-25, proposer un paragraphe par exigence, relire le classeur, puis l'appliquer à la grille
python -m sillage indexer D:\Normes\CS-25 --index D:\Normes\index_CS-25
python -m sillage tracer --spec SPEC_exigences.xlsx --index D:\Normes\index_CS-25 --json trac.json --xlsx trac_relecture.xlsx
python -m sillage grille --spec SPEC_exigences.xlsx --ncm NCM_fournisseur_revB.xlsx --tracabilite trac_relecture.xlsx ^
    --norme-documents D:\Normes\CS-25\CS-25_Amdt27.pdf --sortie grille_revB.xlsx
python -m sillage couverture --spec SPEC_exigences.xlsx --tracabilite trac_relecture.xlsx --norme-documents D:\Normes\CS-25\CS-25_Amdt27.pdf
```

Tout écart sur une exigence tracée vers la norme porte l'alerte « impact
norme » et figure dans la feuille « Impact norme » du classeur : c'est la
liste des risques de certification. La feuille « Couverture norme » donne les
paragraphes cités par la norme et les exigences rattachées à chacun.

```bat
:: lot 5 — fiches de revue des écarts (NC, PC), rédigées par le modèle de la passerelle : dossier Word
python -m sillage fiches --spec SPEC_exigences.xlsx --ncm NCM_fournisseur_revB.xlsx --fournisseur "Fournisseur A" ^
    --appuis appuis_revB.json --tracabilite trac_relecture.xlsx --json fiches_revB.json --docx revue_revB.docx
python -m sillage grille --spec SPEC_exigences.xlsx --ncm NCM_fournisseur_revB.xlsx --fiches fiches_revB.json --sortie grille_revB.xlsx
```

Chaque fiche (écart, risque, questions pour la revue) ne s'appuie que sur la
ligne de NCM et les passages cités ; elle ne contient pas de verdict, qui
reste celui de l'ingénieur.

```bat
:: lot 7 — capitaliser la campagne dans la mémoire des projets, et annoter la grille avec l'historique
python -m sillage grille --spec SPEC_exigences.xlsx --ncm NCM_fournisseur_revB.xlsx --fournisseur "Fournisseur A" ^
    --projet "PROJET-2026-12" --capitaliser --signee --memoire --sortie grille_revB.xlsx
python -m sillage memoire contestees --minimum 2
python -m sillage memoire acceptes --norme "CS 25.1309"
python -m sillage memoire fournisseurs
python -m sillage memoire chercher "DAL C"
python -m sillage memoire exporter --sortie capitalisation.xlsx
```

La mémoire (SQLite dans le dossier de données) retient chaque campagne par
projet, fournisseur et révision ; la grille d'un nouveau projet indique,
exigence par exigence, si elle a déjà été contestée ailleurs et si un écart
a déjà été accepté, avec le projet, le fournisseur et la dérogation.

Les seuils d'appui (`--seuil-etaye 0.5 --seuil-faible 0.25`) dépendent du
reranker servi par la passerelle : à calibrer sur quelques exigences dont on
connaît la réponse, en s'aidant de la distribution des scores affichée.

Le référentiel (`--spec`) est la spécification : un tableur avec une colonne
d'identifiants (et, si possible, le texte de l'exigence et le paragraphe de
norme), ou un document (PDF de texte, Word, texte) dont les paragraphes
commencent par l'identifiant — nu ou entre crochets, seul sur sa ligne ou
suivi du texte ; le texte court jusqu'au prochain identifiant ou titre de
section. L'identifiant peut avoir un préfixe composé (« [REQ_B2-3_15] ») :
le numéro est le dernier groupe de chiffres, et toutes les écritures du
fournisseur (« REQ-B2-3-015 », « req_b2_3_15 ») se ramènent à celle de la
spécification. Un PDF sans couche texte (scan) est signalé comme tel ; avec
l'**OCR de secours** (`--ocr`, case « OCR de secours » dans l'interface), ses
pages sont lues par le modèle Vision de la passerelle, comme dans
l'indexeur de RIVAGE (même clé `RIVAGE_SYNTH_KEY`, même cache de pages ;
chaque page est un appel payant, seules les pages sans texte partent en
OCR). Pour une NCM scannée, les tableaux sont reconstruits depuis les
lignes à barres verticales du texte rendu, et le diagnostic indique
combien de tableaux viennent de l'OCR, à relire.

Les tableaux sont lus **ligne par ligne** : dans un PDF sans quadrillage,
les colonnes sont déduites des alignements du texte et chaque ligne de texte
devient une ligne de tableau ; une cellule haute (le statut « Partially
compliant » écrit sur deux lignes, ou centré dans une ligne haute) ressort
en lignes de suite, qui sont recollées à la ligne de l'identifiant avant
toute lecture. Même recollage dans un tableur ou un tableau OCR.

Une NCM qui se déclare pré-filtrée (« unless listed below, all other items
can be assumed compliant ») est reconnue : les exigences absentes reçoivent
le statut C avec l'alerte « conforme implicite » au lieu de « trou »
(`--prefiltree` / `--non-prefiltree` pour forcer ; case à cocher dans
l'interface).

Données utilisateur (dictionnaire des statuts, profils) :
`%LOCALAPPDATA%\sillage` sous Windows, `~/.sillage` ailleurs, ou le dossier
donné par `SILLAGE_HOME`.

## Tests

```bat
python -m pytest
```
