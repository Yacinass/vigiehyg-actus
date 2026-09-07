"""
VigieHyg - micro-service d'actualites (hygiene hospitaliere), a heberger sur Render.
Sert la liste d'actualites editable dans actualites.json.

Endpoint consomme par l'app Android VigieHyg : GET /actualites
Format renvoye : [ { "titre": "...", "corps": "...", "source": "..." }, ... ]
"""
import json
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="VigieHyg Actualites")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

DATA = os.path.join(os.path.dirname(__file__), "actualites.json")


@app.get("/")
def root():
    return {"service": "vigiehyg-actus", "endpoint": "/actualites"}


@app.get("/actualites")
def actualites():
    """Renvoie la liste d'actualites (editable dans actualites.json)."""
    try:
        with open(DATA, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []
