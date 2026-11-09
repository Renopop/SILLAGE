// Guide d'utilisation SILLAGE — génération du .docx (docx-js).
//
// Usage : node make_guide_sillage.js <dossier_captures> <sortie.docx> <version>
// Dépendance : docx (npm install docx, ou NODE_PATH vers un node_modules qui l'a).
// Les captures viennent de docs/captures (interface sous Xvfb, données de maquette).
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, ImageRun,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle, LevelFormat,
  PageBreak, Header, Footer, PageNumber,
} = require("docx");

const [,, imgDir, outPath, version] = process.argv;
const date = new Date().toISOString().slice(0, 10);

// --- utilitaires ---------------------------------------------------------------
function pngSize(buf) { return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) }; }
function image(name, legende, maxCm = 16) {
  const p = path.join(imgDir, name);
  if (!fs.existsSync(p)) {
    return [new Paragraph({ children: [new TextRun({ text: `[capture ${name} absente]`, italics: true, color: "999999" })] })];
  }
  const buf = fs.readFileSync(p);
  const { w, h } = pngSize(buf);
  const scale = Math.min(1, (maxCm * 37.8) / w);
  const out = [new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { before: 120, after: 60 },
    children: [new ImageRun({ type: "png", data: buf, transformation: { width: Math.round(w * scale), height: Math.round(h * scale) } })],
  })];
  if (legende) out.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 },
    children: [new TextRun({ text: legende, italics: true, size: 18, color: "555555" })] }));
  return out;
}
function H1(t) { return new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 360, after: 160 }, children: [new TextRun(t)] }); }
function H2(t) { return new Paragraph({ heading: HeadingLevel.HEADING_2, spacing: { before: 280, after: 120 }, children: [new TextRun(t)] }); }
function H3(t) { return new Paragraph({ heading: HeadingLevel.HEADING_3, spacing: { before: 200, after: 80 }, children: [new TextRun(t)] }); }
function runs(t) {
  const out = [];
  t.split(/(\*\*[^*]+\*\*)/g).forEach(seg => {
    if (!seg) return;
    if (seg.startsWith("**")) out.push(new TextRun({ text: seg.slice(2, -2), bold: true }));
    else out.push(new TextRun(seg));
  });
  return out;
}
function P(t, opts = {}) { return new Paragraph({ spacing: { after: 120 }, ...opts, children: runs(t) }); }
function B(t) { return new Paragraph({ numbering: { reference: "puces", level: 0 }, spacing: { after: 60 }, children: runs(t) }); }
let numRef = 0;
const NUM_REFS = Array.from({ length: 30 }, (_, i) => `num${i}`);
function liste(items) {               // liste numérotée indépendante (repart à 1)
  const ref = NUM_REFS[numRef++];
  return items.map(t => new Paragraph({ numbering: { reference: ref, level: 0 }, spacing: { after: 60 }, children: runs(t) }));
}
function Note(t, couleur = "2E75B6", fond = "EAF2FB") {
  return new Paragraph({
    spacing: { before: 120, after: 160 }, indent: { left: 360 },
    border: { left: { style: BorderStyle.SINGLE, size: 18, color: couleur, space: 8 } },
    shading: { type: ShadingType.CLEAR, fill: fond, color: "auto" },
    children: runs(t),
  });
}
const Attention = t => Note(t, "C00000", "FBEAEA");
function cell(t, w, opts = {}) {
  return new TableCell({
    width: { size: w, type: WidthType.DXA },
    shading: opts.head ? { type: ShadingType.CLEAR, fill: "D9E2F3", color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: [new Paragraph({ spacing: { after: 0 }, children: opts.head ? [new TextRun({ text: t, bold: true })] : runs(t) })],
  });
}
function table(headers, rows, widths) {
  const total = widths.reduce((a, b) => a + b, 0);
  return new Table({
    width: { size: total, type: WidthType.DXA }, columnWidths: widths,
    rows: [new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, widths[i], { head: true })) }),
      ...rows.map(r => new TableRow({ children: r.map((c, i) => cell(c, widths[i])) }))],
  });
}
const saut = () => new Paragraph({ children: [new PageBreak()] });

// --- contenu --------------------------------------------------------------------
const c = [];

// Page de titre
c.push(new Paragraph({ spacing: { before: 2400, after: 200 }, alignment: AlignmentType.CENTER,
  children: [new TextRun({ text: "SILLAGE", bold: true, size: 72, color: "0F4C5C" })] }));
c.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 },
  children: [new TextRun({ text: "Spécifications, Incomplétudes, Livrables : Lecture, Analyse et Grille des Écarts", size: 24, color: "555555" })] }));
c.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 400, after: 120 },
  children: [new TextRun({ text: "Guide d'utilisation", bold: true, size: 40, color: "1F3864" })] }));
c.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 600 },
  children: [new TextRun({ text: `Version ${version} — ${date}`, size: 22, color: "555555" })] }));
c.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 },
  children: [new TextRun({ text: "Renaud Loison", bold: true, size: 26 })] }));
c.push(new Paragraph({ alignment: AlignmentType.CENTER,
  children: [new TextRun({ text: "auteur, créateur et développeur de SILLAGE", size: 22, color: "555555" })] }));
