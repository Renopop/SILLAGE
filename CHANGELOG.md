# Changelog — SILLAGE

Numérotation MAJEUR.MINEUR.CORRECTIF ; version dans `sillage/version.py`.

## 0.9.1 — 2026-11-09 (NCM Excel : identifiant en tête d'une cellule haute, fusions)

Retour terrain : NCM Excel où chaque exigence est une cellule haute —
« [HLTR_APU_10] » suivi du texte de l'exigence et de ses métadonnées
(Priority, Category, Origin, Type, Rev) dans la même cellule, fusionnée sur
plusieurs lignes — : 258 trous, aucune colonne d'identifiants reconnue,
parce qu'une cellule de plus de 80 caractères n'était jamais comptée.

- Une cellule longue qui **commence** par un identifiant compte comme clé
  (reconnaissance de la colonne, lecture des lignes, lignes de suite).
- Cellules fusionnées sur plusieurs lignes : les lignes qui répètent la
  même cellule d'identifiant sans statut contradictoire sont une seule
  ligne logique ; les colonnes non fusionnées (réponse du fournisseur qui
  continue) la complètent. Plus de faux doublons.
- Feuilles annexes d'un classeur (« Deleted lines », « VC vs VB »,
  « Feuil1 », archives, brouillons) écartées d'après leur nom, avec
  avertissement si elles contenaient des identifiants.

## 0.9.0 — 2026-11-09 (interface : la norme a sa place)

Retour terrain : « où je rentre la ou les normes applicables ? où je rentre
les documents fournisseurs ? » — le fournisseur avait sa ligne, la norme
passait par un menu et un détournement de l'étape d'indexation.

- Cadre Projet : ligne « Norme applicable (documents) » (dossier de la
  CS-25 et de ses AMC) à côté de « Documents du fournisseur » ; l'index du
  fournisseur passe à droite (« Index fournisseur », optionnel) ; les cases
  à cocher occupent une rangée à elles.
- Étape « 5. Tracer vers la norme » dans la rangée des étapes (les
  suivantes sont renumérotées : 6 affaiblissements, 7 fiches, 8
  capitaliser). La norme est indexée automatiquement si son index manque,
  dans un index qui lui est propre (dossier de données, `index/norme_<dossier>`),
  sans toucher aux réglages du fournisseur ; « Outils → Réindexer la
  norme… » force la réindexation. Le dialogue confirme norme, index, seuil et
  reprise des exigences déjà tracées.
- Feuille « Couverture norme » de la grille calculée depuis le dossier de la
  norme ; l'entrée de menu « Documents de la norme (couverture)… » disparaît.
- Le dossier de la norme est mémorisé dans les réglages. README,
  présentation et captures mis à jour.

## 0.8.5 — 2026-11-06 (OCR : lecture par identifiant, identifiants tolérants)

Retour terrain : 17 % des exigences traitées après l'OCR. Les tableaux à
barres rendus par le modèle Vision ne sont pas fiables (cellules longues
renvoyées à la ligne, tableaux coupés, barres absentes sur certaines
rangées), des identifiants sortent avec des confusions de caractères, et
l'interprétation dépendait de l'alignement des colonnes.

- `ocr.tableaux_par_identifiants` : **lecture par identifiant** du texte
  OCR. Chaque identifiant en tête de ligne (après au plus un numéro d'ordre
  et des barres) ouvre une ligne de NCM qui court jusqu'au prochain ; les
  titres de section, le bruit et les pieds de page sont retirés ; les
  barres, quand il y en a, délimitent les cellules ; sinon le statut est
  cherché **après le texte de l'exigence** (fin repérée par ses trois
  derniers mots dans le référentiel), les expressions les plus spécifiques
  masquant les plus générales (« Partially compliant » n'est jamais lu
  « Compliant », « compliant » cité dans un commentaire ne détrône pas le
  statut). Un tableau par page, colonnes Identifiant / Exigence / Statut /
  Commentaire.
- `MotifIdentifiant.tolerant()` : pour le texte OCR, chaque caractère des
  préfixes connus admet ses confusions usuelles (O/0/Q, B/8, I/1/l, S/5,
  Z/2, G/6), les séparateurs sont libres, les chiffres du numéro peuvent
  avoir été lus comme des lettres (« [REO_B2-3_64] », « REQ_82-3_66 »,
  « REQ_B2-3_I6 » → REQ_B2-3_64, _66, _16).
