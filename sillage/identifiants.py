"""Repérage et normalisation des identifiants d'exigences.

L'identifiant est l'invariant de SILLAGE : c'est nous qui l'imposons, le
fournisseur le reprend. Ce module le reconnaît malgré les variations d'écriture
(casse, séparateurs, zéros de tête, crochets), déplie les plages
(« REQ-100 à REQ-120 ») et distingue les sous-identifiants (« REQ-100a »,
« REQ-100.2 »).

Forme d'un identifiant : ``PRÉFIXE sép NUMÉRO [suffixe]``. Le préfixe commence
par une lettre et peut être composé (« REQ », « SYS_REQ », « REQ_B2-3 » : le
code d'une annexe fait partie du préfixe) ; le numéro est le **dernier** groupe
de chiffres ; le séparateur est « - », « _ », un espace, un point ou rien.

Forme normalisée : ``PRÉFIXE-NUMÉRO[suffixe]`` — préfixe en majuscules tel
qu'écrit, numéro sans zéros de tête, suffixe en minuscules. Avec un motif déduit
d'un référentiel, la forme normalisée reprend l'écriture du référentiel
(« REQ_B2-3_15 » si la spécification écrit « [REQ_B2-3_15] ») et toutes les
variantes du fournisseur (« REQ-B2-3-015 », « req_b2_3_15 ») s'y ramènent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Préfixe : un premier segment de 2 à 12 caractères commençant par une lettre, puis
# jusqu'à quatre segments alphanumériques liés par « - » ou « _ » (REQ, SRD, SYS_REQ,
# REQ_B2-3…). Le numéro est le dernier groupe de chiffres : un segment « -3 » suivi
# de « _15 » reste dans le préfixe (lookahead final).
_SEGMENT = r"[A-Za-z0-9]{1,12}"
_PREFIXE = r"[A-Za-z][A-Za-z0-9]{1,11}(?:[-_]" + _SEGMENT + r"){0,4}?"
_SEP = r"([-_ /.]?)"
_SUFFIXE = r"(?:\.\d{1,3}|[a-z])?"
_FIN = r"(?![A-Za-z0-9])(?![-_][A-Za-z0-9])"
_GENERIQUE = re.compile(r"(?<![A-Za-z0-9])(" + _PREFIXE + r")" + _SEP + r"(\d{1,7})(" + _SUFFIXE + r")" + _FIN)

# Liaisons de plage : « à », « a », « to », « - », « – », « — », « … », « ... », « au », « through », « thru ».
_LIAISON = r"(?:\s*(?:à|a|au|to|through|thru|-|–|—|…|\.\.\.)\s*)"

PLAGE_MAX = 2000   # au-delà, une plage n'est pas dépliée (probable erreur de lecture)

_DECOMPOSE = re.compile(r"^(.*?)([-_])(\d+)((?:\.\d+|[a-z])?)$")

# Confusions de l'OCR : un caractère attendu et ce qu'un modèle peut lire à sa place.
_CONFUSIONS = {"0": "0OoQD", "1": "1Il|", "2": "2Z", "5": "5S", "6": "6G", "8": "8B",
               "O": "O0Q", "Q": "Q0O", "B": "B8", "S": "S5", "I": "I1l", "Z": "Z2", "G": "G6", "D": "D0"}
_CHIFFRES = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8"})


def _classe(c: str) -> str:
    """Classe regex d'un caractère de préfixe, confusions OCR comprises, casse libre."""
    variantes = set(_CONFUSIONS.get(c.upper(), c))
    variantes |= {c.upper(), c.lower()}
    return "[" + "".join(sorted(re.escape(v) for v in variantes)) + "]"


def _sep_canonique(sep: str) -> str:
    return "_" if sep == "_" else "-"


def normaliser(prefixe: str, numero: str, suffixe: str = "", sep: str = "-") -> str:
    num = numero.lstrip("0") or "0"
    return f"{prefixe.upper()}{_sep_canonique(sep)}{num}{suffixe.lower()}"


