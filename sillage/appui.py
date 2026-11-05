"""Lot 3 — vérification des « conformes » : un statut « C » est une affirmation ;
SILLAGE cherche, dans la spécification fournisseur indexée par RIVAGE, le
passage qui l'étaye.

Pour chaque exigence retenue (par défaut : statut C ; PC en option), une
question RIVAGE = le texte de l'exigence, posée dans la portée des documents
du fournisseur (préfixes de chemins ou liste de fichiers). Le score de
reranking du meilleur passage classe l'appui :

- **étayé** : score ≥ seuil haut ;
- **faible** : score entre les deux seuils ;
- **absent** : rien de comparable, ou score sous le seuil bas ;
- **non vérifié** : pas de reranking disponible (ordre RRF seul, pas de
  score comparable d'une exigence à l'autre).

Les seuils dépendent du reranker servi par la passerelle (probabilités ou
logits) : ils sont à **calibrer** sur un échantillon d'exigences dont on
connaît la réponse. `Campagne.repartition()` donne la distribution des
scores pour choisir.

Contrôle déterministe en plus : les **valeurs chiffrées** de l'exigence
(« 2 kg », « -40 °C », « 28 V ») sont-elles citées dans le passage d'appui ?
Une exigence chiffrée dont les nombres n'apparaissent pas dans le passage
retenu est signalée (« 2 kg » exigés, « 2,5 kg » annoncés : à lire).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .modele import Exigence, NCM, Statut
from .referentiel import Referentiel
from .rivage import Moteur, Passage

ETAYE = "étayé"
FAIBLE = "faible"
ABSENT = "absent"
NON_VERIFIE = "non vérifié"

SEUIL_ETAYE = 0.5     # à calibrer selon la passerelle (probabilités : 0,5 / 0,25 sont un point de départ)
SEUIL_FAIBLE = 0.25

_NOMBRE = re.compile(r"(?<![A-Za-z0-9])(?<![A-Za-z][-_/.])([-−+]?\d+(?:[.,]\d+)?)\s*(°C|°F|kg|g|mg|t|V|mV|kV|A|mA|W|kW|mW|Hz|kHz|MHz|GHz|"
                     r"ms|µs|s|min|h|m|mm|cm|km|MPa|kPa|Pa|bar|mbar|%|dB|N|kN|daN|Nm|l|L|ml|lb|ft|in|°)?(?![A-Za-z0-9])")


def valeurs(texte: str) -> List[str]:
    """Nombres (avec unité si présente) cités dans un texte, normalisés (« 2 kg »,
    « -40 °C », « 0.5 »). Ignorés : les numéros de section en tête de ligne
    (« 2. Alimentation », « 1.2 Titre ») et les nombres d'un identifiant (« T-12 »)."""
    out = []
    texte = texte or ""
    for m in _NOMBRE.finditer(texte):
        if not m.group(2):
            debut_ligne = m.start() == 0 or texte[m.start() - 1] in "\n\r"
            suite = texte[m.end():m.end() + 2]
            if debut_ligne and (suite[:1] in (".", ")", "") or suite in (". ", ") ") or (suite[:1] == " " and "." in m.group(1))):
                continue
        n = m.group(1).replace("−", "-").replace(",", ".").lstrip("+")
        if n.endswith(".0"):
            n = n[:-2]
        u = m.group(2) or ""
        v = f"{n} {u}".strip()
        if v not in out:
            out.append(v)
    return out


def _nombres(texte: str) -> set:
    return {v.split(" ")[0] for v in valeurs(texte)}


