# grav2xmpp

Notifie un salon XMPP (MUC) lorsqu'un nouvel article est ajouté à un blog
[Grav](https://getgrav.org) versionné avec git.

À chaque commit (ou push) contenant un nouvel article, un message est envoyé
dans le salon configuré, par défaut `xmpp:hacktechdev@conference.tarentule.us` :

```
📝 Nouvel article sur le blog : Mon premier article
https://blog.tarentule.us/blog/hello-xmpp
```

L'installation est décrite dans [INSTALL.md](INSTALL.md).

## Fonctionnement

1. Un hook git (`post-commit` en local ou `post-receive` sur un serveur) appelle
   `grav2xmpp.py`.
2. Le script liste les fichiers **ajoutés** dans le ou les commits concernés.
   Une modification d'un article existant ne déclenche pas de message.
3. Il ne garde que les pages Grav dont le nom correspond à un template
   d'article (`item.md`, `item.fr.md`…) situées sous `pages/`. Il peut aussi se
   limiter à un dossier précis (`blog_folder`).
4. Il lit l'en-tête YAML de la page :
   - `title` : titre affiché dans le message (à défaut, le nom du dossier) ;
   - `slug` : remplace le nom du dossier dans l'URL ;
   - `published: false` : la page est ignorée (brouillon).
5. Il construit l'URL publique à partir de `base_url` et du chemin de la page,
   sans les préfixes numériques (`01.blog/mon-article` → `/blog/mon-article`),
   avec la langue en préfixe pour les pages multilingues (`item.fr.md` →
   `/fr/blog/mon-article`).
6. Il se connecte au serveur XMPP, rejoint le salon et envoie un message par
   nouvel article.

Le dépôt git peut avoir pour racine le site Grav entier ou seulement le dossier
`user/` : le script repère le dossier `pages/` quel que soit son emplacement.

## Utilisation

```bash
grav2xmpp.py                  # analyse le dernier commit (HEAD)
grav2xmpp.py --rev abc123     # analyse un commit précis
grav2xmpp.py --range A..B     # analyse une plage de commits
grav2xmpp.py --post-receive   # lit « old new ref » sur stdin (hook serveur)
grav2xmpp.py --dry-run -v     # affiche les messages sans les envoyer
grav2xmpp.py -c fichier.ini   # utilise un fichier de configuration précis
```

Le script doit être lancé depuis le dépôt git du blog. C'est automatiquement le
cas quand il est appelé par un hook.

Codes de retour : `0` si tout s'est bien passé ou s'il n'y avait aucun nouvel
article, `1` en cas d'erreur (git, configuration ou envoi XMPP).

## Configuration

Le fichier INI est cherché dans cet ordre :

1. le chemin donné par `-c` / `--config` ;
2. la variable d'environnement `GRAV2XMPP_CONFIG` ;
3. `~/.config/grav2xmpp.ini` ;
4. `grav2xmpp.ini` à côté du script.

| Section | Clé           | Variable d'environnement | Défaut                                   | Description |
|---------|---------------|--------------------------|------------------------------------------|-------------|
| `xmpp`  | `jid`         | `GRAV2XMPP_JID`          | *(obligatoire)*                          | Compte XMPP du bot |
| `xmpp`  | `password`    | `GRAV2XMPP_PASSWORD`     | *(obligatoire)*                          | Mot de passe du bot |
| `xmpp`  | `room`        | `GRAV2XMPP_ROOM`         | `hacktechdev@conference.tarentule.us`    | Salon cible (le préfixe `xmpp:` est accepté) |
| `xmpp`  | `nick`        | `GRAV2XMPP_NICK`         | `GravBot`                                | Pseudo utilisé dans le salon |
| `grav`  | `base_url`    | `GRAV2XMPP_BASE_URL`     | *(vide → URL relative)*                  | URL publique du site |
| `grav`  | `templates`   | `GRAV2XMPP_TEMPLATES`    | `item`                                   | Templates d'article, séparés par des virgules |
| `grav`  | `blog_folder` | `GRAV2XMPP_BLOG_FOLDER`  | *(vide → toutes les pages)*              | Ne notifier que sous ce dossier |
| `grav`  | `message`     | `GRAV2XMPP_MESSAGE`      | `📝 Nouvel article : {title}\n{url}`     | Modèle du message |

Les variables d'environnement l'emportent sur le fichier INI.

Variables disponibles dans `message` : `{title}`, `{url}`, `{path}` (chemin du
fichier dans le dépôt) et `{commit}` (SHA court). `\n` insère un saut de ligne.

## Fichiers

| Fichier                 | Rôle |
|-------------------------|------|
| `grav2xmpp.py`          | Script principal |
| `grav2xmpp.ini.example` | Modèle de configuration |
| `hooks/post-commit`     | Hook local, lancé en arrière-plan pour ne pas bloquer le commit |
| `hooks/post-receive`    | Hook pour un dépôt distant qui reçoit les push |
| `requirements.txt`      | Dépendances Python |

## Dépendances

- Python ≥ 3.9
- git
- [slixmpp](https://codeberg.org/poezio/slixmpp) ≥ 1.8 : client XMPP
- [PyYAML](https://pyyaml.org) *(optionnel)* : lecture fiable des en-têtes.
  Sans PyYAML, un analyseur minimal lit uniquement les clés de premier niveau.

## Limites connues

- Seuls les fichiers **ajoutés** déclenchent une notification. Si un article
  est créé avec `published: false` puis publié dans un commit ultérieur, il ne
  sera pas annoncé. Pour éviter ce cas, ne commite l'article qu'au moment de
  le publier.
- Avec `post-receive`, quand une branche est créée par le push, seul le dernier
  commit est analysé.
- Un renommage de dossier (détecté par git comme un renommage) n'est pas
  considéré comme un nouvel article.
