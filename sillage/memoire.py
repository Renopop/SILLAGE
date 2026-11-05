"""Lot 7 — capitalisation entre projets : la mémoire des campagnes.

Chaque grille calculée peut être **enregistrée** comme campagne (projet,
fournisseur, révision de NCM, NCM finale signée ou non), une ligne par
exigence : statut, justification, clôture, dérogation, action, appui,
alertes, paragraphe de norme. La mémoire est un fichier SQLite local
(dossier de données SILLAGE), jamais sur le partage.

Elle répond ensuite, sans aucune IA :

- **exigences contestées** : combien de projets ou fournisseurs ont déclaré
  NC ou PC sur chaque exigence, avec les justifications ;
- **écarts déjà acceptés** : les NC / PC clos par dérogation, par exigence ou
  par paragraphe de norme, avec le projet, le fournisseur et la référence ;
- **par paragraphe de norme** : les écarts rencontrés sur chaque paragraphe ;
- **par fournisseur** : conformité, écarts, écarts ouverts, campagnes ;
- **recherche** dans les justifications et les textes d'exigences.

Et elle **annote la grille courante** : pour chaque exigence, l'historique
(« contestée sur 3 projets ; écart accepté sur PROJET-X, fournisseur B,
RFD-0007 ») — ce que l'ingénieur ne retrouverait qu'en rouvrant les dossiers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import dossier_donnees
from .modele import Statut

ECARTS = ("NC", "PC")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS campagnes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    projet TEXT NOT NULL, fournisseur TEXT NOT NULL, revision TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL, referentiel TEXT, ncm TEXT, signee INTEGER NOT NULL DEFAULT 0,
    UNIQUE (projet, fournisseur, revision)
);
CREATE TABLE IF NOT EXISTS lignes (
    campagne_id INTEGER NOT NULL REFERENCES campagnes(id) ON DELETE CASCADE,
    exigence TEXT NOT NULL, texte TEXT, norme TEXT, statut TEXT NOT NULL, statut_brut TEXT,
    justification TEXT, cloture TEXT, action TEXT, derogation TEXT, appui TEXT, alertes TEXT,
    dans_referentiel INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_lignes_exigence ON lignes(exigence);
CREATE INDEX IF NOT EXISTS ix_lignes_norme ON lignes(norme);
CREATE INDEX IF NOT EXISTS ix_lignes_campagne ON lignes(campagne_id);
"""


@dataclass
class Campagne:
    id: int
    projet: str
    fournisseur: str
    revision: str
    date: str
    referentiel: str
    ncm: str
    signee: bool
    exigences: int = 0
    ecarts: int = 0


@dataclass
class Historique:
    """Ce que la mémoire sait d'une exigence, hors campagne courante."""
    exigence: str
    campagnes: int = 0                 # campagnes distinctes (projet, fournisseur) où l'exigence apparaît
    contestations: int = 0             # … où elle est NC ou PC
    acceptations: List[Tuple[str, str, str, str]] = field(default_factory=list)   # (projet, fournisseur, dérogation, justification)
    justifications: List[Tuple[str, str, str, str]] = field(default_factory=list) # (projet, fournisseur, statut, justification)

    def texte(self) -> str:
        if not self.campagnes:
            return ""
        parts = [f"vue sur {self.campagnes} campagne(s), contestée {self.contestations} fois"]
        if self.acceptations:
            p, f, d, _j = self.acceptations[0]
            parts.append(f"écart déjà accepté ({p}, {f}" + (f", {d}" if d else "") + ")" +
                         (f" et {len(self.acceptations) - 1} autre(s)" if len(self.acceptations) > 1 else ""))
        return " ; ".join(parts)


