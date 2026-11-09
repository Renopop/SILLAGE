// SILLAGE — « Comment ça marche » : principes, étapes, règles, interface.
//
// Usage : node make_deck_sillage.js <sortie.pptx> <version> [dossier_captures]
// Dépendance : pptxgenjs (npm install pptxgenjs, à côté de ce script ou NODE_PATH).
// Le thème de couleurs est écrit dans le fichier par apply_theme.js si son chemin
// est donné par APPLY_THEME_JS ; sinon le deck garde la palette Office.
const fs = require("fs");
const path = require("path");
process.env.NODE_PATH = [path.join(__dirname, "node_modules"), process.env.NODE_PATH].filter(Boolean).join(path.delimiter);
require("module").Module._initPaths();
const pptxgen = require("pptxgenjs");
let applyTheme = null;
const themeJs = process.env.APPLY_THEME_JS || "";
if (themeJs && fs.existsSync(themeJs)) ({ applyTheme } = require(themeJs));

const [,, outPath, version, imgDir] = process.argv;

const THEME = {
  name: "SILLAGE Sillage",
  headFontFace: "Cambria", bodyFontFace: "Calibri",
  colors: { dk1: "16263A", lt1: "FFFFFF", dk2: "0F4C5C", lt2: "EAF1F5",
            accent1: "0F4C5C", accent2: "2A9D8F", accent3: "E0A458", accent4: "6B8F71",
            accent5: "9A3B3B", accent6: "7B6D8D", hlink: "0F4C5C", folHlink: "6B8F71" },
};
const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";                  // 13.333 × 7.5
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.title = "SILLAGE — Comment ça marche";
pres.author = "Renaud Loison";
pres.subject = "SILLAGE — auteur, créateur et développeur : Renaud Loison";
const C = pres.SchemeColor;

// --- layouts -------------------------------------------------------------------
pres.defineSlideMaster({
  title: "TITRE", background: { color: THEME.colors.dk1 },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 0.8, y: 2.3, w: 11.7, h: 1.4,
        fontSize: 44, bold: true, color: C.background1, align: "left", margin: 0 } } },
    { placeholder: { options: { name: "body", type: "body", x: 0.8, y: 3.8, w: 11.7, h: 2.6,
        fontSize: 20, color: C.background2, align: "left", valign: "top", margin: 0 } } },
  ],
});
pres.defineSlideMaster({
  title: "SECTION", background: { color: THEME.colors.dk2 },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 0.8, y: 2.6, w: 11.7, h: 1.3,
        fontSize: 40, bold: true, color: C.background1, align: "left", margin: 0 } } },
    { placeholder: { options: { name: "body", type: "body", x: 0.8, y: 4.0, w: 11.7, h: 1.2,
        fontSize: 20, color: C.background2, align: "left", margin: 0 } } },
  ],
});
pres.defineSlideMaster({
  title: "CONTENU", background: { color: THEME.colors.lt1 },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 0.6, y: 0.35, w: 12.1, h: 0.9,
        fontSize: 30, bold: true, color: C.text2, align: "left", margin: 0 } } },
    { text: { text: `SILLAGE ${version} — comment ça marche — Renaud Loison, auteur, créateur et développeur`, options: { x: 0.6, y: 7.05, w: 10, h: 0.3,
        fontSize: 10, color: "7A8A99", margin: 0, isTextBox: true } } },
  ],
  slideNumber: { x: 12.3, y: 7.05, w: 0.6, h: 0.3, fontSize: 10, color: "7A8A99", align: "right" },
});

// --- aides ---------------------------------------------------------------------
function slide(titre, section) {
  const s = pres.addSlide({ masterName: "CONTENU", sectionTitle: section });
  s.addText(titre, { placeholder: "title" });
  return s;
}
function bullets(s, items, x, y, w, h, size = 15, color = C.text1) {
  s.addText(items.map((t, i) => ({
    text: t, options: { bullet: { indent: 14 }, breakLine: i < items.length - 1, paraSpaceAfter: 6 },
  })), { x, y, w, h, fontSize: size, color, valign: "top", margin: 0, isTextBox: true });
}
function para(s, t, x, y, w, h, size = 14, color = C.text1, opts = {}) {
  s.addText(t, { x, y, w, h, fontSize: size, color, valign: "top", margin: 0, isTextBox: true, ...opts });
}
function card(s, x, y, w, h, titre, texte, fill = C.background2, titreColor = C.text2, size = 13) {
  s.addShape(pres.ShapeType.roundRect, { x, y, w, h, fill: { color: fill }, line: { color: fill }, rectRadius: 0.08 });
  s.addText(titre, { x: x + 0.15, y: y + 0.1, w: w - 0.3, h: 0.4, fontSize: 15, bold: true, color: titreColor, margin: 0, isTextBox: true });
  s.addText(texte, { x: x + 0.15, y: y + 0.5, w: w - 0.3, h: h - 0.6, fontSize: size, color: C.text1, valign: "top", margin: 0, isTextBox: true });
}
function formule(s, t, x, y, w, h, size = 20) {
  s.addShape(pres.ShapeType.roundRect, { x, y, w, h, fill: { color: "F4F7F9" }, line: { color: "D5DEE5", width: 1 }, rectRadius: 0.06 });
  s.addText(t, { x: x + 0.2, y, w: w - 0.4, h, fontSize: size, fontFace: "Cambria", color: C.text2, valign: "middle", align: "center", margin: 0, isTextBox: true });
}
function box(s, x, y, w, h, t, fill = C.accent1, color = C.background1, size = 13, bold = true) {
  s.addShape(pres.ShapeType.roundRect, { x, y, w, h, fill: { color: fill }, line: { color: fill }, rectRadius: 0.08 });
  s.addText(t, { x, y, w, h, fontSize: size, bold, color, align: "center", valign: "middle", margin: 0.05, isTextBox: true });
}
function fleche(s, x1, y, x2) {
  s.addShape(pres.ShapeType.line, { x: x1, y, w: x2 - x1, h: 0, line: { color: "7A8A99", width: 1.5, endArrowType: "triangle" } });
}
function stat(s, x, y, w, valeur, libelle, color = C.accent2) {
  s.addText(valeur, { x, y, w, h: 0.9, fontSize: 40, bold: true, color, align: "center", margin: 0, isTextBox: true });
  s.addText(libelle, { x, y: y + 0.9, w, h: 0.6, fontSize: 12, color: "5A6A79", align: "center", margin: 0, isTextBox: true });
}
function tableau(s, rows, x, y, w, colW, size = 12) {
  const head = rows[0].map(t => ({ text: t, options: { bold: true, color: C.background1, fill: { color: THEME.colors.accent1 }, fontSize: size } }));
  const body = rows.slice(1).map((r, i) => r.map(t => ({ text: t, options: { fontSize: size, color: C.text1,
    fill: { color: i % 2 ? "FFFFFF" : "F4F7F9" } } })));
  s.addTable([head, ...body], { x, y, w, colW, border: { type: "solid", color: "D5DEE5", pt: 0.75 }, margin: 0.06, valign: "middle" });
}
function image(s, name, x, y, w, maxH) {
  const p = imgDir ? path.join(imgDir, name) : "";
  if (!p || !fs.existsSync(p)) return;
  const buf = fs.readFileSync(p);
  const pw = buf.readUInt32BE(16), ph = buf.readUInt32BE(20);
  let h = w * ph / pw;
  if (maxH && h > maxH) { h = maxH; w = h * pw / ph; }
  s.addShape(pres.ShapeType.rect, { x: x - 0.03, y: y - 0.03, w: w + 0.06, h: h + 0.06, fill: { color: "D5DEE5" }, line: { color: "D5DEE5" } });
  s.addImage({ path: p, x, y, w, h });
}