c.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 1200 },
  children: [new TextRun({ text: "RIVAGE cherche, SILLAGE garde la trace.", italics: true, size: 22, color: "777777" })] }));
c.push(saut());

// Sommaire
c.push(H1("Sommaire"));
c.push(...liste([
  "À quoi sert SILLAGE, en une page",
  "Ce qu'il vous faut avant de commencer",
  "L'écran : le cadre Projet, champ par champ",
  "Les huit étapes, dans l'ordre",
  "Lire la grille : statuts, alertes, détail",
  "Comparer deux révisions de NCM",
  "Exporter : grille Excel, fiches Word, fichiers rejouables",
  "Quand ça ne marche pas",
  "Où sont vos données, ce qui part vers la passerelle",
  "Glossaire",
]));
c.push(saut());

// 1
c.push(H1("1. À quoi sert SILLAGE, en une page"));
c.push(P("Vous avez envoyé une **spécification** à un fournisseur. Il répond par sa propre spécification et par une **NCM** (Non-Compliance Matrix), un tableau qui reprend vos numéros d'exigences et dit, pour chacun, s'il est conforme (**C**), partiellement conforme (**PC**), non conforme (**NC**) ou non applicable (**NA**). Les NCM se succèdent, révision après révision, jusqu'à une NCM finale signée."));
c.push(P("SILLAGE lit votre spécification et la NCM, les met en face l'une de l'autre, et vous rend une **grille des écarts** : une ligne par exigence, avec le statut du fournisseur, sa justification, et des alertes quand quelque chose cloche (exigence sans réponse, identifiant inconnu, statut incohérent, écart sans action ni dérogation)."));
c.push(P("Si vous lui donnez aussi les **documents du fournisseur**, il vérifie que chaque « C » est étayé par un passage de la spécification fournisseur, et relève les formules d'affaiblissement (« TBD », « where practicable », « sous réserve »). Si vous lui donnez la **norme applicable** (CS-25, AMC…), il propose pour chaque exigence le paragraphe de norme concerné et signale les écarts qui touchent la certification. Enfin il rédige, pour chaque écart, une **fiche de revue** (écart, risque, questions à poser), et garde la **mémoire** des campagnes : exigences souvent contestées, écarts déjà acceptés."));
c.push(Note("SILLAGE ne décide jamais. Il lit, recoupe, signale et cite. Le verdict — accepter un écart, exiger une correction, demander une dérogation — reste le vôtre. Tout ce qu'il affiche renvoie à la cellule, la page ou le passage d'origine : vous pouvez vérifier chaque ligne en dix secondes."));
c.push(P("Les étapes 1 et 2 (lire la NCM, grille des écarts) fonctionnent **sans réseau et sans clé** : c'est de la lecture et du recoupement. Les étapes 3 à 7 utilisent le moteur de RIVAGE embarqué et la passerelle d'IA (mêmes clés que RIVAGE) ; l'étape 8 est locale."));
c.push(saut());

// 2
c.push(H1("2. Ce qu'il vous faut avant de commencer"));
c.push(table(["Quoi", "Sous quelle forme", "Obligatoire pour"], [
  ["La **spécification** (votre référentiel d'exigences)", "PDF de texte où chaque exigence commence par son identifiant, nu ou entre crochets (« [REQ_B2-3_15] », « [HLTR_APU_10] ») ; ou Word, texte, ou tableur avec une colonne d'identifiants.", "Tout"],
  ["La **NCM** du fournisseur", "Classeur Excel (le plus courant), PDF, Word ou CSV : un tableau dont chaque ligne porte l'identifiant de l'exigence et un statut.", "Tout"],
  ["Les **documents du fournisseur**", "Un dossier : sa spécification, ses rapports, en PDF, Word, Excel, texte.", "Étapes 3, 4, 6, 7"],
  ["La **norme applicable**", "Un dossier : la CS-25 et ses AMC, ou la norme de votre domaine, en PDF ou texte.", "Étape 5 et feuille « Couverture norme »"],
  ["Les **clés de la passerelle**", "Le fichier RIVAGE_keys.env à côté de l'application (le même que pour RIVAGE).", "Étapes 3, 4, 5, 7 et OCR"],
], [2600, 4600, 1872]));
c.push(P(""));
c.push(Attention("Si la spécification ou la NCM est un PDF **scanné** (une image, sans texte sélectionnable), rien n'est lisible directement. Cochez « OCR de secours » : les pages passent par le modèle Vision de la passerelle. Chaque page est un appel payant, mais les pages déjà lues sont gardées en cache. Un PDF produit par un scanner avec une couche texte de mauvaise qualité est traité de la même façon, automatiquement, si l'OCR de secours est coché."));
c.push(P("Pour lancer SILLAGE : double-cliquez sur **launch.bat** (ou sur SILLAGE.exe si vous avez l'exécutable). L'installation, si elle n'est pas faite : **install.bat**, une fois."));
c.push(saut());

