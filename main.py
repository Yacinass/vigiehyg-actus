"""
VigieHyg - micro-service (hygiene hospitaliere & securite sanitaire), a heberger sur Render.

Endpoints consommes par l'app Android VigieHyg :
  - GET  /actualites  -> liste d'actualites editable dans actualites.json
  - POST /interpret   -> interpretation reglementaire + recommandations generees par IA (Claude)

L'endpoint /interpret necessite la variable d'environnement ANTHROPIC_API_KEY (a definir
dans les reglages Render du service). Sans cle, il renvoie 503 et l'app bascule automatiquement
sur son moteur deterministe hors-ligne.
"""
import json
import os
import httpx
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

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# Modele economique et rapide, adapte a la redaction reglementaire courte.
ANTHROPIC_MODEL = os.environ.get("VIGIEHYG_MODEL", "claude-haiku-4-5-20251001")


@app.get("/")
def root():
    return {"service": "vigiehyg-api", "endpoints": ["/actualites", "/interpret"]}


@app.get("/actualites")
def actualites():
    """Renvoie la liste d'actualites (editable dans actualites.json)."""
    try:
        with open(DATA, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


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


def _build_prompt(req: InterpretRequest) -> str:
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
        "Reponds STRICTEMENT par un objet JSON valide, sans texte autour : "
        '{"interpretation": "...", "recommandations": "..."}. '
        "Les sauts de ligne dans les valeurs doivent etre encodes \\n."
    )


@app.post("/interpret")
def interpret(req: InterpretRequest):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return _fail("ANTHROPIC_API_KEY non configuree sur le serveur")

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 900,
        "system": (
            "Tu es un expert marocain en hygiene hospitaliere, securite sanitaire des aliments (ONSSA) "
            "et sante environnementale. Tu rediges des interpretations reglementaires rigoureuses, "
            "sourcees sur les textes marocains, et des recommandations actionnables. "
            "Tu reponds toujours par un JSON valide."
        ),
        "messages": [{"role": "user", "content": _build_prompt(req)}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        with httpx.Client(timeout=45.0) as client:
            r = client.post(ANTHROPIC_URL, headers=headers, json=payload)
        if r.status_code != 200:
            return _fail(f"Erreur API ({r.status_code})")
        data = r.json()
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()
        parsed = _extract_json(text)
        if parsed and (parsed.get("interpretation") or parsed.get("recommandations")):
            return {
                "ok": True,
                "interpretation": (parsed.get("interpretation") or "").strip(),
                "recommandations": (parsed.get("recommandations") or "").strip(),
                "modele": ANTHROPIC_MODEL,
            }
        # Repli : renvoie le texte brut comme interpretation si le JSON n'a pu etre extrait.
        return {"ok": True, "interpretation": text, "recommandations": "", "modele": ANTHROPIC_MODEL}
    except Exception as e:
        return _fail(f"Exception: {type(e).__name__}")


def _fail(msg: str):
    return {"ok": False, "error": msg, "interpretation": "", "recommandations": ""}


def _extract_json(text: str):
    """Extrait le premier objet JSON du texte (le modele encadre parfois de texte)."""
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None
