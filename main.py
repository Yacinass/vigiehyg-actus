"""
VigieHyg - micro-service (hygiene hospitaliere & securite sanitaire), a heberger sur Render.

Endpoints consommes par l'app Android VigieHyg :
  - GET  /actualites  -> liste d'actualites editable dans actualites.json
  - POST /interpret   -> interpretation reglementaire + recommandations generees par IA

L'IA fonctionne EXACTEMENT comme le backend VectoMaroc : le serveur garde la cle et essaie
plusieurs fournisseurs, dans l'ordre OpenAI -> Anthropic -> Gemini (palier GRATUIT). L'app
n'a aucune cle a saisir. Definir AU MOINS UNE de ces variables d'environnement sur Render :
  - OPENAI_API_KEY       (prioritaire ; modele texte, defaut gpt-4.1-mini)
  - ANTHROPIC_API_KEY    (defaut claude-haiku-4-5-20251001)
  - GEMINI_API_KEY  ou  GOOGLE_API_KEY   (GRATUIT ; modeles Flash)
Sans aucune cle, /interpret renvoie ok=false et l'app bascule sur son moteur deterministe hors-ligne.
Aucune dependance externe pour les appels : urllib (bibliotheque standard).
"""
import json
import os
import urllib.error
import urllib.request

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(title="VigieHyg API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

DATA = os.path.join(os.path.dirname(__file__), "actualites.json")

_OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
_DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]


@app.get("/")
def root():
    return {"service": "vigiehyg-api", "endpoints": ["/actualites", "/interpret"], "ia": ia_enabled()}


@app.get("/actualites")
def actualites():
    try:
        with open(DATA, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


@app.get("/health")
def health():
    return {"ok": True, "ia": ia_enabled(), "providers": _providers_available()}


# ---- /interpret ----------------------------------------------------------

class SectionScore(BaseModel):
    titre: str
    score: float


class InterpretRequest(BaseModel):
    grille: str
    lieu: Optional[str] = ""
    typeInspection: Optional[str] = ""
    domaine: Optional[str] = "HOSPITAL"   # HOSPITAL | FOOD | ENV
    score: float
    decision: Optional[str] = ""
    sections: List[SectionScore] = []


DOMAIN_FRAMEWORK = {
    "HOSPITAL": ("hygiene hospitaliere / infections associees aux soins (IAS)",
                 "reglement interieur hospitalier (arrete MS 456-11), programme IAS/CLIN, "
                 "precautions standard et hygiene des mains (OMS), gestion des dechets de soins "
                 "(loi 28-00 et decret 2-09-139), sterilisation (EN 285)"),
    "FOOD": ("securite sanitaire des aliments",
             "loi 28-07 sur la securite sanitaire, controle ONSSA, bonnes pratiques d'hygiene, "
             "plan HACCP / Codex Alimentarius, ISO 22000, chaine du froid"),
    "ENV": ("sante environnementale et salubrite",
            "loi 12-03 (etudes d'impact), loi 28-00 (dechets), loi 13-03 (air), "
            "reglement general d'hygiene de l'habitat"),
}

_SYSTEME = (
    "Tu es un expert marocain en hygiene hospitaliere, securite sanitaire des aliments (ONSSA) "
    "et sante environnementale. Tu rediges des interpretations reglementaires rigoureuses, "
    "sourcees sur les textes marocains, et des recommandations actionnables et priorisees. "
    "Tu reponds toujours UNIQUEMENT par un objet JSON valide, sans texte autour ni Markdown."
)


def _prompt(req: InterpretRequest) -> str:
    dom = DOMAIN_FRAMEWORK.get(req.domaine or "HOSPITAL", DOMAIN_FRAMEWORK["HOSPITAL"])
    lignes = "\n".join(f"  - {s.titre} : {round(s.score)}%" for s in req.sections) or "  (non detaille)"
    return (
        f"Audit realise avec la grille : {req.grille}\n"
        f"Domaine : {dom[0]}\n"
        f"Lieu inspecte : {req.lieu or 'non precise'}\n"
        f"Type d'inspection : {req.typeInspection or 'non precise'}\n"
        f"Score global : {round(req.score)}%  -  Decision : {req.decision or 'non precisee'}\n"
        f"Scores par section :\n{lignes}\n\n"
        f"Cadre reglementaire de reference (Maroc) : {dom[1]}.\n\n"
        "Redige, en francais professionnel et SANS markdown :\n"
        "1) interpretation : 4 a 6 phrases d'interpretation reglementaire du niveau de conformite, "
        "citant les textes/normes marocains reellement pertinents pour CE lieu et CETTE grille "
        "(sois specifique, pas generique) ; mentionne l'autorite competente a informer si pertinent.\n"
        "2) recommandations : 4 a 7 actions correctives concretes, priorisees (URGENT / a court terme), "
        "ciblant en priorite les sections les plus faibles, avec un delai indicatif.\n\n"
        "Reponds STRICTEMENT par un objet JSON valide : "
        '{"interpretation": "...", "recommandations": "..."}. '
        "Les sauts de ligne dans les valeurs doivent etre encodes \\n."
    )


def _gemini_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()


def _providers_available() -> list:
    out = []
    if os.getenv("OPENAI_API_KEY", "").strip():
        out.append("openai")
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        out.append("anthropic")
    if _gemini_key():
        out.append("gemini")
    return out


def ia_enabled() -> bool:
    return bool(_providers_available())


@app.post("/interpret")
def interpret(req: InterpretRequest):
    providers = []
    if os.getenv("OPENAI_API_KEY", "").strip():
        providers.append(_call_openai)
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        providers.append(_call_anthropic)
    if _gemini_key():
        providers.append(_call_gemini)
    if not providers:
        return _fail("Aucune cle IA configuree (OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY)")

    prompt = _prompt(req)
    last = ""
    for provider in providers:
        try:
            text, who = provider(prompt)
            parsed = _extract_json(text)
            if parsed and (parsed.get("interpretation") or parsed.get("recommandations")):
                return {
                    "ok": True,
                    "interpretation": _as_text(parsed.get("interpretation")),
                    "recommandations": _as_text(parsed.get("recommandations")),
                    "provider": who,
                }
            last = f"{who}: reponse non exploitable"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:200]}"
            continue
    return _fail(last or "echec IA")


