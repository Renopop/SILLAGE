# SILLAGE — audit qualité complet (version 0.7.1)

Date : 2026-11-05. Périmètre : tout le code propre à SILLAGE (package
`sillage/`, lanceur, scripts, tests, documentation). Le moteur de RIVAGE
embarqué (`sillage/moteur/`, copie de RIVAGE 2.7.6) n'est pas audité ici :
il relève de l'audit de RIVAGE.

## 1. Méthode

| Moyen | Résultat |
|---|---|
| Suite de tests (Python 3.11, sans Tk) | 53 tests, tous verts |
| Suite de tests (Python 3.12, interface sous Xvfb) | 54 tests, tous verts |
| Couverture (`coverage`, hors `gui.py` et moteur) | **92 %** (2 655 instructions, 221 non couvertes) |
| Analyse statique (`pyflakes`) | 21 imports inutiles → corrigés en 0.7.1 ; aucune autre anomalie |
| Compilation (`compileall`) | OK |
| Captures d'écran de toute l'interface (Xvfb, données de maquette, moteur embarqué avec faux embedder, faux reranker, fausse synthèse) | 15 captures dans `docs/captures/` |
| Lecture du code | 5 135 lignes dans `sillage/` (hors moteur), 1 454 lignes de tests |

Les constats sont classés **Majeur** (empêche un usage fiable sur le
terrain), **Moyen** (dégrade un cas réel plausible), **Mineur** (confort,
maintenabilité). Chaque constat porte un emplacement et une recommandation.

## 2. Synthèse

**Verdict : prêt pour l'essai terrain, pas encore pour la généralisation.**
Le socle déterministe (lots 1 et 2) est solide, testé et reproductible ;
l'architecture tient ses promesses (modèle pivot fixe, adaptation
mémorisée, IA minimale et bornée, moteur embarqué sans dépendance externe).
Ce qui manque n'est pas du code : c'est la confrontation à des NCM réelles
(lecture des tableaux PDF, profils, calibration des seuils d'appui et de
traçabilité) et un premier build Windows.

Points forts :

- séparation nette entre le déterministe (contrôles par identifiant, grille,
  diff, mémoire, affaiblissements) et le génératif (fiches de revue
  seulement, sans verdict, avec citations obligatoires) ;
- chaque donnée de la grille est sourcée (fichier, feuille ou page, ligne) et
  chaque étape est rejouable depuis un fichier (NCM normalisée, appuis,
  traçabilité, fiches en JSON) ;
- moteur de RIVAGE embarqué tel quel, synchronisable en une commande, avec la
  sonde de plateforme pour les postes anciens ;
- tests qui fabriquent leurs fichiers (aucune donnée fournisseur dans le
  dépôt), interface testée de bout en bout sous Xvfb.

Points faibles :

- aucune validation sur des documents réels (NCM PDF, Word hétérogènes,
  norme CS-25 réelle) ;
- seuils d'appui et de traçabilité non calibrés (valeurs de départ) ;
- build PyInstaller et comportement Windows non exécutés ;
- quelques heuristiques encore grossières (détection de révision, colonnes,
  valeurs chiffrées) qui demandent des cas réels pour être réglées.

## 3. Architecture

```
sillage/
  modele.py         modèle pivot (Exigence, LigneNCM, NCM, Diagnostic, Statut)
  identifiants.py   motif d'identifiant, normalisation, plages, sous-identifiants
  statuts.py        dictionnaire des statuts (FR / EN / abréviations), enrichissable
  tableaux.py       lecture brute xlsx / docx / pdf / csv (fusions, recollage PDF)
  textes.py         lecture en paragraphes (docx, txt, md, pdf, tableurs)
  lecture_ncm.py    colonnes par contenu et par en-tête, profils, diagnostic
  profils.py        profil fournisseur (rôle → en-tête), persistant
  referentiel.py    la spécification en exigences (tableur ou document)
  controles.py      trous, orphelins, doublons, incohérences, clôture, diff, grille
  grille.py         export Excel (Grille, Alertes, Impact norme, Couverture, Résumé)
  rivage.py         pont vers le moteur embarqué (Moteur, indexer, version)
  moteur/           copie des 23 modules de RIVAGE 2.7.6 + hook PyInstaller
  appui.py          lot 3 : vérification des « C », valeurs chiffrées
  affaiblissements.py lot 3 : campagne lexicale (TBD, TBC, where practicable…)
  norme.py          lot 4 : traçabilité, relecture, couverture
  fiches.py         lot 5 : fiches de revue (seule étape générative)
  memoire.py        lot 7 : mémoire SQLite des campagnes
  cli.py            ligne de commande (12 commandes)
  gui.py            interface Tkinter (fenêtre principale, 4 dialogues, 3 fenêtres)
  config.py         dossier de données (SILLAGE_HOME)
```

Dépendances entre couches : `gui` et `cli` ne contiennent pas de logique
métier (elles orchestrent) ; `controles` ne connaît les lots 3 à 7 que par
des objets optionnels ; `rivage` est le seul module qui importe le moteur.
Cette discipline est respectée partout, à une exception près (constat M-6).

## 4. Constats

### 4.1 Corrigés dans cet audit (0.7.1)

| Réf. | Gravité | Constat | Correction |
|---|---|---|---|
| C-1 | Moyen | La fiche « Ce poste » affichait la liste Python brute renvoyée par `diagnostic.fiche_texte` (visible sur la capture avant correction) | `gui.fiche_poste` joint les lignes |
| C-2 | Moyen | `revision_depuis_nom` reconnaissait « A1 » dans `Spec_div_A1.xlsx` et « 01 » dans `NCM_valve_01.xlsx` (le « v » de « valve ») | mot-clé exigé en début de mot, « v » seul exige un chiffre |
| C-3 | Mineur | 21 imports inutiles (pyflakes) | retirés |

### 4.2 Majeurs (à lever avant généralisation)

| Réf. | Constat | Emplacement | Recommandation |
|---|---|---|---|
| M-1 | **Aucun document réel n'a été lu.** La lecture des tableaux PDF repose sur `pdfplumber.extract_tables`, qui ne détecte que les tableaux à traits ; un tableau PDF sans bordures, ou avec cellules fusionnées verticalement, donnera des lignes vides ou décalées. Le recollage entre pages est une heuristique (même nombre de colonnes, page suivante, « tableau 1 »). | `tableaux.py` `lire_pdf`, `_recoller` | Essai sur trois NCM réelles de fournisseurs différents ; si un PDF échoue, exiger l'Excel en plus du PDF signé (déjà recommandé dans le plan) ; prévoir un repli « texte » (`textes.paragraphes_pdf` + identifiants en tête de ligne) quand aucun tableau n'est retenu. |
| M-2 | **Seuils non calibrés.** Appui : étayé ≥ 0,5, faible ≥ 0,25 ; traçabilité : retenue ≥ 0,3. Ce sont des valeurs de départ pour un reranker en probabilités ; en logits elles n'ont pas de sens. Le diagnostic affiche la distribution des scores, mais rien ne guide encore le choix. | `appui.py`, `norme.py`, `cli.py`, `gui.py` | Jeu de 20 exigences dont on connaît la réponse, deux campagnes (vraies correspondances / faux amis), choisir les seuils sur la distribution ; ajouter une commande `calibrer` qui propose les seuils depuis un classeur annoté. |
| M-3 | **Build et Windows non exécutés.** `SILLAGE.spec`, `build_exe.bat`, `install.bat`, `launch.bat` suivent RIVAGE mais n'ont pas tourné ; le thème « vista », les dialogues natifs, les chemins UNC et la conversion `.doc` (COM) ne sont pas vérifiés. | racine du dépôt | Premier `build_exe.bat` sur un poste Windows ; vérifier les imports cachés (`sillage.*`, modules du moteur), le hook de la sonde, `RIVAGE_keys.env` à côté de l'exe ; capturer l'interface sous Windows. |

### 4.3 Moyens