- Chaîne de lecture : la lecture par identifiant est le dernier recours
  avec l'OCR de secours ; en mode « forcer », elle remplace les tableaux à
  barres dès qu'elle trouve plus d'identifiants. Le diagnostic le dit.

## 0.8.4 — 2026-11-06 (OCR Vision sur couche texte inexploitable, tableaux reconstruits)

Retour terrain : l'OCR de secours ne se déclenchait pas, parce que la NCM
scannée a une couche texte (produite par le scanner), inexploitable mais
présente ; et les tableaux n'étaient pas reconstruits.

- Chaîne de lecture d'une NCM PDF avec l'OCR de secours : quadrillage →
  lecture ligne par ligne → si rien n'est retenu, **toutes les pages sont
  relues par le modèle Vision** et les tableaux reconstruits depuis sa
  sortie. Même chaîne pour la spécification (aucune exigence dans la couche
  texte → OCR de toutes les pages). Le diagnostic dit quelle étape a servi.
- Mode « forcer » (`--ocr-forcer`, case « forcer sur toutes les pages ») :
  OCR de toutes les pages d'emblée, couche texte ignorée.
- Consigne d'OCR propre à SILLAGE : chaque tableau rendu en lignes à barres
  verticales, une ligne par rangée, cellules multi-lignes recopiées sur une
  seule ligne, identifiants et statuts recopiés tels quels. Appels page par
  page (`pdf_page_to_image` + `ocr_image_with_llm` du moteur, retries de
  RIVAGE), trois pages en parallèle, cache local `ocr_cache.sqlite` dans le
  dossier de données (clé : fichier, taille, date, page, version de la
  consigne) en plus du mémo de session.
- Tableaux OCR sans barres : repli sur les colonnes séparées par trois
  espaces ou plus.
- Sans OCR demandé, une NCM PDF dont la couche texte ne donne rien le dit
  dans le diagnostic et invite à activer l'OCR de secours.
- Commande `inspecter <pdf> [--spec …] [--sortie rapport.txt]` : ce que
  SILLAGE voit page par page (couche texte, tableaux par quadrillage et
  cellules effondrées, lecture ligne par ligne, identifiants, OCR
  disponible), rapport à envoyer quand une lecture échoue.

## 0.8.3 — 2026-11-06 (PDF scannés puis OCRisés : quadrillage inexploitable)

