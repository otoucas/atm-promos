# Serveur MCP — connexion IA par point de vente

Permet à **chaque point de vente** de connecter sa propre IA (Claude Desktop,
Claude Code, ou tout autre client MCP) pour lire les promotions déjà chargées
et en proposer de nouvelles — sans passer par l'IA de l'exploitant du service.
C'est une mise à disposition gracieuse : chaque point de vente utilise son
propre abonnement/jeton IA, pas le nôtre.

## Pourquoi un service séparé ?

Le paquet `mcp` (SDK officiel Model Context Protocol) impose Starlette ≥ 0.48,
incompatible avec la version de Starlette qu'exige `fastapi==0.115.0` (< 0.39)
utilisée par l'appli web principale (`app/`). Installer les deux dans le même
environnement casse l'appli web (`TypeError: Router.__init__() got an
unexpected keyword argument`).

Solution : le serveur MCP tourne dans son **propre conteneur Docker**, avec
son propre `requirements.txt` (voir `docker-compose.yml`, service `mcp`). Il
importe directement `app.models` / `app.database` / `app.config` (ces modules
n'ont aucune dépendance à FastAPI/Starlette), et ne partage avec l'appli web
que le fichier de base SQLite (même volume `./data`).

## Authentification

Jeton d'API dédié par point de vente (`Store.mcp_token`), généré/régénéré
depuis `/{code}/admin/mcp` — volontairement distinct du mot de passe web
humain, pour pouvoir le révoquer indépendamment. Le client MCP l'envoie en
en-tête HTTP :

```
Authorization: Bearer <jeton>
```

## Outils exposés

- **`list_promotions`** — renvoie les promotions actives et en attente du
  point de vente authentifié (pour éviter les doublons avant soumission).
- **`submit_promotion`** — propose une nouvelle promotion (marque, référence
  HighCo, libellé, dates, produits concernés). Selon le réglage
  "publication automatique" du point de vente (`/{code}/admin/mcp`), la
  promotion part directement active, ou en attente de validation manuelle.

Chaque appel (lecture ou soumission) est journalisé dans
`McpActivityLog`, consultable et supprimable par le point de vente lui-même
depuis ses réglages.

## Développement local

Environnement Python isolé (ne pas réutiliser le `.venv` de l'appli web) :

```bash
python3 -m venv .venv-mcp
source .venv-mcp/bin/activate
pip install --only-binary=:all: -r mcp_server/requirements.txt
python -m mcp_server.server
```

Variables d'environnement utiles (mêmes fichiers `.env` que l'appli web) :
`MCP_BIND_HOST` (défaut `0.0.0.0`), `MCP_PORT` (défaut `8000`).

## Déploiement

Géré par `docker-compose.yml` (service `mcp`, port exposé via
`MCP_HOST_PORT`). En prod, exposé publiquement derrière nginx sur un chemin
dédié (ex. `/nifty/mcp/`), avec les mêmes règles de sécurité que le reste du
site public (voir le README principal).
