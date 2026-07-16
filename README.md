# Codes promo pharmacie — HighCo Nifty

Page web à destination des opérateurs de comptoir en pharmacie : affiche les
marques dont une promotion HighCo Nifty est en cours, et génère en un clic le
code à saisir en caisse (au lieu de faire scanner un QR au patient, qui dépend
du réseau mobile / wifi peu fiable en magasin).

## Fonctionnement

1. Les mails HighCo (reçus sur une boîte Gmail) contiennent un QR code (PDF en
   pièce jointe ou image dans le corps du mail). Ce QR pointe vers une requête
   HighCo qui génère un code neuf à chaque appel.
2. L'application relève périodiquement la boîte Gmail, décode le QR, et crée
   une promotion en **attente de validation**.
3. Un admin (pharmacien) valide/corrige chaque promotion en attente (marque,
   dates de validité, logo) depuis `/admin/pending` avant qu'elle apparaisse
   côté opérateur. Les promotions peuvent aussi être ajoutées **manuellement**
   depuis `/admin/promotions/new` (upload d'une image de QR, ou lien collé
   directement) — dans ce cas elles sont actives immédiatement puisque l'admin
   les a déjà relues en les saisissant.
4. Côté comptoir (`/`, accès libre sur le réseau local, pas de login), une
   grille affiche une tuile par opération active. **Un clic sur une tuile**
   appelle HighCo et affiche le code avec un bouton copier.
5. Les promotions expirées sont **archivées automatiquement** (comparaison
   quotidienne avec la date de fin de validité).

## Multi-magasins et exposition publique

Depuis juillet 2026, un même déploiement peut servir **plusieurs points de
vente**, chacun avec sa propre grille et son propre espace d'administration,
sous `/{code}` (code à 3 lettres, ex. `/ATM` pour la pharmacie Artemare) :

- **Pharmacie Artemare** (`ART` → renommé `ATM`) reste sur l'intégration
  ERPNext existante (relevé Gmail, synchro `atm_nifty`). Sa grille opérateur
  est publique (`/nifty/`), ses réglages restent protégés par mot de passe.
- **Les autres points de vente** sont en « format dépannage » : pas de
  relevé Gmail ni d'ERPNext, ils saisissent leurs propres promotions
  manuellement (ou via MCP, voir plus bas) après création d'un compte
  (email + mot de passe, avec vérification par lien et mot de passe oublié).
- **`/hello`** — formulaire public d'auto-inscription d'un nouveau point de
  vente (code à 3 lettres, email de contact). Un email vérifié par point de
  vente ne peut créer qu'un seul code ; toute tentative de doublon envoie une
  alerte au(x) superadmin(s).
- **`/`** (racine publique) liste les points de vente actifs sous forme de
  tuiles, par ordre alphabétique.
- Un compte **superadmin** supervise l'ensemble des points de vente
  (`/superadmin`).

Exposition publique (ex. `https://atm.hellopharmacie.com/nifty/`) : nginx
fait proxy vers l'appli (liée à une IP Tailscale, jamais `0.0.0.0` côté
Internet) en réécrivant le préfixe `/nifty`, et bloque au niveau nginx
**et** au niveau applicatif (en-tête interne `X-Nifty-Public-Gateway`) tout
accès aux routes `/admin`, `/superadmin` depuis la passerelle publique — donc
même une erreur de config nginx ne suffit pas à exposer l'administration.

## Connexion IA par point de vente (MCP)

Chaque point de vente en « format dépannage » peut connecter **sa propre IA**
(Claude Desktop, Claude Code, ou tout client MCP) pour lire ses promotions
en cours et en proposer de nouvelles, à partir de ses propres mails/dossiers —
avec son propre abonnement, pas celui de l'exploitant du service. Jeton
d'API dédié, généré dans `/{code}/admin/mcp`, avec choix entre publication
automatique ou file d'attente à valider manuellement, et un journal
d'activité (lectures/soumissions) consultable et supprimable par le point de
vente. Détails techniques et raison de l'isolation en service séparé :
[`mcp_server/README.md`](mcp_server/README.md).

## Mécanisme HighCo (vérifié le 2026-07-02)

Le lien encodé dans le QR HighCo Nifty pointe vers une plateforme de
distribution de pass Apple Wallet (PassKit). `app/highco.py` reproduit le
parcours réel en 2 requêtes HTTP (pas besoin de navigateur ni de JS) :

1. `GET` la référence avec un User-Agent mobile → page d'atterrissage HTML
   contenant les paramètres du bouton "Ajouter au Wallet" en clair, plus un
   cookie de session.
2. `POST` ces paramètres vers `/pass/apple/generate` (même session) →
   réponse `application/vnd.apple.pkpass` (un zip), dont
   `barcodes[0].message` (`app/pkpass_utils.py`) est le code à saisir en
   caisse (format Code128, ~13 caractères alphanumériques).

