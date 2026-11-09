# OpenSim solo Hypergrid in Docker / OpenSim Hypergrid solo dans Docker

[English](#english) | [Français](#français)

## English

OpenSim Hypergrid, compiled from the upstream Git repository. The initial configuration creates one region;
you can add more regions from the web console. It connects to the same grid as the Windows version:
`http://grid.example.com:8002/`.

### Deploy

1. Copy this directory to your server (for example, `/volume1/docker/opensim-solo/`).
   Copy `.env.example` to `.env`, then replace the example values with your own settings.
   In particular, set your public host, administrator details, and strong unique passwords.
   Never publish or share `.env`: it contains passwords and other private settings.
2. On your router, forward the `opensim 8002` rule (TCP and UDP) to the machine running this container.
   Only one machine should receive this forwarding rule at a time.
   **Never forward port 8090.**
3. Docker uses host networking, so the container uses the host's ports directly: 8002 (TCP and UDP) and 8090.
   Make sure these ports are available. In the DSM firewall, allow TCP/UDP 8002 and allow TCP 8090 only from your LAN.
   With host networking, the host firewall controls access.
4. Build and start with `docker compose up -d --build`, or deploy the project in Container Manager.
   The first build can take several minutes.
5. Open the web console at `http://<LAN-IP>:8090/` and sign in as `WEB_USER`.
   For remote access, use the VPN (the NAS OpenVPN server). Container Manager's **Log** tab also shows the logs.
6. To stop the grid, use the **Stop grid** button or stop the container. It sends `shutdown` and allows 60 seconds for a clean shutdown.

The `.env.example` file documents the available settings and contains only example values.
`WEB_PASS` must be at least 8 characters to enable the web console. Optional settings include
viewer access, TOTP, alerts, and scheduled backups.
The image build defaults to the `master` branch; set `OPENSIM_REF` in `.env` to select a Git branch or tag.

### Data and updates

- Persistent data is stored in `./data/`: databases in `data/db`, region configuration in `data/Regions`,
  and logs in `data/logs`.
- To start over, stop the container, remove `./data`, and start it again. **This permanently deletes grid data.**
- To update, run `docker compose build --no-cache` and then `docker compose up -d`.
  Data in `./data` is retained.

### Web console v2 (`http://<LAN-IP>:8090/`)

Sign-in uses a web form. Sessions last 12 hours; access is temporarily blocked after five failed attempts.
The interface supports French, English, German, and Italian, and remembers the selected language in the browser.

Tabs include the dashboard (process, memory, disk, grid, and region status), a console with filtering,
history and command shortcuts, user management, backups, Hypergrid links, and the action log.

- **Backups:** SQLite databases are backed up online using the SQLite backup API, and the configured region files are copied.
  An OAR backup contains one archive for every configured region; loading its OARs restores each region. Backups are stored in `data/backups/`. Database restoration is
  scheduled and applied at the next startup; the previous state is kept in `data/backups/avant-restauration-…`.
  Set `BACKUP_EVERY_HOURS` to enable automatic database backups.
- **Read-only account:** configure `WEB_VIEWER_USER` and `WEB_VIEWER_PASS`. This account can view status,
  but cannot see email addresses or perform actions.
- **Two-factor authentication:** generate a secret with
  `python3 -c "import base64,os;print(base64.b32encode(os.urandom(10)).decode())"`,
  set `WEB_TOTP_SECRET`, and add it to an authenticator app (TOTP, SHA1, 6 digits).
- **Import / Export:** the dedicated tab supports OAR region export, upload from your computer,
  import options (merge or omit assets), and download. IAR avatar inventory import/export requires the avatar's
  password; it is never saved or written to the action log. Files are stored in `data/transfer/oar` and `data/transfer/iar`.
- **Regions:** the region tab includes a map of configured locations. Click an empty square to fill its position
  in the create-region form. A free position and available port are suggested automatically; overlaps and port
  conflicts are checked. Regions can be removed with an optional OAR export, which is kept in `data/regions-removed`.
  A new region becomes active only after a grid restart. Remember to allow its TCP/UDP port in your router and DSM firewall.
- **Restarting only the web console:** use **Restart console** on the dashboard. OpenSim continues running,
  but you will need to sign in again. `entrypoint.sh` supervises the web console and restarts it after an unexpected failure.
  Files under `webconsole/` (Python and `ui/`) can therefore be updated without rebuilding the image or restarting the container.
- **Dashboard:** view connected avatars, send them a message or eject them, restart immediately or schedule a restart with
  announcements (`RESTART_DAILY_AT` for a daily restart), and configure ntfy, Discord, or Telegram alerts (`ALERT_*`).
  `entrypoint.sh` restarts OpenSim when requested by the console and after a crash, up to five consecutive attempts.

## Français

OpenSim Hypergrid compilé depuis le dépôt Git amont. La configuration initiale crée une région ;
vous pouvez en ajouter d'autres depuis la console web. Il se connecte à la même grille que la version Windows :
`http://grid.example.com:8002/`.

### Déploiement

1. Copier ce dossier sur le serveur (par exemple, `/volume1/docker/opensim-solo/`).
   Copier `.env.example` vers `.env`, puis remplacer les valeurs d'exemple par les paramètres de votre grille.
   Renseigner notamment le nom d'hôte public, les informations de l'administrateur et des mots de passe uniques et robustes.
   Ne jamais publier ni partager `.env` : il contient des mots de passe et d'autres paramètres privés.
2. Sur la box, rediriger la règle « opensim 8002 » (TCP et UDP) vers la machine qui héberge ce conteneur.
   Une seule machine doit recevoir cette redirection à la fois.
   **Ne jamais rediriger le port 8090.**
3. Docker utilise le réseau `host` : le conteneur utilise directement les ports de la machine hôte, soit 8002 (TCP et UDP) et 8090.
   Vérifier qu'ils sont libres. Dans le pare-feu DSM, autoriser TCP/UDP 8002 et le port TCP 8090 depuis le réseau local uniquement.
   En mode `host`, c'est le pare-feu de l'hôte qui contrôle les accès.
4. Construire et démarrer avec `docker compose up -d --build`, ou déployer le projet dans Container Manager.
   Le premier build peut prendre plusieurs minutes.
5. Ouvrir la console web à `http://<IP-LAN>:8090/` et se connecter avec `WEB_USER`.
   À distance, passer par le VPN (OpenVPN du NAS). Les journaux sont aussi visibles dans l'onglet « Journal » de Container Manager.
6. Pour arrêter la grille, utiliser le bouton « Arrêter la grille » ou arrêter le conteneur. La commande `shutdown` est envoyée
   et un délai de 60 secondes est prévu pour un arrêt propre.

Le fichier `.env.example` documente les paramètres disponibles et ne contient que des valeurs d'exemple.
`WEB_PASS` doit contenir au moins 8 caractères pour activer la console web. Les paramètres facultatifs
comprennent le compte lecteur, le TOTP, les alertes et les sauvegardes planifiées.
Par défaut, le build utilise la branche `master` ; définir `OPENSIM_REF` dans `.env` pour choisir une branche ou une étiquette Git.

### Données et mises à jour

- Les données persistantes sont dans `./data/` : bases dans `data/db`, configuration de la région dans `data/Regions`
  et journaux dans `data/logs`.
- Pour repartir de zéro, arrêter le conteneur, supprimer `./data`, puis le relancer. **Cette opération supprime définitivement les données de la grille.**
- Pour mettre à jour, lancer `docker compose build --no-cache`, puis `docker compose up -d`.
  Les données dans `./data` sont conservées.

### Console web v2 (`http://<IP-LAN>:8090/`)

La connexion se fait par formulaire. Les sessions durent 12 heures ; l'accès est temporairement bloqué après cinq échecs.
L'interface est disponible en français, anglais, allemand et italien, et mémorise la langue choisie dans le navigateur.

Les onglets comprennent le tableau de bord (processus, mémoire, disque, état de la grille et des régions), une console avec filtre,
historique et commandes rapides, la gestion des utilisateurs, les sauvegardes, les liens Hypergrid et le journal d'actions.

- **Sauvegardes :** les bases SQLite sont copiées **à chaud** via l'API de sauvegarde SQLite, et les fichiers de configuration des régions sont copiés.
  La sauvegarde OAR contient un fichier par région configurée ; le chargement restaure toutes les régions sauvegardées. Les fichiers sont dans `data/backups/`. La restauration des bases est programmée
  puis appliquée au prochain démarrage ; l'état précédent est conservé dans `data/backups/avant-restauration-…`.
  Définir `BACKUP_EVERY_HOURS` pour activer les sauvegardes automatiques des bases.
- **Compte lecteur :** configurer `WEB_VIEWER_USER` et `WEB_VIEWER_PASS`. Ce compte peut consulter l'état,
  mais ne voit pas les adresses e-mail et ne peut effectuer aucune action.
- **Authentification à deux facteurs :** générer un secret avec
  `python3 -c "import base64,os;print(base64.b32encode(os.urandom(10)).decode())"`,
  renseigner `WEB_TOTP_SECRET` et l'ajouter à une application d'authentification (TOTP, SHA1, 6 chiffres).
- **Import / Export :** l'onglet dédié permet d'exporter une région OAR, d'envoyer un fichier depuis l'ordinateur,
  de choisir les options d'import (fusion ou sans assets) et de télécharger les fichiers. L'import/export IAR de l'inventaire
  d'un avatar nécessite son mot de passe ; celui-ci n'est jamais enregistré ni écrit dans le journal d'actions.
  Les fichiers sont dans `data/transfer/oar` et `data/transfer/iar`.
- **Régions :** l'onglet comprend une carte des emplacements configurés. Cliquer sur une case libre préremplit sa position
  dans le formulaire de création. Une position libre et un port disponible sont proposés automatiquement ; les chevauchements
  et conflits de ports sont vérifiés. Une région peut être retirée avec un export OAR facultatif, conservé dans `data/regions-removed`.
  Une nouvelle région n'est active qu'après le redémarrage de la grille. Penser à autoriser son port TCP/UDP sur la box et le pare-feu DSM.
- **Redémarrer uniquement la console web :** utiliser le bouton « Redémarrer la console » du tableau de bord.
  OpenSim continue de tourner, mais il faudra se reconnecter. `entrypoint.sh` supervise la console web et la relance après une panne inattendue.
  Les fichiers de `webconsole/` (Python et `ui/`) peuvent donc être mis à jour sans reconstruire l'image ni redémarrer le conteneur.
- **Tableau de bord :** consulter les avatars connectés, leur envoyer un message ou les expulser, redémarrer immédiatement ou programmer
  un redémarrage avec annonces (`RESTART_DAILY_AT` pour un redémarrage quotidien), et configurer les alertes ntfy, Discord ou Telegram (`ALERT_*`).
  `entrypoint.sh` relance OpenSim à la demande de la console et après un plantage, jusqu'à cinq fois de suite.
