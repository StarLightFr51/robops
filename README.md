# robops — DevOps Roblox en ligne de commande

`robops` est une CLI Python qui **écrase le contenu de tes places Roblox** (et, si tu le veux, les données de tes DataStores) depuis ton dépôt Git, via l'API officielle **Open Cloud**. Elle est pensée pour tourner dans **GitHub Actions** : un push sur `develop` met à jour le staging, un push sur `main` met à jour la production.

```
push main ──► build Rojo ──► vérifications ──► publication des places ──► métadonnées
                                                   │                        │
                                                   └──► DataStores (opt-in) ─┴──► redémarrage des serveurs
```

## Ce que ça fait

| Étape | Détail | Permission de la clé API |
|---|---|---|
| Build | lance `rojo build` (ou toute autre commande) | — |
| Vérifications | fichiers présents, `.rbxl`/`.rbxlx`, JSON des données valides — **avant** tout appel réseau | — |
| Publication | chaque place est écrasée par son fichier (nouvelle version `Published` ou `Saved`) | `universe-places:write` |
| Métadonnées | nom, description, nombre de joueurs par serveur | `universe.place:write` |
| DataStores | snapshot de sauvegarde, puis écrasement des clés depuis un JSON | `universe-datastores.objects:update` + `:create` + `universe-datastores.control:snapshot` |
| Redémarrage | bascule les serveurs sur la nouvelle version | `universe:write` |
| Joueurs (`robops player`) | backup, restauration, édition des profils ProfileStore / DataService | `universe-datastores.objects:list` + `:read` + `:update` (+ `:create`, `:delete`) |

Autres points :
- retries automatiques (429 / 5xx) avec backoff exponentiel et respect de `Retry-After` ;
- messages d'erreur en français avec la cause probable (clé expirée, IP non autorisée, permission manquante…) ;
- en CI : logs groupés, annotations `::error::`, résumé du déploiement dans l'onglet *Summary*, versions publiées exposées en *outputs* ;
- `--dry-run` : build + vérifications sans jamais contacter Roblox (utilisé sur les pull requests).

## Arborescence

```
robops/
├── action.yml                  # action GitHub composite (uses: ./tools/robops)
├── exe/                        # compilation de robops.exe et robops-setup.exe (PyInstaller)
├── pyproject.toml
├── src/robops/
│   ├── cli.py                  # commandes
│   ├── client.py               # client Open Cloud (HTTP, retries, erreurs)
│   ├── config.py               # lecture de robops.toml
│   ├── deploy.py               # orchestration du déploiement
│   ├── output.py               # sortie console / GitHub Actions
│   ├── players.py              # profils ProfileStore / DataService (verrous, backup, restauration)
│   ├── updater.py              # mise à jour automatique depuis les GitHub Releases
│   ├── installer.py            # installateur Windows (robops-setup.exe)
│   ├── winsetup.py             # PATH utilisateur et « Applications et fonctionnalités »
│   └── templates/              # fichiers générés par `robops init`
├── tests/                      # 73 tests (API Roblox, GitHub et registre Windows simulés)
└── .github/workflows/
    ├── ci.yml                  # tests de l'outil lui-même
    └── release.yml             # compile les .exe et publie une release à chaque tag v*
```

## Version Windows (.exe) avec installateur et mise à jour automatique

Sur ton PC, tu peux utiliser `robops.exe` sans installer Python. Les exécutables sont compilés par GitHub Actions sur une machine Windows et publiés dans les *Releases* du dépôt de l'outil.

### Mise en place (une seule fois)

1. Crée sur GitHub un dépôt nommé `robops` (privé ou public) et envoie-y le contenu de ce dossier, à la racine :

   ```bash
   cd robops
   git init
   git add .
   git commit -m "robops 1.2.0"
   git branch -M main
   git remote add origin https://github.com/TON-COMPTE/robops.git
   git push -u origin main
   ```

2. Lance la compilation : onglet **Actions** du dépôt `robops` → **Release** (colonne de gauche) → **Run workflow** → branche `main` → **Run workflow**.

   En 5 minutes environ, le workflow lance les tests, compile `robops.exe` et `robops-setup.exe` sur une machine Windows, les teste, calcule leurs empreintes SHA-256 et crée la release `v1.2.0`. Le nom du dépôt est gravé dans les exécutables : rien à configurer.

3. Les fichiers apparaissent dans la section **Releases**, à droite de la page d'accueil du dépôt.

Si tu préfères git, pousser le tag `v1.2.0` (`git tag v1.2.0 && git push origin v1.2.0`) déclenche la même chose.

