"""Export de la grille des écarts et du diff en classeur Excel."""

from __future__ import annotations

from pathlib import Path
from typing import List

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .controles import Diff, Grille
from .modele import Statut
from .version import VERSION_LABEL

COULEURS = {
    Statut.CONFORME: "C6EFCE",
    Statut.PARTIEL: "FFEB9C",
    Statut.NON_CONFORME: "FFC7CE",
    Statut.NON_APPLICABLE: "E7E6E6",
    Statut.INCONNU: "FFF2CC",
}
_EN_TETE_FILL = PatternFill("solid", fgColor="0B4F6C")
_EN_TETE_FONT = Font(bold=True, color="FFFFFF")
_ALERTE_FILL = PatternFill("solid", fgColor="F8CBAD")
_COULEURS_APPUI = {"étayé": "C6EFCE", "faible": "FFEB9C", "absent": "FFC7CE", "non vérifié": "E7E6E6"}

COLONNES = [
    ("Identifiant", 14), ("Exigence", 60), ("Statut", 9), ("Libellé", 22), ("Statut brut", 16),
    ("Justification", 60), ("Clôture", 12), ("Action", 30), ("Échéance", 12), ("Référence", 18),
    ("Dérogation", 16), ("Commentaire", 30), ("Norme", 14), ("Alertes", 24), ("Chemin", 9),
    ("Origine", 34), ("Lignes NCM", 10),
    ("Appui", 11), ("Score", 8), ("Passage d'appui", 60), ("Document d'appui", 30), ("Valeurs absentes", 18),
    ("Affaiblissements", 24), ("Fiche de revue", 70),
]


def _feuille(wb: Workbook, titre: str, colonnes: List[tuple]):
    ws = wb.create_sheet(titre)
    ws.append([c[0] for c in colonnes])
    for i, (_, largeur) in enumerate(colonnes, start=1):
        ws.column_dimensions[get_column_letter(i)].width = largeur
        cell = ws.cell(1, i)
        cell.fill, cell.font = _EN_TETE_FILL, _EN_TETE_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    return ws


def exporter_grille(grille: Grille, chemin, couverture=None) -> Path:
    """Classeur : Grille, Alertes, Impact norme (si des exigences sont tracées),
    Couverture norme (si `couverture` est donné), Résumé."""
    wb = Workbook()
    wb.remove(wb.active)

    ws = _feuille(wb, "Grille", COLONNES)
    for lg in grille.lignes:
        ws.append([
            lg.id, lg.texte, lg.statut.value, lg.statut.libelle if lg.dans_referentiel or lg.nb_lignes_ncm else "",
            lg.statut_brut, lg.justification, lg.cloture,
            lg.champs.get("action", ""), lg.champs.get("echeance", ""), lg.champs.get("reference", ""),
            lg.champs.get("derogation", ""), lg.champs.get("commentaire", ""), lg.norme,
            ", ".join(lg.alertes), lg.chemin, lg.origine, lg.nb_lignes_ncm,
            lg.appui_niveau, round(lg.appui_score, 3) if lg.appui_score is not None else "",
            lg.appui_passage, lg.appui_document, ", ".join(lg.valeurs_absentes), ", ".join(lg.affaiblissements),
            lg.fiche or (f"(non rédigée : {lg.fiche_erreur})" if lg.fiche_erreur else ""),
        ])
        r = ws.max_row
        ws.cell(r, 3).fill = PatternFill("solid", fgColor=COULEURS[lg.statut])
        if lg.alertes:
            ws.cell(r, 14).fill = _ALERTE_FILL
        if lg.appui_niveau:
            ws.cell(r, 18).fill = PatternFill("solid", fgColor=_COULEURS_APPUI.get(lg.appui_niveau, "FFFFFF"))
        for c in (2, 6, 8, 20, 24):
            ws.cell(r, c).alignment = Alignment(wrap_text=True, vertical="top")
    ws.auto_filter.ref = ws.dimensions

    wa = _feuille(wb, "Alertes", [("Type", 16), ("Identifiant", 14), ("Message", 70), ("Origine", 40)])
    for a in grille.alertes:
        wa.append([a.type, a.id, a.message, a.origine])
    wa.auto_filter.ref = wa.dimensions

    if any(l.norme for l in grille.lignes):
        wi = _feuille(wb, "Impact norme", [("Identifiant", 14), ("Norme", 16), ("Statut", 9), ("Libellé", 22),
                                            ("Exigence", 60), ("Justification", 60), ("Clôture", 12), ("Dérogation", 16)])
        for lg in grille.impacts_norme():
            wi.append([lg.id, lg.norme, lg.statut.value, lg.statut.libelle, lg.texte, lg.justification, lg.cloture,
                       lg.champs.get("derogation", "")])
            r = wi.max_row
            wi.cell(r, 3).fill = PatternFill("solid", fgColor=COULEURS[lg.statut])
            for c in (5, 6):
                wi.cell(r, c).alignment = Alignment(wrap_text=True, vertical="top")
        wi.auto_filter.ref = wi.dimensions

    if couverture is not None:
        wc = _feuille(wb, "Couverture norme", [("Paragraphe", 16), ("Cité (occurrences)", 16), ("Exigences tracées", 50),
                                                ("Nombre", 8), ("Applicable ?", 12)])
        for p, n, ids in couverture.lignes():
            wc.append([p, n, ", ".join(ids), len(ids), ""])
            if not ids:
                wc.cell(wc.max_row, 3).fill = PatternFill("solid", fgColor=COULEURS[Statut.INCONNU])
        wc.auto_filter.ref = wc.dimensions

    wr = _feuille(wb, "Résumé", [("Indicateur", 30), ("Valeur", 14)])
    wr.append(["Référentiel", grille.referentiel])
    wr.append(["NCM", grille.ncm])
    wr.append(["Fournisseur", grille.fournisseur])
    wr.append(["Révision", grille.revision])
    for k, v in grille.resume().items():
        wr.append([k, v])
    wr.append(["Produit par", VERSION_LABEL])

    p = Path(chemin)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(p))
    return p


def exporter_diff(diff: Diff, chemin) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    ws = _feuille(wb, "Diff", [("Identifiant", 14), ("Nature", 14), ("Avant", 40), ("Après", 40), ("Sens", 14), ("Origine", 40)])
    for c in diff.changements:
        ws.append([c.id, c.nature, c.avant, c.apres, c.sens, c.origine])
        r = ws.max_row
        if c.sens == "régression":
            ws.cell(r, 5).fill = PatternFill("solid", fgColor=COULEURS[Statut.NON_CONFORME])
        elif c.sens == "amélioration":
            ws.cell(r, 5).fill = PatternFill("solid", fgColor=COULEURS[Statut.CONFORME])
        for col in (3, 4):
            ws.cell(r, col).alignment = Alignment(wrap_text=True, vertical="top")
    ws.auto_filter.ref = ws.dimensions
    wr = _feuille(wb, "Résumé", [("Indicateur", 30), ("Valeur", 14)])
    wr.append(["Avant", diff.avant])
    wr.append(["Après", diff.apres])
    for k, v in diff.resume().items():
        wr.append([k, v])
    wr.append(["Produit par", VERSION_LABEL])
    p = Path(chemin)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(p))
    return p