@dataclass
class Appui:
    id: str
    niveau: str = NON_VERIFIE
    score: Optional[float] = None
    passages: List[Passage] = field(default_factory=list)
    question: str = ""
    reranked: bool = False
    valeurs_exigence: List[str] = field(default_factory=list)
    valeurs_absentes: List[str] = field(default_factory=list)
    erreur: str = ""

    @property
    def meilleur(self) -> Optional[Passage]:
        return self.passages[0] if self.passages else None

    def to_dict(self) -> dict:
        return {"id": self.id, "niveau": self.niveau, "score": self.score, "question": self.question,
                "reranked": self.reranked, "valeurs_exigence": self.valeurs_exigence,
                "valeurs_absentes": self.valeurs_absentes, "erreur": self.erreur,
                "passages": [p.to_dict() for p in self.passages]}

    @classmethod
    def from_dict(cls, d: dict) -> "Appui":
        return cls(id=d["id"], niveau=d.get("niveau", NON_VERIFIE), score=d.get("score"),
                   passages=[Passage.from_dict(x) for x in d.get("passages", [])], question=d.get("question", ""),
                   reranked=d.get("reranked", False), valeurs_exigence=list(d.get("valeurs_exigence", [])),
                   valeurs_absentes=list(d.get("valeurs_absentes", [])), erreur=d.get("erreur", ""))


@dataclass
class Campagne:
    index_dir: str = ""
    prefixes: List[str] = field(default_factory=list)
    fichiers: List[str] = field(default_factory=list)
    statuts: List[str] = field(default_factory=lambda: ["C"])
    seuil_etaye: float = SEUIL_ETAYE
    seuil_faible: float = SEUIL_FAIBLE
    n_passages: int = 3
    appuis: Dict[str, Appui] = field(default_factory=dict)
    nb_fichiers_portee: int = 0

    def resume(self) -> Dict[str, int]:
        r = {ETAYE: 0, FAIBLE: 0, ABSENT: 0, NON_VERIFIE: 0, "valeurs_absentes": 0, "erreurs": 0}
        for a in self.appuis.values():
            r[a.niveau] += 1
            if a.valeurs_absentes:
                r["valeurs_absentes"] += 1
            if a.erreur:
                r["erreurs"] += 1
        r["total"] = len(self.appuis)
        return r

    def repartition(self) -> Dict[str, float]:
        """Quartiles des meilleurs scores, pour calibrer les seuils."""
        s = sorted(a.score for a in self.appuis.values() if a.score is not None)
        if not s:
            return {}
        def q(p: float) -> float:
            return s[min(len(s) - 1, int(p * (len(s) - 1)))]
        return {"min": s[0], "q1": q(0.25), "mediane": q(0.5), "q3": q(0.75), "max": s[-1], "n": len(s)}

    def texte_resume(self) -> str:
        r = self.resume()
        lignes = [f"Exigences vérifiées : {r['total']} (statuts {', '.join(self.statuts)}) sur {self.nb_fichiers_portee} fichier(s) de la portée",
                  f"Étayées : {r[ETAYE]}, faibles : {r[FAIBLE]}, absentes : {r[ABSENT]}, non vérifiées : {r[NON_VERIFIE]}",
                  f"Exigences chiffrées dont aucune valeur n'est citée : {r['valeurs_absentes']}"]
        rep = self.repartition()
        if rep:
            lignes.append("Scores (min / q1 / médiane / q3 / max) : " +
                          " / ".join(f"{rep[k]:.2f}" for k in ("min", "q1", "mediane", "q3", "max")) +
                          f" — seuils : étayé ≥ {self.seuil_etaye}, faible ≥ {self.seuil_faible}")
        if r["erreurs"]:
            lignes.append(f"⚠ erreurs de recherche : {r['erreurs']}")
        return "\n".join(lignes)

    def to_dict(self) -> dict:
        return {"sillage": "appuis", "version": 1, "index_dir": self.index_dir, "prefixes": self.prefixes,
                "fichiers": self.fichiers, "statuts": self.statuts, "seuil_etaye": self.seuil_etaye,
                "seuil_faible": self.seuil_faible, "n_passages": self.n_passages,
                "nb_fichiers_portee": self.nb_fichiers_portee,
                "appuis": [a.to_dict() for a in self.appuis.values()]}

    @classmethod
    def from_dict(cls, d: dict) -> "Campagne":
        if d.get("sillage") != "appuis":
            raise ValueError("ce fichier n'est pas une campagne d'appuis SILLAGE")
        c = cls(index_dir=d.get("index_dir", ""), prefixes=list(d.get("prefixes", [])), fichiers=list(d.get("fichiers", [])),
                statuts=list(d.get("statuts", ["C"])), seuil_etaye=d.get("seuil_etaye", SEUIL_ETAYE),
                seuil_faible=d.get("seuil_faible", SEUIL_FAIBLE), n_passages=d.get("n_passages", 3),
                nb_fichiers_portee=d.get("nb_fichiers_portee", 0))
        for x in d.get("appuis", []):
            a = Appui.from_dict(x)
            c.appuis[a.id] = a
        return c

    def enregistrer(self, chemin) -> None:
        Path(chemin).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def charger(cls, chemin) -> "Campagne":
        return cls.from_dict(json.loads(Path(chemin).read_text(encoding="utf-8")))