### Installer sur un PC

Télécharge `robops-setup.exe` depuis la page *Releases* et double-clique dessus. L'installateur :

- interroge le dépôt et récupère la dernière version de `robops.exe` ;
- vérifie son empreinte SHA-256 avant de l'installer ;
- l'installe dans `%LOCALAPPDATA%\Programs\robops` (sans droits administrateur) ;
- ajoute ce dossier au **PATH** de ton compte Windows ;
- l'enregistre dans **Applications et fonctionnalités** pour pouvoir le désinstaller.

Ouvre ensuite un **nouveau** terminal et tape `robops --version`. Relancer l'installateur plus tard met à jour l'outil (`--force` pour réinstaller la même version).

> Les .exe ne sont pas signés numériquement : au premier lancement, Windows SmartScreen affiche « Windows a protégé votre ordinateur ». Clique sur **Informations complémentaires** puis **Exécuter quand même**. Certains antivirus signalent aussi à tort les programmes compilés avec PyInstaller. Pour supprimer ces avertissements, il faut un certificat de signature de code (payant).

### Mise à jour automatique

- Une fois par jour au maximum, après une commande, `robops.exe` regarde s'il existe une release plus récente. Si oui, il la télécharge, vérifie son empreinte et remplace l'exécutable ; la nouvelle version sert dès le lancement suivant.
- Une mise à jour ratée (pas de réseau, fichier corrompu…) affiche un avertissement mais ne fait jamais échouer ta commande.
- `robops self-update` force la mise à jour tout de suite ; `robops self-update --check` dit seulement s'il y en a une.
- La mise à jour automatique est **désactivée en CI** (variables `CI` / `GITHUB_ACTIONS`) et avec `ROBOPS_NO_UPDATE=1`. Avec une installation pip, elle n'existe pas : on met à jour avec pip.

### Publier une nouvelle version

Augmente `__version__` dans `src/robops/__init__.py` (par exemple `1.2.1`), commit et push, puis relance **Actions → Release → Run workflow**. Si cette version est déjà publiée, le workflow s'arrête et te demande d'augmenter le numéro. Les PC équipés se mettent à jour d'eux-mêmes dans les 24 h.

### Dépôt privé

Les appels à GitHub nécessitent alors un token. Crée un *fine-grained token* avec uniquement **Contents : Read** sur le dépôt de robops, puis enregistre-le dans une variable d'environnement utilisateur Windows nommée `ROBOPS_GITHUB_TOKEN`. L'installateur et la mise à jour automatique l'utilisent tous les deux.

### Désinstaller

*Paramètres → Applications → robops → Désinstaller*, ou `robops-setup.exe --uninstall`. Le dossier, l'entrée du PATH et l'entrée du registre sont retirés.

### Compiler et installer sur ton PC, sans GitHub

Sur un PC Windows avec Python 3.11+ (installé depuis python.org, case *Add python.exe to PATH* cochée), ouvre un terminal dans le dossier `robops` et lance :

```powershell
python exe\build.py --install
```

Le script installe PyInstaller s'il manque, compile `robops.exe` (1 à 2 minutes), le copie dans `%LOCALAPPDATA%\Programs\robops` et ajoute ce dossier à ton PATH. Ouvre ensuite un **nouveau** terminal : `robops --version`.

Pour mettre à jour, relance la même commande après avoir récupéré le nouveau code. L'exécutable se met aussi à jour tout seul depuis les releases GitHub de `StarLightFr51/robops` (change de dépôt avec `--repo owner/nom`). Sans `--install`, les fichiers sont simplement produits dans `dist/`. PyInstaller ne sait pas produire un .exe Windows depuis Linux ou macOS.

## Installation dans ton projet Roblox

1. Copie le dossier `robops/` dans ton dépôt de jeu, sous `tools/robops/`.
2. Installe-le localement (Python 3.11 minimum) puis génère la configuration :

   ```bash
   pip install ./tools/robops
   robops init
   ```

   Cela crée `robops.toml`, `.github/workflows/roblox-deploy.yml` et `rokit.toml` (Rojo 7.7.0) — les fichiers existants ne sont pas écrasés sans `--force`.
3. Remplace les `0` de `robops.toml` par tes vrais IDs, puis vérifie :

   ```bash
   robops check                       # configuration seule
   ROBLOX_API_KEY=... robops check staging --online   # + clé et IDs auprès de Roblox
   ```

> Tu préfères un dépôt séparé pour l'outil ? Pousse `robops/` sur GitHub et remplace `uses: ./tools/robops` par `uses: StarLightFr51/robops@v1` dans le workflow.

## Créer la clé API Open Cloud

Creator Dashboard → **Credentials** → *Create API Key* :

- **Access permissions** : ajoute l'expérience concernée et les permissions du tableau ci-dessus (au minimum `universe-places` → *Write*).
- **Security → Accepted IP addresses** : les runners GitHub Actions changent d'IP à chaque exécution, il faut donc autoriser `0.0.0.0/0`. C'est la cause n°1 des erreurs **401** en CI.
- Fixe une **date d'expiration** et utilise idéalement **une clé par environnement** (staging / production).

## Configurer GitHub

Dans le dépôt de ton jeu : *Settings → Secrets and variables → Actions → onglet Secrets → New repository secret*, nom **`ROBLOX_API_KEY`**, valeur = ta clé Roblox. C'est tout. Tu peux ajouter un second secret `ROBLOX_API_KEY_PRODUCTION` pour utiliser une clé différente sur le vrai jeu.

Le workflow généré fonctionne avec un dépôt **privé** et un compte GitHub **gratuit** :

| Événement | Action |
|---|---|
| Pull request | build + vérifications (`--dry-run`), rien n'est publié |
| Push sur `main` | build + vérifications, rien n'est publié |
| Push sur `develop` | publication automatique sur le jeu de **test** (`staging`) |
| Bouton *Run workflow* | publication sur le **vrai jeu** (ou le staging), avec les options au choix |

Le vrai jeu ne se met donc **jamais à jour tout seul** : c'est toi qui valides en cliquant.

**Publier sur le vrai jeu :**

1. Pousse ton code sur `main` et attends la coche verte ✔ dans l'onglet *Actions* (le build fonctionne).
2. Onglet *Actions* → *Déploiement Roblox* dans la colonne de gauche → bouton *Run workflow* à droite.
3. *Use workflow from* : `main` ; *Où publier* : `production` → *Run workflow*.

Une minute ou deux plus tard, le jeu est à jour. Seules les personnes qui ont un accès en écriture au dépôt voient ce bouton. La production refuse de partir d'une autre branche que `main`.

Deux publications sur la même cible ne tournent jamais en même temps, et le fichier publié est conservé 30 jours en artefact.

> Dépôt **public** (ou offre GitHub Enterprise) : tu peux aussi utiliser les *Environments* de GitHub avec *Required reviewers* pour une validation par bouton « Approve ». Sur un dépôt privé en offre gratuite, ces options ne sont pas disponibles, d'où le fonctionnement ci-dessus.

## Le fichier `robops.toml`

```toml
api_key_env = "ROBLOX_API_KEY"

[build]
command = "rojo build default.project.json --output build/game.rbxl"

[environments.production]
universe_id = 1234567890
version_type = "Published"     # ou "Saved" (sauvegardé, pas joué)
restart_servers = false

[[environments.production.places]]
name = "main"
place_id = 9876543210
file = "build/game.rbxl"
display_name = "Mon jeu"       # optionnel
server_size = 30               # optionnel

[[environments.production.datastores]]   # écrit seulement avec --with-datastores
name = "GameConfig"
file = "data/config.production.json"     # { "clé": valeur, ... }
```

Toute valeur peut venir de l'environnement : `universe_id = "${ROBLOX_UNIVERSE_ID}"` ou `"${ROBLOX_UNIVERSE_ID:-123}"` avec valeur par défaut. Les variables ne sont résolues que pour l'environnement déployé.

## Commandes

```bash
# Déploiement complet d'un environnement
robops deploy staging
robops deploy production --with-datastores --restart
robops deploy production --place lobby --version-type Saved
robops deploy staging --dry-run

# Publication ponctuelle d'un fichier
robops publish build/game.rbxl --env staging
robops publish game.rbxlx --universe-id 123 --place-id 456 --version-type Saved

# Places et serveurs
robops place get --env production
robops place update --env production --name "Mon jeu" --server-size 30
robops servers restart --env production

# DataStores (alias : robops ds)
robops ds get GameConfig motd --env production
robops ds set GameConfig motd --value '"Double XP ce week-end !"' --env production
robops ds list PlayerData --prefix user_ --env staging
robops ds export GameConfig --out backup.json --env production
robops ds import GameConfig data/config.json --env production --dry-run
robops ds delete GameConfig old_key --env staging
robops ds snapshot --env production
```

Codes de sortie : `0` succès, `1` échec Roblox/déploiement, `2` erreur de configuration ou d'usage.

La clé API n'est **jamais** passée en argument (elle apparaîtrait dans les logs) : elle est lue dans la variable indiquée par `api_key_env`.