// 3
c.push(H1("3. L'écran : le cadre Projet, champ par champ"));
c.push(...image("60_accueil.png", "L'écran d'accueil : le cadre Projet en haut, les huit étapes, les outils, la grille et le détail, les onglets Diagnostic / Résumé / Journal en bas."));
c.push(P("Tout ce qui entre dans SILLAGE se donne dans le cadre **Projet**. Le bouton « … » ouvre un sélecteur de fichier ou de dossier. Les chemins sont mémorisés d'une session à l'autre."));
c.push(table(["Champ", "Quoi mettre", "Remarques"], [
  ["Spécification (référentiel)", "Votre spécification (PDF, Word, texte ou tableur).", "C'est elle qui fixe la liste des exigences et la forme des identifiants."],
  ["NCM", "La NCM du fournisseur à analyser (la plus récente).", "La révision se déduit du nom du fichier (« _revB », « Rev4 ») ; corrigez-la si besoin."],
  ["Documents du fournisseur", "Le dossier de ses documents.", "Indexé à l'étape 3, lu aux étapes 4, 6 et 7."],
  ["Norme applicable (documents)", "Le dossier des documents de la norme.", "Indexé automatiquement à l'étape 5, dans un index qui lui est propre."],
  ["Préfixe d'identifiant", "Le début des identifiants d'exigences, avec son séparateur : « HLTR_APU_ », « REQ_B2-3_ ». Plusieurs : séparés par des virgules.", "**Remplissez-le.** Vide, SILLAGE déduit le motif de la spécification, et peut prendre « TSO-C77b » ou « DO-160 » pour des exigences."],
  ["Projet", "Le nom de l'affaire.", "Sert à la mémoire des campagnes (étape 8)."],
  ["Fournisseur", "Le nom du fournisseur.", "Sert au profil de ses colonnes, à son index et à la mémoire."],
  ["Révision", "La révision de la NCM.", "Déduite du nom de fichier à la lecture."],
  ["Index fournisseur", "Laissez vide.", "Par défaut : un index par fournisseur dans le dossier de données. À remplir seulement pour réutiliser un index existant."],
], [2500, 3300, 3272]));
c.push(P(""));
c.push(H2("Les cases à cocher"));
c.push(table(["Case", "Quand la cocher", "Effet"], [
  ["NCM finale signée", "Cette NCM est la version signée par les deux parties.", "Mémorisé avec la campagne à l'étape 8 ; les NCM signées pèsent plus dans l'historique."],
  ["NCM pré-filtrée", "La NCM ne liste que les écarts et déclare conformes les exigences absentes (« unless listed below, all other items can be assumed compliant »).", "Les exigences absentes reçoivent C avec l'alerte « conforme implicite » au lieu de « trou ». SILLAGE coche la case lui-même s'il lit cette phrase dans le document."],
  ["OCR de secours", "La spécification ou la NCM est un PDF scanné, ou sa couche texte ne donne rien.", "Les pages sans texte, puis toutes les pages si nécessaire, sont lues par le modèle Vision (appels payants, cache local)."],
  ["forcer sur toutes les pages", "La couche texte existe mais vous savez qu'elle est fausse.", "Toutes les pages partent en OCR d'emblée."],
], [2200, 3700, 3172]));
c.push(saut());

// 4
c.push(H1("4. Les huit étapes, dans l'ordre"));
c.push(P("Les boutons de la rangée des étapes se suivent de gauche à droite. Pendant qu'une étape travaille, les boutons sont grisés, la barre d'état en bas indique l'avancement, et l'onglet **Journal** garde la trace de tout. Une erreur ouvre une boîte de dialogue et le détail est dans le Journal."));
c.push(table(["Étape", "Ce qu'elle fait", "Il faut", "Passerelle"], [
  ["1. Lire la NCM", "Lit la spécification et la NCM, reconnaît les colonnes, normalise les identifiants et les statuts, affiche le diagnostic.", "Spécification, NCM", "Non (sauf OCR)"],
  ["2. Grille des écarts", "Met les exigences en face des lignes de NCM : statuts, justifications, trous, orphelins, doublons, incohérences, écarts ouverts.", "Étape 1", "Non"],
  ["3. Indexer les documents", "Construit l'index de recherche des documents du fournisseur.", "Dossier des documents", "Oui (embeddings)"],
  ["4. Vérifier les « C »", "Pour chaque « C », cherche le passage de la spécification fournisseur qui l'étaye et le note : étayé, faible, absent.", "Étapes 1 et 3", "Oui (reranking)"],
  ["5. Tracer vers la norme", "Pour chaque exigence, propose le paragraphe de la norme concerné ; vous relisez et validez dans un classeur.", "Dossier de la norme", "Oui"],
  ["6. Affaiblissements", "Relève dans les documents du fournisseur les formules d'affaiblissement proches d'un identifiant (TBD, where practicable, sous réserve…).", "Dossier des documents", "Non"],
  ["7. Fiches de revue", "Rédige pour chaque écart (NC, PC) une fiche : écart, risque, questions pour la revue, avec citations.", "Étape 1 (et 4 ou 3)", "Oui (modèle de synthèse)"],
  ["8. Capitaliser", "Enregistre la campagne dans la mémoire des projets.", "Étape 2, nom du projet", "Non"],
], [2000, 3900, 1900, 1272]));
c.push(P(""));

