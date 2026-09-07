# VigieHyg - service d'actualites (Render)

Micro-service qui fournit les actualites du volet **Actualite -> Actualiser en ligne** de l'app VigieHyg.

L'app appelle : `GET https://vigiehyg-actus.onrender.com/actualites`

> Le service Render **doit se nommer `vigiehyg-actus`** (pour coller a l'URL codee dans l'app).

## Modifier les actualites
Editer `actualites.json` (liste d'objets `{ "titre", "corps", "source" }`) puis `git push` -> Render redeploie.

## Note
Plan gratuit Render : le service s'endort apres inactivite, le 1er appel peut prendre ~30 s.
Le contenu de reference (hors-ligne) reste toujours visible dans l'app.