| Réf. | Constat | Emplacement | Recommandation |
|---|---|---|---|
| M-4 | Les **valeurs chiffrées** ne sont comparées que par présence des nombres dans le passage d'appui : « ≤ 2 kg » exigés et « 2 kg » cités sont déclarés satisfaits même si le passage dit « au moins 2 kg » ; les unités ne sont pas rapprochées (« 2000 g » vs « 2 kg »). | `appui.py` `valeurs`, `verifier` | Documenter la limite (fait ici) ; deuxième temps : extraire opérateur + valeur + unité de l'exigence, convertir les unités courantes, et juger la comparaison dans la fiche de revue (lot 5) plutôt que par règle. |
| M-5 | **Traçabilité** : un fragment de 800 caractères peut couvrir la fin d'un paragraphe de norme et le titre du suivant ; la pénalité de position (−0,02 par référence suivante) limite le risque sans l'écarter. Les références sans point (« CS-E 510 ») ne sont pas reconnues. | `norme.py` `tracer`, `MOTIF_NORME` | Vérifier sur la CS-25 réelle (extraction PDF) ; rendre le motif configurable depuis la ligne de commande (`--motif-norme`) ; relecture humaine obligatoire (déjà le cas). |
| M-6 | `gui._charger_ref` écrit `self.ref` **depuis le fil de travail** (lecture de la NCM). Le fil principal ne lit `self.ref` qu'après la fin du travail, la course est bénigne, mais c'est la seule entorse à la règle « le fil de travail ne touche pas l'état de l'interface ». | `gui.py` `_charger_ref`, `_lire_ncm_travail` | Renvoyer `(ref, ncm)` au fil principal et affecter `self.ref` dans `fin`. |
| M-7 | **Mémoire des projets** : `annoter` fait une requête SQL par exigence (index présents) dans le fil principal, à chaque calcul de grille ; `calculer_grille` ouvre et ferme la base à chaque fois. Acceptable jusqu'à quelques milliers d'exigences et quelques dizaines de campagnes, non mesuré au-delà. | `memoire.py` `annoter`, `gui.py` `calculer_grille` | Mesurer avec 5 000 exigences × 50 campagnes ; au besoin, une seule requête groupée (`GROUP BY exigence`) et annotation dans le fil de travail. |
| M-8 | La mémoire enregistre **en clair, sur le poste**, les justifications des fournisseurs (données contractuelles). C'est voulu (jamais sur le partage), mais à dire à l'utilisateur, et le fichier n'est pas chiffré. | `memoire.py`, dossier de données | Mentionner dans le README et l'écran « À propos » ; si une mémoire d'équipe est souhaitée, ne pas pointer `SILLAGE_HOME` sur un partage sans régler l'accès concurrent (SQLite sur SMB est fragile) : prévoir un export / import de campagnes plutôt qu'une base partagée. |
| M-9 | **Dictionnaire des statuts** : les lettres seules « o », « y », « n », « p » sont reconnues (oui / yes / non / partiel). Dans une colonne qui n'est pas celle des statuts, elles peuvent faire monter le taux de reconnaissance d'une mauvaise colonne (par exemple une colonne « Applicable O/N »). Le choix de colonne par ratio et l'en-tête limitent le risque. | `statuts.py` | Après l'essai terrain, retirer les lettres seules ambiguës du dictionnaire par défaut, ou ne les accepter que si l'en-tête de colonne contient un mot de statut. |
| M-10 | **Reconnaissance des colonnes** : un tableau dont moins de la moitié des lignes portent un identifiant (beaucoup de lignes de commentaires, de sous-totaux) est rejeté (`SEUIL_ID = 0,5`), sans message précis. | `lecture_ncm.py` `reconnaitre_colonnes` | Abaisser le seuil à 0,3 et, surtout, écrire dans le diagnostic « tableau X ignoré : 12 % de lignes avec identifiant » pour que l'utilisateur comprenne. |
| M-11 | `exporter_grille` adresse les colonnes par **indices littéraux** (3, 14, 18, 24, 25) pour les couleurs et le retour à la ligne ; ajouter une colonne au milieu décalerait silencieusement les mises en forme (déjà arrivé entre 0.2 et 0.7 : la liste a été allongée par la fin pour cette raison). | `grille.py` | Calculer les indices depuis `COLONNES` par nom. |
| M-12 | Le journal de l'interface force le **niveau INFO sur le logger racine** ; les journaux du moteur (indexation, retrieval) deviennent verbeux dans l'onglet Journal, ce qui est utile, mais aussi sur la console en mode script. | `gui.py` `__init__` | Poser le niveau sur `sillage.*` et sur les loggers du moteur explicitement, pas sur la racine. |
| M-13 | **Tests** : `textes.py` n'est couvert qu'à 56 % (lecteurs PDF et tableurs non exercés), `__main__.py` à 0 %, `sillage_app.py` jamais lancé ; le test de l'interface est un scénario unique de 150 lignes qui remplace des attributs de modules (`messagebox`, `filedialog`, `DialogueProfil`) : efficace mais fragile. | `tests/` | Ajouter un test PDF pour `paragraphes_pdf` (PyMuPDF fabrique le PDF, comme `test_pdf.py`), un test de `sillage_app.py --help` en sous-processus, et découper le scénario d'interface en trois (lecture et grille ; lots 3 à 5 ; diff et mémoire). |

### 4.4 Mineurs