// ===============================================================================
// Titre
pres.addSection({ title: "Introduction" });
let s = pres.addSlide({ masterName: "TITRE", sectionTitle: "Introduction" });
s.addText("SILLAGE — comment ça marche", { placeholder: "title" });
s.addText([
  { text: "De la spécification à la grille des écarts : principes, étapes, règles et interface", options: { breakLine: true } },
  { text: `Version ${version} — Spécifications, Incomplétudes, Livrables : Lecture, Analyse et Grille des Écarts`, options: { fontSize: 14, color: "9FB3C8", breakLine: true } },
  { text: " ", options: { fontSize: 10, breakLine: true } },
  { text: "Renaud Loison", options: { fontSize: 20, bold: true, color: C.background1, breakLine: true } },
  { text: "auteur, créateur et développeur de SILLAGE", options: { fontSize: 14, color: "9FB3C8" } },
], { placeholder: "body" });

// Sommaire
s = slide("Ce que nous allons voir", "Introduction");
const som = [["1", "Pourquoi SILLAGE, le problème et les principes", "complétude des réponses fournisseurs, trois matrices, l'identifiant comme invariant"],
  ["2", "Lire une NCM", "identifiants, statuts, tableaux, colonnes reconnues, profils, diagnostic"],
  ["3", "Contrôler et comparer", "trous, orphelins, doublons, incohérences, clôture, grille, diff entre révisions"],
  ["4", "Le moteur embarqué au service de la conformité", "appuis des « C », valeurs chiffrées, affaiblissements, traçabilité vers la norme"],
  ["5", "Fiches de revue et mémoire des projets", "la seule étape générative ; capitaliser entre projets"],
  ["6", "L'interface et l'autonomie", "les huit étapes à l'écran, lanceur, exécutable, postes anciens"],
  ["7", "Atouts, gains, audit et perspectives", "et les règles en une page"]];
som.forEach((r, i) => {
  const y = 1.35 + i * 0.8;
  box(s, 0.8, y, 0.6, 0.6, r[0], C.accent2, C.background1, 18);
  para(s, r[1], 1.7, y - 0.02, 8, 0.4, 18, C.text2, { bold: true });
  para(s, r[2], 1.7, y + 0.34, 10.5, 0.3, 12, "5A6A79");
});

// Pourquoi SILLAGE ?
s = slide("Pourquoi SILLAGE ?", "Introduction");
para(s, "On fournit une spécification à un fournisseur. Il répond par sa propre spécification, puis par une ou plusieurs matrices de non-conformité (NCM), tableaux reprenant la numérotation de nos exigences, qui convergent vers une NCM finale signée. Il faut s'assurer que la réponse est complète, que chaque statut est sincère, et que les écarts ne mettent pas en cause la norme fixée (CS-25, par exemple).", 0.6, 1.4, 7.4, 1.9, 15);
bullets(s, [
  "Aujourd'hui, c'est un travail de relecture à la main : trois documents à recouper pour deux cents exigences, à chaque révision de NCM.",
  "Les NCM varient d'un fournisseur à l'autre (colonnes, vocabulaire des statuts, granularité), mais elles reprennent toujours nos identifiants.",
  "Un « conforme » est une affirmation : personne ne vérifie, ligne par ligne, qu'il est étayé par la spécification du fournisseur.",
  "Ce qu'on a accepté la dernière fois, et pourquoi, dort dans les dossiers des projets précédents.",
], 0.6, 3.4, 7.4, 3.2, 14);
card(s, 8.4, 1.4, 4.3, 1.5, "Complétude", "Chaque exigence a-t-elle une réponse ? Aucune ligne ne cite-t-elle un identifiant inconnu ?", C.background2, C.text2, 12);
card(s, 8.4, 3.05, 4.3, 1.5, "Sincérité et norme", "Les « C » sont-ils étayés ? Les écarts touchent-ils un paragraphe de la norme ?", C.background2, C.text2, 12);
card(s, 8.4, 4.7, 4.3, 1.9, "L'origine", "SILLAGE a été imaginé, conçu et développé par Renaud Loison, dans le sillage de RIVAGE : les mêmes principes et le même moteur, appliqués à la conformité des fournisseurs.", "FFF4E3", C.accent3, 12);

// Le problème : trois matrices
s = slide("Le problème : trois matrices, pas une", "Introduction");
para(s, "Trois objets : votre spécification (S), la réponse du fournisseur (R : sa spécification et ses NCM), la norme (N). La conformité se décompose en trois questions distinctes, qu'il vaut mieux ne pas mélanger :", 0.6, 1.4, 12.1, 0.9, 15);
tableau(s, [["Matrice", "Question", "Référentiel", "Corpus interrogé"],
  ["R contre S", "Chaque exigence de ma spec a-t-elle une réponse ? Est-elle satisfaite ?", "S (liste d'exigences)", "R (NCM, spécification fournisseur)"],
  ["S contre N", "Ma spec couvre-t-elle les paragraphes applicables de la norme ?", "N (paragraphes)", "S"],
  ["R contre N", "Le fournisseur démontre-t-il la conformité à chaque paragraphe applicable ?", "N", "R"]],
  0.6, 2.5, 12.1, [1.8, 5.3, 2.4, 2.6], 13);
bullets(s, [
  "Dans les trois cas, la mécanique est la même : une ligne du référentiel devient une question posée dans un corpus restreint. C'est la chaîne de RIVAGE (portée, recherche hybride, reranking), embarquée dans SILLAGE.",
  "Mais la plus grande partie du travail est une jointure : la NCM reprend nos identifiants, donc R contre S se calcule sans aucune IA.",
], 0.6, 4.6, 12.1, 2.0, 14);

// Principes
s = slide("Les principes", "Introduction");
const princ = [["L'identifiant, invariant", "C'est nous qui imposons la numérotation des exigences ; le fournisseur la reprend. Tout s'y rattache : SILLAGE ne dépend que de ce qui vient de nous."],
  ["Modèle pivot fixe", "Une ligne de NCM, quel que soit le fournisseur, devient : identifiant, statut (C, PC, NC, NA), justification, champs optionnels. Le reste est conservé en brut."],
  ["Déterminisme d'abord", "Trous, orphelins, doublons, diff, clôture, mémoire : aucune IA. Deux campagnes sur les mêmes fichiers donnent la même grille."],
  ["Adaptation mémorisée", "Les colonnes se reconnaissent par leur contenu ; ce qui ne se reconnaît pas est proposé, confirmé une fois, mémorisé dans un profil fournisseur."],
  ["IA minimale et bornée", "Le modèle n'écrit que les fiches de revue, à partir d'éléments cités, sans jamais chercher ni décider. Le verdict est celui de l'ingénieur."],
  ["Chaque cellule sourcée", "Fichier, feuille ou page, ligne ; chemin de lecture (tableau, profil) ; passage et document pour les appuis. Tout se vérifie d'un clic."]];
