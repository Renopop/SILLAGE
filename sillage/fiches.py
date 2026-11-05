"""Lot 5 — fiches de revue des écarts : la seule étape générative de SILLAGE.

Pour chaque exigence non conforme ou partiellement conforme (ou ceux de
`statuts`), le modèle de la passerelle reçoit **une exigence**, **sa ligne de
NCM** (statut, justification, action, échéance, dérogation), le **paragraphe
de norme** tracé s'il existe, et les **passages de la spécification
fournisseur** déjà trouvés par la vérification des « C » (lot 3) ou cherchés
à la demande. Il rédige une fiche en trois parties : l'écart en deux phrases,
le risque (technique, et certification si l'exigence est tracée), et les
questions à poser au fournisseur en revue. Il ne cherche jamais, ne décide
jamais : pas de verdict, c'est celui de l'ingénieur. Chaque affirmation cite
un passage [i] ou la ligne de NCM ; quand les passages ne disent rien, il le
dit.

Même client et même chaîne de robustesse que RIVAGE (`synthesis.Synthesizer`
du moteur embarqué : retries, repli, mode dégradé, troncature signalée).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .modele import Exigence, LigneNCM, NCM, Statut
from .referentiel import Referentiel
from .rivage import Passage

CONSIGNE = (
    "Tu es l'assistant d'un ingénieur chargé de la revue de conformité d'un fournisseur. "
    "On te donne UNE exigence du client, la ligne de la matrice de non-conformité (NCM) du fournisseur, "
    "le paragraphe de norme auquel l'exigence est tracée s'il existe, et des passages numérotés [1]…[n] "
    "de la spécification du fournisseur. Rédige, en français, une fiche de revue en trois parties, "
    "avec exactement ces titres :\n"
    "ÉCART : en deux phrases au plus, ce que le fournisseur ne fait pas ou fait autrement, et ce qu'il propose.\n"
    "RISQUE : conséquence technique pour le client ; si un paragraphe de norme est donné, conséquence pour la certification.\n"
    "QUESTIONS POUR LA REVUE : au plus trois questions précises à poser au fournisseur.\n"
    "Règles : n'affirme rien qui ne soit dans la ligne de NCM ou dans un passage ; cite le passage [i] ou écris « (NCM) » "
    "après chaque affirmation ; si les passages ne disent rien d'utile, écris-le au lieu d'inventer ; ne donne pas de "
    "verdict (accepter, refuser), c'est la décision de l'ingénieur ; pas de formule de politesse."
)


@dataclass
class Fiche:
    id: str
    statut: str
    texte: str = ""
    sources: List[dict] = field(default_factory=list)
    modele: str = ""
    degrade: bool = False
    tronquee: bool = False
    erreur: str = ""
    question: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "statut": self.statut, "texte": self.texte, "sources": self.sources, "modele": self.modele,
                "degrade": self.degrade, "tronquee": self.tronquee, "erreur": self.erreur, "question": self.question}

    @classmethod
    def from_dict(cls, d: dict) -> "Fiche":
        return cls(**{k: d.get(k, "" if k not in ("sources",) else []) for k in
                      ("id", "statut", "texte", "sources", "modele", "degrade", "tronquee", "erreur", "question")})


@dataclass
class Fiches:
    fournisseur: str = ""
    revision: str = ""
    statuts: List[str] = field(default_factory=lambda: ["NC", "PC"])
    fiches: Dict[str, Fiche] = field(default_factory=dict)

    def resume(self) -> Dict[str, int]:
        r = {"total": len(self.fiches), "redigees": 0, "degradees": 0, "tronquees": 0, "erreurs": 0}
        for f in self.fiches.values():
            if f.texte and not f.erreur:
                r["redigees"] += 1
            if f.degrade:
                r["degradees"] += 1
            if f.tronquee:
                r["tronquees"] += 1
            if f.erreur:
                r["erreurs"] += 1
        return r

    def texte_resume(self) -> str:
        r = self.resume()
        s = f"Fiches de revue : {r['total']} écart(s) (statuts {', '.join(self.statuts)}), rédigées : {r['redigees']}"
        if r["degradees"]:
            s += f", en mode dégradé : {r['degradees']}"
        if r["tronquees"]:
            s += f", tronquées : {r['tronquees']}"
        if r["erreurs"]:
            premiere = next(f.erreur for f in self.fiches.values() if f.erreur)
            s += f"\n⚠ {r['erreurs']} fiche(s) sans réponse du modèle : {premiere}"
        return s

    def to_dict(self) -> dict:
        return {"sillage": "fiches", "version": 1, "fournisseur": self.fournisseur, "revision": self.revision,
                "statuts": self.statuts, "fiches": [f.to_dict() for f in self.fiches.values()]}

    @classmethod
    def from_dict(cls, d: dict) -> "Fiches":
        if d.get("sillage") != "fiches":
            raise ValueError("ce fichier n'est pas un jeu de fiches de revue SILLAGE")
        out = cls(fournisseur=d.get("fournisseur", ""), revision=d.get("revision", ""), statuts=list(d.get("statuts", ["NC", "PC"])))
        for x in d.get("fiches", []):
            f = Fiche.from_dict(x)
            out.fiches[f.id] = f
        return out

    def enregistrer(self, chemin) -> None:
        Path(chemin).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def charger(cls, chemin) -> "Fiches":
        return cls.from_dict(json.loads(Path(chemin).read_text(encoding="utf-8")))


def _ligne_effective(lignes: Sequence[LigneNCM]) -> LigneNCM:
    return lignes[0]


def construire_contexte(e: Exigence, lignes: Sequence[LigneNCM], passages: Sequence[Passage]) -> str:
    """Le contenu transmis au modèle : exigence, ligne(s) de NCM, norme, passages numérotés."""
    bloc = [f"EXIGENCE {e.id_brut or e.id} :\n{(e.texte or '').strip() or '(texte non disponible)'}"]
    if e.norme:
        bloc.append(f"PARAGRAPHE DE NORME TRACÉ : {e.norme}")
    for k, l in enumerate(lignes, start=1):
        lignes_ncm = [f"LIGNE DE NCM{' ' + str(k) if len(lignes) > 1 else ''} : statut « {l.statut_brut or l.statut.value} » ({l.statut.libelle})"]
        if l.justification:
            lignes_ncm.append(f"Justification du fournisseur : {l.justification.strip()}")
        for cle, lib in (("action", "Action proposée"), ("echeance", "Échéance"), ("reference", "Référence"),
                         ("derogation", "Dérogation"), ("commentaire", "Commentaire")):
            if l.champs.get(cle):
                lignes_ncm.append(f"{lib} : {l.champs[cle].strip()}")
        bloc.append("\n".join(lignes_ncm))
    if passages:
        lignes_p = ["PASSAGES DE LA SPÉCIFICATION FOURNISSEUR :"]
        for i, p in enumerate(passages, start=1):
            page = f", page {p.page}" if p.page else ""
            lignes_p.append(f"[{i}] (source : {Path(p.chemin).name}{page})\n{' '.join((p.texte or '').split())}")
        bloc.append("\n\n".join(lignes_p))
    else:
        bloc.append("PASSAGES DE LA SPÉCIFICATION FOURNISSEUR : aucun passage disponible.")
    return "\n\n".join(bloc)


def _sources(passages: Sequence[Passage]) -> List[dict]:
    return [{"index": i, "chemin": p.chemin, "page": p.page, "texte": p.texte, "faiss_id": p.faiss_id}
            for i, p in enumerate(passages, start=1)]


def rediger(ref: Referentiel, ncm: NCM, synthesizer, campagne=None, moteur=None, statuts: Sequence[str] = ("NC", "PC"),
            n_passages: int = 3, prefixes: Optional[Sequence[str]] = None, fichiers: Optional[Sequence[str]] = None,
            max_tokens: int = 700, progression: Optional[Callable[[int, int, str], None]] = None, log=None) -> Fiches:
    """Une fiche par exigence dont le statut NCM est dans `statuts`. Les
    passages viennent de la campagne d'appuis (lot 3) si elle les a, sinon
    du moteur s'il est donné (une recherche par exigence), sinon aucun."""
    voulus = {Statut.depuis_code(s) for s in statuts}
    out = Fiches(fournisseur=ncm.fournisseur, revision=ncm.revision, statuts=[s.value for s in voulus if s is not Statut.INCONNU])
    par_id = ncm.par_id()
    exigences = ref.par_id()
    cibles = [i for i in ref.ids if i in par_id and any(l.statut in voulus for l in par_id[i])]
    for k, ident in enumerate(cibles, start=1):
        if progression:
            progression(k, len(cibles), ident)
        e = exigences[ident]
        lignes = [l for l in par_id[ident] if l.statut in voulus] or par_id[ident]
        passages: List[Passage] = []
        if campagne is not None and ident in campagne.appuis and campagne.appuis[ident].passages:
            passages = list(campagne.appuis[ident].passages[:n_passages])
        elif moteur is not None:
            try:
                q = " ".join((e.texte or "").split()) or e.id
                passages, _ = moteur.passages(q, path_prefixes=prefixes, allowed_paths=fichiers, rerank_k=max(1, n_passages))
                passages = passages[:n_passages]
            except Exception as exc:
                (log.warning if log else print)(f"[fiches] recherche impossible pour {ident} : {exc}")
        fiche = Fiche(id=ident, statut=lignes[0].statut.value, question=f"Fiche de revue de l'écart {e.id_brut or ident}")
        contenu = construire_contexte(e, lignes, passages)
        try:
            res = synthesizer.synthesize_raw(fiche.question, contenu, _sources(passages), log=log, temperature=0.1,
                                             max_tokens=max_tokens, system_prompt=CONSIGNE)
            fiche.texte = (res.answer or "").strip()
            fiche.sources = list(res.sources or _sources(passages))
            fiche.modele = res.model or ""
            fiche.degrade = bool(getattr(res, "fallback_used", False))
            fiche.tronquee = bool(getattr(res, "tronquee", False))
            fiche.erreur = res.error or ("" if fiche.texte else "réponse vide")
        except Exception as exc:
            fiche.erreur = f"{type(exc).__name__}: {exc}"
        out.fiches[ident] = fiche
    return out