| Réf. | Constat | Emplacement | Recommandation |
|---|---|---|---|
| m-1 | 20 `except Exception` : la plupart sont justifiés (fil de travail, fiche poste, mémoire indisponible, clients de la passerelle), trois sont trop larges : `tableaux.lire_pdf` (avale toute erreur de `extract_tables`), `rivage.Moteur.close`, `norme.paragraphes_cites` (un fichier illisible disparaît sans trace). | `tableaux.py:105`, `rivage.py:158`, `norme.py:321` | Journaliser l'exception (`logger.warning`) avant de continuer. |
| m-2 | Fonctions longues : `gui._construire` (156 lignes), `cli.construire_parseur` (142), `lecture_ncm.reconnaitre_colonnes` (95). | idem | Découper `_construire` par zone (projet, actions, filtres, grille, bas) ; `reconnaitre_colonnes` en quatre étapes nommées (identifiants, en-tête, statut, justification). |
| m-3 | `cmd_grille` accumule les options des sept lots (16 options) ; la ligne de commande devient longue à lire. | `cli.py` | Un fichier de campagne (`campagne.json` : spec, ncm, fournisseur, projet, index, dossiers, seuils) et `sillage grille --campagne campagne.json`. |
| m-4 | Le diff entre révisions compare par statut effectif ; une exigence passée de deux lignes à une (doublon résolu) n'est pas signalée. | `controles.comparer` | Ajouter la nature « lignes » quand le nombre de lignes change. |
| m-5 | L'interface ne mémorise pas la géométrie de la fenêtre ni la largeur des colonnes ; pas de raccourcis clavier ; pas de suppression d'une campagne de la mémoire depuis l'interface (ligne de commande seulement). | `gui.py` | Géométrie dans `gui.json` ; bouton « Supprimer » dans la fenêtre Mémoire ; `Ctrl+O`, `F5`. |
| m-6 | `requirements.txt` installe `pytest` sur les postes utilisateurs ; `.coverage` n'est pas ignoré par git. | racine | Déplacer `pytest` dans `requirements-dev.txt` ; `.coverage` ajouté au `.gitignore` en 0.7.1. |
| m-7 | Les en-têtes de colonnes de la grille à l'écran et les codes d'alertes (« ecart_ouvert ») sont des identifiants techniques ; le filtre et le détail donnent les libellés, mais pas la colonne « Alertes ». | `gui.py` `_rafraichir_grille` | Afficher les libellés courts dans la colonne, garder les codes dans l'export. |
| m-8 | Pas de guide utilisateur illustré (l'équivalent de `RIVAGE_guide_utilisation.docx`) ; README et plan sont complets mais techniques. | `docs/` | Rédiger le guide après l'essai terrain, avec des captures Windows. |

### 4.5 Sécurité et données

- **Fichiers d'entrée** : les NCM et spécifications sont des fichiers de
  tiers, lus par openpyxl, python-docx, pdfplumber (et le moteur pour les
  documents indexés). Même profil de risque que RIVAGE ; aucune exécution de
  contenu ; pas de macro exécutée (les `.xlsm` sont lus en données).
- **Réseau** : aucun appel sortant hors la passerelle d'IA (embedding,
  reranking, synthèse, OCR), avec les clés de `RIVAGE_keys.env`, jamais
  versionné (`.gitignore`). TLS inchangé par rapport à RIVAGE.
- **Base locale** : requêtes SQLite entièrement paramétrées ; la recherche
  (`LIKE`) prend le texte comme paramètre. Pas d'`eval`, pas de chemin
  construit depuis une entrée non contrôlée.
- **Ce qui part vers la passerelle** : pour la vérification et la
  traçabilité, le texte de l'exigence ; pour les fiches, l'exigence, la
  ligne de NCM et au plus trois passages de 800 caractères. Jamais un
  document entier. À rappeler dans le guide utilisateur.

### 4.6 Performance (non mesurée, estimée)

| Opération | Coût | Remarque |
|---|---|---|
| Lecture d'une NCM de 5 000 lignes | quelques secondes | regex par cellule, une passe |
| Grille et diff | négligeable | jointures en mémoire |
| Vérification des « C » | 1 embedding + 1 recherche + 1 reranking **par exigence** | 1 000 « C » ≈ 1 000 appels : minutes ; pas de lot |
| Traçabilité | idem, par exigence sans norme | |
| Fiches de revue | 1 appel génératif par NC/PC | le plus coûteux par unité, mais peu d'unités |
| Mémoire | 1 requête SQL par exigence à chaque grille | voir M-7 |

Recommandation : regrouper les embeddings des questions par lot
(`embed_passages` du moteur accepte une liste) pour les lots 3 et 4 ; le
reranking reste un appel par exigence.

## 5. Tests