princ.forEach((p, i) => { const col = i % 3, row = Math.floor(i / 3); card(s, 0.6 + col * 4.1, 1.4 + row * 2.6, 3.9, 2.4, p[0], p[1], row ? "FFF4E3" : C.background2, row ? C.accent3 : C.text2, 12); });

// Architecture
s = slide("Architecture : un socle déterministe, un moteur embarqué", "Introduction");
box(s, 0.6, 1.5, 2.6, 1.0, "Spécification\n(référentiel)", C.accent6);
box(s, 0.6, 2.7, 2.6, 1.0, "NCM du fournisseur\n(tableaux)", C.accent6);
box(s, 0.6, 3.9, 2.6, 1.0, "Documents du fournisseur,\nnorme (CS-25)", C.accent6);
fleche(s, 3.2, 2.0, 3.7); fleche(s, 3.2, 3.2, 3.7); fleche(s, 3.2, 4.4, 3.7);
box(s, 3.7, 1.5, 3.2, 2.2, "Socle déterministe\nidentifiants · statuts · tableaux\ncolonnes · profils · contrôles\ndiff · mémoire · affaiblissements", C.accent1);
box(s, 3.7, 3.9, 3.2, 0.9, "Moteur de RIVAGE embarqué\nindex · portée · hybride · reranking", C.accent2);
fleche(s, 6.9, 2.6, 7.4); fleche(s, 6.9, 4.35, 7.4);
box(s, 7.4, 1.5, 2.6, 1.0, "Grille des écarts\n(Excel, écran)", C.accent3, C.text1);
box(s, 7.4, 2.7, 2.6, 1.0, "Appuis · traçabilité\n· fiches (JSON, Word)", C.accent3, C.text1);
box(s, 7.4, 3.9, 2.6, 0.9, "Mémoire des projets\n(SQLite local)", C.accent3, C.text1);
s.addShape(pres.ShapeType.line, { x: 5.3, y: 4.8, w: 0, h: 0.25, line: { color: "7A8A99", width: 1.5, endArrowType: "triangle", beginArrowType: "triangle" } });
box(s, 3.7, 5.05, 3.2, 0.55, "Passerelle d'IA (réseau) : embedding · reranking · synthèse", C.accent4, C.background1, 11);
para(s, "Entrées", 0.6, 1.2, 2.6, 0.3, 11, "5A6A79", { bold: true });
para(s, "Sur le poste, sans IA", 3.7, 1.2, 3.2, 0.3, 11, "5A6A79", { bold: true });
para(s, "Sorties", 7.4, 1.2, 2.6, 0.3, 11, "5A6A79", { bold: true });
card(s, 10.3, 1.5, 2.4, 3.3, "Seul appel réseau", "La passerelle d'IA de RIVAGE, avec les mêmes fichiers de clés. Sans elle : lecture, grille, diff, affaiblissements et mémoire fonctionnent ; les appuis sont « non vérifiés ».", C.background2, C.text2, 11);
bullets(s, [
  "Tout tourne sur le poste : lecture, contrôles, grille, mémoire. Le réseau ne sert qu'aux appels de modèle (embedding, reranking, synthèse).",
  "Le moteur de RIVAGE est copié tel quel (23 modules, synchronisables en une commande) : aucune installation de RIVAGE n'est nécessaire.",
  "Chaque étape écrit un fichier rejouable (NCM normalisée, appuis, traçabilité, fiches) : rien ne repart vers la passerelle deux fois.",
], 0.6, 5.8, 12.1, 1.2, 12);

// ===============================================================================
pres.addSection({ title: "Lire une NCM" });
s = pres.addSlide({ masterName: "SECTION", sectionTitle: "Lire une NCM" });
s.addText("1. Lire une NCM", { placeholder: "title" });
s.addText("Du tableau du fournisseur, quel qu'il soit, aux lignes pivot — sans IA", { placeholder: "body" });

// Identifiants
s = slide("Identifiants : reconnaître, normaliser, déplier", "Lire une NCM");
para(s, "L'identifiant est écrit de mille façons ([REQ_B2-3_15], REQ-001, req_0042, REQ 7, REQ-100a, « REQ-100 à REQ-120 »). SILLAGE le reconnaît par un motif, le ramène à l'écriture de la spécification, et déplie les plages en autant de lignes.", 0.6, 1.4, 7.4, 1.2, 15);
formule(s, "PRÉFIXE sép NUMÉRO[suffixe]        [REQ_B2-3_15], REQ-B2-3-015 → REQ_B2-3_15", 0.6, 2.7, 7.4, 0.8, 15);
bullets(s, [
  "Préfixe composé possible (le code de l'annexe en fait partie) : le numéro est le dernier groupe de chiffres ; crochets, casse, séparateurs et zéros de tête sont tolérés.",
  "Motif restreint aux préfixes du référentiel (déduits de la spécification) : « CS 25.1309 », « DO-178C » ou « B2-1 » ne sont jamais pris pour des exigences.",
  "Plages dépliées : « REQ-100 à REQ-103 », « REQ-100 to 103 », « REQ-100 – REQ-103 » donnent quatre lignes pivot ; plage inversée ou préfixes différents : pas de dépliage.",
  "Une cellule qui cite plusieurs identifiants donne plusieurs lignes ; le sous-identifiant (REQ-100a, REQ-100.2) est une exigence à part entière.",
  "Tri naturel : REQ-2 avant REQ-10, suffixes après le numéro.",
], 0.6, 3.7, 7.4, 3.0, 13);
card(s, 8.4, 1.4, 4.3, 2.3, "Le motif", "Préfixe commençant par une lettre, composé de segments liés par - ou _ (REQ, SYS_REQ, REQ_B2-3) ; séparateur optionnel ; numéro ; suffixe optionnel (lettre ou .n). Avec le référentiel : seuls ses préfixes, ramenés à son écriture.", C.background2, C.text2, 12);
card(s, 8.4, 3.9, 4.3, 2.7, "Pourquoi normaliser", "Un fournisseur écrit REQ-001, un autre req_1, un troisième REQ 1 : sans forme unique, la jointure rate et chaque exigence devient un trou. La forme normalisée est la clé de tout SILLAGE, jusqu'à la mémoire des projets.", "FFF4E3", C.accent3, 12);