c.push(H2("Étape 1 — Lire la NCM"));
c.push(P("Renseignez la spécification, la NCM et le préfixe d'identifiant, puis cliquez **1. Lire la NCM**. La lecture prend quelques secondes (plus avec l'OCR). À la fin, l'onglet **Diagnostic** s'ouvre et la grille (étape 2) est calculée dans la foulée."));
c.push(...image("61_diagnostic_apres_lecture.png", "Le diagnostic après lecture : ce que SILLAGE a compris de la NCM, avant toute analyse."));
c.push(P("Lisez le diagnostic ligne par ligne, c'est lui qui dit si la NCM est exploitable :"));
c.push(B("**Référentiel : N exigences — préfixes d'identifiant : …** : le nombre d'exigences lues dans la spécification et les préfixes retenus (imposés par vous, ou déduits). Si un préfixe parasite s'y trouve, remplissez le champ « Préfixe d'identifiant »."));
c.push(B("**Tableaux lus / retenus** : les tableaux trouvés dans la NCM et ceux qui ont une colonne d'identifiants. « retenus : 0 » = rien n'a été reconnu (voir la section 8)."));
c.push(B("**Lignes, avec identifiant, lignes pivot** : les lignes lues, celles qui portent un identifiant, et le nombre de lignes finales après dépliage des plages (« REQ-100 à REQ-103 » donne quatre lignes)."));
c.push(B("**Statuts reconnus** : la part des lignes dont le statut a été compris. Les valeurs inconnues sont listées ; une fenêtre vous propose de dire à quoi elles correspondent."));
c.push(B("**Identifiants du référentiel retrouvés** : la part de vos exigences présentes dans la NCM. Près de 100 % pour une NCM complète ; bas pour une NCM partielle ou pré-filtrée."));
c.push(B("**Colonnes** : quelle colonne a été prise pour l'identifiant, le statut, la justification, l'exigence répétée, les champs optionnels (action, échéance, dérogation…). C'est la première chose à vérifier quand un résultat surprend."));
c.push(B("Les lignes **⚠** sont des avertissements : NCM pré-filtrée détectée, quadrillage inexploitable relu ligne par ligne, feuille ignorée, OCR utilisé…"));
c.push(H3("Les valeurs de statut inconnues"));
c.push(...image("66_dialogue_statuts.png", "Une valeur de statut que SILLAGE ne connaît pas : dites à quoi elle correspond, elle est mémorisée pour toutes les NCM suivantes.", 12));
c.push(P("SILLAGE connaît le vocabulaire courant (C / PC / NC / NA, Compliant / Partially compliant / Non compliant / N/A, Conforme / Partiel…). Une valeur nouvelle (« Vert », « Full », « Accepté sous réserve ») est proposée dans cette fenêtre : choisissez le statut pivot, ou « ignorer ». Le choix est enregistré dans le dictionnaire du poste (menu Outils → Dictionnaire des statuts)."));
c.push(H3("Le profil des colonnes du fournisseur"));
c.push(...image("67_dialogue_profil.png", "Le profil des colonnes : ce que SILLAGE a reconnu, à confirmer une fois par fournisseur.", 12));
c.push(P("Les colonnes sont reconnues par leur contenu, pas par leur nom. Le bouton **Profil des colonnes…** montre le résultat et vous laisse corriger (par exemple si la justification a été prise dans la mauvaise colonne). Une fois enregistré, le profil s'applique aux NCM suivantes du même fournisseur tant que ses en-têtes ne changent pas."));
c.push(saut());

c.push(H2("Étape 2 — Grille des écarts"));
c.push(P("La grille se calcule seule après la lecture ; le bouton **2. Grille des écarts** la recalcule après un changement (case « NCM pré-filtrée », traçabilité chargée, etc.) et annonce son résultat dans la barre d'état."));
c.push(...image("62_grille_complete_detail_nc.png", "La grille : une ligne par exigence, colorée par statut ; à droite, le détail de la ligne sélectionnée."));
c.push(P("Chaque ligne donne l'identifiant, le statut pivot, le niveau d'appui (après l'étape 4), la clôture de l'écart (dérogation, action, ouvert), les alertes et le texte de l'exigence. Cliquez une ligne : le volet **Détail** montre l'exigence, la ligne de NCM (statut brut, justification, origine : fichier, feuille, ligne), les alertes, le passage d'appui, la fiche de revue et l'historique."));
c.push(P("Au-dessus de la grille, trois **filtres** : par statut (« écarts (PC + NC) »…), par alerte (« trou », « « C » sans appui »…), et par texte. Le compteur « n / N lignes » indique ce qui est affiché. L'onglet **Résumé** donne les totaux."));
c.push(...image("65_filtre_ecarts.png", "Le filtre « écarts (PC + NC) » : la liste de ce qu'il y a à discuter avec le fournisseur.", 13));
c.push(saut());

