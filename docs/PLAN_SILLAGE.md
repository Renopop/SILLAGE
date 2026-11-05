# SILLAGE — plan de réalisation par lots

**SILLAGE** — Spécifications, Incomplétudes, Livrables : Lecture, Analyse et
Grille des Écarts.

*RIVAGE cherche, SILLAGE garde la trace.*

## 1. Le besoin

On fournit une spécification à un fournisseur. Il répond par sa propre
spécification, puis par une ou plusieurs **NCM** (non-compliance matrix),
tableaux reprenant la numérotation de nos exigences, qui convergent vers une
NCM finale signée. Il faut s'assurer :

1. de la **complétude** de la réponse : chaque exigence a une réponse, aucune
   ligne de NCM ne cite une exigence inconnue ;
2. de la **sincérité** des statuts : un « conforme » est étayé par la
   spécification fournisseur, un écart est justifié et suivi jusqu'à sa clôture ;
3. de la **conformité à la norme** fixée (par exemple CS-25) : les écarts sur
   des exigences tracées vers la norme sont des risques de certification ;
4. de la **traçabilité dans le temps** : ce qui change d'une révision de NCM à
   l'autre, et ce que la NCM signée engage pour la suite.

Les NCM varient d'un fournisseur à l'autre (colonnes, vocabulaire des
statuts, granularité), mais sont **toujours des tableaux** qui reprennent
**nos identifiants d'exigences**. SILLAGE ne dépend donc que de ce qui vient de
nous : les identifiants sont l'invariant, tout le reste s'y rattache.

## 2. Principes

- **Modèle pivot fixe** : une ligne de NCM, quel que soit le fournisseur,
  devient *identifiant, statut (C / PC / NC / NA), justification, champs
  optionnels (action, échéance, référence, dérogation)* ; les colonnes non
  interprétées sont conservées en brut.
- **Déterminisme d'abord** : la jointure par identifiant, les trous, les
  orphelins, les doublons, le diff entre révisions et l'état de clôture ne
  font appel à aucune IA. Deux campagnes sur les mêmes documents donnent la
  même grille.
- **Adaptation mémorisée** : les colonnes se reconnaissent par leur contenu ;
  ce qui ne se reconnaît pas est proposé, confirmé une fois et mémorisé dans
  un **profil fournisseur**. Le **dictionnaire des statuts** s'enrichit à la
  première rencontre d'une valeur inconnue.
- **Exploitabilité mesurée** : avant toute analyse, un diagnostic en quelques
  chiffres (identifiants retrouvés, statuts reconnus, chemin de lecture).
- **Chaque cellule sourcée** : fichier, feuille ou page, ligne ; chaque ligne
  de la grille porte son chemin d'origine (tableau, profil, extrait).
- **IA minimale**, comme RIVAGE : le modèle n'intervient que pour résumer un
  écart ou juger une paire (exigence, passage) ; il ne cherche jamais et ne
  décide jamais. Le verdict est celui de l'ingénieur.

## 3. Les lots

| Lot | Contenu | IA | État |
|-----|---------|----|------|
| 0 | Squelette : dépôt, version, modèle pivot, configuration, tests | non | fait (0.1.0) |
| 1 | Lecture des NCM : tableaux xlsx / docx / pdf / csv, repérage des identifiants (plages, sous-identifiants), reconnaissance des colonnes par contenu, dictionnaire des statuts, profil fournisseur, diagnostic d'exploitabilité, NCM normalisée (JSON) | non | fait (0.1.0) |
| 2 | Référentiel et contrôles : lecture de la spécification (tableur ou document), trous, orphelins, doublons, incohérences, diff entre révisions, clôture des écarts, **grille des écarts** (xlsx) et ligne de commande | non | fait (0.1.0) |
| 3 | Vérification des « C » avec RIVAGE : pour chaque exigence déclarée conforme, le passage de la spécification fournisseur qui l'étaye et son score ; « C » sans appui en tête de revue ; campagne lexicale des affaiblissements (TBD, TBC, where practicable…) | non (scores) | à faire |
| 4 | Traçabilité vers la norme : pré-remplissage par RIVAGE (exigence → paragraphe), relecture, figement ; liste des NC à impact certification par jointure | non (scores) | à faire |
| 5 | Fiches de revue pour les NC et PC : exigence, ligne de NCM, passages, résumé de l'écart et du risque — seule étape générative, verdict laissé à l'ingénieur | oui, bornée | à faire |
| 6 | Interface graphique : ouvrir une spec et des NCM, voir la grille, confirmer un profil, enrichir les statuts, exporter | non | à faire |
| 7 | Capitalisation : une base par projet, questions transverses entre projets (exigences souvent contestées, écarts déjà acceptés) | non | à faire |

Les lots 1 et 2 forment déjà un outil utile et entièrement déterministe ;
RIVAGE n'entre qu'au lot 3, en dépendance optionnelle.

