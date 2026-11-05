"""Repérage et normalisation des identifiants d'exigences.

L'identifiant est l'invariant de SILLAGE : c'est nous qui l'imposons, le
fournisseur le reprend. Ce module le reconnaît malgré les variations d'écriture
(casse, séparateur, zéros de tête), déplie les plages (« REQ-100 à REQ-120 »)
et distingue les sous-identifiants (« REQ-100a », « REQ-100.2 »).

Forme normalisée : ``PREFIXE-NUMERO[suffixe]`` — préfixe en majuscules, un
seul tiret, numéro sans zéros de tête, suffixe en minuscules tel quel
(« a », « .2 »).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set, Tuple

# Préfixe : 2 à 12 caractères commençant par une lettre (REQ, SRD, EXI, SYS_REQ…),
# séparateur optionnel, numéro, suffixe optionnel (lettre minuscule ou .n).
_SEP = r"[-_ /.]?"
_SUFFIXE = r"(?:\.\d{1,3}|[a-z])?"
_GENERIQUE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_]{1,11}?)" + _SEP + r"(\d{1,7})(" + _SUFFIXE + r")(?![A-Za-z0-9])"
)

# Liaisons de plage : « à », « a », « to », « - », « – », « — », « … », « ... », « au », « through », « thru ».
_LIAISON = r"(?:\s*(?:à|a|au|to|through|thru|-|–|—|…|\.\.\.)\s*)"

PLAGE_MAX = 2000   # au-delà, une plage n'est pas dépliée (probable erreur de lecture)


def normaliser(prefixe: str, numero: str, suffixe: str = "") -> str:
    num = numero.lstrip("0") or "0"
    return f"{prefixe.upper()}-{num}{suffixe.lower()}"


@dataclass
class MotifIdentifiant:
    """Motif de reconnaissance.

    Sans préfixes connus, le motif générique s'applique (toute forme
    « LETTRES-chiffres »). Avec des préfixes (déduits du référentiel), seuls
    ces préfixes sont reconnus, ce qui évite de prendre « CS 25 » ou
    « ISO 9001 » pour des exigences.
    """
    prefixes: Set[str] = field(default_factory=set)   # en majuscules, sans séparateur

    def __post_init__(self):
        self.prefixes = {p.upper() for p in self.prefixes}
        if self.prefixes:
            alt = "|".join(re.escape(p) for p in sorted(self.prefixes, key=len, reverse=True))
            self._re = re.compile(
                r"(?<![A-Za-z0-9])(" + alt + r")" + _SEP + r"(\d{1,7})(" + _SUFFIXE + r")(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
        else:
            self._re = _GENERIQUE

    # --- reconnaissance ---------------------------------------------------
    def trouver(self, texte: str) -> List[Tuple[str, str, int, int]]:
        """Occurrences (id normalisé, texte brut, début, fin) dans l'ordre."""
        if not texte:
            return []
        out = []
        for m in self._re.finditer(texte):
            out.append((normaliser(m.group(1), m.group(2), m.group(3)), m.group(0), m.start(), m.end()))
        return out

    def extraire(self, texte: str, deplier_plages: bool = True) -> List[str]:
        """Identifiants normalisés cités dans `texte`, plages dépliées, sans doublon."""
        occ = self.trouver(texte)
        if not occ:
            return []
        ids: List[str] = []
        i = 0
        while i < len(occ):
            cur = occ[i]
            if deplier_plages and i + 1 < len(occ):
                nxt = occ[i + 1]
                entre = texte[cur[3]:nxt[2]]
                plage = self._deplier(cur[0], nxt[0], entre)
                if plage:
                    ids.extend(plage)
                    i += 2
                    continue
            ids.append(cur[0])
            i += 1
        # plage abrégée « REQ-100 à 120 »
        if deplier_plages and len(occ) == 1:
            ids = self._plage_abregee(texte, occ[0]) or ids
        vus, uniq = set(), []
        for x in ids:
            if x not in vus:
                vus.add(x)
                uniq.append(x)
        return uniq

    def premier(self, texte: str) -> Optional[str]:
        occ = self.trouver(texte)
        return occ[0][0] if occ else None

    def commence_par_id(self, texte: str, marge: int = 3) -> Optional[str]:
        """Identifiant si `texte` commence (à `marge` caractères près) par un identifiant."""
        occ = self.trouver(texte.lstrip(" \t-•*·"))
        if occ and occ[0][2] <= marge:
            return occ[0][0]
        return None

    # --- plages -------------------------------------------------------------
    @staticmethod
    def _decomposer(ident: str) -> Tuple[str, int, str]:
        m = re.match(r"([A-Z][A-Z0-9_]*)-(\d+)(.*)$", ident)
        return m.group(1), int(m.group(2)), m.group(3)

    def _deplier(self, a: str, b: str, entre: str) -> Optional[List[str]]:
        if not re.fullmatch(_LIAISON, entre):
            return None
        pa, na, sa = self._decomposer(a)
        pb, nb, sb = self._decomposer(b)
        if pa != pb or sa or sb or nb <= na or nb - na > PLAGE_MAX:
            return None
        return [f"{pa}-{n}" for n in range(na, nb + 1)]

    def _plage_abregee(self, texte: str, occ: Tuple[str, str, int, int]) -> Optional[List[str]]:
        reste = texte[occ[3]:]
        m = re.match(_LIAISON + r"(\d{1,7})(?![A-Za-z0-9])", reste)
        if not m:
            return None
        pa, na, sa = self._decomposer(occ[0])
        nb = int(m.group(1))
        if sa or nb <= na or nb - na > PLAGE_MAX:
            return None
        return [f"{pa}-{n}" for n in range(na, nb + 1)]


def deduire_motif(identifiants: Iterable[str]) -> MotifIdentifiant:
    """Motif restreint aux préfixes réellement présents dans un référentiel."""
    prefixes = set()
    for ident in identifiants:
        for norm, _brut, _d, _f in _GENERIQUE_MOTIF.trouver(ident):
            prefixes.add(norm.split("-", 1)[0])
    return MotifIdentifiant(prefixes)


_GENERIQUE_MOTIF = MotifIdentifiant()


def cle_tri(ident: str) -> Tuple[str, int, str]:
    """Clé de tri naturel : préfixe, numéro, suffixe."""
    m = re.match(r"([A-Z][A-Z0-9_]*)-(\d+)(.*)$", ident)
    if not m:
        return (ident, 0, "")
    return (m.group(1), int(m.group(2)), m.group(3))


def trier(ids: Sequence[str]) -> List[str]:
    return sorted(ids, key=cle_tri)