c.push(H2("Étape 3 — Indexer les documents du fournisseur"));
c.push(P("Donnez le dossier « Documents du fournisseur » et cliquez **3. Indexer les documents**. Le moteur de RIVAGE embarqué extrait le texte (OCR compris pour les scans), le découpe et calcule les vecteurs via la passerelle. L'index est propre à SILLAGE et au fournisseur ; relancer l'étape après l'ajout de documents ne retraite que les nouveaux."));
c.push(Note("Première fois sur un poste ancien : le moteur sonde la configuration qui fonctionne (c'est le même mécanisme que RIVAGE). Le menu Outils → « Ce poste » montre ce qui tourne."));

c.push(H2("Étape 4 — Vérifier les « C »"));
c.push(P("Un « conforme » est une affirmation du fournisseur. L'étape 4 pose chaque exigence déclarée conforme comme question dans ses documents et regarde si un passage y répond vraiment. Le résultat est un niveau d'**appui** :"));
c.push(table(["Niveau", "Sens", "À faire"], [
  ["étayé", "Un passage de la spécification fournisseur répond nettement à l'exigence.", "Rien, sauf si les valeurs chiffrées diffèrent (alerte « valeurs absentes »)."],
  ["faible", "Un passage ressemble, sans être convaincant.", "À regarder en revue."],
  ["absent", "Rien de comparable dans ses documents.", "En tête de l'ordre du jour : un « C » de complaisance ?"],
  ["non vérifié", "Le reranking n'était pas disponible.", "Vérifier la passerelle, relancer."],
], [1800, 4300, 2972]));
c.push(P(""));
c.push(...image("63_detail_c_appui_journal.png", "Le détail d'un « C » étayé : le niveau, le score, le document et le passage qui l'appuient."));
c.push(P("Les réglages à droite des outils : **Vérification — statuts** (C par défaut ; mettez « C,PC » pour vérifier aussi les partiels), **seuil étayé** et **faible** (0,5 et 0,25 par défaut). Ces seuils dépendent du modèle de reranking de la passerelle : calibrez-les sur une vingtaine d'exigences dont vous connaissez la réponse, en regardant la distribution des scores dans le Journal."));
c.push(...image("64_filtre_c_sans_appui.png", "Le filtre « « C » sans appui » : les conformes que rien n'étaye.", 13));
c.push(saut());

c.push(H2("Étape 5 — Tracer vers la norme"));
c.push(P("Donnez le dossier « Norme applicable (documents) » et cliquez **5. Tracer vers la norme**. La première fois, la norme est indexée dans un index qui lui est propre (rien à régler) ; puis, pour chaque exigence sans paragraphe de norme, SILLAGE propose les paragraphes cités par ses meilleurs passages, avec un score."));
c.push(...image("68_dialogue_tracer.png", "L'étape 5 : la norme, son index, le seuil de proposition, et la reprise ou non des exigences déjà tracées.", 12));
c.push(P("À la fin, SILLAGE vous propose d'enregistrer un **classeur de relecture** : une ligne par exigence avec le paragraphe proposé, les candidats et leurs scores, le passage et le document. Vous corrigez la colonne « Norme retenue », cochez « Validé », puis **Outils → Charger une traçabilité** pour l'appliquer. L'applicabilité d'un paragraphe est une décision humaine : rien n'est pris sans relecture."));
c.push(P("Une fois la traçabilité chargée, tout écart (NC, PC, trou, statut inconnu) sur une exigence tracée porte l'alerte **impact norme**, et la grille exportée contient une feuille « Impact norme » : la liste des risques de certification. La feuille « Couverture norme » liste les paragraphes de la norme cités et les exigences qui s'y rattachent ; un paragraphe sans exigence est soit non applicable, soit un manque de votre spécification."));
c.push(Note("Si les documents de la norme changent : **Outils → Réindexer la norme…**"));

c.push(H2("Étape 6 — Affaiblissements"));
c.push(P("Un fournisseur qui se ménage une sortie l'écrit : « TBD », « TBC », « where practicable », « best effort », « sous réserve », « dans la mesure du possible », « ultérieurement »… L'étape 6 parcourt ses documents et relève ces formules quand elles sont proches d'un identifiant d'exigence. Chaque exigence concernée reçoit l'alerte **affaiblissement**, avec les expressions et les fichiers dans le détail. Aucun appel à la passerelle."));

c.push(H2("Étape 7 — Fiches de revue"));
c.push(P("Pour chaque exigence NC ou PC, le modèle de la passerelle reçoit l'exigence, la ligne de NCM, le paragraphe de norme s'il est tracé et au plus trois passages de la spécification fournisseur (ceux de l'étape 4, ou cherchés dans l'index). Il rédige une fiche en trois parties : **ÉCART** (ce que le fournisseur ne fait pas ou fait autrement), **RISQUE** (technique, et certification si un paragraphe est tracé), **QUESTIONS POUR LA REVUE** (au plus trois). Chaque affirmation cite sa source ([1], [2] ou « (NCM) ») ; le modèle ne rend aucun verdict."));
c.push(P("La fiche apparaît dans le volet Détail et dans la grille exportée. **Outils → Exporter les fiches de revue (Word)** produit le dossier de revue, une fiche par page, à emporter en réunion."));
c.push(Attention("Ce qui part vers la passerelle à l'étape 7 : une exigence, une ligne de NCM, un paragraphe de norme, trois passages de 800 caractères. Jamais un document entier, jamais la NCM entière."));