## 4. Lot 1 — lecture des NCM

**Entrée** : un fichier (xlsx, xlsm, docx, pdf, csv), un motif d'identifiant
(déduit du référentiel si fourni, sinon motif générique), le dictionnaire des
statuts, éventuellement un profil fournisseur.

**Tableaux** : chaque feuille d'un classeur, chaque table d'un Word, chaque
tableau de chaque page d'un PDF. Les cellules fusionnées d'un classeur sont
dépliées (la valeur est recopiée dans toutes les cellules de la zone). Les
tableaux PDF coupés sur plusieurs pages sont recollés quand le nombre de
colonnes est le même et que l'en-tête se répète.

**Colonnes, par contenu** : la colonne des identifiants est celle où le plus de
cellules contiennent un identifiant ; la colonne des statuts est celle où le
plus de cellules sont reconnues par le dictionnaire ; la justification est la
colonne de texte le plus long parmi les autres. Les colonnes optionnelles
(action, échéance, référence, dérogation, commentaire) sont reconnues par
mots-clés d'en-tête. La ligne d'en-tête est la première ligne dont la cellule
d'identifiant n'en contient pas.

**Profil fournisseur** : le choix de colonnes (par en-tête) est mémorisé par
fournisseur ; à la révision suivante, si les en-têtes correspondent, le profil
s'applique (chemin « profil ») ; sinon la reconnaissance par contenu reprend
et le profil est à confirmer de nouveau.

**Identifiants** : normalisés (préfixe en majuscules, séparateur unique,
zéros de tête retirés du numéro, suffixe conservé) ; une cellule « REQ-100 à
REQ-120 » se déplie en autant de lignes pivot ; « REQ-100a » est un
identifiant à part entière ; une cellule qui cite plusieurs identifiants donne
plusieurs lignes.

**Statuts** : dictionnaire de synonymes (français, anglais, abréviations)
vers quatre valeurs pivot ; une valeur inconnue donne le statut « ? » et
figure dans le diagnostic pour enrichissement.

**Diagnostic** : lignes lues, lignes avec identifiant, statuts reconnus,
colonnes retenues, chemin, part des identifiants du référentiel retrouvés
(si référentiel), valeurs de statut inconnues, avertissements.

## 5. Lot 2 — référentiel, contrôles, grille

**Référentiel** : la spécification sous forme de liste d'exigences. Depuis un
tableur (colonnes identifiant, texte, éventuellement paragraphe de norme) ou
depuis un document (chaque paragraphe commençant par un identifiant est une
exigence ; les paragraphes suivants sans identifiant la complètent).

**Contrôles par identifiant** :

- *trou* : exigence sans ligne de NCM ;
- *orphelin* : ligne de NCM citant un identifiant absent du référentiel ;
- *doublon* : plusieurs lignes pour une exigence avec le même statut ;
- *incohérence* : plusieurs lignes pour une exigence avec des statuts
  différents (statut effectif « ? », à trancher) ;
- *statut inconnu* : valeur non reconnue par le dictionnaire ;
- *clôture* : pour chaque NC ou PC, « dérogation » si une référence de
  dérogation existe, « action » si une action est décrite, sinon « ouvert ».

**Diff entre révisions** : exigences apparues, disparues, statut changé (avec
le sens : amélioration ou régression), justification réécrite.

**Grille des écarts** : un classeur avec une ligne par exigence (identifiant,
texte, statut effectif, statut brut, justification, clôture, action, échéance,
référence, dérogation, chemin, origine, alertes, norme), une feuille des
alertes, une feuille de résumé. Couleurs par statut, filtres, volets figés.

## 6. Conventions

- Python 3.9 et plus ; dépendances : openpyxl, python-docx, pdfplumber.
- Tout en français (noms, messages, documentation), comme RIVAGE.
- Aucune donnée fournisseur dans le dépôt : les tests fabriquent leurs
  propres fichiers.
- Dossier de données utilisateur : `%LOCALAPPDATA%\sillage` (Windows) ou
  `~/.sillage`, surchargeable par `SILLAGE_HOME` : `statuts.json` (ajouts au
  dictionnaire), `profils/<fournisseur>.json`.
- Numérotation MAJEUR.MINEUR.CORRECTIF dans `sillage/version.py` ; historique
  dans `CHANGELOG.md`.

## 7. Questions ouvertes

- La NCM finale liste-t-elle toutes les exigences ou seulement les écarts ?
  Selon la réponse, un trou est une anomalie ou vaut « conforme par silence »
  (option `--silence-vaut-conforme`, à décider).
- Forme des lignes de plage et des sous-identifiants chez les fournisseurs
  réels : à vérifier sur trois NCM de fournisseurs différents, qui serviront
  aussi à construire les trois premiers profils.
- Lecture des tableaux PDF coupés et fusionnés : à valider sur des NCM
  réelles ; demander l'Excel en plus du PDF signé si nécessaire.