class Memoire:
    def __init__(self, chemin: Optional[Path] = None):
        self.chemin = Path(chemin) if chemin else dossier_donnees() / "memoire.sqlite"
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.chemin))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- enregistrement ----------------------------------------------------------------------
    def enregistrer(self, grille, projet: str, signee: bool = False, fournisseur: Optional[str] = None,
                    revision: Optional[str] = None) -> int:
        """Enregistre (ou remplace) la campagne (projet, fournisseur, révision) depuis une grille."""
        projet = (projet or "").strip()
        if not projet:
            raise ValueError("un nom de projet est requis pour capitaliser")
        fournisseur = (fournisseur if fournisseur is not None else grille.fournisseur or "").strip() or "(sans fournisseur)"
        revision = (revision if revision is not None else grille.revision or "").strip()
        with self.conn:
            self.conn.execute("DELETE FROM campagnes WHERE projet = ? AND fournisseur = ? AND revision = ?",
                              (projet, fournisseur, revision))
            cur = self.conn.execute(
                "INSERT INTO campagnes (projet, fournisseur, revision, date, referentiel, ncm, signee) VALUES (?,?,?,?,?,?,?)",
                (projet, fournisseur, revision, datetime.now().isoformat(timespec="seconds"),
                 grille.referentiel, grille.ncm, 1 if signee else 0))
            cid = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO lignes (campagne_id, exigence, texte, norme, statut, statut_brut, justification, cloture, action, "
                "derogation, appui, alertes, dans_referentiel) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(cid, l.id, l.texte, l.norme, l.statut.value, l.statut_brut, l.justification, l.cloture,
                  l.champs.get("action", ""), l.champs.get("derogation", ""), l.appui_niveau, ",".join(l.alertes),
                  1 if l.dans_referentiel else 0) for l in grille.lignes])
        return int(cid)

    def supprimer(self, projet: str, fournisseur: str, revision: str = "") -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM campagnes WHERE projet = ? AND fournisseur = ? AND revision = ?",
                                    (projet, fournisseur, revision))
        return cur.rowcount

    # --- lecture -------------------------------------------------------------------------------
    def campagnes(self) -> List[Campagne]:
        rows = self.conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM lignes l WHERE l.campagne_id = c.id AND l.dans_referentiel = 1) AS exigences, "
            "(SELECT COUNT(*) FROM lignes l WHERE l.campagne_id = c.id AND l.statut IN ('NC','PC')) AS ecarts "
            "FROM campagnes c ORDER BY projet, fournisseur, revision").fetchall()
        return [Campagne(r["id"], r["projet"], r["fournisseur"], r["revision"], r["date"], r["referentiel"] or "",
                         r["ncm"] or "", bool(r["signee"]), r["exigences"], r["ecarts"]) for r in rows]

    def _exclusion(self, exclure: Optional[Tuple[str, str, str]]) -> Tuple[str, list]:
        if not exclure:
            return "", []
        return " AND NOT (c.projet = ? AND c.fournisseur = ? AND c.revision = ?)", list(exclure)

    def historique(self, exigence: str, exclure: Optional[Tuple[str, str, str]] = None,
                   signees_seulement: bool = False) -> Historique:
        """Historique d'une exigence sur les autres campagnes. Une campagne =
        un couple (projet, fournisseur) ; si plusieurs révisions existent, la
        plus récente (signée de préférence) compte."""
        sql_excl, params = self._exclusion(exclure)
        rows = self.conn.execute(
            "SELECT c.projet, c.fournisseur, c.revision, c.signee, c.date, l.statut, l.cloture, l.derogation, l.justification "
            "FROM lignes l JOIN campagnes c ON c.id = l.campagne_id WHERE l.exigence = ?" + sql_excl +
            (" AND c.signee = 1" if signees_seulement else "") + " ORDER BY c.projet, c.fournisseur, c.signee DESC, c.date DESC",
            [exigence] + params).fetchall()
        h = Historique(exigence=exigence)
        vus = set()
        for r in rows:
            cle = (r["projet"], r["fournisseur"])
            if cle in vus:
                continue
            vus.add(cle)
            h.campagnes += 1
            if r["statut"] in ECARTS:
                h.contestations += 1
                h.justifications.append((r["projet"], r["fournisseur"], r["statut"], r["justification"] or ""))
                if r["cloture"] == "dérogation" or r["derogation"]:
                    h.acceptations.append((r["projet"], r["fournisseur"], r["derogation"] or "", r["justification"] or ""))
        return h

    def exigences_contestees(self, minimum: int = 1, exclure: Optional[Tuple[str, str, str]] = None) -> List[dict]:
        """Exigences classées par nombre de campagnes (projet, fournisseur) où elles sont NC ou PC."""
        sql_excl, params = self._exclusion(exclure)
        rows = self.conn.execute(
            "SELECT l.exigence, MAX(l.texte) AS texte, MAX(l.norme) AS norme, c.projet, c.fournisseur, "
            "MAX(CASE WHEN l.statut IN ('NC','PC') THEN 1 ELSE 0 END) AS conteste, "
            "MAX(CASE WHEN l.statut IN ('NC','PC') AND (l.cloture = 'dérogation' OR l.derogation <> '') THEN 1 ELSE 0 END) AS accepte "
            "FROM lignes l JOIN campagnes c ON c.id = l.campagne_id WHERE l.dans_referentiel = 1" + sql_excl +
            " GROUP BY l.exigence, c.projet, c.fournisseur", params).fetchall()
        agg: Dict[str, dict] = {}
        for r in rows:
            d = agg.setdefault(r["exigence"], {"exigence": r["exigence"], "texte": r["texte"] or "", "norme": r["norme"] or "",
                                               "campagnes": 0, "contestations": 0, "acceptations": 0, "ou": []})
            d["campagnes"] += 1
            if r["conteste"]:
                d["contestations"] += 1
                d["ou"].append(f"{r['projet']} / {r['fournisseur']}")
            if r["accepte"]:
                d["acceptations"] += 1
            if r["texte"] and not d["texte"]:
                d["texte"] = r["texte"]
            if r["norme"] and not d["norme"]:
                d["norme"] = r["norme"]
        out = [d for d in agg.values() if d["contestations"] >= minimum]
        from .identifiants import cle_tri
        out.sort(key=lambda d: (-d["contestations"], -d["campagnes"], cle_tri(d["exigence"])))
        return out

    def ecarts_acceptes(self, exigence: Optional[str] = None, norme: Optional[str] = None) -> List[dict]:
        sql = ("SELECT c.projet, c.fournisseur, c.revision, c.signee, l.exigence, l.norme, l.statut, l.derogation, l.justification, l.action "
               "FROM lignes l JOIN campagnes c ON c.id = l.campagne_id "
               "WHERE l.statut IN ('NC','PC') AND (l.cloture = 'dérogation' OR l.derogation <> '')")
        params: list = []
        if exigence:
            sql += " AND l.exigence = ?"
            params.append(exigence)
        if norme:
            sql += " AND l.norme LIKE ?"
            params.append(f"%{norme}%")
        sql += " ORDER BY l.exigence, c.projet, c.fournisseur"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def par_norme(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT l.norme, COUNT(DISTINCT l.exigence) AS exigences, "
            "SUM(CASE WHEN l.statut IN ('NC','PC') THEN 1 ELSE 0 END) AS ecarts, "
            "SUM(CASE WHEN l.statut IN ('NC','PC') AND (l.cloture = 'dérogation' OR l.derogation <> '') THEN 1 ELSE 0 END) AS acceptes, "
            "COUNT(DISTINCT l.campagne_id) AS campagnes "
            "FROM lignes l WHERE l.norme <> '' AND l.dans_referentiel = 1 GROUP BY l.norme").fetchall()
        from .norme import cle_norme
        return sorted((dict(r) for r in rows), key=lambda d: cle_norme(d["norme"]))

    def par_fournisseur(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT c.fournisseur, COUNT(DISTINCT c.id) AS campagnes, COUNT(DISTINCT c.projet) AS projets, "
            "SUM(CASE WHEN l.dans_referentiel = 1 THEN 1 ELSE 0 END) AS exigences, "
            "SUM(CASE WHEN l.statut = 'C' THEN 1 ELSE 0 END) AS conformes, "
            "SUM(CASE WHEN l.statut IN ('NC','PC') THEN 1 ELSE 0 END) AS ecarts, "
            "SUM(CASE WHEN l.cloture = 'ouvert' THEN 1 ELSE 0 END) AS ouverts, "
            "SUM(CASE WHEN l.appui = 'absent' AND l.statut = 'C' THEN 1 ELSE 0 END) AS c_sans_appui "
            "FROM campagnes c JOIN lignes l ON l.campagne_id = c.id GROUP BY c.fournisseur ORDER BY c.fournisseur").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["taux_conformite"] = (d["conformes"] / d["exigences"]) if d["exigences"] else 0.0
            out.append(d)
        return out

    def chercher(self, texte: str, limite: int = 100) -> List[dict]:
        motif = f"%{texte.strip()}%"
        rows = self.conn.execute(
            "SELECT c.projet, c.fournisseur, c.revision, l.exigence, l.statut, l.norme, l.texte, l.justification "
            "FROM lignes l JOIN campagnes c ON c.id = l.campagne_id "
            "WHERE l.justification LIKE ? OR l.texte LIKE ? OR l.exigence LIKE ? ORDER BY l.exigence, c.projet LIMIT ?",
            (motif, motif, motif, limite)).fetchall()
        return [dict(r) for r in rows]

    # --- annotation de la grille courante -------------------------------------------------------
    def annoter(self, grille, projet: str = "", signees_seulement: bool = False) -> int:
        """Reporte sur chaque ligne de la grille l'historique de son exigence
        (campagnes autres que celle de la grille) ; renvoie le nombre de lignes annotées."""
        exclure = (projet.strip(), (grille.fournisseur or "").strip() or "(sans fournisseur)", (grille.revision or "").strip()) if projet else None
        n = 0
        for lg in grille.lignes:
            h = self.historique(lg.id, exclure=exclure, signees_seulement=signees_seulement)
            lg.historique = h.texte()
            lg.contestations = h.contestations
            lg.ecart_accepte_avant = bool(h.acceptations)
            if h.campagnes:
                n += 1
        return n

    # --- export --------------------------------------------------------------------------------
    def exporter(self, chemin) -> Path:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        wb.remove(wb.active)

        def feuille(titre, colonnes):
            ws = wb.create_sheet(titre)
            ws.append([c[0] for c in colonnes])
            for i, (_, w) in enumerate(colonnes, start=1):
                ws.column_dimensions[get_column_letter(i)].width = w
                ws.cell(1, i).font = Font(bold=True, color="FFFFFF")
                ws.cell(1, i).fill = PatternFill("solid", fgColor="0B4F6C")
            ws.freeze_panes = "A2"
            return ws

        ws = feuille("Campagnes", [("Projet", 20), ("Fournisseur", 20), ("Révision", 10), ("Date", 20), ("Signée", 8),
                                   ("Exigences", 10), ("Écarts", 8), ("Référentiel", 40), ("NCM", 40)])
        for c in self.campagnes():
            ws.append([c.projet, c.fournisseur, c.revision, c.date, "oui" if c.signee else "non", c.exigences, c.ecarts, c.referentiel, c.ncm])
        ws = feuille("Exigences contestées", [("Identifiant", 14), ("Exigence", 60), ("Norme", 14), ("Campagnes", 10),
                                              ("Contestations", 12), ("Acceptations", 12), ("Où", 50)])
        for d in self.exigences_contestees():
            ws.append([d["exigence"], d["texte"], d["norme"], d["campagnes"], d["contestations"], d["acceptations"], "; ".join(d["ou"])])
        ws = feuille("Écarts acceptés", [("Identifiant", 14), ("Norme", 14), ("Projet", 20), ("Fournisseur", 20), ("Révision", 10),
                                         ("Signée", 8), ("Statut", 8), ("Dérogation", 16), ("Justification", 60), ("Action", 30)])
        for d in self.ecarts_acceptes():
            ws.append([d["exigence"], d["norme"], d["projet"], d["fournisseur"], d["revision"], "oui" if d["signee"] else "non",
                       d["statut"], d["derogation"], d["justification"], d["action"]])
        ws = feuille("Par norme", [("Paragraphe", 16), ("Exigences", 10), ("Écarts", 8), ("Acceptés", 8), ("Campagnes", 10)])
        for d in self.par_norme():
            ws.append([d["norme"], d["exigences"], d["ecarts"], d["acceptes"], d["campagnes"]])
        ws = feuille("Par fournisseur", [("Fournisseur", 20), ("Campagnes", 10), ("Projets", 8), ("Exigences", 10), ("Conformes", 10),
                                         ("Taux", 8), ("Écarts", 8), ("Ouverts", 8), ("C sans appui", 12)])
        for d in self.par_fournisseur():
            ws.append([d["fournisseur"], d["campagnes"], d["projets"], d["exigences"], d["conformes"],
                       round(d["taux_conformite"], 3), d["ecarts"], d["ouverts"], d["c_sans_appui"]])
        p = Path(chemin)
        p.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(p))
        return p

    def texte_resume(self) -> str:
        cs = self.campagnes()
        projets = {c.projet for c in cs}
        fournisseurs = {c.fournisseur for c in cs}
        contestees = self.exigences_contestees(minimum=2)
        acceptes = self.ecarts_acceptes()
        return (f"Mémoire : {len(cs)} campagne(s), {len(projets)} projet(s), {len(fournisseurs)} fournisseur(s) — "
                f"{len(contestees)} exigence(s) contestée(s) sur au moins deux campagnes, {len(acceptes)} écart(s) accepté(s)")