c.push(H2("Étape 8 — Capitaliser"));
c.push(P("Donnez le nom du projet, cochez « NCM finale signée » si c'est le cas, et cliquez **8. Capitaliser** : la grille est enregistrée dans la mémoire des projets (une base locale sur le poste, jamais sur le partage). À la lecture suivante d'une NCM, chaque exigence est annotée : « vue sur 3 campagnes, contestée 2 fois ; écart déjà accepté (PROJET-1, Fournisseur B, RFD-0101) »."));
c.push(...image("70_memoire_campagnes.png", "Mémoire des projets : les campagnes enregistrées.", 12));
c.push(...image("71_memoire_contestees.png", "Les exigences souvent contestées : à savoir avant la prochaine consultation, elles sont peut-être mal écrites ou trop ambitieuses.", 12));
c.push(...image("72_memoire_acceptes.png", "Les écarts déjà acceptés, par exigence ou par paragraphe de norme, avec la dérogation et la justification.", 12));
c.push(P("Le bouton **Mémoire des projets…** ouvre cette fenêtre : campagnes, exigences contestées, écarts acceptés, bilans par paragraphe de norme et par fournisseur, recherche dans les justifications, export Excel."));
c.push(saut());

// 6
c.push(H1("5. Lire la grille : statuts, alertes, détail"));
c.push(H2("Les quatre statuts pivot"));
c.push(table(["Statut", "Couleur", "Sens"], [
  ["C", "vert", "Conforme : le fournisseur dit satisfaire l'exigence."],
  ["PC", "jaune", "Partiellement conforme : une partie seulement, ou sous conditions."],
  ["NC", "rouge", "Non conforme : il ne satisfait pas l'exigence (dérogation ou action attendue)."],
  ["NA", "gris", "Non applicable selon le fournisseur : à confirmer de votre côté."],
  ["?", "beige", "Inconnu : pas de ligne dans la NCM (trou), statut vide, valeur hors dictionnaire, ou lignes contradictoires."],
], [1200, 1400, 6472]));
c.push(P(""));
c.push(H2("Les alertes"));
c.push(table(["Alerte", "Ce que ça veut dire", "Ce qu'on en fait"], [
  ["trou", "L'exigence n'a aucune ligne dans la NCM.", "Demander la réponse manquante (ou cocher « NCM pré-filtrée » si la NCM ne liste que les écarts)."],
  ["conforme implicite", "Absente d'une NCM pré-filtrée : conforme par défaut, sans justification.", "Accepter ou demander une confirmation explicite sur les exigences critiques."],
  ["orphelin", "La NCM cite un identifiant qui n'est pas dans votre spécification.", "Erreur de numérotation du fournisseur, ou exigence d'une autre révision de la spécification."],
  ["doublon", "Plusieurs lignes pour la même exigence, même statut.", "Sans gravité ; la NCM est à nettoyer."],
  ["incohérence", "Plusieurs lignes pour la même exigence, statuts différents.", "À trancher en revue : lequel est le bon ?"],
  ["statut inconnu", "La valeur de statut n'est pas dans le dictionnaire.", "L'apprendre au dictionnaire (fenêtre à la lecture, ou Outils)."],
  ["écart ouvert", "NC ou PC sans action ni dérogation.", "Exiger un plan d'action ou une demande de dérogation."],
  ["« C » sans appui / à appui faible", "Rien, ou peu, dans les documents du fournisseur n'étaye ce conforme.", "Demander la preuve."],
  ["valeurs absentes", "Les nombres de l'exigence (« 2 kg », « 28 V ») ne figurent pas dans le passage d'appui.", "Vérifier la valeur annoncée par le fournisseur."],
  ["affaiblissement", "Une formule d'affaiblissement près de l'identifiant dans ses documents.", "Lire le passage ; faire préciser."],
  ["impact norme", "Écart sur une exigence tracée vers un paragraphe de la norme.", "Risque de certification : priorité de la revue."],
], [2300, 3400, 3372]));
c.push(saut());

// 7
c.push(H1("6. Comparer deux révisions de NCM"));
c.push(P("Lisez la NCM la plus récente (étape 1), puis **Comparer avec une révision antérieure…** et choisissez l'ancienne NCM. La fenêtre liste, par identifiant : les exigences nouvelles, disparues, les statuts changés (amélioration ou **régression**) et les justifications réécrites. Une ligne disparue est ambiguë (corrigée ou oubliée ?) : elle est signalée, jamais tranchée. **Fichier → Exporter le diff** écrit le classeur."));
c.push(...image("69_diff.png", "Le diff entre deux révisions : régressions en rouge, améliorations en vert."));
c.push(saut());