def exporter_fiches(fiches: Fiches, ref: Referentiel, ncm: NCM, chemin) -> Path:
    """Dossier de revue en Word : une fiche par page (exigence, ligne de NCM,
    norme, fiche rédigée, passages cités)."""
    import docx
    from docx.shared import Pt
    from .version import VERSION_LABEL
    doc = docx.Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    titre = f"Fiches de revue — {fiches.fournisseur or 'fournisseur'}" + (f", NCM révision {fiches.revision}" if fiches.revision else "")
    doc.add_heading(titre, level=0)
    p = doc.add_paragraph(f"{fiches.texte_resume()}\nRéférentiel : {Path(ref.source).name} — NCM : {Path(ncm.fichier).name}. "
                          f"Produit par {VERSION_LABEL}. Les fiches sont rédigées par le modèle à partir des seuls éléments "
                          f"cités ; le verdict est celui de l'ingénieur.")
    p.runs[0].font.size = Pt(9)
    exigences = ref.par_id()
    par_id = ncm.par_id()
    premiere = True
    for ident in ref.ids:
        f = fiches.fiches.get(ident)
        if f is None:
            continue
        if not premiere:
            doc.add_page_break()
        premiere = False
        e = exigences[ident]
        doc.add_heading(f"{e.id_brut or ident} — {Statut.depuis_code(f.statut).libelle}", level=1)
        doc.add_heading("Exigence", level=2)
        doc.add_paragraph(e.texte or "(texte non disponible)")
        if e.norme:
            doc.add_paragraph(f"Paragraphe de norme tracé : {e.norme}")
        doc.add_heading("Ligne de NCM", level=2)
        for l in par_id.get(ident, [])[:3]:
            doc.add_paragraph(f"Statut « {l.statut_brut or l.statut.value} » — {l.justification or '(sans justification)'}")
            for cle, lib in (("action", "Action"), ("echeance", "Échéance"), ("derogation", "Dérogation"), ("reference", "Référence")):
                if l.champs.get(cle):
                    doc.add_paragraph(f"{lib} : {l.champs[cle]}")
            if l.origine:
                q = doc.add_paragraph(f"Origine : {l.origine}")
                q.runs[0].font.size = Pt(9)
        doc.add_heading("Fiche de revue", level=2)
        if f.erreur and not f.texte:
            doc.add_paragraph(f"Fiche non rédigée : {f.erreur}")
        else:
            for ligne in f.texte.splitlines():
                if ligne.strip():
                    para = doc.add_paragraph(ligne.strip())
                    if ligne.strip().rstrip(":").upper() in ("ÉCART", "ECART", "RISQUE", "QUESTIONS POUR LA REVUE"):
                        para.runs[0].bold = True
            avert = []
            if f.degrade:
                avert.append("rédigée en mode dégradé (modèle de repli)")
            if f.tronquee:
                avert.append("réponse tronquée par la longueur maximale")
            if avert:
                q = doc.add_paragraph("Attention : " + " ; ".join(avert) + ".")
                q.runs[0].italic = True
        if f.sources:
            doc.add_heading("Passages cités", level=2)
            for s in f.sources:
                page = f", page {s.get('page')}" if s.get("page") else ""
                q = doc.add_paragraph(f"[{s.get('index')}] {Path(s.get('chemin', '')).name}{page} : "
                                      f"{' '.join((s.get('texte') or '').split())[:700]}")
                q.runs[0].font.size = Pt(9)
    out = Path(chemin)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out


def ouvrir_synthesizer(log=None):
    """Le client de synthèse du moteur embarqué (passerelle, clés RIVAGE_SYNTH_KEY / RIVAGE_DALLEM_KEY)."""
    from .rivage import disponible, message_absent
    if not disponible():
        raise ImportError(message_absent())
    from synthesis import Synthesizer
    return Synthesizer()