Retour terrain, NCM PDF signée : « Tableaux lus : 2, retenus : 0 ». Le
quadrillage détecté par pdfplumber sur un scan OCRisé n'a que des traits de
colonnes : toute une colonne tient dans une cellule, les identifiants
s'empilent dans une seule cellule et chacun héritait du premier statut de
la colonne (d'où les « Partially compliant » lus « Compliant »), ou la
cellule dépassait la longueur admise et le tableau était rejeté.

- `lecture_ncm._effondre` : un tableau PDF dont une cellule porte plusieurs
  identifiants sur des lignes différentes, avec d'autres cellules
  multi-lignes, est reconnu comme effondré et écarté.
- Repli automatique : quand aucun tableau d'une NCM PDF n'est retenu, les
  pages sont relues ligne par ligne sans tenir compte du quadrillage
  (`lire_tableaux(..., alignements=True)`), et le diagnostic le dit.
- `tableau_par_alignements` : deux débuts de colonne voisins qui ne
  coexistent jamais sur une même ligne sont fusionnés — les statuts centrés
  dans leur colonne (« Compliant », « Partially compliant », « N/A »
  commencent à des abscisses différentes) restent une seule colonne.
- Test : NCM PDF à quadrillage partiel (cadre et traits de colonnes, pas de
  lignes) avec statuts centrés, lue correctement après repli.

## 0.8.2 — 2026-11-06 (tableaux lus ligne par ligne)

Retour terrain : des « Partially compliant » ressortaient en « Compliant ».
Dans un PDF sans quadrillage, une cellule haute est découpée en lignes de
texte : la ligne de l'identifiant ne portait qu'un morceau du statut (ou
rien), le reste était perdu.

- `tableaux.tableau_par_alignements` : une page PDF sans quadrillage est
  lue ligne par ligne, les colonnes déduites des abscisses où commencent
  des segments de texte sur au moins trois lignes ; chaque ligne de texte
  devient une ligne du tableau (le repli « text » de pdfplumber découpait
  les mots en colonnes fantômes).
- `lecture_ncm._regrouper` : les lignes de suite (vides jusqu'à la colonne
  des identifiants incluse, sans titre de section) sont recollées cellule à
  cellule à la ligne de l'identifiant qui précède ; les seuils de
  reconnaissance des colonnes comptent les lignes logiques ; la colonne qui
  répète l'exigence est reconnue même quand la ligne n'en porte que le début.
  Même recollage pour les tableurs et les tableaux OCR.
- Tests : NCM PDF sans quadrillage avec statuts sur deux lignes, tableur
  avec réponses sur plusieurs lignes et ligne de titre intercalée.

## 0.8.1 — 2026-11-06 (OCR de secours)

- `sillage/ocr.py` : OCR de secours des PDF scannés, comme dans l'indexeur
  de RIVAGE — les pages sans couche texte sont rendues et lues par le
  modèle Vision de la passerelle (`llm_ocr` du moteur embarqué, clé
  `RIVAGE_SYNTH_KEY`, cache de pages de RIVAGE réutilisé, mémo de session).
  Déclenché seulement à la demande (`--ocr` sur lire, grille, diff, appui,
  tracer, couverture, fiches ; case « OCR de secours » dans l'interface,
  mémorisée dans les réglages) et seulement sur les pages sans texte.
- Spécification scannée : ses pages OCR passent par la même lecture ligne
  par ligne (titres, en-têtes courants, identifiants entre crochets).
- NCM scannée : les tableaux sont reconstruits depuis les lignes à barres
  verticales du texte OCR (séparateurs Markdown ignorés, lignes complétées
  au nombre de colonnes) et marqués `ocr` ; le diagnostic compte les
  tableaux reconstruits ; un OCR indisponible (clé absente, moteur) ou
  incomplet (passerelle saturée) est expliqué dans le diagnostic pour la
  NCM, levé en erreur claire pour la spécification.
- `tests/test_ocr.py` : faux modèle Vision, spécification et NCM scannées,
  PDF mixte (seules les pages sans texte partent en OCR), OCR indisponible.

## 0.8.0 — 2026-11-06 (terrain : spécifications PDF, identifiants composés, NCM pré-filtrées)

Premier retour terrain : SILLAGE ne trouvait aucune exigence dans une
spécification PDF réelle (identifiants « [REQ_B2-3_15] » seuls sur leur
ligne) ni dans la NCM correspondante.

- Identifiants : préfixe composé accepté (« REQ_B2-3 » — le code de
  l'annexe en fait partie ; le numéro est le dernier groupe de chiffres),
  crochets et parenthèses tolérés en tête de ligne. La forme normalisée
  reprend l'écriture de la spécification (« REQ_B2-3_15 ») ; les variantes
  du fournisseur (« REQ-B2-3-015 », « req_b2_3_15 ») s'y ramènent. Sans
  référentiel, le séparateur est normalisé en « - » comme avant.
- Spécification PDF : lecture ligne par ligne (une page n'est plus un seul
  paragraphe), titres de section reconnus par leur numérotation, en-tête et
  pied courants et numéros de page ignorés, exigence à cheval sur deux pages
  recollée ; identifiant suivi du texte sur la même ligne ou seul sur sa
  ligne. Un PDF sans couche texte (scan) est signalé comme tel, pour la
  spécification comme pour la NCM.
- NCM : la colonne qui répète le texte de l'exigence (fréquente) est
  reconnue par comparaison avec le référentiel et n'est plus prise pour la
  justification ; une cellule de statut vide n'est plus comptée comme
  valeur inconnue.
- NCM pré-filtrée (« unless listed below, all other items can be assumed
  compliant ») : détectée dans le document ; les exigences absentes
  reçoivent le statut C avec l'alerte « conforme_implicite » au lieu de
  « trou ». `--prefiltree` / `--non-prefiltree` en ligne de commande, case
  « NCM pré-filtrée » dans l'interface (cochée automatiquement à la
  détection), ligne dédiée dans le résumé.
- `tests/test_terrain.py` : spécification PDF et NCM PDF fabriquées sous la
  forme observée (titres, « ** », en-têtes courants, exigence sur deux
  pages, lignes de titre dans le tableau, statuts vides).

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
- `docs/captures/` : les 15 captures refaites avec un dossier de projet
  neutre (`/home/user/Projets/Calculateur_cabine`) à la place du dossier
  temporaire de la session qui apparaissait dans les champs de chemin.

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