// 8
c.push(H1("7. Exporter : grille Excel, fiches Word, fichiers rejouables"));
c.push(B("**Exporter la grille…** (bouton ou menu Fichier) : un classeur avec les feuilles **Grille** (une ligne par exigence, vingt-cinq colonnes : statut, justification, clôture, action, échéance, dérogation, alertes, origine, appui, passage, valeurs absentes, affaiblissements, fiche de revue, historique), **Alertes**, **Impact norme** et **Couverture norme** (si la norme est donnée), **Résumé**."));
c.push(B("**Outils → Exporter les fiches de revue (Word)** : le dossier de revue, une fiche par page."));
c.push(B("**Outils → Enregistrer… (JSON)** : la NCM normalisée, la campagne d'appuis, les fiches, en fichiers rejouables. **Reprendre…** les recharge sans nouvel appel à la passerelle : utile pour reprendre une revue le lendemain."));
c.push(saut());

// 9
c.push(H1("8. Quand ça ne marche pas"));
c.push(H2("« Tableaux lus : N, retenus : 0 », tout est en trou"));
c.push(P("SILLAGE a trouvé des tableaux mais aucun n'a de colonne d'identifiants reconnue. Dans l'ordre :"));
c.push(...liste([
  "Regardez la première ligne du diagnostic : les **préfixes d'identifiant** retenus. S'ils ne correspondent pas à la NCM (« REQ_B2-3 » dans la spécification, « REQ-B2-3 » dans la NCM, c'est toléré ; mais « TSO-C7 » n'est pas un préfixe), remplissez le champ **Préfixe d'identifiant** et relancez.",
  "Si la NCM est un PDF scanné (texte non sélectionnable dans votre lecteur PDF), cochez **OCR de secours** et relancez. Si la couche texte existe mais est fausse (OCR du scanner), SILLAGE basculera de lui-même sur le modèle Vision ; cochez « forcer sur toutes les pages » pour aller droit au but.",
  "Si la NCM est un classeur, vérifiez que l'onglet de la NCM n'a pas un nom qui la fait écarter (« Deleted lines », « VC vs VB », « Feuil1 » sont ignorés : le diagnostic le dit).",
  "Toujours rien : la commande d'inspection ci-dessous montre ce que SILLAGE voit, page par page.",
]));
c.push(H2("La moitié des exigences est en trou"));
c.push(P("Soit la NCM est partielle ou pré-filtrée (« unless listed below, all other items can be assumed compliant ») : cochez **NCM pré-filtrée**. Soit les identifiants sont écrits de deux façons dans la NCM : le diagnostic donne le taux d'identifiants retrouvés ; le préfixe imposé règle la plupart des cas."));
c.push(H2("Les statuts sont tous « ? »"));
c.push(P("La colonne des statuts n'a pas été reconnue (ligne « Colonnes » du diagnostic sans « statut »), ou son vocabulaire est inconnu (« Statuts inconnus » dans le diagnostic) : la fenêtre des statuts inconnus s'ouvre à la lecture, dites à quoi correspondent les valeurs. Si la colonne elle-même est mauvaise : **Profil des colonnes…**."));
c.push(H2("« Partially compliant » ressort en « Compliant »"));
c.push(P("Cas typique d'un PDF sans quadrillage ou d'un scan, où le statut est sur une autre ligne que l'identifiant. Depuis la version 0.8, les lignes de suite sont recollées et l'OCR est lu par identifiant ; si cela se reproduit, envoyez le rapport d'inspection."));
c.push(H2("« 2. Grille des écarts » ne fait rien"));
c.push(P("La grille est déjà affichée après la lecture ; le bouton la recalcule et le dit dans la barre d'état. Si vous venez de modifier le préfixe d'identifiant, il relance la lecture."));
c.push(H2("Une boîte d'erreur s'ouvre"));
c.push(P("Lisez le message, puis l'onglet **Journal** : la trace complète y est. Les erreurs des étapes 3 à 7 viennent le plus souvent de la passerelle (clé absente ou refusée : « RIVAGE_SYNTH_KEY », « 401 », « 403 »), du réseau, ou d'un index absent (« Index introuvable » : refaites l'étape 3). **Outils → Ce poste** montre la version du moteur, de faiss, du processeur et de la mémoire."));
c.push(...image("74_fiche_poste.png", "La fiche « Ce poste » : moteur, faiss, processeur, mémoire.", 11));
c.push(H2("Le rapport d'inspection"));
c.push(P("Quand une lecture échoue et que vous ne pouvez pas transmettre le document, la commande suivante écrit un rapport qui ne contient que des comptes et des débuts de cellules (25 caractères) :"));
c.push(new Paragraph({ spacing: { after: 120 }, indent: { left: 360 },
  children: [new TextRun({ text: "sillage_app.py inspecter chemin\\vers\\NCM.pdf --spec chemin\\vers\\spec.pdf --sortie rapport.txt", font: "Consolas", size: 20 })] }));
c.push(P("Il dit, page par page : la couche texte, les tableaux trouvés par quadrillage et leurs cellules effondrées, la lecture ligne par ligne, les identifiants trouvés, et si l'OCR est disponible. Avec `--pages 0`, aucun extrait de texte n'est inclus."));
c.push(saut());