// Statuts
s = slide("Dictionnaire des statuts : quatre valeurs pivot", "Lire une NCM");
["C — conforme", "PC — partiellement conforme", "NC — non conforme", "NA — non applicable", "? — inconnu"].forEach((t, i) => {
  box(s, 0.6 + i * 2.45, 1.4, 2.3, 0.7, t, [C.accent4, C.accent3, C.accent5, C.accent6, "B0BEC5"][i], i === 1 || i === 4 ? C.text1 : C.background1, 12);
});
bullets(s, [
  "Chaque fournisseur a son vocabulaire : C / PC / NC / NA, Compliant / Partially / Non-compliant, Full / Partial / Deviation, Oui / Partiel / Non… Un dictionnaire français-anglais-abréviations ramène tout à quatre valeurs.",
  "Reconnaissance tolérante : « C (see note 3) », « PC - voir §4 », « NC: deviation requested » sont reconnus (valeur entière, puis tête avant parenthèse ou tiret, puis premier mot, puis mots-clés contenus).",
  "Une valeur inconnue donne « ? » et figure dans le diagnostic ; l'utilisateur dit à quoi elle correspond, elle rejoint le dictionnaire du poste (statuts.json) pour toutes les NCM suivantes.",
], 0.6, 2.4, 7.4, 3.6, 13);
card(s, 8.4, 2.4, 4.3, 1.9, "Pourquoi un pivot", "Les contrôles, le diff, la mémoire et les fiches raisonnent sur quatre valeurs. Le statut brut est conservé à côté (la grille montre les deux).", C.background2, C.text2, 12);
card(s, 8.4, 4.5, 4.3, 2.0, "Ce que le dictionnaire ne fait pas", "Il ne devine pas : « Vert », « Rouge », « Full compliance » restent inconnus jusqu'à ce qu'on les lui apprenne, une fois.", "FFF4E3", C.accent3, 12);

// Tableaux
s = slide("Lire les tableaux : xlsx, docx, pdf, csv", "Lire une NCM");
const fmts = [["Classeur Excel (xlsx, xlsm)", "une feuille = un tableau ; cellules fusionnées dépliées (la valeur est recopiée dans toute la zone) ; lignes vides ignorées, numéros d'origine conservés"],
  ["Word (docx)", "chaque table du document ; cellules fusionnées horizontalement lues une fois"],
  ["PDF", "tableaux détectés par pdfplumber page par page ; tableaux coupés entre pages recollés (même nombre de colonnes, en-tête répété retiré)"],
  ["CSV", "délimiteur détecté (; , tabulation |)"]];
fmts.forEach((f, i) => card(s, 0.6, 1.4 + i * 1.3, 7.4, 1.2, f[0], f[1], C.background2, C.text2, 12));
card(s, 8.4, 1.4, 4.3, 2.4, "Chaque ligne garde son origine", "Fichier, feuille (ou page et tableau), numéro de ligne : la grille et le détail de l'interface renvoient à la cellule d'origine. Un contrôle contesté se vérifie en dix secondes.", C.background2, C.text2, 12);
card(s, 8.4, 4.0, 4.3, 2.6, "Lecture ligne par ligne", "Sans quadrillage, les colonnes sont déduites des alignements du texte et chaque ligne de texte devient une ligne ; les cellules hautes (« Partially » / « compliant ») ressortent en lignes de suite, recollées à la ligne de l'identifiant. Un PDF scanné passe par l'OCR de secours.", "FFF4E3", C.accent3, 12);

// Colonnes
s = slide("Reconnaître les colonnes par leur contenu, pas par leur nom", "Lire une NCM");
para(s, "Les en-têtes changent d'un fournisseur à l'autre (« Req ID », « Exigence », « Compliance », « Statut », « Our answer »…). SILLAGE regarde ce que les cellules contiennent :", 0.6, 1.4, 12.1, 0.8, 15);
formule(s, "colonne des identifiants = argmax(c) · #{ lignes dont la cellule c cite un identifiant }        retenue si ≥ 50 % des lignes", 0.6, 2.3, 12.1, 0.7, 15);
formule(s, "colonne des statuts = argmax(c ≠ id) · #{ cellules reconnues par le dictionnaire }        retenue si ≥ 40 % des lignes", 0.6, 3.1, 12.1, 0.7, 15);
bullets(s, [
  "Ligne d'en-tête : la première ligne dont la cellule d'identifiant n'en contient pas.",
  "Justification : la colonne de texte le plus long parmi les autres ; un en-tête explicite (« justification », « rationale ») prime.",
  "Colonnes optionnelles par mots-clés d'en-tête, du plus spécifique au plus général : dérogation (waiver, RFD), échéance (date, due), action (plan, mitigation), référence (ref, §, clause), commentaire.",
  "Repli : un en-tête de statut explicite (« status », « compliance ») avec des valeurs courtes est pris même si le vocabulaire est inconnu du dictionnaire.",
], 0.6, 4.0, 7.4, 2.8, 13);
card(s, 8.4, 4.0, 4.3, 2.7, "Le profil fournisseur", "Le choix de colonnes (rôle → en-tête) est proposé, confirmé une fois, mémorisé par fournisseur. À la révision suivante, si les en-têtes correspondent, le profil s'applique (chemin « profil ») ; sinon la reconnaissance reprend et le signale. Même idée que la sonde par poste de RIVAGE : essayer, mémoriser, redemander quand ça casse.", "FFF4E3", C.accent3, 12);

// Diagnostic
s = slide("Le diagnostic d'exploitabilité, avant toute analyse", "Lire une NCM");
image(s, "61_diagnostic_apres_lecture.png", 0.6, 1.4, 7.6);
bullets(s, [
  "Tableaux lus et retenus, chemin de lecture (tableau, profil).",
  "Lignes, lignes avec identifiant, lignes pivot (après dépliage des plages).",
  "Statuts reconnus, valeurs inconnues et leur fréquence.",
  "Part des identifiants du référentiel retrouvés dans la NCM.",
  "Colonnes retenues pour chaque rôle, avertissements.",
], 8.5, 1.4, 4.2, 3.2, 13);
card(s, 8.5, 4.8, 4.2, 1.8, "À quoi ça sert", "Une NCM à 98 % d'identifiants retrouvés se traite automatiquement ; à 40 %, on la renvoie au fournisseur avec un argument factuel.", "FFF4E3", C.accent3, 12);

// ===============================================================================
pres.addSection({ title: "Contrôler et comparer" });
s = pres.addSlide({ masterName: "SECTION", sectionTitle: "Contrôler et comparer" });
s.addText("2. Contrôler et comparer", { placeholder: "title" });
s.addText("Une jointure, des ensembles, des règles : la grille des écarts sans aucune IA", { placeholder: "body" });

// Contrôles
s = slide("Les contrôles par identifiant : des opérations sur des ensembles", "Contrôler et comparer");
para(s, "Soit S l'ensemble des identifiants du référentiel et N celui des identifiants cités par la NCM (une ligne pivot peut en citer plusieurs ; un identifiant peut avoir plusieurs lignes).", 0.6, 1.4, 12.1, 0.8, 15);
formule(s, "trous = S \\ N          orphelins = N \\ S          couverts = S ∩ N", 0.6, 2.3, 7.4, 0.8, 20);
formule(s, "doublon(i) ⇔ |lignes(i)| > 1 ∧ |statuts(i)| = 1       incohérence(i) ⇔ |statuts(i)| > 1", 0.6, 3.2, 7.4, 0.8, 14);
bullets(s, [
  "Statut effectif d'une exigence : celui de ses lignes si elles sont d'accord ; « ? » en cas d'incohérence (à trancher en revue), avec l'alerte.",
  "Statut inconnu : la valeur brute n'est pas dans le dictionnaire (alerte, et proposition d'enrichissement).",
  "Clôture d'un écart (NC ou PC) : « dérogation » si une référence de dérogation existe, « action » si une action est décrite, sinon « ouvert » (alerte).",
], 0.6, 4.1, 7.4, 2.6, 13);
tableau(s, [["Alerte", "Signification"], ["trou", "exigence sans ligne de NCM"], ["conforme_implicite", "absente d'une NCM pré-filtrée : C par défaut"],
  ["orphelin", "identifiant absent du référentiel"],
  ["doublon", "plusieurs lignes, même statut"], ["incoherence", "plusieurs lignes, statuts différents"], ["statut_inconnu", "valeur hors dictionnaire"],
  ["ecart_ouvert", "NC / PC sans action ni dérogation"]], 8.4, 2.3, 4.3, [1.6, 2.7], 11);