## Données joueurs (ProfileStore / DataService)

Les commandes `robops player …` comprennent le format de **ProfileStore**, utilisé par le DataService de leifstout (et compatible avec ProfileService). Par défaut elles visent le DataStore `Default` avec des clés `PLAYER_<UserId>`, les valeurs par défaut de DataService. Si ton `DataService:init` passe `profileStoreIndex` ou `profileStoreDataPrefix`, recopie-les dans la section `[players]` de `robops.toml` (ou utilise `--datastore` / `--prefix`).

```bash
# Voir un joueur (Data sur stdout, état de session sur stderr)
robops player get 123456 --env production
robops player get 123456 --env production --raw          # profil complet

# Sauvegarder tous les joueurs (.json.gz pour compresser)
robops player backup --out backups/players-2026-10-15.json.gz --env production

# Restaurer un joueur depuis un backup
robops player restore 123456 --from backups/players-2026-10-15.json.gz --env production --dry-run
robops player restore 123456 --from backups/players-2026-10-15.json.gz --env production

# Corriger un joueur hors ligne
robops player edit 123456 --set coins=500 --set stats.level=10 --env production
robops player edit 123456 --set 'inventory=["sword","shield"]' --unset tempFlag --env production

# Catastrophe : tout le monde revient à l'état du backup
robops player restore --all --from backups/players-2026-10-15.json.gz --env production --dry-run
robops player restore --all --from backups/players-2026-10-15.json.gz --env production --yes

# Demande d'effacement de données (droit à l'oubli)
robops player delete 123456 --env production --yes
```

Protections intégrées :

- **Joueur en ligne = écriture refusée.** Le profil porte un verrou (`MetaData.ActiveSession`) tant qu'un serveur le possède. Ce serveur sauvegarde toutes les 5 minutes et écraserait la modification. Pour corriger un joueur connecté, passe par une commande admin dans le jeu.
- **Verrou périmé = retiré.** Si la dernière sauvegarde date de plus de 630 s (le seuil de ProfileStore), le serveur est considéré comme planté et le verrou est retiré à l'écriture.
- **Seul `Data` est remplacé.** `MetaData` (compteurs), `GlobalUpdates` (messages en attente), `UserIds` et les métadonnées Roblox de l'entrée sont conservés.
- **Écriture conditionnelle (etag).** Si le joueur se connecte entre la lecture et l'écriture, Roblox refuse la mise à jour et rien n'est écrit.
- **Copie de sécurité locale.** Avant chaque écriture ou suppression, l'ancien profil est copié dans `.robops/safety/`.
- **`--all` exige `--yes`**, et les joueurs en ligne sont ignorés (et listés) au lieu de bloquer toute la restauration.

Le backup est écrit au fil de l'eau avec 4 lectures en parallèle (`--workers`, jusqu'à 16) ; le fichier n'apparaît qu'une fois complet. Il accepte aussi les exports faits avec `robops ds export`.

> ⚠️ Les backups et les copies de sécurité contiennent des **données personnelles de joueurs**. `robops init` ajoute `.robops/` et `backups/` au `.gitignore` : ne les committe jamais, surtout dans un dépôt public.

Si `useMock` est activé dans DataService (tests dans Studio), ces données ne sont jamais écrites dans un DataStore : il n'y a rien à sauvegarder.

## Revenir en arrière

Relance le workflow manuellement sur le commit précédent (ou un tag), ou republie l'artefact conservé :

```bash
robops publish place-production-<sha>/game.rbxl --env production
robops servers restart --env production
```

Pour les données, chaque déploiement avec `--with-datastores` prend d'abord un snapshot : les valeurs précédentes restent récupérables pendant 30 jours via l'historique des révisions.

## Limites à connaître

- L'API de publication **ne met pas à jour** certains types d'instances : `EditableImage`, `EditableMesh`, `PartOperation` (unions), `SurfaceAppearance`, `BaseWrap`. Si tu les modifies, publie une fois depuis Studio.
- La mise à jour des métadonnées échoue (409) si une session **Team Create** est ouverte sur la place : c'est un avertissement, pas un échec (sauf avec `--strict-metadata`).
- La publication est limitée à environ **30 requêtes par minute** par propriétaire de clé.
- Un seul snapshot de DataStores par jour UTC ; les suivants sont sans effet.
- Écraser une entrée de DataStore efface ses `users` et `attributes` côté Roblox : `robops` les relit et les renvoie automatiquement (désactivable avec `--clear-metadata`).

## Développement

```bash
pip install -e ".[dev]"
pytest
```
