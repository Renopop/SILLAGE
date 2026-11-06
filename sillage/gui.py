"""Interface graphique de SILLAGE (Tkinter), autonome.

Un **projet** : la spécification (référentiel), le projet, le fournisseur, sa
NCM et sa révision, le dossier de ses documents, l'index du moteur. Sept
**étapes** : lire la NCM (diagnostic d'exploitabilité, statuts inconnus
proposés à l'enrichissement), grille des écarts, indexer les documents du
fournisseur (moteur de RIVAGE embarqué), vérifier que les « conformes » sont
étayés, relever les affaiblissements, rédiger les fiches de revue (seule
étape générative), capitaliser la campagne dans la mémoire des projets. Des
**outils** : profil des colonnes, dictionnaire des statuts, comparaison avec
une révision antérieure, mémoire des projets, traçabilité vers la norme
(proposer, charger, documents de la norme), fiche « ce poste ». La
**grille** à l'écran : une ligne par exigence, colorée par statut, filtrable
par statut, par alerte et par texte, avec un volet de détail (exigence, ligne
de NCM, clôture, origine, appui et passage, valeurs absentes,
affaiblissements, fiche de revue, historique, alertes). **Exports** : grille
et diff en Excel, fiches en Word, NCM normalisée, appuis et fiches en JSON.

Même modèle que RIVAGE : les travaux longs tournent dans un fil, les
résultats reviennent par une file relevée toutes les 100 ms ; un seul
travail à la fois, les boutons sont verrouillés pendant.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

from .config import dossier_donnees, dossier_index
from .modele import Statut
from .version import VERSION_LABEL

logger = logging.getLogger("sillage.gui")

EXT_DOCUMENTS = (".docx", ".pdf", ".txt", ".md", ".xlsx", ".xlsm", ".csv", ".pptx")
COULEURS_STATUT = {"C": "#c6efce", "PC": "#ffeb9c", "NC": "#ffc7ce", "NA": "#e7e6e6", "?": "#fff2cc"}
ALERTES = ["trou", "conforme_implicite", "orphelin", "doublon", "incoherence", "statut_inconnu", "ecart_ouvert",
           "c_sans_appui", "c_faible", "valeurs_absentes", "affaiblissement", "impact_norme"]
LIBELLES_ALERTES = {
    "trou": "trou (exigence sans ligne)", "conforme_implicite": "conforme implicite (absente de la NCM pré-filtrée)",
    "orphelin": "orphelin (identifiant inconnu)", "doublon": "doublon",
    "incoherence": "incohérence (statuts différents)", "statut_inconnu": "statut inconnu",
    "ecart_ouvert": "écart ouvert (ni action ni dérogation)", "c_sans_appui": "« C » sans appui",
    "c_faible": "« C » à appui faible", "valeurs_absentes": "valeurs chiffrées absentes",
    "affaiblissement": "affaiblissement à proximité",
    "impact_norme": "impact norme (écart sur exigence tracée)",
}


# --- journal vers l'interface ----------------------------------------------------------
class _FileJournal(logging.Handler):
    def __init__(self, q: "queue.Queue"):
        super().__init__(level=logging.INFO)
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("journal", self.format(record)))
        except Exception:
            pass


def _fichiers_documents(dossier: str) -> List[str]:
    out = []
    for racine, dirs, noms in os.walk(dossier):
        dirs[:] = [d for d in dirs if not d.startswith((".", "~", "$"))]
        for n in sorted(noms):
            if n.lower().endswith(EXT_DOCUMENTS) and not n.startswith("~$"):
                out.append(os.path.join(racine, n))
    return out


class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{VERSION_LABEL} — Spécifications, Incomplétudes, Livrables : Lecture, Analyse et Grille des Écarts")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1280, sw - 80)}x{min(820, sh - 100)}")
        self.minsize(960, 620)
        self.q: "queue.Queue" = queue.Queue()
        self.ref = None
        self.ncm = None
        self.campagne = None
        self.releve = None
        self.grille = None
        self.diff = None
        self.tracabilite = None
        self.norme_documents: List[str] = []
        self.fiches = None
        self.en_cours = False
        self._lignes_par_item: Dict[str, Any] = {}
        self.reglages = self._charger_reglages()
        self._construire()
        self._journal_handler = _FileJournal(self.q)
        self._journal_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(self._journal_handler)
        if logging.getLogger().level == logging.NOTSET or logging.getLogger().level > logging.INFO:
            logging.getLogger().setLevel(logging.INFO)
        self.protocol("WM_DELETE_WINDOW", self.quitter)
        self.after(100, self._relever)
        self._journal(f"{VERSION_LABEL} prêt. Moteur embarqué : RIVAGE {self._version_moteur()}.")

    # --- réglages persistants ----------------------------------------------------------
    def _fichier_reglages(self) -> Path:
        return dossier_donnees() / "gui.json"

    def _charger_reglages(self) -> dict:
        try:
            d = json.loads(self._fichier_reglages().read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _enregistrer_reglages(self) -> None:
        d = {"spec": self.v_spec.get(), "projet": self.v_projet.get(), "fournisseur": self.v_fournisseur.get(), "ncm": self.v_ncm.get(),
             "documents": self.v_documents.get(), "index": self.v_index.get(),
             "seuil_etaye": self.v_seuil_etaye.get(), "seuil_faible": self.v_seuil_faible.get(),
             "statuts": self.v_statuts.get(), "ocr": self.v_ocr.get(), "ocr_forcer": self.v_ocr_forcer.get()}
        try:
            p = self._fichier_reglages()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def _version_moteur(self) -> str:
        try:
            from .rivage import version_rivage
            return version_rivage()
        except Exception:
            return "?"

    # --- construction ------------------------------------------------------------------
    def _construire(self) -> None:
        style = ttk.Style(self)
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
            elif "clam" in style.theme_names():
                style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Titre.TLabel", font=("", 10, "bold"))

        self._construire_menu()
        r = self.reglages
        self.v_spec = tk.StringVar(value=r.get("spec", ""))
        self.v_projet = tk.StringVar(value=r.get("projet", ""))
        self.v_signee = tk.BooleanVar(value=False)
        self.v_prefiltree = tk.BooleanVar(value=False)
        self.v_ocr = tk.BooleanVar(value=bool(r.get("ocr", False)))
        self.v_ocr_forcer = tk.BooleanVar(value=bool(r.get("ocr_forcer", False)))
        self.v_fournisseur = tk.StringVar(value=r.get("fournisseur", ""))
        self.v_ncm = tk.StringVar(value=r.get("ncm", ""))
        self.v_revision = tk.StringVar(value="")
        self.v_documents = tk.StringVar(value=r.get("documents", ""))
        self.v_index = tk.StringVar(value=r.get("index", ""))
        self.v_seuil_etaye = tk.StringVar(value=str(r.get("seuil_etaye", "0.5")))
        self.v_seuil_faible = tk.StringVar(value=str(r.get("seuil_faible", "0.25")))
        self.v_statuts = tk.StringVar(value=r.get("statuts", "C"))
        self.v_statut_filtre = tk.StringVar(value="tous")
        self.v_alerte_filtre = tk.StringVar(value="toutes")
        self.v_recherche = tk.StringVar(value="")
        self.v_statut_bar = tk.StringVar(value="Prêt.")

        # zone projet
        projet = ttk.LabelFrame(self, text=" Projet ", padding=(8, 4))
        projet.pack(fill="x", padx=8, pady=(8, 4))
        projet.columnconfigure(1, weight=1)
        projet.columnconfigure(4, weight=1)

        def ligne(rang, libelle, var, bouton=None, colonne=0, largeur=52):
            ttk.Label(projet, text=libelle).grid(row=rang, column=colonne, sticky="w", padx=(0, 6), pady=2)
            e = ttk.Entry(projet, textvariable=var, width=largeur)
            e.grid(row=rang, column=colonne + 1, sticky="ew", pady=2)
            if bouton:
                ttk.Button(projet, text="…", width=3, command=bouton).grid(row=rang, column=colonne + 2, padx=(2, 12))
            return e

        ligne(0, "Spécification (référentiel) :", self.v_spec, self._choisir_spec)
        ligne(0, "Projet :", self.v_projet, None, colonne=3, largeur=28)
        ligne(1, "NCM :", self.v_ncm, self._choisir_ncm)
        ligne(1, "Fournisseur :", self.v_fournisseur, None, colonne=3, largeur=28)
        ligne(2, "Documents du fournisseur :", self.v_documents, self._choisir_documents)
        ligne(2, "Révision :", self.v_revision, None, colonne=3, largeur=28)
        ligne(3, "Index (moteur) :", self.v_index, self._choisir_index)
        cases = ttk.Frame(projet)
        cases.grid(row=3, column=3, columnspan=2, sticky="w")
        ttk.Checkbutton(cases, text="NCM finale signée", variable=self.v_signee).pack(side="left")
        ttk.Checkbutton(cases, text="NCM pré-filtrée (absentes réputées conformes)", variable=self.v_prefiltree,
                        command=lambda: self.calculer_grille() if self.ncm is not None else None).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(cases, text="OCR de secours (PDF scannés, appels payants)", variable=self.v_ocr).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(cases, text="forcer sur toutes les pages", variable=self.v_ocr_forcer).pack(side="left", padx=(6, 0))

        # actions : les étapes sur une rangée, les outils et réglages sur la suivante
        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=8, pady=(0, 2))
        self.boutons: Dict[str, ttk.Button] = {}
        for nom, libelle, cmd in [
            ("lire", "1. Lire la NCM", self.lire_ncm),
            ("grille", "2. Grille des écarts", self.calculer_grille),
            ("indexer", "3. Indexer les documents", self.indexer),
            ("verifier", "4. Vérifier les « C »", self.verifier),
            ("affaiblissements", "5. Affaiblissements", self.affaiblissements),
            ("fiches", "6. Fiches de revue", self.rediger_fiches),
            ("capitaliser", "7. Capitaliser", self.capitaliser),
            ("exporter", "Exporter la grille…", self.exporter_grille),
        ]:
            b = ttk.Button(actions, text=libelle, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.boutons[nom] = b
        outils = ttk.Frame(self)
        outils.pack(fill="x", padx=8, pady=(0, 4))
        for nom, libelle, cmd in [
            ("profil", "Profil des colonnes…", self.confirmer_profil),
            ("statuts", "Dictionnaire des statuts…", lambda: self.dialogue_statuts()),
            ("diff", "Comparer avec une révision antérieure…", self.comparer),
            ("memoire", "Mémoire des projets…", self.fenetre_memoire),
        ]:
            b = ttk.Button(outils, text=libelle, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.boutons[nom] = b
        reg = ttk.Frame(outils)
        reg.pack(side="right")
        ttk.Label(reg, text="Vérification — statuts :").pack(side="left")
        ttk.Entry(reg, textvariable=self.v_statuts, width=6).pack(side="left", padx=(2, 8))
        ttk.Label(reg, text="seuil étayé :").pack(side="left")
        ttk.Entry(reg, textvariable=self.v_seuil_etaye, width=5).pack(side="left", padx=(2, 8))
        ttk.Label(reg, text="faible :").pack(side="left")
        ttk.Entry(reg, textvariable=self.v_seuil_faible, width=5).pack(side="left", padx=(2, 0))

        # filtres
        filtres = ttk.Frame(self)
        filtres.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(filtres, text="Statut :").pack(side="left")
        cb = ttk.Combobox(filtres, textvariable=self.v_statut_filtre, state="readonly", width=16,
                          values=["tous", "C", "PC", "NC", "NA", "?", "écarts (PC + NC)"])
        cb.pack(side="left", padx=(2, 10))
        cb.bind("<<ComboboxSelected>>", lambda e: self._rafraichir_grille())
        ttk.Label(filtres, text="Alerte :").pack(side="left")
        cb2 = ttk.Combobox(filtres, textvariable=self.v_alerte_filtre, state="readonly", width=34,
                           values=["toutes", "avec alerte", "sans alerte"] + [LIBELLES_ALERTES[a] for a in ALERTES])
        cb2.pack(side="left", padx=(2, 10))
        cb2.bind("<<ComboboxSelected>>", lambda e: self._rafraichir_grille())
        ttk.Label(filtres, text="Rechercher :").pack(side="left")
        er = ttk.Entry(filtres, textvariable=self.v_recherche, width=28)
        er.pack(side="left", padx=(2, 10))
        er.bind("<KeyRelease>", lambda e: self._rafraichir_grille())
        self.l_compte = ttk.Label(filtres, text="")
        self.l_compte.pack(side="left")

        # grille + détail
        pan = ttk.PanedWindow(self, orient="horizontal")
        pan.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        gauche = ttk.Frame(pan)
        cols = ("statut", "appui", "cloture", "alertes", "exigence")
        self.tree = ttk.Treeview(gauche, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Identifiant")
        self.tree.column("#0", width=120, stretch=False)
        for c, lib, w in (("statut", "Statut", 60), ("appui", "Appui", 80), ("cloture", "Clôture", 80),
                          ("alertes", "Alertes", 180), ("exigence", "Exigence", 420)):
            self.tree.heading(c, text=lib)
            self.tree.column(c, width=w, stretch=(c == "exigence"))
        for code, coul in COULEURS_STATUT.items():
            self.tree.tag_configure(f"s_{code}", background=coul)
        self.tree.tag_configure("orphelin", foreground="#7a4a00")
        sb = ttk.Scrollbar(gauche, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._afficher_detail)
        pan.add(gauche, weight=3)

        droite = ttk.Frame(pan)
        ttk.Label(droite, text="Détail", style="Titre.TLabel").pack(anchor="w")
        self.detail = tk.Text(droite, wrap="word", width=48, state="disabled", font=("", 10))
        self.detail.tag_configure("titre", font=("", 10, "bold"), spacing1=6)
        self.detail.tag_configure("gris", foreground="#5a6a79")
        self.detail.tag_configure("rouge", foreground="#9a3b3b")
        sbd = ttk.Scrollbar(droite, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=sbd.set)
        self.detail.pack(side="left", fill="both", expand=True)
        sbd.pack(side="right", fill="y")
        pan.add(droite, weight=2)

        # bas : onglets diagnostic / résumé / journal
        self.onglets = ttk.Notebook(self, height=150)
        self.onglets.pack(fill="x", padx=8, pady=(0, 4))
        self.t_diag = self._onglet_texte("Diagnostic")
        self.t_resume = self._onglet_texte("Résumé")
        self.t_journal = self._onglet_texte("Journal")

        # barre d'état
        bas = ttk.Frame(self)
        bas.pack(fill="x", padx=8, pady=(0, 6))
        self.progres = ttk.Progressbar(bas, mode="indeterminate", length=160)
        self.progres.pack(side="right")
        ttk.Label(bas, textvariable=self.v_statut_bar).pack(side="left", fill="x", expand=True)

    def _onglet_texte(self, titre: str) -> tk.Text:
        f = ttk.Frame(self.onglets)
        t = tk.Text(f, wrap="word", height=8, state="disabled", font=("", 9))
        sb = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.onglets.add(f, text=titre)
        return t

    def _construire_menu(self) -> None:
        m = tk.Menu(self)
        fichier = tk.Menu(m, tearoff=False)
        fichier.add_command(label="Ouvrir une spécification…", command=self._choisir_spec)
        fichier.add_command(label="Ouvrir une NCM…", command=self._choisir_ncm)
        fichier.add_separator()
        fichier.add_command(label="Exporter la grille…", command=self.exporter_grille)
        fichier.add_command(label="Exporter le diff…", command=self.exporter_diff)
        fichier.add_separator()
        fichier.add_command(label="Quitter", command=self.quitter)
        m.add_cascade(label="Fichier", menu=fichier)
        outils = tk.Menu(m, tearoff=False)
        outils.add_command(label="Dictionnaire des statuts…", command=self.dialogue_statuts)
        outils.add_command(label="Profil des colonnes du fournisseur…", command=self.confirmer_profil)
        outils.add_command(label="Enregistrer la NCM normalisée (JSON)…", command=self.enregistrer_ncm_json)
        outils.add_command(label="Enregistrer la campagne d'appuis (JSON)…", command=self.enregistrer_appuis_json)
        outils.add_command(label="Reprendre une campagne d'appuis (JSON)…", command=self.charger_appuis_json)
        outils.add_separator()
        outils.add_command(label="Exporter les fiches de revue (Word)…", command=self.exporter_fiches)
        outils.add_command(label="Enregistrer les fiches de revue (JSON)…", command=self.enregistrer_fiches_json)
        outils.add_command(label="Reprendre des fiches de revue (JSON)…", command=self.charger_fiches_json)
        outils.add_separator()
        outils.add_separator()
        outils.add_command(label="Tracer les exigences vers la norme…", command=self.tracer_norme)
        outils.add_command(label="Charger une traçabilité (JSON ou classeur relu)…", command=self.charger_tracabilite)
        outils.add_command(label="Documents de la norme (couverture)…", command=self.choisir_norme_documents)
        outils.add_separator()
        outils.add_command(label="Ce poste (moteur, processeur, mémoire)…", command=self.fiche_poste)
        m.add_cascade(label="Outils", menu=outils)
        aide = tk.Menu(m, tearoff=False)
        aide.add_command(label="À propos", command=self.a_propos)
        m.add_cascade(label="Aide", menu=aide)
        self.config(menu=m)

    # --- choix de fichiers --------------------------------------------------------------
    def _choisir_spec(self):
        p = filedialog.askopenfilename(title="Spécification (référentiel)", filetypes=[
            ("Spécification", "*.xlsx *.xlsm *.csv *.docx *.txt *.md *.pdf"), ("Tous", "*.*")])
        if p:
            self.v_spec.set(p)
            self.ref = None

    def _choisir_ncm(self):
        p = filedialog.askopenfilename(title="NCM du fournisseur", filetypes=[
            ("NCM", "*.xlsx *.xlsm *.docx *.pdf *.csv"), ("Tous", "*.*")])
        if p:
            self.v_ncm.set(p)
            from .lecture_ncm import revision_depuis_nom
            self.v_revision.set(revision_depuis_nom(p))
            self.ncm = None

    def _choisir_documents(self):
        p = filedialog.askdirectory(title="Dossier des documents du fournisseur")
        if p:
            self.v_documents.set(p)

    def _choisir_index(self):
        p = filedialog.askdirectory(title="Dossier de l'index (moteur)")
        if p:
            self.v_index.set(p)

    def _index_dir(self) -> str:
        return self.v_index.get().strip() or str(dossier_index(self.v_fournisseur.get().strip()))

    # --- fil de travail -----------------------------------------------------------------
    def _lancer(self, nom: str, travail: Callable[[], Any], fin: Callable[[Any], None], message: str) -> None:
        if self.en_cours:
            messagebox.showinfo("SILLAGE", "Un travail est déjà en cours.")
            return
        self.en_cours = True
        for b in self.boutons.values():
            b.state(["disabled"])
        self.v_statut_bar.set(message)
        self.progres.start(12)
        self._journal(message)

        def worker():
            try:
                res = travail()
                self.q.put(("fin", nom, fin, res))
            except Exception as exc:
                logger.exception("échec de « %s »", nom)
                self.q.put(("erreur", nom, f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _relever(self):
        try:
            while True:
                msg = self.q.get_nowait()
                genre = msg[0]
                if genre == "journal":
                    self._ecrire(self.t_journal, msg[1] + "\n", ajouter=True)
                elif genre == "progression":
                    self.v_statut_bar.set(msg[1])
                elif genre == "fin":
                    _, nom, fin, res = msg
                    self._terminer()
                    try:
                        fin(res)
                    except Exception as exc:
                        logger.exception("échec à la fin de « %s »", nom)
                        messagebox.showerror("SILLAGE", f"{type(exc).__name__}: {exc}")
                elif genre == "erreur":
                    self._terminer()
                    self.v_statut_bar.set(f"Échec : {msg[2]}")
                    messagebox.showerror("SILLAGE", f"« {msg[1]} » a échoué :\n{msg[2]}")
        except queue.Empty:
            pass
        self.after(100, self._relever)

    def _terminer(self):
        self.en_cours = False
        self.progres.stop()
        for b in self.boutons.values():
            b.state(["!disabled"])
        self.v_statut_bar.set("Prêt.")

    def _journal(self, texte: str):
        logger.info(texte)

    def _ecrire(self, widget: tk.Text, texte: str, ajouter: bool = False):
        widget.configure(state="normal")
        if not ajouter:
            widget.delete("1.0", "end")
        widget.insert("end", texte)
        widget.see("end")
        widget.configure(state="disabled")

    # --- actions ---------------------------------------------------------------------------
    def _regime_ocr(self):
        """False, True (secours) ou « forcer » — à lire dans le fil principal."""
        if self.v_ocr_forcer.get():
            return "forcer"
        return self.v_ocr.get()

    def _charger_ref(self, spec: Optional[str] = None, ocr=None):
        """Le référentiel (relu si la spécification a changé). Hors du fil
        principal, `spec` et `ocr` doivent être donnés : les variables Tk n'y
        sont pas lisibles."""
        from .referentiel import lire_referentiel
        if spec is None:
            spec = self.v_spec.get().strip()
        if ocr is None:
            ocr = self._regime_ocr()
        if not spec:
            raise ValueError("choisissez d'abord la spécification (référentiel)")
        if self.ref is None or self.ref.source != spec:
            self.ref = lire_referentiel(spec, ocr=ocr)
        return self.ref

    def _lire_ncm_travail(self, spec: str, ncm_f: str, fournisseur: str, revision: str, ocr=False):
        from .lecture_ncm import lire_ncm
        from .profils import Profil
        from .statuts import DictionnaireStatuts
        ref = self._charger_ref(spec, ocr)
        if not ncm_f:
            raise ValueError("choisissez la NCM à lire")
        profil = Profil.charger(fournisseur) if fournisseur else None
        return lire_ncm(ncm_f, motif=ref.motif, dico=DictionnaireStatuts.charger(), profil=profil,
                        fournisseur=fournisseur, revision=revision, ids_referentiel=ref.ids,
                        textes_referentiel={e.id: e.texte for e in ref.exigences}, ocr=ocr)

    def lire_ncm(self):
        self._enregistrer_reglages()
        # valeurs lues ICI, dans le fil principal : le fil de travail ne touche pas aux variables Tk
        spec, ncm_f = self.v_spec.get().strip(), self.v_ncm.get().strip()
        fournisseur, revision = self.v_fournisseur.get().strip(), self.v_revision.get().strip()
        ocr = self._regime_ocr()

        def fin(ncm):
            self.ncm = ncm
            self.campagne = None
            self.releve = None
            self.v_revision.set(ncm.revision)
            self._ecrire(self.t_diag, f"Référentiel : {len(self.ref)} exigences ({Path(self.ref.source).name})\n"
                                      f"NCM : {Path(ncm.fichier).name}" + (f", révision {ncm.revision}" if ncm.revision else "") +
                                      "\n" + ncm.diagnostic.texte())
            self.onglets.select(0)
            self._journal(f"NCM lue : {len(ncm.lignes)} lignes pivot, {ncm.diagnostic.taux_couverture:.0%} du référentiel retrouvé")
            if ncm.diagnostic.prefiltree and not self.v_prefiltree.get():
                self.v_prefiltree.set(True)
                self._journal("La NCM se déclare pré-filtrée : les exigences absentes sont réputées conformes "
                              "(case « NCM pré-filtrée » cochée ; décochez-la pour les traiter en trous)")
            if ncm.diagnostic.statuts_inconnus:
                if self.dialogue_statuts(ncm.diagnostic.statuts_inconnus):
                    self.lire_ncm()
                    return
            self.calculer_grille()

        self._lancer("lecture de la NCM", lambda: self._lire_ncm_travail(spec, ncm_f, fournisseur, revision, ocr),
                     fin, "Lecture de la NCM…")

    def calculer_grille(self):
        if self.ncm is None:
            self.lire_ncm()
            return
        from .controles import controler
        self.grille = controler(self.ref, self.ncm, campagne=self.campagne, releve=self.releve, tracabilite=self.tracabilite,
                                fiches=self.fiches, prefiltree=self.v_prefiltree.get())
        try:
            from .memoire import Memoire
            with Memoire() as mem:
                mem.annoter(self.grille, projet=self.v_projet.get().strip())
        except Exception as exc:   # la mémoire ne doit jamais empêcher la grille
            logger.warning("mémoire des projets indisponible : %s", exc)
        self._ecrire(self.t_resume, self.grille.texte_resume())
        self._rafraichir_grille()
        self.onglets.select(1)

    def indexer(self):
        from .rivage import disponible, indexer, message_absent
        if not disponible():
            messagebox.showerror("SILLAGE", message_absent())
            return
        docs = self.v_documents.get().strip()
        if not docs or not os.path.isdir(docs):
            messagebox.showwarning("SILLAGE", "Choisissez le dossier des documents du fournisseur.")
            return
        index_dir = self._index_dir()
        self.v_index.set(index_dir)
        self._enregistrer_reglages()

        def travail():
            def prog(ratio, message):
                self.q.put(("progression", f"Indexation {ratio:.0%} — {message}"))
            return indexer(docs, index_dir, progress_cb=prog)

        def fin(stats):
            self._journal(f"Indexation terminée : {stats}")
            messagebox.showinfo("SILLAGE", f"Indexation terminée.\n{stats}")

        self._lancer("indexation", travail, fin, f"Indexation de {docs} par le moteur embarqué…")

    def verifier(self):
        from .rivage import Moteur, disponible, message_absent
        if not disponible():
            messagebox.showerror("SILLAGE", message_absent())
            return
        if self.ncm is None:
            messagebox.showwarning("SILLAGE", "Lisez d'abord la NCM.")
            return
        try:
            seuil_e, seuil_f = float(self.v_seuil_etaye.get().replace(",", ".")), float(self.v_seuil_faible.get().replace(",", "."))
        except ValueError:
            messagebox.showwarning("SILLAGE", "Seuils invalides (nombres attendus, ex. 0.5 et 0.25).")
            return
        statuts = [s.strip().upper() for s in self.v_statuts.get().split(",") if s.strip()] or ["C"]
        index_dir = self._index_dir()
        if not os.path.isdir(index_dir):
            messagebox.showwarning("SILLAGE", f"Index introuvable : {index_dir}\nIndexez d'abord les documents du fournisseur.")
            return
        docs = self.v_documents.get().strip()
        prefixes = [docs] if docs and os.path.isdir(docs) else None
        self._enregistrer_reglages()
        ref, ncm = self.ref, self.ncm

        def travail():
            from .appui import verifier
            def prog(k, n, ident):
                self.q.put(("progression", f"Vérification {k}/{n} — {ident}"))
            with Moteur(index_dir) as moteur:
                return verifier(ref, ncm, moteur, prefixes=prefixes, statuts=statuts, seuil_etaye=seuil_e,
                                seuil_faible=seuil_f, progression=prog)

        def fin(campagne):
            self.campagne = campagne
            self._journal("Vérification terminée.\n" + campagne.texte_resume())
            self._ecrire(self.t_diag, campagne.texte_resume(), ajouter=False)
            self.calculer_grille()

        self._lancer("vérification des conformes", travail, fin, "Vérification des « C » dans la spécification fournisseur…")

    def affaiblissements(self):
        if self.ncm is None:
            messagebox.showwarning("SILLAGE", "Lisez d'abord la NCM.")
            return
        docs = self.v_documents.get().strip()
        fichiers = _fichiers_documents(docs) if docs and os.path.isdir(docs) else []
        if not fichiers:
            messagebox.showwarning("SILLAGE", "Choisissez le dossier des documents du fournisseur (docx, pdf, txt, xlsx…).")
            return
        ref = self.ref

        def travail():
            from .affaiblissements import relever
            return relever(fichiers, motif=ref.motif, ids_connus=ref.ids)

        def fin(releve):
            self.releve = releve
            self._journal(releve.texte_resume())
            self._ecrire(self.t_diag, releve.texte_resume())
            self.calculer_grille()

        self._lancer("affaiblissements", travail, fin, f"Relevé des affaiblissements dans {len(fichiers)} fichier(s)…")

    def comparer(self):
        if self.ncm is None:
            messagebox.showwarning("SILLAGE", "Lisez d'abord la NCM courante (la plus récente).")
            return
        p = filedialog.askopenfilename(title="NCM antérieure à comparer", filetypes=[
            ("NCM", "*.xlsx *.xlsm *.docx *.pdf *.csv"), ("Tous", "*.*")])
        if not p:
            return
        ref, apres = self.ref, self.ncm
        fournisseur = self.v_fournisseur.get().strip()
        ocr = self._regime_ocr()

        def travail():
            from .controles import comparer
            from .lecture_ncm import lire_ncm
            from .profils import Profil
            from .statuts import DictionnaireStatuts
            avant = lire_ncm(p, motif=ref.motif, dico=DictionnaireStatuts.charger(),
                             profil=Profil.charger(fournisseur) if fournisseur else None, fournisseur=fournisseur, ocr=ocr)
            return comparer(avant, apres)

        def fin(diff):
            self.diff = diff
            self._journal(f"Diff {diff.avant} → {diff.apres} : " + diff.texte_resume())
            FenetreDiff(self, diff)

        self._lancer("comparaison", travail, fin, "Comparaison des deux révisions…")

    def exporter_grille(self):
        if self.grille is None:
            messagebox.showwarning("SILLAGE", "Calculez d'abord la grille.")
            return
        defaut = Path(self.v_ncm.get()).stem + "_grille.xlsx" if self.v_ncm.get() else "grille.xlsx"
        p = filedialog.asksaveasfilename(title="Exporter la grille", defaultextension=".xlsx", initialfile=defaut,
                                         filetypes=[("Classeur Excel", "*.xlsx")])
        if not p:
            return
        from .grille import exporter_grille
        couv = None
        if self.norme_documents or any(e.norme for e in self.ref.exigences):
            from .norme import couverture, paragraphes_cites
            couv = couverture(self.ref, paragraphes_cites(self.norme_documents) if self.norme_documents else None)
        exporter_grille(self.grille, p, couverture=couv)
        self._journal(f"Grille écrite : {p}")
        messagebox.showinfo("SILLAGE", f"Grille écrite :\n{p}")

    def exporter_diff(self):
        if self.diff is None:
            messagebox.showwarning("SILLAGE", "Comparez d'abord deux révisions.")
            return
        p = filedialog.asksaveasfilename(title="Exporter le diff", defaultextension=".xlsx", initialfile="diff.xlsx",
                                         filetypes=[("Classeur Excel", "*.xlsx")])
        if not p:
            return
        from .grille import exporter_diff
        exporter_diff(self.diff, p)
        self._journal(f"Diff écrit : {p}")

    def enregistrer_ncm_json(self):
        if self.ncm is None:
            messagebox.showwarning("SILLAGE", "Lisez d'abord la NCM.")
            return
        p = filedialog.asksaveasfilename(title="NCM normalisée", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if p:
            self.ncm.enregistrer(p)
            self._journal(f"NCM normalisée écrite : {p}")

    def enregistrer_appuis_json(self):
        if self.campagne is None:
            messagebox.showwarning("SILLAGE", "Vérifiez d'abord les « C ».")
            return
        p = filedialog.asksaveasfilename(title="Campagne d'appuis", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if p:
            self.campagne.enregistrer(p)
            self._journal(f"Campagne d'appuis écrite : {p}")

    # --- lot 7 : mémoire des projets ------------------------------------------------------------------
    def capitaliser(self):
        if self.grille is None:
            messagebox.showwarning("SILLAGE", "Calculez d'abord la grille.")
            return
        projet = self.v_projet.get().strip()
        if not projet:
            messagebox.showwarning("SILLAGE", "Indiquez le nom du projet pour capitaliser cette campagne.")
            return
        from .memoire import Memoire
        with Memoire() as mem:
            mem.enregistrer(self.grille, projet, signee=self.v_signee.get())
            resume = mem.texte_resume()
        self._enregistrer_reglages()
        self._journal(f"Campagne enregistrée dans la mémoire : projet « {projet} », fournisseur « {self.grille.fournisseur or '(sans fournisseur)'} », "
                      f"révision « {self.grille.revision} »" + (" (NCM signée)" if self.v_signee.get() else "") + f". {resume}")
        self.calculer_grille()

    def fenetre_memoire(self):
        try:
            from .memoire import Memoire
            mem = Memoire()
        except Exception as exc:
            messagebox.showerror("SILLAGE", f"Mémoire indisponible : {exc}")
            return
        FenetreMemoire(self, mem)

    # --- lot 5 : fiches de revue -----------------------------------------------------------------
    def rediger_fiches(self):
        from .rivage import Moteur, disponible, message_absent
        if not disponible():
            messagebox.showerror("SILLAGE", message_absent())
            return
        if self.ncm is None:
            messagebox.showwarning("SILLAGE", "Lisez d'abord la NCM.")
            return
        ref, ncm, campagne = self.ref, self.ncm, self.campagne
        index_dir = self._index_dir()
        docs = self.v_documents.get().strip()
        prefixes = [docs] if docs and os.path.isdir(docs) else None
        avec_moteur = campagne is None and os.path.isdir(index_dir)
        if campagne is None and not avec_moteur:
            if not messagebox.askyesno("SILLAGE", "Aucune campagne d'appuis (étape 4) et aucun index des documents du fournisseur : "
                                       "les fiches seront rédigées sans passage de la spécification fournisseur, à partir "
                                       "de la seule ligne de NCM.\nContinuer ?"):
                return

        def travail():
            from .fiches import ouvrir_synthesizer, rediger
            def prog(k, n, ident):
                self.q.put(("progression", f"Fiche {k}/{n} — {ident}"))
            synth = ouvrir_synthesizer()
            try:
                if avec_moteur:
                    with Moteur(index_dir) as moteur:
                        return rediger(ref, ncm, synth, campagne=None, moteur=moteur, prefixes=prefixes, progression=prog)
                return rediger(ref, ncm, synth, campagne=campagne, progression=prog)
            finally:
                try:
                    synth.close()
                except Exception:
                    pass

        def fin(fiches):
            self.fiches = fiches
            self._journal(fiches.texte_resume())
            self._ecrire(self.t_diag, fiches.texte_resume())
            self.calculer_grille()
            r = fiches.resume()
            if r["erreurs"] and not r["redigees"]:
                messagebox.showwarning("SILLAGE", "Aucune fiche rédigée.\n" + fiches.texte_resume())

        self._lancer("fiches de revue", travail, fin, "Rédaction des fiches de revue par le modèle…")

    def exporter_fiches(self):
        if self.fiches is None or self.ncm is None:
            messagebox.showwarning("SILLAGE", "Rédigez d'abord les fiches de revue (étape 6).")
            return
        defaut = Path(self.v_ncm.get()).stem + "_fiches_revue.docx" if self.v_ncm.get() else "fiches_revue.docx"
        p = filedialog.asksaveasfilename(title="Dossier de revue (Word)", defaultextension=".docx", initialfile=defaut,
                                         filetypes=[("Document Word", "*.docx")])
        if not p:
            return
        from .fiches import exporter_fiches
        exporter_fiches(self.fiches, self.ref, self.ncm, p)
        self._journal(f"Dossier de revue écrit : {p}")
        messagebox.showinfo("SILLAGE", f"Dossier de revue écrit :\n{p}")

    def enregistrer_fiches_json(self):
        if self.fiches is None:
            messagebox.showwarning("SILLAGE", "Rédigez d'abord les fiches de revue.")
            return
        p = filedialog.asksaveasfilename(title="Fiches de revue", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if p:
            self.fiches.enregistrer(p)
            self._journal(f"Fiches écrites : {p}")

    def charger_fiches_json(self):
        p = filedialog.askopenfilename(title="Fiches de revue", filetypes=[("JSON", "*.json")])
        if not p:
            return
        from .fiches import Fiches
        try:
            self.fiches = Fiches.charger(p)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("SILLAGE", f"Fichier refusé : {exc}")
            return
        self._journal(f"Fiches reprises : {p} — " + self.fiches.texte_resume())
        if self.ncm is not None:
            self.calculer_grille()

    # --- lot 4 : norme ----------------------------------------------------------------------
    def tracer_norme(self):
        from .rivage import Moteur, disponible, message_absent
        if not disponible():
            messagebox.showerror("SILLAGE", message_absent())
            return
        try:
            ref = self._charger_ref()
        except ValueError as exc:
            messagebox.showwarning("SILLAGE", str(exc))
            return
        dlg = DialogueTracer(self, self.reglages.get("index_norme", ""))
        self.wait_window(dlg)
        if dlg.resultat is None:
            return
        index_dir, seuil, tout = dlg.resultat
        if not os.path.isdir(index_dir):
            messagebox.showwarning("SILLAGE", f"Index de la norme introuvable : {index_dir}\n"
                                   "Indexez d'abord les documents de la norme (étape 3 avec ce dossier comme index).")
            return
        self.reglages["index_norme"] = index_dir
        self._enregistrer_reglages()

        def travail():
            from .norme import tracer
            def prog(k, n, ident):
                self.q.put(("progression", f"Traçabilité {k}/{n} — {ident}"))
            with Moteur(index_dir) as moteur:
                return tracer(ref, moteur, seulement_sans_norme=not tout, seuil_retenue=seuil, progression=prog)

        def fin(t):
            self.tracabilite = t
            self._journal(t.texte_resume())
            self._ecrire(self.t_diag, t.texte_resume())
            defaut = Path(self.v_spec.get()).stem + "_tracabilite.xlsx"
            p = filedialog.asksaveasfilename(title="Classeur de relecture de la traçabilité", defaultextension=".xlsx",
                                             initialfile=defaut, filetypes=[("Classeur Excel", "*.xlsx")])
            if p:
                from .norme import exporter_tracabilite
                exporter_tracabilite(ref, t, p)
                self._journal(f"Classeur de relecture écrit : {p} — corrigez « Norme retenue », cochez « Validé », "
                              f"puis Outils → Charger une traçabilité")
            if self.ncm is not None:
                self.calculer_grille()

        self._lancer("traçabilité vers la norme", travail, fin, "Recherche des paragraphes de norme pour chaque exigence…")

    def charger_tracabilite(self):
        p = filedialog.askopenfilename(title="Traçabilité (JSON de « tracer » ou classeur relu)",
                                       filetypes=[("Traçabilité", "*.json *.xlsx *.xlsm *.csv"), ("Tous", "*.*")])
        if not p:
            return
        from .norme import Tracabilite
        try:
            self.tracabilite = Tracabilite.charger(p)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("SILLAGE", f"Fichier refusé : {exc}")
            return
        self._journal(f"Traçabilité chargée : {p} — " + self.tracabilite.texte_resume())
        if self.ncm is not None:
            self.calculer_grille()

    def choisir_norme_documents(self):
        fichiers = filedialog.askopenfilenames(title="Documents de la norme (pour la feuille de couverture)",
                                               filetypes=[("Documents", "*.pdf *.docx *.txt *.md *.xlsx *.csv"), ("Tous", "*.*")])
        if fichiers:
            self.norme_documents = list(fichiers)
            self._journal(f"Documents de la norme : {len(fichiers)} fichier(s) ; la couverture sera jointe à l'export de la grille.")

    def charger_appuis_json(self):
        p = filedialog.askopenfilename(title="Campagne d'appuis", filetypes=[("JSON", "*.json")])
        if not p:
            return
        from .appui import Campagne
        try:
            self.campagne = Campagne.charger(p)
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror("SILLAGE", f"Fichier refusé : {exc}")
            return
        self._journal(f"Campagne d'appuis reprise : {p} ({len(self.campagne.appuis)} exigences)")
        if self.ncm is not None:
            self.calculer_grille()

    # --- dialogues -------------------------------------------------------------------------
    def dialogue_statuts(self, inconnus: Optional[Dict[str, int]] = None) -> bool:
        """Enrichit le dictionnaire des statuts ; renvoie True si quelque chose a été ajouté."""
        from .statuts import DictionnaireStatuts
        dico = DictionnaireStatuts.charger()
        dlg = DialogueStatuts(self, dico, inconnus or {})
        self.wait_window(dlg)
        if dlg.ajouts:
            self._journal("Statuts ajoutés : " + ", ".join(f"« {v} » → {s}" for v, s in dlg.ajouts.items()))
        return bool(dlg.ajouts)

    def confirmer_profil(self):
        from .identifiants import MotifIdentifiant
        from .lecture_ncm import reconnaitre_colonnes
        from .profils import Profil
        from .statuts import DictionnaireStatuts
        from .tableaux import lire_tableaux
        fournisseur = self.v_fournisseur.get().strip()
        ncm_f = self.v_ncm.get().strip()
        if not fournisseur or not ncm_f:
            messagebox.showwarning("SILLAGE", "Indiquez le fournisseur et la NCM.")
            return
        try:
            motif = self._charger_ref().motif if self.v_spec.get().strip() else MotifIdentifiant()
            tableaux = lire_tableaux(ncm_f)
        except (OSError, ValueError) as exc:
            messagebox.showerror("SILLAGE", str(exc))
            return
        dico = DictionnaireStatuts.charger()
        for t in tableaux:
            cols = reconnaitre_colonnes(t, motif, dico)
            if cols is not None:
                break
        else:
            messagebox.showwarning("SILLAGE", "Aucun tableau de NCM reconnu dans ce fichier.")
            return
        existant = Profil.charger(fournisseur)
        dlg = DialogueProfil(self, fournisseur, cols.en_tetes, cols.roles(), existant)
        self.wait_window(dlg)
        if dlg.profil is not None:
            chemin = dlg.profil.enregistrer()
            self._journal(f"Profil « {fournisseur} » enregistré : {chemin}")
            self.ncm = None

    def fiche_poste(self):
        try:
            from .rivage import localiser
            localiser()
            import diagnostic
            lignes = diagnostic.fiche_texte(diagnostic.fiche_plateforme())
            texte = "\n".join(lignes) if isinstance(lignes, (list, tuple)) else str(lignes)
        except Exception as exc:
            texte = f"Fiche indisponible : {type(exc).__name__}: {exc}"
        FenetreTexte(self, "Ce poste", f"{VERSION_LABEL} — moteur embarqué RIVAGE {self._version_moteur()}\n\n{texte}")

    def a_propos(self):
        messagebox.showinfo("À propos de SILLAGE",
                            f"{VERSION_LABEL}\nSpécifications, Incomplétudes, Livrables :\nLecture, Analyse et Grille des Écarts\n\n"
                            f"Auteur, créateur et développeur : Renaud Loison\n"
                            f"Moteur embarqué : RIVAGE {self._version_moteur()}\n\nRIVAGE cherche, SILLAGE garde la trace.")

    # --- grille à l'écran ---------------------------------------------------------------------
    def _filtre(self, lg) -> bool:
        sf = self.v_statut_filtre.get()
        if sf == "écarts (PC + NC)":
            if lg.statut not in (Statut.PARTIEL, Statut.NON_CONFORME):
                return False
        elif sf != "tous" and lg.statut.value != sf:
            return False
        af = self.v_alerte_filtre.get()
        if af == "avec alerte" and not lg.alertes:
            return False
        if af == "sans alerte" and lg.alertes:
            return False
        if af not in ("toutes", "avec alerte", "sans alerte"):
            code = next((a for a, lib in LIBELLES_ALERTES.items() if lib == af), None)
            if code and code not in lg.alertes:
                return False
        r = self.v_recherche.get().strip().lower()
        if r and r not in (lg.id + " " + lg.texte + " " + lg.justification + " " + lg.statut_brut).lower():
            return False
        return True

    def _rafraichir_grille(self):
        self.tree.delete(*self.tree.get_children())
        self._lignes_par_item.clear()
        if self.grille is None:
            self.l_compte.configure(text="")
            return
        n = 0
        for lg in self.grille.lignes:
            if not self._filtre(lg):
                continue
            n += 1
            tags = [f"s_{lg.statut.value}"]
            if not lg.dans_referentiel:
                tags.append("orphelin")
            item = self.tree.insert("", "end", text=lg.id, tags=tags, values=(
                lg.statut.value, lg.appui_niveau, lg.cloture, ", ".join(lg.alertes),
                " ".join((lg.texte or "").split())[:160]))
            self._lignes_par_item[item] = lg
        self.l_compte.configure(text=f"{n} / {len(self.grille.lignes)} lignes")

    def _afficher_detail(self, _evt=None):
        sel = self.tree.selection()
        lg = self._lignes_par_item.get(sel[0]) if sel else None
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if lg is None:
            self.detail.configure(state="disabled")
            return
        ins = self.detail.insert
        ins("end", f"{lg.id}  —  {lg.statut.libelle} ({lg.statut.value})\n", "titre")
        if not lg.dans_referentiel:
            ins("end", "Identifiant absent du référentiel (orphelin).\n", "rouge")
        if lg.norme:
            ins("end", f"Norme : {lg.norme}\n", "gris")
        ins("end", "Exigence\n", "titre")
        ins("end", (lg.texte or "(texte absent du référentiel)") + "\n")
        ins("end", "Ligne de NCM\n", "titre")
        ins("end", f"Statut brut : {lg.statut_brut or '—'}\n")
        ins("end", f"Justification : {lg.justification or '—'}\n")
        for k, lib in (("action", "Action"), ("echeance", "Échéance"), ("reference", "Référence"),
                       ("derogation", "Dérogation"), ("commentaire", "Commentaire")):
            if lg.champs.get(k):
                ins("end", f"{lib} : {lg.champs[k]}\n")
        if lg.cloture:
            ins("end", f"Clôture de l'écart : {lg.cloture}\n")
        ins("end", f"Origine : {lg.origine or '—'} (chemin : {lg.chemin or '—'}, {lg.nb_lignes_ncm} ligne(s))\n", "gris")
        if lg.appui_niveau:
            ins("end", "Appui dans la spécification fournisseur\n", "titre")
            score = f" (score {lg.appui_score:.2f})" if lg.appui_score is not None else ""
            ins("end", f"{lg.appui_niveau}{score} — {lg.appui_document or '—'}\n")
            if lg.appui_passage:
                ins("end", lg.appui_passage.strip() + "\n", "gris")
            if lg.valeurs_absentes:
                ins("end", "Valeurs de l'exigence non citées : " + ", ".join(lg.valeurs_absentes) + "\n", "rouge")
        if lg.affaiblissements:
            ins("end", "Affaiblissements à proximité\n", "titre")
            ins("end", ", ".join(lg.affaiblissements) + "\n", "rouge")
        if lg.fiche or lg.fiche_erreur:
            ins("end", "Fiche de revue (rédigée par le modèle, verdict à vous)\n", "titre")
            ins("end", (lg.fiche or f"non rédigée : {lg.fiche_erreur}") + "\n")
        if lg.historique:
            ins("end", "Mémoire des projets\n", "titre")
            ins("end", lg.historique + "\n", "rouge" if lg.ecart_accepte_avant else "gris")
        if lg.alertes:
            ins("end", "Alertes\n", "titre")
            ins("end", "\n".join(LIBELLES_ALERTES.get(a, a) for a in lg.alertes) + "\n", "rouge")
        self.detail.configure(state="disabled")

    def quitter(self):
        self._enregistrer_reglages()
        try:
            logging.getLogger().removeHandler(self._journal_handler)
        except Exception:
            pass
        self.destroy()


# --- dialogues ------------------------------------------------------------------------------
class DialogueStatuts(tk.Toplevel):
    """Valeurs de statut inconnues → valeur pivot ; ajouts persistants."""

    def __init__(self, parent, dico, inconnus: Dict[str, int]):
        super().__init__(parent)
        self.title("Dictionnaire des statuts")
        self.transient(parent)
        self.dico = dico
        self.ajouts: Dict[str, str] = {}
        self.vars: Dict[str, tk.StringVar] = {}
        cadre = ttk.Frame(self, padding=10)
        cadre.pack(fill="both", expand=True)
        if inconnus:
            ttk.Label(cadre, text="Valeurs lues dans la NCM mais inconnues du dictionnaire. Indiquez à quoi elles correspondent :",
                      wraplength=520).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            for i, (valeur, n) in enumerate(sorted(inconnus.items(), key=lambda kv: -kv[1]), start=1):
                ttk.Label(cadre, text=f"« {valeur} »  (×{n})").grid(row=i, column=0, sticky="w", pady=2)
                v = tk.StringVar(value="ignorer")
                self.vars[valeur] = v
                ttk.Combobox(cadre, textvariable=v, state="readonly", width=26,
                             values=["ignorer", "C — conforme", "PC — partiellement conforme",
                                     "NC — non conforme", "NA — non applicable"]).grid(row=i, column=1, sticky="w", padx=8)
            rang = len(inconnus) + 1
        else:
            rang = 0
        ttk.Label(cadre, text="Ajouter une valeur :").grid(row=rang, column=0, sticky="w", pady=(10, 2))
        self.v_valeur = tk.StringVar()
        self.v_code = tk.StringVar(value="C — conforme")
        ttk.Entry(cadre, textvariable=self.v_valeur, width=28).grid(row=rang, column=1, sticky="w", padx=8, pady=(10, 2))
        ttk.Combobox(cadre, textvariable=self.v_code, state="readonly", width=26,
                     values=["C — conforme", "PC — partiellement conforme", "NC — non conforme", "NA — non applicable"]
                     ).grid(row=rang, column=2, sticky="w", pady=(10, 2))
        ttk.Label(cadre, text="Ajouts actuels : " + (", ".join(f"{k} → {v}" for k, v in sorted(dico.ajouts().items())) or "aucun"),
                  wraplength=520, foreground="#5a6a79").grid(row=rang + 1, column=0, columnspan=3, sticky="w", pady=(8, 8))
        boutons = ttk.Frame(cadre)
        boutons.grid(row=rang + 2, column=0, columnspan=3, sticky="e")
        ttk.Button(boutons, text="Valider", command=self.valider).pack(side="left", padx=4)
        ttk.Button(boutons, text="Fermer", command=self.destroy).pack(side="left")
        self.grab_set()

    def valider(self):
        for valeur, v in self.vars.items():
            code = v.get().split(" ")[0]
            if code != "ignorer":
                self.dico.ajouter(valeur, Statut.depuis_code(code))
                self.ajouts[valeur] = code
        val = self.v_valeur.get().strip()
        if val:
            code = self.v_code.get().split(" ")[0]
            self.dico.ajouter(val, Statut.depuis_code(code))
            self.ajouts[val] = code
        self.destroy()


class DialogueProfil(tk.Toplevel):
    """Rôle pivot → en-tête de colonne, proposé par la reconnaissance, confirmé une fois."""

    def __init__(self, parent, fournisseur: str, en_tetes: List[str], proposition: Dict[str, str], existant=None):
        super().__init__(parent)
        self.title(f"Profil des colonnes — {fournisseur}")
        self.transient(parent)
        from .profils import Profil, ROLES
        self.profil: Optional[Profil] = None
        self.fournisseur = fournisseur
        cadre = ttk.Frame(self, padding=10)
        cadre.pack(fill="both", expand=True)
        ttk.Label(cadre, text="Colonnes de la NCM pour chaque rôle (en-têtes du tableau). « — » = absent.",
                  wraplength=480).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        choix = ["—"] + [h for h in en_tetes if h]
        self.vars: Dict[str, tk.StringVar] = {}
        base = dict(existant.colonnes) if existant is not None else {}
        for i, role in enumerate(ROLES, start=1):
            ttk.Label(cadre, text=role).grid(row=i, column=0, sticky="w", pady=2)
            valeur = base.get(role) or proposition.get(role) or "—"
            if valeur not in choix:
                valeur = "—"
            v = tk.StringVar(value=valeur)
            self.vars[role] = v
            ttk.Combobox(cadre, textvariable=v, state="readonly", width=40, values=choix).grid(row=i, column=1, sticky="w", padx=8)
        boutons = ttk.Frame(cadre)
        boutons.grid(row=len(ROLES) + 1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(boutons, text="Enregistrer", command=self.valider).pack(side="left", padx=4)
        ttk.Button(boutons, text="Annuler", command=self.destroy).pack(side="left")
        self.grab_set()

    def valider(self):
        from .profils import Profil
        colonnes = {r: v.get() for r, v in self.vars.items() if v.get() != "—"}
        if "id" not in colonnes or "statut" not in colonnes:
            messagebox.showwarning("SILLAGE", "Les rôles « id » et « statut » sont obligatoires.", parent=self)
            return
        self.profil = Profil(fournisseur=self.fournisseur, colonnes=colonnes)
        self.destroy()


class DialogueTracer(tk.Toplevel):
    """Index de la norme, seuil de proposition, exigences déjà tracées à reprendre ou non."""

    def __init__(self, parent, index_defaut: str = ""):
        super().__init__(parent)
        self.title("Tracer les exigences vers la norme")
        self.transient(parent)
        self.resultat = None
        cadre = ttk.Frame(self, padding=10)
        cadre.pack(fill="both", expand=True)
        ttk.Label(cadre, text="La norme (CS-25, AMC…) doit d'abord être indexée : étape 3 « Indexer les documents » avec le "
                              "dossier de la norme et un index dédié. Pour chaque exigence, les paragraphes cités par ses "
                              "meilleurs passages sont proposés ; vous relisez et validez dans le classeur produit.",
                  wraplength=520).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Label(cadre, text="Index de la norme :").grid(row=1, column=0, sticky="w")
        self.v_index = tk.StringVar(value=index_defaut)
        ttk.Entry(cadre, textvariable=self.v_index, width=48).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(cadre, text="…", width=3, command=self._choisir).grid(row=1, column=2)
        ttk.Label(cadre, text="Seuil de proposition :").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.v_seuil = tk.StringVar(value="0.3")
        ttk.Entry(cadre, textvariable=self.v_seuil, width=6).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        self.v_tout = tk.BooleanVar(value=False)
        ttk.Checkbutton(cadre, text="Reprendre aussi les exigences qui ont déjà un paragraphe dans la spécification",
                        variable=self.v_tout).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        boutons = ttk.Frame(cadre)
        boutons.grid(row=4, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(boutons, text="Tracer", command=self.valider).pack(side="left", padx=4)
        ttk.Button(boutons, text="Annuler", command=self.destroy).pack(side="left")
        self.grab_set()

    def _choisir(self):
        p = filedialog.askdirectory(title="Index de la norme", parent=self)
        if p:
            self.v_index.set(p)

    def valider(self):
        try:
            seuil = float(self.v_seuil.get().replace(",", "."))
        except ValueError:
            messagebox.showwarning("SILLAGE", "Seuil invalide.", parent=self)
            return
        if not self.v_index.get().strip():
            messagebox.showwarning("SILLAGE", "Indiquez l'index de la norme.", parent=self)
            return
        self.resultat = (self.v_index.get().strip(), seuil, self.v_tout.get())
        self.destroy()


class FenetreDiff(tk.Toplevel):
    def __init__(self, parent, diff):
        super().__init__(parent)
        self.title(f"Diff {diff.avant} → {diff.apres}")
        self.geometry("900x520")
        self.diff = diff
        ttk.Label(self, text=diff.texte_resume(), padding=8).pack(anchor="w")
        cols = ("nature", "avant", "apres", "sens")
        tree = ttk.Treeview(self, columns=cols, show="tree headings")
        tree.heading("#0", text="Identifiant")
        tree.column("#0", width=120, stretch=False)
        for c, lib, w in (("nature", "Nature", 100), ("avant", "Avant", 280), ("apres", "Après", 280), ("sens", "Sens", 100)):
            tree.heading(c, text=lib)
            tree.column(c, width=w)
        tree.tag_configure("régression", background="#ffc7ce")
        tree.tag_configure("amélioration", background="#c6efce")
        for c in diff.changements:
            tree.insert("", "end", text=c.id, values=(c.nature, " ".join(c.avant.split())[:200], " ".join(c.apres.split())[:200], c.sens),
                        tags=[c.sens] if c.sens else [])
        sb = ttk.Scrollbar(self, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        sb.pack(side="left", fill="y", pady=(0, 8))
        ttk.Button(self, text="Exporter…", command=self.exporter).pack(side="bottom", anchor="e", padx=8, pady=8)

    def exporter(self):
        p = filedialog.asksaveasfilename(title="Exporter le diff", defaultextension=".xlsx", initialfile="diff.xlsx",
                                         filetypes=[("Classeur Excel", "*.xlsx")], parent=self)
        if p:
            from .grille import exporter_diff
            exporter_diff(self.diff, p)
            messagebox.showinfo("SILLAGE", f"Diff écrit :\n{p}", parent=self)


class FenetreMemoire(tk.Toplevel):
    """La mémoire des projets : campagnes, exigences contestées, écarts acceptés, normes, fournisseurs, recherche."""

    def __init__(self, parent, mem):
        super().__init__(parent)
        self.title("Mémoire des projets")
        self.geometry("1000x560")
        self.mem = mem
        ttk.Label(self, text=mem.texte_resume(), padding=8, wraplength=960).pack(anchor="w")
        self.onglets = ttk.Notebook(self)
        self.onglets.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._tableau("Campagnes", [("Projet", 160), ("Fournisseur", 160), ("Révision", 70), ("Date", 130), ("Signée", 60),
                                    ("Exigences", 80), ("Écarts", 60)],
                      [(c.projet, c.fournisseur, c.revision, c.date[:16], "oui" if c.signee else "non", c.exigences, c.ecarts)
                       for c in mem.campagnes()])
        self._tableau("Exigences contestées", [("Identifiant", 110), ("Contestations", 90), ("Campagnes", 80), ("Acceptations", 90),
                                               ("Norme", 100), ("Exigence", 400)],
                      [(d["exigence"], d["contestations"], d["campagnes"], d["acceptations"], d["norme"], d["texte"][:160])
                       for d in mem.exigences_contestees()])
        self._tableau("Écarts acceptés", [("Identifiant", 110), ("Norme", 100), ("Projet", 140), ("Fournisseur", 140), ("Révision", 60),
                                          ("Dérogation", 110), ("Justification", 360)],
                      [(d["exigence"], d["norme"], d["projet"], d["fournisseur"], d["revision"], d["derogation"], d["justification"][:160])
                       for d in mem.ecarts_acceptes()])
        self._tableau("Par norme", [("Paragraphe", 120), ("Exigences", 80), ("Écarts", 70), ("Acceptés", 70), ("Campagnes", 80)],
                      [(d["norme"], d["exigences"], d["ecarts"], d["acceptes"], d["campagnes"]) for d in mem.par_norme()])
        self._tableau("Par fournisseur", [("Fournisseur", 160), ("Campagnes", 80), ("Projets", 70), ("Exigences", 80), ("Conformité", 80),
                                          ("Écarts", 60), ("Ouverts", 60), ("C sans appui", 90)],
                      [(d["fournisseur"], d["campagnes"], d["projets"], d["exigences"], f"{d['taux_conformite']:.0%}", d["ecarts"],
                        d["ouverts"], d["c_sans_appui"]) for d in mem.par_fournisseur()])
        # recherche
        f = ttk.Frame(self.onglets)
        self.onglets.add(f, text="Recherche")
        haut = ttk.Frame(f)
        haut.pack(fill="x", pady=4)
        self.v_texte = tk.StringVar()
        ttk.Entry(haut, textvariable=self.v_texte, width=40).pack(side="left", padx=(4, 6))
        ttk.Button(haut, text="Chercher", command=self._chercher).pack(side="left")
        self.t_rech = self._arbre(f, [("Identifiant", 110), ("Statut", 60), ("Projet", 140), ("Fournisseur", 140), ("Justification", 480)])
        bas = ttk.Frame(self)
        bas.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bas, text="Exporter le rapport…", command=self._exporter).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._fermer)

    def _arbre(self, parent, colonnes):
        cols = [c[0] for c in colonnes]
        tree = ttk.Treeview(parent, columns=cols, show="headings")
        for nom, w in colonnes:
            tree.heading(nom, text=nom)
            tree.column(nom, width=w, stretch=(w >= 300))
        sb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tree

    def _tableau(self, titre, colonnes, lignes):
        f = ttk.Frame(self.onglets)
        self.onglets.add(f, text=titre)
        tree = self._arbre(f, colonnes)
        for l in lignes:
            tree.insert("", "end", values=l)
        return tree

    def _chercher(self):
        self.t_rech.delete(*self.t_rech.get_children())
        for d in self.mem.chercher(self.v_texte.get()):
            self.t_rech.insert("", "end", values=(d["exigence"], d["statut"], d["projet"], d["fournisseur"], d["justification"][:200]))

    def _exporter(self):
        p = filedialog.asksaveasfilename(title="Rapport de capitalisation", defaultextension=".xlsx", initialfile="capitalisation.xlsx",
                                         filetypes=[("Classeur Excel", "*.xlsx")], parent=self)
        if p:
            self.mem.exporter(p)
            messagebox.showinfo("SILLAGE", f"Rapport écrit :\n{p}", parent=self)

    def _fermer(self):
        try:
            self.mem.close()
        finally:
            self.destroy()


class FenetreTexte(tk.Toplevel):
    def __init__(self, parent, titre: str, texte: str):
        super().__init__(parent)
        self.title(titre)
        self.geometry("640x480")
        t = tk.Text(self, wrap="word", font=("Consolas", 9))
        t.insert("1.0", texte)
        t.configure(state="disabled")
        sb = ttk.Scrollbar(self, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")


def main() -> int:
    app = Application()
    app.mainloop()
    return 0