Vérifié en conditions réelles contre une promotion HighCo en cours : deux
appels distincts ont chacun renvoyé un code valide. Si HighCo fait évoluer
sa plateforme de distribution, ce flux pourra nécessiter un ajustement —
en cas d'échec inattendu, inspecter `HighCoResponseError.raw_excerpt` pour
voir ce qui a réellement été reçu.

## Lancer en local (Docker — recommandé)

```bash
cp .env.example .env
# éditer .env : ADMIN_PASSWORD, SECRET_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD
docker compose up --build
```

L'app est disponible sur `http://localhost:8000` (grille opérateur) et
`http://localhost:8000/admin/login` (administration).

Les données (base SQLite + logos uploadés) sont persistées dans `./data`.

## Lancer en local (sans Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # puis éditer
uvicorn app.main:app --reload
```

Nécessite la librairie système `libzbar0` pour le décodage QR
(`apt-get install libzbar0` sur Debian/Ubuntu).

## Tests

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python3 -m pytest
```

Couvre l'extraction de dates, les règles de conflit entre promotions, la
logique de dédoublonnage/fusion des mails, et l'heuristique de choix d'image
produit dans les PDF. Ne touche à aucun vrai mail Gmail ni à un vrai lien
HighCo (aucun appel réseau, aucun code réel généré).

## Configuration Gmail

1. Activer la validation en 2 étapes sur le compte Google utilisé.
2. Créer un mot de passe d'application : https://myaccount.google.com/apppasswords
3. Renseigner `GMAIL_ADDRESS` et `GMAIL_APP_PASSWORD` dans `.env` (pas le mot
   de passe habituel du compte).

Le relevé automatique peut être désactivé (`DISABLE_GMAIL_POLLER=1`) pour
n'utiliser que la saisie manuelle.

## Rappel mensuel par e-mail

Si `MONTHLY_PREVIEW_RECIPIENT` est renseigné dans `.env`, un e-mail récapitulatif
est envoyé chaque mois (jour/heure configurables via `MONTHLY_PREVIEW_DAY` /
`MONTHLY_PREVIEW_HOUR`) pour préparer le mois suivant : promotions en attente
de validation, campagnes qui démarrent ou se terminent (avec le rappel du
commentaire produit Winpharma à poser/enlever), et conflits actifs non
résolus. Envoyé via la même boîte Gmail que le relevé — aucun identifiant
supplémentaire nécessaire.

## Déploiement pour une autre pharmacie

Ce dépôt est conçu pour être réutilisable : chaque pharmacie déploie sa propre
instance (Docker Compose ci-dessus) avec sa propre boîte Gmail et son propre
mot de passe admin, sur un serveur local à son comptoir. Aucun identifiant
n'est codé en dur — tout passe par `.env` (non versionné, voir `.gitignore`).

## Déploiement sur un hôte déjà accessible depuis Internet

La grille opérateur n'a pas de login : elle part du principe que seul le
réseau du comptoir peut l'atteindre. Si l'hôte qui fait tourner Docker a
lui-même une IP publique (serveur distant, VPS...), **ne pas exposer le
port par défaut** — n'importe qui pourrait alors générer de vrais codes
HighCo. Utiliser `BIND_ADDRESS` dans `.env` pour ne lier le conteneur qu'à
une interface privée (par exemple une IP Tailscale), afin que seuls les
appareils du réseau privé/VPN de la pharmacie puissent y accéder :

```bash
# .env
BIND_ADDRESS=100.x.x.x   # IP Tailscale (ou équivalent VPN) de l'hôte
HOST_PORT=8010
```

Les postes de comptoir doivent alors rejoindre ce même réseau privé
(Tailscale ou équivalent) pour accéder à `http://<BIND_ADDRESS>:<HOST_PORT>/`.

## Prochaine étape (phase 2, hors périmètre de cette version)

Exploiter l'historique des promotions (dates de validité, marques) pour
alimenter automatiquement les fiches produits du LGO et retirer les mentions
de promotion une fois l'offre terminée. Le modèle `Promotion` de cette V1
(marque, dates, statut) sert de base pour cette phase future.

## Module Affiches (remplace PNR, en évaluation)

Ce dépôt héberge aussi un second module, sans rapport avec les codes HighCo :
la génération d'affiches promotionnelles prêtes à imprimer (`app/affiches.py`,
modèles `AfficheProduit`/`AfficheSelection`), destiné à remplacer la partie
"création visuelle" de l'outil PNR utilisé par le groupement. Import du
tableau de suivi des promotions existant, 5 gabarits (WeasyPrint), aperçu
instantané et publication côté siège (`/superadmin/affiches`), portail de
sélection + téléchargement PDF côté pharmacie (`/{code}/admin/affiches`).

Volontairement fermé aux vrais points de vente pendant la validation : seul
le magasin `config.DEFAULT_STORE_CODE` voit le lien et peut accéder aux
routes portail. Détail de la conception : page BookStack "Hello Affiches —
Conception & état du projet" (book ATM).