def question_pour(e: Exigence, avec_identifiant: bool = False) -> str:
    q = " ".join((e.texte or "").split())
    if not q:
        q = e.id_brut or e.id
    elif avec_identifiant and e.id_brut:
        q = f"{e.id_brut} {q}"
    return q


def classer(score: Optional[float], reranked: bool, seuil_etaye: float, seuil_faible: float, n_passages: int) -> str:
    if n_passages == 0:
        return ABSENT
    if not reranked or score is None:
        return NON_VERIFIE
    if score >= seuil_etaye:
        return ETAYE
    if score >= seuil_faible:
        return FAIBLE
    return ABSENT


def verifier(ref: Referentiel, ncm: NCM, moteur: Moteur, prefixes: Optional[Sequence[str]] = None,
             fichiers: Optional[Sequence[str]] = None, statuts: Sequence[str] = ("C",),
             seuil_etaye: float = SEUIL_ETAYE, seuil_faible: float = SEUIL_FAIBLE, n_passages: int = 3,
             retrieve_k: Optional[int] = None, avec_identifiant: bool = False,
             progression: Optional[Callable[[int, int, str], None]] = None) -> Campagne:
    """Campagne de vérification : une question RIVAGE par exigence dont le statut
    NCM est dans `statuts`, restreinte à la portée (`prefixes` de chemins et/ou
    `fichiers` indexés). Un embedding, une recherche hybride et un reranking
    par exigence ; jamais de synthèse."""
    voulus = {Statut.depuis_code(s) for s in statuts}
    c = Campagne(index_dir=moteur.index_dir, prefixes=list(prefixes or []), fichiers=list(fichiers or []),
                 statuts=[s.value for s in voulus if s is not Statut.INCONNU], seuil_etaye=seuil_etaye,
                 seuil_faible=seuil_faible, n_passages=n_passages)
    try:
        c.nb_fichiers_portee = len(fichiers) if fichiers else len(moteur.fichiers(prefixes))
    except Exception:  # pragma: no cover - registre indisponible
        c.nb_fichiers_portee = 0
    par_id = ncm.par_id()
    exigences = ref.par_id()
    cibles = [i for i in ref.ids if i in par_id and any(l.statut in voulus for l in par_id[i])]
    for k, ident in enumerate(cibles, start=1):
        e = exigences[ident]
        q = question_pour(e, avec_identifiant)
        a = Appui(id=ident, question=q, valeurs_exigence=valeurs(e.texte))
        if progression:
            progression(k, len(cibles), ident)
        try:
            passages, reranked = moteur.passages(q, path_prefixes=prefixes, allowed_paths=fichiers,
                                                 retrieve_k=retrieve_k, rerank_k=max(1, n_passages))
        except Exception as exc:
            a.erreur = f"{type(exc).__name__}: {exc}"
            passages, reranked = [], False
        a.passages = passages[:n_passages]
        a.reranked = reranked
        a.score = a.passages[0].score if a.passages and reranked else None
        a.niveau = classer(a.score, reranked, seuil_etaye, seuil_faible, len(a.passages))
        if a.valeurs_exigence and a.passages:
            # comparées au seul passage d'appui (le meilleur) : c'est lui qui est cité dans la grille
            cites = _nombres(a.passages[0].texte)
            a.valeurs_absentes = [v for v in a.valeurs_exigence if v.split(" ")[0] not in cites]
        elif a.valeurs_exigence:
            a.valeurs_absentes = list(a.valeurs_exigence)
        c.appuis[ident] = a
    return c