// Grille
s = slide("La grille des écarts : une ligne par exigence, tout sourcé", "Contrôler et comparer");
image(s, "62_grille_complete_detail_nc.png", 0.6, 1.4, 8.0);
bullets(s, [
  "À l'écran : identifiant, statut (couleur), appui, clôture, alertes, exigence ; filtres par statut, par alerte, par texte ; volet de détail.",
  "En Excel : 25 colonnes (ligne de NCM, champs optionnels, origine, appui, passage, valeurs absentes, affaiblissements, fiche, historique), feuilles Alertes, Impact norme, Couverture norme, Résumé.",
  "Chaque campagne se rejoue à l'identique : les mêmes fichiers donnent la même grille.",
], 8.9, 1.4, 3.8, 4.8, 12);

// Diff
s = slide("Comparer deux révisions de NCM", "Contrôler et comparer");
image(s, "69_diff.png", 0.6, 1.4, 7.6);
formule(s, "gravité : C = NA = 0 < PC = ? = 1 < NC = 2\nrégression si g(après) > g(avant)", 8.4, 1.4, 4.3, 1.1, 13);
bullets(s, [
  "Par identifiant : nouvelle, disparue, statut changé (avec le sens : amélioration, régression), justification réécrite.",
  "Une ligne disparue de la nouvelle révision est ambiguë (corrigée ou oubliée ?) : signalée, jamais tranchée.",
  "Une NCM finale signée fige l'état ; les révisions antérieures restent lisibles pour l'historique.",
], 8.4, 2.7, 4.3, 3.6, 12);

// ===============================================================================
pres.addSection({ title: "Le moteur embarqué" });
s = pres.addSlide({ masterName: "SECTION", sectionTitle: "Le moteur embarqué" });
s.addText("3. Le moteur embarqué au service de la conformité", { placeholder: "title" });
s.addText("Là où il faut lire du texte par le sens : appuis des « C », traçabilité vers la norme", { placeholder: "body" });

// Rappel moteur
s = slide("Ce que le moteur de RIVAGE apporte (rappel)", "Le moteur embarqué");
bullets(s, [
  "Indexation des documents du fournisseur (et de la norme) : extraction, découpage en fragments de 800 caractères, déduplication, vecteurs (embedding), index FAISS exact et BM25, publication atomique.",
  "Une question = un embedding, une recherche hybride (FAISS top-50 + BM25 top-50), une fusion RRF, un reranking cross-encoder des 50 candidats. Jamais de synthèse ici.",
  "La portée : les documents du fournisseur (préfixe de chemin ou liste de fichiers) ; l'index peut être propre à SILLAGE (dossier de données) ou l'index RIVAGE du partage.",
], 0.6, 1.4, 7.4, 3.2, 14);
formule(s, "sim(q, p) = q · p          RRF(p) = Σᵢ 1 / (60 + rangᵢ(p))", 0.6, 4.7, 7.4, 0.8, 17);
formule(s, "score(q, p) = σ( f([q ; p]) )  — le reranker lit la paire", 0.6, 5.7, 7.4, 0.8, 17);
card(s, 8.4, 1.4, 4.3, 2.4, "Pourquoi embarquer", "SILLAGE est autonome : les 23 modules de RIVAGE qui font l'indexation et la recherche sont copiés tels quels dans sillage/moteur/, avec la sonde de plateforme pour les postes anciens. Un script reprend une version plus récente en une commande.", C.background2, C.text2, 12);
card(s, 8.4, 4.0, 4.3, 2.6, "Ce qui reste externe", "La passerelle d'IA (embedding, reranking, synthèse, OCR), avec les mêmes fichiers RIVAGE.env et RIVAGE_keys.env. Sans elle : lecture, grille, diff, affaiblissements et mémoire fonctionnent ; les appuis sont « non vérifiés ».", "FFF4E3", C.accent3, 12);

// Appuis
s = slide("Vérifier les « C » : l'appui est un passage, pas une affirmation", "Le moteur embarqué");
para(s, "Pour chaque exigence déclarée conforme, SILLAGE pose le texte de l'exigence comme question dans les documents du fournisseur. Le score de reranking du meilleur passage classe l'appui.", 0.6, 1.4, 7.4, 1.0, 15);
formule(s, "s = max score(exigence, p)  sur les passages p\nétayé : s ≥ 0,5      faible : 0,25 ≤ s < 0,5      absent : s < 0,25", 0.6, 2.5, 7.4, 0.9, 14);
bullets(s, [
  "Un « C » absent (rien de comparable) est un « C » de complaisance : en tête de l'ordre du jour de la revue. C'est le signal d'incomplétude de RIVAGE, lu à l'envers : ici, l'absence est l'information.",
  "« Non vérifié » quand le reranking n'est pas disponible : l'ordre RRF seul n'est pas comparable d'une exigence à l'autre.",
  "Les seuils dépendent du reranker (probabilités ou logits) : à calibrer sur vingt exigences connues ; la campagne affiche les quartiles des scores pour choisir.",
  "Un embedding, une recherche, un reranking par exigence ; la campagne s'enregistre (JSON) et se rejoue dans la grille sans nouvel appel.",
], 0.6, 3.6, 7.4, 3.2, 13);
image(s, "63_detail_c_appui_journal.png", 8.4, 1.4, 4.3);
para(s, "Détail d'un « C » étayé : niveau, score, document, passage.", 8.4, 4.25, 4.3, 0.4, 11, "5A6A79");

// Valeurs et affaiblissements
s = slide("Valeurs chiffrées et affaiblissements : deux contrôles sans IA", "Le moteur embarqué");
card(s, 0.6, 1.4, 5.9, 2.6, "Valeurs chiffrées", "Les nombres de l'exigence (« 2 kg », « -40 °C », « 28 V ») sont cherchés dans le passage d'appui. Absents, l'exigence est signalée : « 2 kg » exigés, « 2,5 kg » annoncés, à lire. Numéros de section (« 2. Alimentation ») et nombres d'identifiants (« T-12 ») sont ignorés.", C.background2, C.text2, 12);
formule(s, "valeurs(exigence) ⊄ nombres(passage d'appui)\n⇒  alerte « valeurs_absentes »", 0.6, 4.2, 5.9, 0.8, 13);
card(s, 6.8, 1.4, 5.9, 2.6, "Affaiblissements", "Un fournisseur qui se ménage une sortie l'écrit : TBD, TBC, where practicable, best effort, sous réserve, dans la mesure du possible, except, not covered, ultérieurement… Campagne lexicale sur ses documents, chaque occurrence rattachée aux identifiants cités dans le paragraphe.", C.background2, C.text2, 12);
formule(s, "occurrence(expr, paragraphe)  ∧  identifiants(paragraphe) ≠ ∅\n⇒  alerte « affaiblissement »", 6.8, 4.2, 5.9, 0.8, 13);
bullets(s, [
  "Les expressions courantes (may, should, sauf, partially, le cas échéant) ne sont retenues qu'à côté d'un identifiant ; les fortes (TBD, point ouvert) toujours.",
  "Les deux contrôles sont déterministes et ne coûtent aucun appel : ils tournent sans passerelle.",
  "Limite connue : la comparaison numérique ne lit ni l'opérateur (≤) ni l'unité (2000 g) ; c'est la fiche de revue qui juge, ou l'ingénieur.",
], 0.6, 5.1, 12.1, 1.8, 13);