// 10
c.push(H1("9. Où sont vos données, ce qui part vers la passerelle"));
c.push(P("Tout ce que SILLAGE apprend et calcule reste **sur le poste**, dans le dossier de données (%LOCALAPPDATA%\\sillage sous Windows) :"));
c.push(table(["Quoi", "Où", "Rôle"], [
  ["statuts.json", "dossier de données", "Les valeurs de statut que vous avez apprises au dictionnaire."],
  ["profils\\<fournisseur>.json", "dossier de données", "Les colonnes confirmées pour chaque fournisseur."],
  ["index\\<fournisseur>, index\\norme_<dossier>", "dossier de données", "Les index de recherche (étapes 3 et 5)."],
  ["memoire.sqlite", "dossier de données", "La mémoire des campagnes (étape 8)."],
  ["ocr_cache.sqlite", "dossier de données", "Les pages déjà lues par l'OCR : relire ne repaie rien."],
  ["gui.json", "dossier de données", "Vos chemins et réglages de la dernière session."],
], [3300, 2200, 3572]));
c.push(P(""));
c.push(P("Ce qui part vers la passerelle d'IA, et seulement si vous lancez l'étape correspondante : des fragments de texte des documents du fournisseur et de la norme pour les vecteurs (étapes 3 et 5), une exigence et des passages pour le reranking (4, 5), une exigence, une ligne de NCM et trois passages pour une fiche (7), l'image d'une page pour l'OCR. Jamais la NCM entière, jamais un document entier, jamais la mémoire des projets."));
c.push(saut());

// 11
c.push(H1("10. Glossaire"));
c.push(table(["Terme", "Définition"], [
  ["NCM", "Non-Compliance Matrix : le tableau par lequel le fournisseur répond, exigence par exigence, à votre spécification."],
  ["Référentiel", "Votre spécification, lue comme liste d'exigences identifiées."],
  ["Identifiant", "Le numéro d'une exigence (« HLTR_APU_10 », « REQ_B2-3_15 »). C'est vous qui l'imposez ; le fournisseur le reprend ; tout s'y rattache."],
  ["Statut pivot", "C, PC, NC, NA : les quatre valeurs auxquelles tout vocabulaire de fournisseur est ramené."],
  ["Ligne pivot", "Une ligne de NCM normalisée : un identifiant, un statut, une justification, des champs optionnels."],
  ["Grille des écarts", "Une ligne par exigence de votre spécification, avec ce que la NCM en dit et les alertes."],
  ["Appui", "Le passage de la spécification fournisseur qui étaye un « C », et son niveau (étayé, faible, absent)."],
  ["Affaiblissement", "Une formule qui atténue un engagement (TBD, where practicable, sous réserve…)."],
  ["Traçabilité", "Le lien entre une exigence et le paragraphe de la norme qu'elle décline."],
  ["Impact norme", "Un écart sur une exigence tracée : un risque pour la certification."],
  ["Fiche de revue", "Le texte ÉCART / RISQUE / QUESTIONS rédigé pour un écart, à partir d'éléments cités."],
  ["Mémoire des projets", "La base locale des campagnes passées : exigences contestées, écarts acceptés."],
  ["Passerelle", "Le service d'IA de l'entreprise (embeddings, reranking, synthèse, OCR Vision), le même que pour RIVAGE."],
  ["Moteur embarqué", "La copie du moteur de recherche de RIVAGE incluse dans SILLAGE : aucune installation de RIVAGE n'est nécessaire."],
], [2600, 6472]));
c.push(P(""));
c.push(new Paragraph({ spacing: { before: 400 }, alignment: AlignmentType.CENTER,
  children: [new TextRun({ text: `SILLAGE ${version} — guide généré le ${date}. Pour aller plus loin : docs/SILLAGE_comment_ca_marche_principes.pptx, README.md, CHANGELOG.md.`, italics: true, size: 18, color: "777777" })] }));

// --- document --------------------------------------------------------------------
const doc = new Document({
  creator: "Renaud Loison", title: "SILLAGE — Guide d'utilisation",
  description: "Guide d'utilisation de SILLAGE, par Renaud Loison, auteur, créateur et développeur",
  styles: {
    default: { document: { run: { font: "Calibri", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 34, bold: true, color: "0F4C5C", font: "Calibri" }, paragraph: { outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 27, bold: true, color: "2A9D8F", font: "Calibri" }, paragraph: { outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 23, bold: true, color: "2A9D8F", font: "Calibri" }, paragraph: { outlineLevel: 2 } },
    ],
  },
  numbering: { config: [
    { reference: "puces", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 540, hanging: 270 } } } }] },
    ...NUM_REFS.map(r => ({ reference: r, levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 540, hanging: 360 } } } }] })),
  ] },
  sections: [{
    properties: { page: { margin: { top: 1418, bottom: 1418, left: 1418, right: 1418 } } },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT,
      children: [new TextRun({ text: `SILLAGE ${version} — Guide d'utilisation — Renaud Loison`, size: 18, color: "777777" })] })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
      children: [new TextRun({ text: "Page ", size: 18, color: "777777" }),
        new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "777777" })] })] }) },
    children: c,
  }],
});

Packer.toBuffer(doc).then(buf => { fs.writeFileSync(outPath, buf); console.log("écrit", outPath, buf.length, "octets"); });