| Fichier | Objet | Tests |
|---|---|---|
| test_identifiants | formes, plages, préfixes, tri | 5 |
| test_statuts | vocabulaires, tolérance, persistance | 3 |
| test_tableaux | xlsx (fusions, vides), docx, csv, recollage PDF, format inconnu | 5 |
| test_pdf | tableau PDF coupé sur deux pages (PyMuPDF) | 1 |
| test_lecture_ncm | colonnes, lecture complète, plages et statuts inconnus, sans statut, sans identifiants, profil, JSON, révision | 9 |
| test_referentiel | tableur, csv, document, doublons | 4 |
| test_controles | grille de base, doublons / incohérences / inconnus, diff | 3 |
| test_grille_cli | export, ligne de commande des lots 1-2 | 2 |
| test_moteur_embarque | copie, localisation, imports, fichiers env, sonde fils | 5 |
| test_lot3_rivage | valeurs, campagne d'appui, sans reranker, affaiblissements, grille et CLI, indexer | 6 |
| test_lot4_norme | références, tracer / relire / appliquer, couverture, grille et CLI | 4 |
| test_lot5_fiches | contexte, rédaction, Word et grille, CLI | 4 |
| test_lot7_memoire | mémoire complète, CLI | 2 |
| test_gui | scénario complet de l'interface sous Xvfb | 1 |

Couverture par module (hors interface et moteur) : de 85 % (`rivage.py`) à
98 % (`controles.py`, `memoire.py`) ; `textes.py` 56 % (voir M-13).

## 6. Documentation

`README.md` (installation, interface, toutes les commandes), `docs/PLAN_SILLAGE.md`
(besoin, principes, lots, un chapitre par lot, conventions, questions
ouvertes), `CHANGELOG.md` (une entrée par version, 0.1.0 à 0.7.1),
`RIVAGE.env.example` et `RIVAGE_keys.env.example`. Manque : le guide
utilisateur illustré (m-8) et une page « limites connues » à destination de
l'utilisateur (M-4, M-5 en langage simple).

## 7. Plan d'actions

**Priorité 1 — terrain (sans code, ou presque)**
1. Trois NCM réelles de fournisseurs différents : lecture, profils,
   diagnostic ; corriger ce qui casse (M-1, M-10).
2. Premier `build_exe.bat` sur un poste Windows ; captures Windows (M-3).
3. Jeu de 20 exigences connues et calibration des seuils (M-2).

**Priorité 2 — robustesse (une version 0.8)**
4. Repli « texte » pour les PDF sans tableau détecté (M-1).
5. `self.ref` affecté dans le fil principal (M-6) ; niveau de journal ciblé
   (M-12) ; indices de colonnes par nom (M-11) ; exceptions journalisées (m-1).
6. Tests manquants (M-13) ; `requirements-dev.txt` (m-6).
7. Embeddings par lot pour les lots 3 et 4 (§ 4.6).

**Priorité 3 — confort (0.9)**
8. Fichier de campagne (m-3), libellés d'alertes à l'écran (m-7), géométrie
   et suppression de campagne dans l'interface (m-5), guide utilisateur (m-8).

## 8. Annexe — captures (`docs/captures/`)

| Fichier | Contenu |
|---|---|
| 60_accueil.png | fenêtre principale, projet renseigné, avant lecture |
| 61_diagnostic_apres_lecture.png | grille après lecture de la NCM, onglet Diagnostic |
| 62_grille_complete_detail_nc.png | grille après les étapes 1 à 6, détail d'un NC (ligne de NCM, affaiblissement, fiche de revue), résumé |
| 63_detail_c_appui_journal.png | détail d'un « C » étayé (passage d'appui, document, score), onglet Journal |
| 64_filtre_c_sans_appui.png | filtre par alerte |
| 65_filtre_ecarts.png | filtre « écarts (PC + NC) » |
| 66_dialogue_statuts.png | dialogue des statuts inconnus |
| 67_dialogue_profil.png | dialogue du profil des colonnes |
| 68_dialogue_tracer.png | dialogue de traçabilité vers la norme |
| 69_diff.png | comparaison de deux révisions |
| 70 à 73_memoire_*.png | fenêtre de la mémoire : campagnes, exigences contestées, écarts acceptés, recherche |
| 74_fiche_poste.png | fiche « ce poste » (moteur, faiss, SIMD, processeur, mémoire) |

Les captures viennent d'un serveur X virtuel sous Linux (thème « clam ») ;
sous Windows, le thème « vista » et les dialogues natifs changent l'aspect,
pas la disposition.
