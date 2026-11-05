# Changelog — SILLAGE

Numérotation MAJEUR.MINEUR.CORRECTIF ; version dans `sillage/version.py`.

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
