# synthesis.py
"""
Synthèse finale - seul appel génératif de la requête (§7)

L'IA générative n'intervient, côté requête, qu'ici, sur un contexte déjà propre
et minimal (8-12 chunks). La tâche est la combinatoire : assembler des
informations éparses en une réponse cohérente, et citer chaque source (le chemin
de fichier de chaque chunk devient la référence documentaire).

Portée par mistral-small-4 via l'API interne (§7.1, §12). On ne transmet que les
8-12 extraits sélectionnés, jamais le corpus.

Consigne de prompt (§7.1) : ne répondre qu'à partir des extraits fournis,
signaler quand l'information est absente, rattacher chaque affirmation à sa
source — pour éviter l'invention et garantir la traçabilité.
"""

import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

# Retries INTERACTIFS sur congestion passerelle (504/502/503/429) : un
# utilisateur attend sa réponse — peu de tentatives, délais courts (2 s puis
# 6 s ±30 %, plafond 15 s), configurables : RIVAGE_SYNTH_RETRIES /
# RIVAGE_SYNTH_RETRY_BASE_S. Les timeouts ne sont PAS re-tentés (120 s déjà
# attendues) ; les erreurs de connexion pure le sont.
#
# REPLI « dallem » : si RIVAGE_DALLEM_KEY est renseignée, un échec du modèle
# principal déclenche une seconde tentative complète sur le modèle « dallem »
# (RIVAGE_DALLEM_MODEL), avec SA propre clé — une clé par modèle, comme
# partout ailleurs. Seulement sur les échecs que le repli peut réparer
# (audit V2, D-8) : 5xx épuisés, erreur réseau, timeout, réponse vide. Un 4xx
# (401 clé invalide, 400/413/422 contexte trop long ou requête refusée) est
# renvoyé tel quel : le second appel serait voué au même refus, et masquait
# la vraie cause.
_RETRIABLE_STATUS = (502, 503, 504, 429)

# Classification d'un échec de `_complete` (SynthesisResult.echec) :
#   "http"    : réponse 4xx/5xx non re-tentable (code dans `http_status`) ;
#   "sature"  : 5xx/429 répétés (retries épuisés) ;
#   "reseau"  : erreur de transport, timeout ;
#   "vide"    : réponse 200 sans contenu.
_ECHECS_AVEC_REPLI = ("sature", "reseau", "vide")
_RETRY_CAP_S = 15.0
_CONGESTION_FACTOR = 3.0


def _synth_retries() -> int:
    try:
        v = os.environ.get("RIVAGE_SYNTH_RETRIES")
        return max(1, int(v)) if v is not None and v.strip() else 3
    except (TypeError, ValueError):
        return 3


def _synth_retry_delay(attempt: int) -> float:
    try:
        v = os.environ.get("RIVAGE_SYNTH_RETRY_BASE_S")
        base = float(v) if v is not None and v.strip() else 2.0
    except (TypeError, ValueError):
        base = 2.0
    return min(_RETRY_CAP_S,
               base * _CONGESTION_FACTOR ** (attempt - 1)) * random.uniform(0.7, 1.3)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Tu es un assistant de recherche documentaire technique.
Tu réponds à une question en t'appuyant EXCLUSIVEMENT sur les extraits fournis.

Règles impératives :
1. Ne réponds qu'à partir des extraits numérotés fournis. N'invente rien.
2. Si l'information demandée est absente des extraits, dis-le explicitement.
3. Rattache chaque affirmation à sa source en citant le numéro d'extrait
   correspondant, sous la forme [1], [2], etc.
