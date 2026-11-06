# Changelog — SILLAGE

Numérotation MAJEUR.MINEUR.CORRECTIF ; version dans `sillage/version.py`.

## 0.7.2 — 2026-11-06 (documentation : commentaires, présentation)

- Revue de tous les commentaires et docstrings du code propre à SILLAGE
  (hors copie du moteur) : périmètre complet dans `sillage/__init__.py`,
  rôle de chaque module, lots 3 à 7 décrits dans `controles.py`, règle
  d'ajout de colonne dans `grille.py`, chemin `extrait` marqué réservé,
  aides de la ligne de commande reformulées. Aucun changement de
  comportement.
- `docs/SILLAGE_comment_ca_marche_principes.pptx` : présentation de 35
  diapositives (pourquoi SILLAGE, principes, architecture, lecture des NCM,
  contrôles, diff, moteur embarqué, appuis, affaiblissements, traçabilité,
  fiches, mémoire, interface avec les captures, autonomie, atouts, gains,
  audit, pistes, règles en une page), produite par
  `docs/outils/make_deck_sillage.js` (pptxgenjs).

## 0.7.1 — 2026-11-05 (audit qualité, corrections)

- `docs/AUDIT_QUALITE_SILLAGE.md` : audit complet (méthode, synthèse,
  architecture, constats classés, sécurité, performance, tests,
  documentation, plan d'actions) et `docs/captures/` (15 captures de toute
  l'interface sous Xvfb).
- Corrections : la fiche « Ce poste » affichait la liste brute des lignes ;
  la révision déduite du nom de fichier prenait « div » ou « valve » pour un
  marqueur de version ; imports inutiles retirés ; `.coverage` ignoré.

## 0.7.0 — 2026-11-05 (lot 7 : capitalisation entre projets)

- `memoire.py` : mémoire locale des campagnes (SQLite dans le dossier de
  données) ; `enregistrer` (projet, fournisseur, révision, NCM signée, une
  ligne par exigence : statut, justification, clôture, dérogation, action,
  appui, alertes, norme), remplacement d'une campagne identique,
  suppression ; `exigences_contestees` (par couples projet / fournisseur),
  `ecarts_acceptes` (par exigence ou paragraphe de norme), `par_norme`,
  `par_fournisseur` (conformité, écarts, ouverts, « C » sans appui),
  `chercher` ; `historique` d'une exigence hors campagne courante et
  `annoter` la grille (« vue sur n campagnes, contestée k fois ; écart déjà
  accepté (projet, fournisseur, dérogation) ») ; rapport Excel en cinq
  feuilles.
- Grille : colonne « Historique (mémoire) », exigences déjà contestées et
  écarts déjà acceptés dans le résumé.
- Ligne de commande : `memoire campagnes | contestees | acceptes | norme |
  fournisseurs | chercher | exporter | supprimer` ; `grille --projet
  --memoire --capitaliser --signee --memoire-fichier`.
- Interface : champ Projet et case « NCM finale signée », étape
  « 7. Capitaliser », fenêtre « Mémoire des projets » (campagnes, exigences
  contestées, écarts acceptés, par norme, par fournisseur, recherche,
  export), historique dans le détail ; la grille s'annote automatiquement.
- Tests du lot 7 (mémoire, annotation, export, ligne de commande, interface).

## 0.6.0 — 2026-11-05 (lot 5 : fiches de revue des écarts, seule étape générative)

- `fiches.py` : pour chaque exigence NC ou PC (ou `--statuts`), le modèle
  de la passerelle reçoit l'exigence, sa ou ses lignes de NCM (statut,
  justification, action, échéance, dérogation), le paragraphe de norme tracé
  et les passages de la spécification fournisseur (campagne d'appuis du
  lot 3, ou recherche à la demande par le moteur), et rédige une fiche en
  trois parties : ÉCART, RISQUE, QUESTIONS POUR LA REVUE. Pas de verdict ;
  chaque affirmation cite un passage [i] ou la ligne de NCM ; les passages
  muets sont dits tels. Même client que RIVAGE (`synthesis.Synthesizer`
  embarqué : retries, repli, mode dégradé et troncature signalés).
- Dossier de revue Word (`exporter_fiches`) : une fiche par page
  (exigence, norme, ligne de NCM, fiche, passages cités) ; JSON
  aller-retour ; fiche reportée dans la grille (colonne « Fiche de revue »)
  et dans le détail de l'interface.
- Ligne de commande : `fiches` (`--appuis`, `--tracabilite`, `--index`,
  `--statuts`, `--json`, `--docx`) ; `grille --fiches`.
- Interface : étape « 6. Fiches de revue », Outils → exporter en Word,
  enregistrer / reprendre en JSON.
- Tests du lot 5 avec un faux client de synthèse (contexte, rédaction avec
  campagne, à la demande, sans passerelle, Word, grille, ligne de commande,
  interface).