def _compact(prefixe: str) -> str:
    return re.sub(r"[-_ ]", "", prefixe).upper()


def decomposer(ident: str) -> Tuple[str, str, int, str]:
    """(préfixe, séparateur, numéro, suffixe) d'un identifiant normalisé."""
    m = _DECOMPOSE.match(ident)
    if not m:
        return ident, "-", 0, ""
    return m.group(1), m.group(2), int(m.group(3)), m.group(4)


@dataclass
class MotifIdentifiant:
    """Motif de reconnaissance.

    Sans préfixes connus, le motif générique s'applique (toute forme
    « LETTRES-chiffres », le séparateur normalisé en « - »). Avec des préfixes
    (déduits du référentiel), seuls ces préfixes sont reconnus — ce qui évite
    de prendre « CS 25 » ou « ISO 9001 » pour des exigences — et chaque
    occurrence est ramenée à l'écriture du référentiel : préfixe tel qu'il y
    est écrit, séparateur du référentiel (« REQ_B2-3_15 »). Les séparateurs
    intérieurs du préfixe sont tolérés à la lecture (« REQ-B2-3-15 »).

    `prefixes` : préfixes en majuscules ; `separateurs` : préfixe → « - » ou
    « _ » (défaut « - »).
    """
    prefixes: Set[str] = field(default_factory=set)
    separateurs: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.prefixes = {p.upper() for p in self.prefixes}
        self.separateurs = {p.upper(): _sep_canonique(s) for p, s in self.separateurs.items()}
        self._canon: Dict[str, Tuple[str, str]] = {}      # préfixe compact -> (préfixe écrit, séparateur)
        for p in self.prefixes:
            self._canon[_compact(p)] = (p, self.separateurs.get(p, "-"))
        if self.prefixes:
            alts = []
            for p in sorted(self.prefixes, key=len, reverse=True):
                segments = [s for s in re.split(r"[-_ ]", p) if s]
                alts.append(r"[-_ ]?".join(re.escape(s) for s in segments))
            self._re = re.compile(
                r"(?<![A-Za-z0-9])(" + "|".join(alts) + r")" + _SEP + r"(\d{1,7})(" + _SUFFIXE + r")(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
        else:
            self._re = _GENERIQUE

    def _normaliser(self, m: "re.Match") -> str:
        prefixe, sep = m.group(1), "-"
        if self.prefixes:
            prefixe, sep = self._canon[_compact(prefixe)]
        return normaliser(prefixe, m.group(3), m.group(4), sep)

    # --- lecture tolérante (texte OCR) -------------------------------------------
    def tolerant(self) -> "MotifIdentifiant":
        """Variante pour du texte OCR : chaque caractère des préfixes connus
        admet ses confusions usuelles (O/0/Q, B/8, I/1/l, S/5, Z/2, G/6), les
        séparateurs sont libres, et les chiffres du numéro peuvent être lus
        comme des lettres (O, I, l, S, B). Sans préfixes connus : inchangé."""
        if not self.prefixes:
            return self
        m = MotifIdentifiant(self.prefixes, self.separateurs)
        m._par_prefixe = []
        for p in sorted(self.prefixes, key=len, reverse=True):
            segments = [s for s in re.split(r"[-_ ]", p) if s]
            motif = r"[-_ .]?".join("".join(_classe(c) for c in s) for s in segments)
            m._par_prefixe.append((p, self.separateurs.get(p, "-"), re.compile(
                r"(?<![A-Za-z0-9])" + motif + r"[-_ .:]?\s?([0-9OoIlSB]{1,7})(" + _SUFFIXE + r")(?![A-Za-z0-9])")))
        return m

    def canoniser(self, ident: str) -> str:
        """Ramène un identifiant normalisé par le motif générique à l'écriture
        de ce motif (même préfixe compact) ; inchangé s'il n'en relève pas."""
        if not self.prefixes:
            return ident
        prefixe, _sep, num, suffixe = decomposer(ident)
        canon = self._canon.get(_compact(prefixe))
        if canon is None:
            return ident
        return normaliser(canon[0], str(num), suffixe, canon[1])

    # --- reconnaissance ---------------------------------------------------
    def trouver(self, texte: str) -> List[Tuple[str, str, int, int]]:
        """Occurrences (id normalisé, texte brut, début, fin) dans l'ordre."""
        if not texte:
            return []
        par_prefixe = getattr(self, "_par_prefixe", None)
        if par_prefixe:
            occ = []
            for prefixe, sep, rx in par_prefixe:
                for m in rx.finditer(texte):
                    numero = m.group(1).translate(_CHIFFRES)
                    if not numero.isdigit():
                        continue
                    occ.append((normaliser(prefixe, numero, m.group(2), sep), m.group(0), m.start(), m.end()))
            occ.sort(key=lambda o: o[2])
            # deux préfixes connus qui se recouvrent : garder la plus longue occurrence
            retenues: List[Tuple[str, str, int, int]] = []
            for o in occ:
                if retenues and o[2] < retenues[-1][3]:
                    if o[3] - o[2] > retenues[-1][3] - retenues[-1][2]:
                        retenues[-1] = o
                    continue
                retenues.append(o)
            return retenues
        return [(self._normaliser(m), m.group(0), m.start(), m.end()) for m in self._re.finditer(texte)]

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
        """Identifiant si `texte` commence (à `marge` caractères près, crochets,
        parenthèses et puces compris) par un identifiant."""
        occ = self.trouver(texte.lstrip(" \t-•*·[(<«\""))
        if occ and occ[0][2] <= marge:
            return occ[0][0]
        return None

    # --- plages -------------------------------------------------------------
    def _deplier(self, a: str, b: str, entre: str) -> Optional[List[str]]:
        if not re.fullmatch(_LIAISON, entre):
            return None
        pa, sepa, na, sa = decomposer(a)
        pb, _sepb, nb, sb = decomposer(b)
        if pa != pb or sa or sb or nb <= na or nb - na > PLAGE_MAX:
            return None
        return [f"{pa}{sepa}{n}" for n in range(na, nb + 1)]

    def _plage_abregee(self, texte: str, occ: Tuple[str, str, int, int]) -> Optional[List[str]]:
        reste = texte[occ[3]:]
        m = re.match(_LIAISON + r"(\d{1,7})(?![A-Za-z0-9])", reste)
        if not m:
            return None
        pa, sepa, na, sa = decomposer(occ[0])
        nb = int(m.group(1))
        if sa or nb <= na or nb - na > PLAGE_MAX:
            return None
        return [f"{pa}{sepa}{n}" for n in range(na, nb + 1)]


def deduire_motif(identifiants: Iterable[str]) -> MotifIdentifiant:
    """Motif restreint aux préfixes réellement présents dans un référentiel.

    Accepte des identifiants normalisés ou bruts (« [REQ_B2-3_15] ») : le
    préfixe est pris tel qu'écrit, et le séparateur écrit entre préfixe et
    numéro devient celui de la forme normalisée (« _ » ou « - »)."""
    prefixes: Dict[str, str] = {}
    for ident in identifiants:
        for m in _GENERIQUE.finditer(ident or ""):
            p = m.group(1).upper()
            prefixes.setdefault(p, _sep_canonique(m.group(2)))
    return MotifIdentifiant(set(prefixes), prefixes)


_GENERIQUE_MOTIF = MotifIdentifiant()


def cle_tri(ident: str) -> Tuple[str, int, str]:
    """Clé de tri naturel : préfixe, numéro, suffixe."""
    p, _s, n, suf = decomposer(ident)
    return (p, n, suf)


def trier(ids: Sequence[str]) -> List[str]:
    return sorted(ids, key=cle_tri)