// Traçabilité norme
s = slide("Traçabilité vers la norme : proposer, relire, figer", "Le moteur embarqué");
para(s, "La norme (CS-25 et ses AMC) est indexée dans un index dédié. Pour chaque exigence sans paragraphe de norme, SILLAGE extrait des meilleurs passages les références de paragraphes citées, avec le score du passage.", 0.6, 1.4, 7.4, 1.1, 15);
formule(s, "candidat (réf, s) :  s = score(passage) − 0,02 · k,   k = rang de la référence dans le passage\nretenue = argmax s   si s ≥ 0,3", 0.6, 2.6, 7.4, 0.9, 13);
bullets(s, [
  "Références reconnues et normalisées : « CS 25.1309 », « CS-25.1309(b) » → CS 25.1309 ; AMC 25.1309 ; FAR 25.1353 ; 14 CFR 25.1301 → FAR 25.1301.",
  "Un passage peut citer la fin d'un paragraphe et le titre du suivant : la première référence est la plus probable, les suivantes sont légèrement pénalisées.",
  "Relecture obligatoire : un classeur par exigence (proposé, candidats et scores, passage, document) ; l'ingénieur corrige « Norme retenue » et coche « Validé ». L'applicabilité est une décision humaine.",
  "Figé : la grille porte l'alerte « impact norme » sur tout écart (NC, PC, trou, inconnu) d'une exigence tracée ; la feuille « Impact norme » est la liste des risques de certification.",
], 0.6, 3.7, 7.4, 3.2, 12);
card(s, 8.4, 1.4, 4.3, 2.4, "Couverture de la norme", "Avec les documents de la norme : chaque paragraphe cité et les exigences qui s'y rattachent. Un paragraphe sans exigence est soit non applicable, soit un manque de la spécification : colonne « Applicable ? » à remplir.", C.background2, C.text2, 12);
image(s, "68_dialogue_tracer.png", 8.4, 4.0, 4.3, 2.7);

// ===============================================================================
pres.addSection({ title: "Fiches et mémoire" });
s = pres.addSlide({ masterName: "SECTION", sectionTitle: "Fiches et mémoire" });
s.addText("4. Fiches de revue et mémoire des projets", { placeholder: "title" });
s.addText("La seule étape générative, bornée ; puis la capitalisation sans IA", { placeholder: "body" });

// Fiches
s = slide("Fiches de revue : le modèle rédige, il ne cherche ni ne décide", "Fiches et mémoire");
para(s, "Pour chaque exigence NC ou PC, le modèle de la passerelle reçoit l'exigence, sa ligne de NCM, le paragraphe de norme tracé et au plus trois passages de la spécification fournisseur (ceux de la vérification des « C », ou cherchés à la demande).", 0.6, 1.4, 7.4, 1.2, 14);
const partiesF = [["ÉCART", "en deux phrases : ce que le fournisseur ne fait pas ou fait autrement, et ce qu'il propose"],
  ["RISQUE", "conséquence technique ; conséquence pour la certification si un paragraphe de norme est tracé"],
  ["QUESTIONS POUR LA REVUE", "au plus trois questions précises à poser au fournisseur"]];
partiesF.forEach((p, i) => { box(s, 0.6, 2.8 + i * 0.95, 2.6, 0.8, p[0], i === 1 ? C.accent5 : C.accent1, C.background1, 12); para(s, p[1], 3.4, 2.85 + i * 0.95, 4.6, 0.8, 12); });
bullets(s, [
  "Règles de la consigne : ne rien affirmer qui ne soit dans la NCM ou un passage ; citer [i] ou « (NCM) » ; dire quand les passages ne disent rien ; pas de verdict (accepter, refuser) : c'est la décision de l'ingénieur.",
  "Même client que RIVAGE : retries, repli sur le modèle de secours, mode dégradé et troncature signalés. Sans passerelle : fiche en erreur, rien d'inventé.",
], 0.6, 5.7, 7.4, 1.3, 12);
card(s, 8.4, 1.4, 4.3, 2.2, "Ce qui part vers la passerelle", "Une exigence, une ligne de NCM, un paragraphe de norme, trois passages de 800 caractères. Jamais un document entier, jamais la NCM entière.", C.background2, C.text2, 12);
card(s, 8.4, 3.8, 4.3, 2.8, "Ce qu'on obtient", "Un dossier de revue Word, une fiche par page (exigence, norme, ligne de NCM, fiche, passages cités) ; la fiche dans la grille et dans le détail de l'interface ; un JSON rejouable. Une revue de NCM préparée en une heure au lieu d'une journée.", "FFF4E3", C.accent3, 12);

// Mémoire
s = slide("La mémoire des projets : capitaliser sans IA", "Fiches et mémoire");
para(s, "Chaque grille calculée peut être enregistrée comme campagne : projet, fournisseur, révision, NCM finale signée ou non, et une ligne par exigence (statut, justification, clôture, dérogation, action, appui, alertes, norme). SQLite local, jamais sur le partage.", 0.6, 1.4, 7.4, 1.3, 14);
formule(s, "contestations(e) = #{ (projet, fournisseur) : statut(e) ∈ {NC, PC} }\nacceptations(e) = #{ (projet, fournisseur) : clôture(e) = dérogation }", 0.6, 2.8, 7.4, 0.9, 13);
bullets(s, [
  "Exigences contestées : les exigences systématiquement contestées sont mal écrites ou trop ambitieuses, à savoir avant la prochaine consultation.",
  "Écarts déjà acceptés, par exigence ou par paragraphe de norme : ce qu'on a accepté la dernière fois, avec quelle dérogation et quelle justification.",
  "Bilans par paragraphe de norme et par fournisseur (conformité, écarts, ouverts, « C » sans appui) ; recherche dans les justifications.",
  "La grille courante s'annote : « vue sur 3 campagnes, contestée 2 fois ; écart déjà accepté (PROJET-1, Fournisseur B, RFD-0101) ». La campagne courante n'est jamais comptée.",
], 0.6, 3.9, 7.4, 3.0, 12);
image(s, "71_memoire_contestees.png", 8.4, 1.4, 4.3, 2.6);
image(s, "72_memoire_acceptes.png", 8.4, 4.2, 4.3, 2.6);

// ===============================================================================
pres.addSection({ title: "Interface et autonomie" });
s = pres.addSlide({ masterName: "SECTION", sectionTitle: "Interface et autonomie" });
s.addText("5. L'interface et l'autonomie", { placeholder: "title" });
s.addText("Huit étapes à l'écran, un exécutable, rien à installer d'autre", { placeholder: "body" });