## 0.5.0 — 2026-11-05 (lot 4 : traçabilité vers la norme)

- `norme.py` : références de paragraphes reconnues et normalisées (CS, AMC,
  FAR, JAR, 14 CFR : « CS 25.1309 », « AMC 25.1309 », « FAR 25.1353 ») ;
  `tracer` propose, pour chaque exigence sans paragraphe, les paragraphes
  cités par ses meilleurs passages dans l'index de la norme (score du
  passage, première référence du passage privilégiée), avec un paragraphe
  « retenu » au-dessus du seuil ; classeur de relecture (`exporter_tracabilite`,
  colonnes Norme retenue / Validé) réimportable (`importer_tracabilite`) ;
  `appliquer` reporte les paragraphes dans le référentiel ; JSON
  aller-retour.
- Couverture de la norme : paragraphes cités dans les documents de la norme
  et exigences rattachées à chacun ; les paragraphes sans exigence sont
  listés (applicabilité à décider par l'ingénieur).
- Grille : alerte `impact_norme` (NC, PC, trou ou statut inconnu sur une
  exigence tracée), feuilles « Impact norme » et « Couverture norme »,
  compte des exigences tracées dans le résumé.
- Ligne de commande : `tracer`, `couverture` ; `grille --tracabilite`,
  `--norme-documents`.
- Interface : Outils → tracer vers la norme (dialogue index / seuil),
  charger une traçabilité (JSON ou classeur relu), documents de la norme
  pour la couverture ; filtre et détail « impact norme ».
- Tests du lot 4 (références, traçabilité proposée / relue / appliquée,
  couverture, grille, ligne de commande, interface).

## 0.4.0 — 2026-11-05 (interface graphique autonome, lanceur, scripts, spec)

- `sillage/gui.py` (Tkinter) : projet (spécification, fournisseur, NCM et
  révision, documents du fournisseur, index), étapes 1 à 5 (lire la NCM,
  grille, indexer, vérifier les « C », affaiblissements), outils (profil
  des colonnes, dictionnaire des statuts, comparaison avec une révision
  antérieure), grille à l'écran colorée par statut avec filtres (statut,
  alerte, recherche) et volet de détail (exigence, ligne de NCM, clôture,
  origine, appui et passage, valeurs absentes, affaiblissements, alertes),
  onglets diagnostic / résumé / journal, exports Excel (grille, diff),
  NCM normalisée et campagne d'appuis en JSON, fiche « ce poste », menus.
  Les valeurs de statut inconnues sont proposées à l'enrichissement dès la
  lecture, puis la NCM est relue. Travaux dans un fil, file relevée toutes
  les 100 ms, réglages mémorisés (`gui.json` du dossier de données).
- `sillage_app.py` : lanceur script / exécutable (sonde fils,
  `freeze_support`, flux absents en exécutable fenêtré, interface sans
  argument, ligne de commande sinon) ; `sillage gui` en ligne de commande.
- `install.bat`, `launch.bat`, `build_exe.bat`, `SILLAGE.spec` (ONEDIR,
  moteur embarqué, hook de la sonde), `RIVAGE.env.example` et
  `RIVAGE_keys.env.example` repris de RIVAGE (mêmes clés, mêmes options).
- Test de l'interface sous Xvfb (lecture, grille, filtres, détail, export,
  diff, affaiblissements, profil).

## 0.3.0 — 2026-11-05 (moteur de RIVAGE embarqué : SILLAGE autonome)

- `sillage/moteur/` : copie embarquée des 23 modules de RIVAGE 2.7.6
  (commit ded0695) qui font l'indexation et la recherche — extraction,
  découpage, déduplication, embeddings, index vectoriel et lexical,
  registre, stockage, indexeur, retrieval, cache lecteur, synthèse, clients
  de la passerelle, diagnostic, sonde de plateforme — plus le hook
  PyInstaller `hooks/rth_plateforme.py`. Fichiers copiés tels quels (imports
  plats inchangés) ; `VERSION_RIVAGE.txt` note la version et le commit.
- `outils/synchroniser_rivage.py chemin/vers/RIVAGE` : reprend une version
  plus récente de RIVAGE en une commande (copie seulement ce qui a changé).
- `sillage/moteur/__init__.py` (`activer`) : rend le moteur importable,
  désigne au moteur les `RIVAGE.env` / `RIVAGE_keys.env` du dossier de
  SILLAGE (exécutable ou racine du dépôt) et applique la sonde de plateforme
  avant le premier import de faiss, comme le lanceur de RIVAGE.
- `sillage/moteur/rivage_app.py` (propre à SILLAGE) et `python -m sillage
  --sonde-plateforme` : processus fils de la sonde, en script et en exécutable.