def _post(url: str, body: dict, headers: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={**headers, "content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_openai(prompt: str):
    key = os.getenv("OPENAI_API_KEY", "").strip()
    mdl = os.getenv("VIGIEHYG_MODEL", _DEFAULT_OPENAI_MODEL).strip()
    body = {
        "model": mdl,
        "instructions": _SYSTEME,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "max_output_tokens": 1000,
    }
    payload = _post(_OPENAI_ENDPOINT, body, {"authorization": f"Bearer {key}"})
    text = payload.get("output_text") or "".join(
        c.get("text", "")
        for item in payload.get("output", []) or []
        for c in item.get("content", []) or []
        if c.get("type") in ("output_text", "text")
    )
    return text, "openai"


def _call_anthropic(prompt: str):
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    mdl = os.getenv("VIGIEHYG_MODEL", _DEFAULT_ANTHROPIC_MODEL).strip()
    # Si VIGIEHYG_MODEL vise OpenAI (gpt-*), ne pas l'imposer a Anthropic.
    if mdl.startswith("gpt"):
        mdl = _DEFAULT_ANTHROPIC_MODEL
    body = {
        "model": mdl,
        "max_tokens": 1000,
        "system": _SYSTEME,
        "messages": [{"role": "user", "content": prompt}],
    }
    payload = _post(_ANTHROPIC_ENDPOINT, body,
                    {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION})
    text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
    return text, "anthropic"


def _call_gemini(prompt: str):
    key = _gemini_key()
    last_err = None
    for mdl in _GEMINI_MODELS:
        gen = {"maxOutputTokens": 1200, "temperature": 0.3, "responseMimeType": "application/json"}
        if any(t in mdl for t in ("2.5", "flash-latest", "3-", "3.")):
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        body = {
            "system_instruction": {"parts": [{"text": _SYSTEME}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
        }
        try:
            payload = _post(_GEMINI_ENDPOINT.format(model=mdl), body, {"x-goog-api-key": key})
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in (404, 429, 500, 503):
                continue
            raise
        text = "".join(
            p.get("text", "")
            for cand in payload.get("candidates", []) or []
            for p in (cand.get("content", {}) or {}).get("parts", []) or []
            if "text" in p
        )
        if text.strip():
            return text, f"gemini:{mdl}"
    if last_err:
        raise last_err
    raise RuntimeError("gemini: aucun modele disponible")


def _as_text(v) -> str:
    """Le modele peut renvoyer une chaine OU une liste (de phrases/points) : on normalise en texte."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        parts = []
        for item in v:
            if isinstance(item, dict):
                # ex. {"action": "...", "priorite": "...", "delai": "..."}
                vals = [str(x).strip() for x in item.values() if str(x).strip()]
                parts.append(" — ".join(vals) if vals else "")
            else:
                parts.append(str(item).strip())
        return "\n".join(f"- {p}" for p in parts if p)
    if isinstance(v, dict):
        return "\n".join(f"- {k} : {str(val).strip()}" for k, val in v.items() if str(val).strip())
    return str(v).strip()


def _fail(msg: str):
    return {"ok": False, "error": msg, "interpretation": "", "recommandations": ""}


def _extract_json(text: str):
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None