// Interface
s = slide("L'interface : un projet, huit étapes, une grille", "Interface et autonomie");
image(s, "62_grille_complete_detail_nc.png", 0.6, 1.4, 8.4);
const etapesI = ["1. Lire la NCM — diagnostic, statuts inconnus proposés", "2. Grille des écarts", "3. Indexer les documents du fournisseur", "4. Vérifier les « C »", "5. Tracer vers la norme (index propre à la norme)", "6. Affaiblissements", "7. Fiches de revue", "8. Capitaliser"];
etapesI.forEach((t, i) => { box(s, 9.3, 1.4 + i * 0.56, 3.4, 0.48, t, i === 6 ? C.accent5 : C.accent1, C.background1, 11); });
para(s, "Le cadre Projet reçoit la spécification, la NCM, les documents du fournisseur et ceux de la norme applicable. Outils : profil des colonnes, dictionnaire des statuts, comparaison de révisions, mémoire des projets, fiche « ce poste ».", 9.3, 5.95, 3.4, 1.1, 10, "5A6A79");

// Dialogues
s = slide("Les dialogues : apprendre une fois, réutiliser toujours", "Interface et autonomie");
image(s, "66_dialogue_statuts.png", 0.6, 1.4, 6.0, 2.6);
image(s, "67_dialogue_profil.png", 6.9, 1.4, 5.8, 2.6);
image(s, "70_memoire_campagnes.png", 0.6, 4.2, 6.0, 2.6);
image(s, "73_memoire_recherche.png", 6.9, 4.2, 5.8, 2.6);

// Autonomie
s = slide("Autonome : lanceur, scripts, exécutable, postes anciens", "Interface et autonomie");
bullets(s, [
  "sillage_app.py : interface sans argument, ligne de commande sinon (douze commandes : lire, grille, diff, statuts, profils, indexer, appui, affaiblissements, tracer, couverture, fiches, memoire).",
  "install.bat, launch.bat, build_exe.bat, SILLAGE.spec : sur le modèle de RIVAGE ; l'exécutable embarque le moteur, faiss, PyMuPDF et le hook de la sonde de plateforme.",
  "Au premier lancement sur un poste ancien, la sonde du moteur mémorise la configuration la plus rapide qui s'exécute sans mourir (niveau SIMD, sous-index, mmap) ; la fiche « ce poste » dit ce qui tourne.",
  "Données du poste : dictionnaire des statuts, profils, index des fournisseurs, mémoire, réglages, dans %LOCALAPPDATA%\\sillage. Rien sur le partage, rien dans le dépôt.",
], 0.6, 1.4, 7.4, 4.2, 13);
stat(s, 0.6, 5.6, 2.4, "23", "modules de RIVAGE embarqués, tels quels");
stat(s, 3.1, 5.6, 2.4, "12", "commandes en ligne", C.accent3);
stat(s, 5.6, 5.6, 2.4, "0", "installation de RIVAGE requise", C.accent4);
image(s, "74_fiche_poste.png", 8.4, 1.4, 4.3, 2.9);
card(s, 8.4, 4.5, 4.3, 2.2, "Synchroniser le moteur", "python outils/synchroniser_rivage.py chemin\\vers\\RIVAGE : reprend une version plus récente de RIVAGE en une commande ; VERSION_RIVAGE.txt note la version et le commit copiés.", C.background2, C.text2, 11);

// ===============================================================================
pres.addSection({ title: "Atouts, gains, audit" });
s = pres.addSlide({ masterName: "SECTION", sectionTitle: "Atouts, gains, audit" });
s.addText("6. Atouts, gains, audit et perspectives", { placeholder: "title" });
s.addText("Ce que SILLAGE apporte, ce que l'audit dit, ce qu'il peut encore devenir", { placeholder: "body" });

// Atouts
s = slide("Les atouts de SILLAGE", "Atouts, gains, audit");
const atouts = [["Déterministe", "Jointure, contrôles, diff, mémoire : deux campagnes sur les mêmes fichiers donnent la même grille. Reproductible, auditable."],
  ["S'adapte au fournisseur", "Colonnes reconnues par contenu, profils mémorisés, dictionnaire de statuts enrichi : le format change, pas l'outil."],
  ["Sourcé jusqu'à la cellule", "Fichier, feuille, ligne ; passage et document pour les appuis ; projet et dérogation pour l'historique."],
  ["IA minimale", "Le modèle n'écrit que les fiches, à partir d'éléments cités, sans verdict. Le reste n'appelle personne."],
  ["Norme en vue", "Traçabilité proposée puis relue ; tout écart tracé est un risque de certification listé."],
  ["Mémoire", "Exigences contestées, écarts acceptés, bilans par norme et par fournisseur : la spec suivante s'écrit mieux."],
  ["Autonome", "Moteur embarqué, exécutable, mêmes clés que RIVAGE ; sans passerelle, tout le socle fonctionne."],
  ["Rejouable", "NCM normalisée, appuis, traçabilité, fiches en JSON ; rien ne repart deux fois vers la passerelle."]];
atouts.forEach((a, i) => { const col = i % 4, row = Math.floor(i / 4); card(s, 0.6 + col * 3.075, 1.4 + row * 2.05, 2.9, 1.9, a[0], a[1], i === 7 ? "FFF4E3" : C.background2, i === 7 ? C.accent3 : C.text2, 11); });
stat(s, 0.6, 5.5, 3.9, "3", "passages au plus par fiche vers la passerelle", C.accent2);
stat(s, 4.7, 5.5, 3.9, "0", "document transmis en entier", C.accent5);
stat(s, 8.8, 5.5, 3.9, "1", "fichier JSON par étape, rejouable", C.accent4);

// Gains
s = slide("Les gains : avant / avec SILLAGE", "Atouts, gains, audit");
tableau(s, [["Situation", "Avant", "Avec SILLAGE"],
  ["Compléter une NCM", "Trois documents recoupés à la main, exigence par exigence", "Trous, orphelins, doublons, incohérences en quelques secondes"],
  ["Vérifier un « C »", "On fait confiance, ou on relit toute la spécification fournisseur", "Le passage d'appui et son score, ou l'alerte « sans appui »"],
  ["Nouvelle révision de NCM", "Tout relire", "Le diff : régressions, nouvelles, disparues, justifications réécrites"],
  ["Préparer la revue", "Une journée de lecture et de notes", "Fiches ÉCART / RISQUE / QUESTIONS, dossier Word, une heure"],
  ["Risque certification", "Dépend de la mémoire de l'ingénieur", "Écarts tracés vers la norme listés dans « Impact norme »"],
  ["Capitaliser", "Dossiers des projets précédents, rarement rouverts", "Exigences contestées, écarts acceptés, bilans fournisseurs, grille annotée"],
  ["Format du fournisseur", "Un tableur à refaire par fournisseur", "Profil confirmé une fois, dictionnaire enrichi une fois"]],
  0.6, 1.4, 12.1, [2.6, 4.5, 5.0], 12);
para(s, "Les durées sont des ordres de grandeur attendus ; la mesure viendra des premières campagnes réelles.", 0.6, 6.3, 12.1, 0.4, 11, "5A6A79");