- `rivage.py` : le pont utilise la copie embarquée ; `SILLAGE_RIVAGE_DIR` ne
  sert plus qu'au développement (essai d'un RIVAGE plus récent) ; message
  d'erreur explicite si une dépendance du moteur manque (faiss, numpy…).
- Dépendances : celles de RIVAGE reprises dans `requirements.txt` et
  `pyproject.toml` ; `.gitignore` exclut `RIVAGE.env`, `RIVAGE_keys.env`,
  `rivage.config.json`, `build_info.json`.
- Tests : copie complète, localisation embarquée, import de tous les
  modules, fichiers d'options du dossier de SILLAGE, sonde fils ; toute la
  suite tourne sans RIVAGE extérieur (43 tests).

## 0.2.0 — 2026-11-05 (lot 3 : vérification des « conformes » avec RIVAGE)

- Pont vers RIVAGE (`rivage.py`) : localisation des modules (dépendance
  installée, `SILLAGE_RIVAGE_DIR`, dossier voisin), `Moteur` (un retriever
  RIVAGE pour toute une campagne : portée par préfixes de chemins ou
  fichiers, recherche hybride, reranking), `indexer` (l'indexeur de RIVAGE
  sur les documents d'un fournisseur, dans un index propre à SILLAGE).
- Appuis (`appui.py`) : une question RIVAGE par exigence déclarée « C »
  (ou `--statuts`), niveau étayé / faible / absent / non vérifié selon le
  score de reranking, valeurs chiffrées de l'exigence cherchées dans le
  passage d'appui, campagne enregistrable en JSON et rejouable.
- Affaiblissements (`affaiblissements.py`) : campagne lexicale (TBD, TBC,
  where practicable, best effort, sous réserve…) rattachée aux identifiants
  cités dans le paragraphe.
- Lecture en paragraphes partagée (`textes.py`) : docx, txt, md, pdf, tableurs.
- Grille : colonnes appui, score, passage, document, valeurs absentes,
  affaiblissements ; alertes `c_sans_appui`, `c_faible`, `valeurs_absentes`,
  `affaiblissement`.
- Ligne de commande : `indexer`, `appui`, `affaiblissements` ; `grille
  --verifier`, `--appuis`, `--affaiblissements`, `--index`, `--dossier`,
  `--documents`, `--statuts`, `--seuil-etaye`, `--seuil-faible`.
- Tests du lot 3 avec le moteur de RIVAGE hors ligne (faux embedder, faux
  reranker), ignorés si RIVAGE est introuvable.

## 0.1.0 — 2026-11-05 (lots 0, 1 et 2 : socle déterministe)

- Modèle pivot (`modele.py`) : exigence, ligne de NCM (identifiant, statut
  C / PC / NC / NA / ?, justification, champs optionnels, brut, origine,
  chemin), NCM normalisée (JSON), diagnostic d'exploitabilité.
- Identifiants (`identifiants.py`) : motif générique ou restreint aux préfixes
  du référentiel, normalisation, plages dépliées (« REQ-100 à REQ-120 »,
  « REQ-100 à 120 »), sous-identifiants (« REQ-100a », « REQ-100.2 »).
- Statuts (`statuts.py`) : dictionnaire français / anglais / abréviations vers
  quatre valeurs pivot, reconnaissance tolérante (« C (see note) »,
  « PC - voir §4 »), enrichissement persistant dans `statuts.json`.
- Tableaux (`tableaux.py`) : xlsx / xlsm (cellules fusionnées dépliées),
  docx, pdf (tableaux recollés entre pages), csv.
- Lecture des NCM (`lecture_ncm.py`) : colonnes reconnues par contenu
  (identifiants, statuts, justification) et par en-tête (action, échéance,
  référence, dérogation, commentaire), ligne d'en-tête détectée, profil
  fournisseur prioritaire, révision déduite du nom de fichier.
- Profils fournisseurs (`profils.py`) : rôle → en-tête, mémorisés dans
  `profils/<fournisseur>.json`.
- Référentiel (`referentiel.py`) : spécification depuis un tableur (colonnes
  identifiant, texte, norme) ou un document (paragraphes commençant par un
  identifiant).
- Contrôles (`controles.py`) : trous, orphelins, doublons, incohérences,
  statuts inconnus, clôture des écarts (dérogation / action / ouvert), diff
  entre deux révisions (nouvelles, disparues, statut changé avec sens,
  justification réécrite).
- Grille (`grille.py`) : classeur Excel (Grille, Alertes, Résumé) avec
  couleurs par statut, filtres, volets figés ; export du diff.
- Ligne de commande (`cli.py`) : `lire`, `grille`, `diff`, `statuts`,
  `profils`, `version`.
- Tests : identifiants, statuts, tableaux, lecture de NCM, référentiel,
  contrôles, grille, ligne de commande ; les fichiers d'essai sont fabriqués
  par les tests, aucune donnée fournisseur dans le dépôt.