4. Regroupe les informations éparses en une réponse unique et cohérente.
5. Réponds dans la langue de la question (français ou anglais).
6. Sois précis et factuel : valeurs, références, codes doivent être exacts."""


@dataclass
class SynthesisResult:
    answer: str
    sources: List[Dict[str, Any]] = field(default_factory=list)
    model: str = ""
    error: Optional[str] = None
    # MODE DÉGRADÉ : la réponse vient du modèle de repli (« dallem ») parce
    # que le modèle principal a échoué — fallback_reason porte la raison.
    # À AFFICHER à l'utilisateur (bandeau GUI, CLI).
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    # Réponse TRONQUÉE par le plafond `max_tokens` (finish_reason=length,
    # audit V2, D-3) : rendue quand même (ce n'est pas une erreur), mais des
    # éléments sourcés manquent probablement — à AFFICHER.
    tronquee: bool = False
    # Nature de l'échec (voir _ECHECS_AVEC_REPLI) et code HTTP le cas échéant :
    # le repli « dallem » ne se déclenche que sur un échec réparable.
    echec: Optional[str] = None
    http_status: Optional[int] = None


def build_context(chunks) -> (str):
    """
    Construit le bloc d'extraits numérotés passé au modèle.
    Chaque extrait porte son numéro et sa source (chemin + page).
    """
    lines = []
    for i, c in enumerate(chunks, 1):
        page = f", page {c.page}" if getattr(c, "page", None) else ""
        lines.append(f"[{i}] (source : {c.chemin}{page})\n{c.texte}")
    return "\n\n".join(lines)


def build_sources(chunks) -> List[Dict[str, Any]]:
    """
    Liste des sources citables — UNE entrée par extrait, alignée sur la
    numérotation `[1..N]` de `build_context` (M3). Ne pas dédoublonner par
    chemin : sinon une citation `[2]` légitime pourrait ne pas figurer dans la
    liste. Un même document peut donc apparaître à plusieurs numéros (extraits
    de pages différentes), ce qui est exact.
    """
    sources = []
    for i, c in enumerate(chunks, 1):
        sources.append({
            "index": i,
            "chemin": c.chemin,
            "famille": getattr(c, "famille", None),
            "page": getattr(c, "page", None),
            # Texte brut du passage (sans préfixe de contexte) : sert à
            # l'aperçu dans l'interface et à la recherche Ctrl+F dans le fichier.
            "texte": getattr(c, "texte", "") or "",
            # Identifiant stable de l'extrait dans l'index : clé de
            # dédoublonnage du panier de sources (panier.py).
            "faiss_id": getattr(c, "faiss_id", None),
        })
    return sources


class Synthesizer:
    """Client de synthèse via l'API interne mistral-small-4 (§7.1)."""

    # RÉENTRANT (audit V2, D-10) : `synthesize` / `synthesize_raw` /
    # `_complete` ne touchent aucun état de l'instance pendant un appel (tout
    # est local), et httpx.Client est thread-safe. La synthèse étagée du
    # panier peut donc appeler la même instance depuis plusieurs threads. Un
    # synthétiseur de remplacement qui ne le garantit pas laisse ce drapeau
    # à False (ou absent) : il sera appelé séquentiellement.
    reentrant = True

    def __init__(self, api_base=None, api_key=None, model=None, http_client=None,
                 fallback_model=None, fallback_key=None):
        try:
            from models_utils import (
                SYNTH_API_BASE, SYNTH_MODEL, SYNTH_API_KEY,
                SYNTH_FALLBACK_MODEL, SYNTH_FALLBACK_KEY, create_http_client,
            )
        except ImportError:
            SYNTH_API_BASE = SYNTH_MODEL = SYNTH_API_KEY = None
            SYNTH_FALLBACK_MODEL = SYNTH_FALLBACK_KEY = None
            create_http_client = None
        self.api_base = api_base or SYNTH_API_BASE
        # Clé DÉDIÉE à mistral-small (RIVAGE_SYNTH_KEY) — une clé par modèle.
        self.api_key = api_key or SYNTH_API_KEY
        self.model = model or SYNTH_MODEL
        # Repli « dallem » : modèle distinct portant SA propre clé
        # (RIVAGE_DALLEM_KEY) — inactif tant que la clé n'est pas renseignée.
        self.fallback_model = fallback_model or SYNTH_FALLBACK_MODEL
        self.fallback_key = fallback_key or SYNTH_FALLBACK_KEY
        self._own = http_client is None
        if http_client is None and create_http_client is not None:
            http_client = create_http_client()
        self.http = http_client

    def available(self) -> bool:
        return bool(self.http and self.api_base
                    and (self.api_key or self.fallback_key))

    def synthesize(
        self,
        question: str,
        chunks,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        log=None,
    ) -> SynthesisResult:
        """
        Produit la réponse sourcée à partir des extraits (§7).

        Args:
            question: question de l'utilisateur
            chunks: extraits sélectionnés (8-12), objets portant .texte/.chemin/.page
            temperature, max_tokens: paramètres de génération
            log: logger optionnel
        """
        _log = log or logger
        sources = build_sources(chunks)

        if not chunks:
            return SynthesisResult(
                answer="Aucun extrait pertinent n'a été trouvé pour cette question.",
                sources=[], model=self.model,
            )

        if not self.available():
            return SynthesisResult(
                answer="", sources=sources, model=self.model,
                error="API de synthèse indisponible (clé RIVAGE_SYNTH_KEY / "
                      "endpoint non configuré — voir RIVAGE_keys.env).",
            )

        context = build_context(chunks)
        user_content = (
            f"Question : {question}\n\n"
            f"Extraits disponibles :\n\n{context}\n\n"
            f"Rédige une réponse sourcée en citant les numéros d'extraits."
        )
        return self.synthesize_raw(question, user_content, sources, log=_log,
                                   temperature=temperature, max_tokens=max_tokens)

    def synthesize_raw(
        self,
        question: str,
        user_content: str,
        sources: List[Dict[str, Any]],
        log=None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        system_prompt: Optional[str] = None,
    ) -> SynthesisResult:
        """Appel de synthèse sur un contexte DÉJÀ construit (synthèse étagée du
        panier : lots à numérotation globale, puis assemblage — panier.py).
        Même chaîne que `synthesize` : retries congestion, repli « dallem »,
        mode dégradé. `system_prompt` remplace la consigne standard."""
        _log = log or logger
        if not self.available():
            return SynthesisResult(
                answer="", sources=sources, model=self.model,
                error="API de synthèse indisponible (clé RIVAGE_SYNTH_KEY / "
                      "endpoint non configuré — voir RIVAGE_keys.env).",
            )

        url = self.api_base.rstrip("/") + "/chat/completions"

        # Modèles tentés dans l'ordre, chacun avec SA clé : mistral-small,
        # puis — si RIVAGE_DALLEM_KEY est renseignée — le repli « dallem ».
        # Seul un échec RÉPARABLE du principal (5xx épuisés, réseau, timeout,
        # réponse vide) déclenche le repli (audit V2, D-8) : un 4xx est
        # rendu immédiatement, sans second appel.
        candidates = []
        if self.api_key:
            candidates.append((self.model, self.api_key))
        if self.fallback_key and self.fallback_model:
            candidates.append((self.fallback_model, self.fallback_key))

        result = None
        prev_error = None
        replis_tentes = 0
        for pos, (model, key) in enumerate(candidates):
            result = self._complete(url, model, key, user_content,
                                    temperature, max_tokens, sources, _log,
                                    system_prompt=system_prompt)
            if result.error is None:
                if pos > 0:
                    # MODE DÉGRADÉ : le principal a échoué, le repli a répondu.
                    result.fallback_used = True
                    result.fallback_reason = prev_error
                    _log.warning(f"[synthesis] MODE DÉGRADÉ — réponse rendue "
                                 f"par le modèle de repli {model} "
                                 f"(principal : {prev_error})")
                return result
            prev_error = result.error
            if pos + 1 < len(candidates):
                if result.echec not in _ECHECS_AVEC_REPLI:
                    _log.error(f"[synthesis] Échec de {model} ({result.error}) "
                               f"— refus définitif (HTTP {result.http_status}), "
                               f"pas de repli sur {candidates[pos + 1][0]}")
                    return result
                replis_tentes += 1
                _log.warning(f"[synthesis] Échec de {model} ({result.error}) "
                             f"— repli sur {candidates[pos + 1][0]}")
        if result is not None and replis_tentes:
            result.error = (f"Échec du modèle principal puis du repli "
                            f"{candidates[-1][0]} : {result.error}")
        return result

    def _complete(self, url, model, api_key, user_content,
                  temperature, max_tokens, sources, _log,
                  system_prompt=None) -> SynthesisResult:
        """Un appel de synthèse complet (avec retries congestion) pour UN
        modèle et SA clé. Retourne toujours un SynthesisResult (jamais
        d'exception) — error=None en cas de succès."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        retries = _synth_retries()
        for attempt in range(1, retries + 1):
            try:
                resp = self.http.post(url, headers=headers, json=payload,
                                      timeout=120.0)
                # Congestion passerelle : re-tenter brièvement plutôt que de
                # faire échouer la question (le 504 est transitoire par nature).
                if resp.status_code in _RETRIABLE_STATUS:
                    if attempt < retries:
                        delay = _synth_retry_delay(attempt)
                        _log.warning(
                            f"[synthesis] {model}: HTTP {resp.status_code} "
                            f"— retry {attempt}/{retries} dans {delay:.1f}s")
                        time.sleep(delay)
                        continue
                    _log.error(f"[synthesis] {model}: HTTP "
                               f"{resp.status_code} après {retries} "
                               f"tentatives — abandon")
                    return SynthesisResult(
                        answer="", sources=sources, model=model,
                        error=f"Passerelle saturée (HTTP {resp.status_code} "
                              f"répété) — réessayez dans un instant.",
                        echec="sature", http_status=resp.status_code,
                    )
                status = int(getattr(resp, "status_code", 200) or 200)
                if status >= 400:
                    # Code HTTP exposé (audit V2, D-8) : l'appelant décide du
                    # repli selon la classe (4xx = refus définitif, 5xx isolé
                    # = passerelle en difficulté). Lu AVANT raise_for_status,
                    # dont l'exception ne porte pas toujours le code.
                    detail = ""
                    try:
                        detail = (resp.text or "")[:200].strip()
                    except Exception:
                        pass
                    _log.error(f"[synthesis] {model}: HTTP {status} {detail}")
                    return SynthesisResult(
                        answer="", sources=sources, model=model,
                        error=f"HTTP {status}" + (f" : {detail}" if detail else ""),
                        echec="sature" if status >= 500 else "http",
                        http_status=status,
                    )
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                fr = choice.get("finish_reason")
                # `content` peut être null (finish_reason=length, message avec
                # seulement des tool_calls, etc.) : ne pas planter sur None (m2).
                content = (choice["message"].get("content") or "").strip()
                if not content:
                    return SynthesisResult(
                        answer="", sources=sources, model=model,
                        error=f"Réponse vide du modèle (finish_reason={fr}).",
                        echec="vide",
                    )
                # Réponse coupée par max_tokens (audit V2, D-3) : rendue, mais
                # signalée — jusqu'ici elle passait pour complète.
                tronquee = (str(fr or "").lower() == "length")
                if tronquee:
                    _log.warning(f"[synthesis] {model}: réponse TRONQUÉE "
                                 f"(finish_reason=length, max_tokens="
                                 f"{max_tokens})")
                return SynthesisResult(answer=content, sources=sources,
                                       model=model, tronquee=tronquee)
            except Exception as e:
                # Connexion pure (passerelle qui refuse un instant) : re-tenter ;
                # timeout : NE PAS re-tenter (120 s déjà attendues — l'utilisateur
                # reposera sa question) ; le reste : échec propre immédiat.
                msg = str(e).lower()
                if (attempt < retries and "connect" in msg
                        and "timeout" not in msg):
                    delay = _synth_retry_delay(attempt)
                    _log.warning(f"[synthesis] {type(e).__name__} — retry "
                                 f"{attempt}/{retries} dans {delay:.1f}s")
                    time.sleep(delay)
                    continue
                _log.error(f"[synthesis] Échec de la synthèse ({model}): {e}")
                # Un client qui lève sur statut HTTP (HTTPStatusError) porte
                # sa réponse : le code est récupéré pour classer l'échec.
                http_status = None
                try:
                    http_status = int(getattr(getattr(e, "response", None),
                                              "status_code", None))
                except (TypeError, ValueError):
                    http_status = None
                if http_status is not None and 400 <= http_status < 500:
                    echec = "http"
                elif http_status is not None and http_status >= 500:
                    echec = "sature"
                else:
                    echec = "reseau"
                return SynthesisResult(answer="", sources=sources,
                                       model=model, error=str(e),
                                       echec=echec, http_status=http_status)
        return SynthesisResult(answer="", sources=sources, model=model,
                               error="Passerelle saturée (réponses 5xx "
                                     "répétées) — réessayez dans un instant.",
                               echec="sature")

    def close(self):
        if self._own and self.http:
            try:
                self.http.close()
            except Exception:
                pass