// Audit
s = slide("L'audit qualité (0.7.1) en bref", "Atouts, gains, audit");
stat(s, 0.6, 1.4, 2.9, "54", "tests, tous verts (interface comprise)");
stat(s, 3.6, 1.4, 2.9, "92 %", "de couverture hors interface et moteur", C.accent4);
stat(s, 6.6, 1.4, 2.9, "5 135", "lignes propres à SILLAGE", C.accent3);
stat(s, 9.6, 1.4, 2.9, "15", "captures de l'interface", C.accent6);
card(s, 0.6, 3.2, 5.9, 1.9, "Verdict", "Prêt pour l'essai terrain, pas encore pour la généralisation. Le socle déterministe est solide ; ce qui manque n'est pas du code, c'est la confrontation à des NCM réelles et un premier build Windows.", C.background2, C.text2, 12);
card(s, 6.8, 3.2, 5.9, 1.9, "Majeurs", "Aucun document réel lu (tableaux PDF sans traits) ; seuils d'appui et de traçabilité non calibrés ; build PyInstaller et comportement Windows non exécutés.", "FFF4E3", C.accent5, 12);
card(s, 0.6, 5.3, 5.9, 1.5, "Moyens", "Valeurs chiffrées sans opérateur ni unité ; chunk à cheval sur deux paragraphes ; mémoire interrogée exigence par exigence ; justifications stockées en clair sur le poste ; lettres seules dans le dictionnaire.", C.background2, C.text2, 11);
card(s, 6.8, 5.3, 5.9, 1.5, "Plan d'actions", "P1 terrain : trois NCM réelles, build Windows, calibration. P2 robustesse (0.8) : repli texte PDF, fil de travail, journal, tests. P3 confort (0.9) : fichier de campagne, libellés, guide illustré.", C.background2, C.text2, 11);

// Pistes
s = slide("Pistes d'amélioration", "Atouts, gains, audit");
const pistes = [["Court terme", C.accent2, [
    "Essai sur trois NCM réelles et premiers profils ; calibration des seuils sur vingt exigences connues.",
    "Repli « texte » pour les PDF sans tableau détecté ; diagnostic qui explique un tableau ignoré.",
    "Commande « calibrer » : propose les seuils depuis un classeur annoté."]],
  ["Moyen terme", C.accent3, [
    "Valeurs chiffrées avec opérateur et unité, jugées dans la fiche de revue.",
    "Fichier de campagne (spec, NCM, fournisseur, projet, index, seuils) pour une ligne de commande courte.",
    "Embeddings par lot pour les appuis et la traçabilité ; guide utilisateur illustré."]],
  ["Long terme", C.accent4, [
    "Rapprochement des exigences par texte quand la numérotation change d'un projet à l'autre.",
    "Vérification des livrables de preuve (rapports d'essais) contre la NCM signée : même campagne, autre corpus.",
    "Export / import de campagnes pour une mémoire d'équipe sans base partagée."]]];
pistes.forEach((p, i) => {
  const x = 0.6 + i * 4.1;
  box(s, x, 1.4, 3.9, 0.6, p[0], p[1], i === 1 ? C.text1 : C.background1, 15);
  s.addShape(pres.ShapeType.roundRect, { x, y: 2.0, w: 3.9, h: 2.9, fill: { color: THEME.colors.lt2 }, line: { color: THEME.colors.lt2 }, rectRadius: 0.08 });
  bullets(s, p[2], x + 0.2, 2.2, 3.5, 2.6, 12);
});
card(s, 0.6, 5.1, 12.1, 1.6, "Le fil conducteur", "Chaque piste garde les principes : l'identifiant comme invariant, le déterminisme d'abord, l'IA réservée à la rédaction et toujours citée, le verdict à l'ingénieur. Et toute évolution d'un seuil ou d'une règle commence par la mesure sur des cas connus.", "FFF4E3", C.accent3, 12);

// ===============================================================================
pres.addSection({ title: "Synthèse" });
s = slide("Les règles de SILLAGE en une page", "Synthèse");
tableau(s, [["Étape", "Objet", "Règle", "Paramètres"],
  ["Identifiants", "motif, normalisation", "PRÉFIXE sép NUMÉRO[suffixe], préfixe composé ; écriture de la spec ; plages dépliées", "préfixes du référentiel"],
  ["Statuts", "dictionnaire → pivot", "valeur, tête, premier mot, mots-clés", "C, PC, NC, NA, ?"],
  ["Colonnes", "argmax par contenu", "identifiants ≥ 50 % ; statuts ≥ 40 % ; texte le plus long", "profil fournisseur"],
  ["Contrôles", "ensembles", "trous = S \\ N ; orphelins = N \\ S ; incohérence si |statuts| > 1", "—"],
  ["Clôture", "règle", "dérogation > action > ouvert", "champs optionnels"],
  ["Diff", "gravité", "C = NA = 0 < PC = ? = 1 < NC = 2 ; régression si elle croît", "—"],
  ["Appui", "score de reranking", "s = max score(exigence, passage)", "étayé ≥ 0,5 ; faible ≥ 0,25"],
  ["Valeurs", "nombres", "valeurs(exigence) ⊄ nombres(passage) ⇒ alerte", "sections et identifiants ignorés"],
  ["Affaiblissements", "lexical", "expression ∧ identifiant dans le paragraphe", "liste FR / EN"],
  ["Traçabilité", "références de norme", "s = score(passage) − 0,02·k ; retenue si s ≥ 0,3", "relecture, « Validé »"],
  ["Fiches", "génératif borné", "ÉCART / RISQUE / QUESTIONS, citations [i] ou (NCM), pas de verdict", "3 passages, 700 jetons"],
  ["Mémoire", "SQL", "contestations, acceptations par (projet, fournisseur) ; campagne courante exclue", "SQLite local"]],
  0.6, 1.4, 12.1, [2.0, 2.4, 5.2, 2.5], 11);

// Fin
s = pres.addSlide({ masterName: "TITRE", sectionTitle: "Synthèse" });
s.addText("En résumé", { placeholder: "title" });
s.addText([
  { text: "L'identifiant d'exigence est l'invariant ; une jointure et des règles font la grille ; le moteur de RIVAGE lit ce qu'il faut lire par le sens ; un modèle rédige des fiches citées, sans verdict ; et chaque campagne enrichit la mémoire des projets.", options: { breakLine: true } },
  { text: "Références : docs/PLAN_SILLAGE.md, docs/AUDIT_QUALITE_SILLAGE.md, README.md, CHANGELOG.md.", options: { fontSize: 14, color: "9FB3C8", breakLine: true } },
  { text: " ", options: { fontSize: 10, breakLine: true } },
  { text: "SILLAGE — Renaud Loison, auteur, créateur et développeur", options: { fontSize: 16, bold: true, color: C.background1, breakLine: true } },
  { text: "RIVAGE cherche, SILLAGE garde la trace.", options: { fontSize: 14, italic: true, color: "9FB3C8" } },
], { placeholder: "body" });

(async () => {
  await pres.writeFile({ fileName: outPath });
  if (applyTheme) await applyTheme(outPath, THEME);
  else console.warn("apply_theme.js introuvable : palette Office conservée (APPLY_THEME_JS).");
  console.log("écrit", outPath, "diapositives :", pres.slides.length);
})();
