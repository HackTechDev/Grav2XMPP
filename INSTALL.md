# Installation de grav2xmpp

Le fonctionnement et les options sont décrits dans [README.md](README.md).

Les chemins ci-dessous supposent que le projet est dans `~/APP/Grav2XMPP`.
Adapte-les si besoin.

## 1. Prérequis système

```bash
sudo apt install git python3 python3-venv
```

Sous Debian/Ubuntu, `python3-venv` peut porter le numéro de version, par
exemple `python3.14-venv`.

## 2. Environnement Python

```bash
cd ~/APP/Grav2XMPP
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x grav2xmpp.py hooks/*
```

Les hooks utilisent automatiquement `~/APP/Grav2XMPP/.venv/bin/python` s'il
existe, sinon `python3`.

## 3. Compte XMPP du bot

Il est conseillé de créer un compte dédié, par exemple `gravbot@tarentule.us`.
Avec Prosody :

```bash
sudo prosodyctl adduser gravbot@tarentule.us
```

Avec ejabberd :

```bash
sudo ejabberdctl register gravbot tarentule.us 'mot-de-passe'
```

Si le salon `hacktechdev@conference.tarentule.us` est réservé aux membres,
ou modéré, ajoute le bot comme membre (ou donne-lui la parole) depuis ton
client XMPP.

## 4. Configuration

```bash
mkdir -p ~/.config
cp grav2xmpp.ini.example ~/.config/grav2xmpp.ini
chmod 600 ~/.config/grav2xmpp.ini
```

Édite `~/.config/grav2xmpp.ini` et renseigne au minimum :

- `jid` et `password` : le compte du bot ;
- `base_url` : l'URL publique du blog, par exemple `https://blog.tarentule.us` ;
- `blog_folder` : le dossier des articles sans son préfixe numérique
  (`01.blog` → `blog`). Laisse vide pour surveiller toutes les pages.

Si tes articles n'utilisent pas le template `item`, adapte `templates`
(par exemple `templates = item, post`).

## 5. Tester sans envoyer

Depuis le dépôt git du blog Grav :

```bash
cd /chemin/vers/depot-grav
~/APP/Grav2XMPP/.venv/bin/python ~/APP/Grav2XMPP/grav2xmpp.py --dry-run -v
```

Le script affiche les messages qu'il aurait envoyés pour le dernier commit.
Pour tester sur un commit qui a ajouté un article :

```bash
~/APP/Grav2XMPP/.venv/bin/python ~/APP/Grav2XMPP/grav2xmpp.py --rev <sha> --dry-run -v
```

Retire `--dry-run` pour faire un vrai envoi dans le salon.

## 6. Installer le hook

### Cas A : commit local (le plus courant)

Le message part dès que tu commites un nouvel article.

```bash
cp ~/APP/Grav2XMPP/hooks/post-commit /chemin/vers/depot-grav/.git/hooks/post-commit
chmod +x /chemin/vers/depot-grav/.git/hooks/post-commit
```

L'envoi tourne en arrière-plan : le commit n'est ni ralenti ni bloqué, même
si le serveur XMPP ne répond pas. Les messages et erreurs sont enregistrés
dans `/chemin/vers/depot-grav/.git/grav2xmpp.log`.

S'il existe déjà un hook `post-commit`, ajoute plutôt cette ligne à la fin :

```sh
( ~/APP/Grav2XMPP/.venv/bin/python ~/APP/Grav2XMPP/grav2xmpp.py >>"$(git rev-parse --git-dir)/grav2xmpp.log" 2>&1 & )
```

### Cas B : push vers un dépôt sur le serveur

Le message part quand le push arrive sur le serveur, par exemple si le site est
déployé à partir d'un dépôt nu. Sur le serveur, installe le projet (étapes 1 à
4) puis :

```bash
cp ~/APP/Grav2XMPP/hooks/post-receive /chemin/vers/depot.git/hooks/post-receive
chmod +x /chemin/vers/depot.git/hooks/post-receive
```

Le résultat s'affiche dans la sortie de `git push`, préfixé par `remote:`. Un
échec de notification ne fait jamais échouer le push.

Si un hook `post-receive` existe déjà (déploiement, par exemple), note qu'il
lit lui aussi stdin : sauvegarde les lignes reçues et transmets-les aux deux
traitements :

```sh
input=$(cat)
echo "$input" | ~/APP/Grav2XMPP/.venv/bin/python ~/APP/Grav2XMPP/grav2xmpp.py --post-receive
# ... puis ton déploiement, en réutilisant "$input" si besoin
```

N'installe pas les deux hooks sur la même chaîne, sinon chaque article serait
annoncé deux fois (au commit, puis au push).

### Chemins personnalisés

Si le projet n'est pas dans `~/APP/Grav2XMPP`, définis ces variables dans
l'environnement du hook, ou modifie directement les deux premières lignes du
hook :

- `GRAV2XMPP` : chemin de `grav2xmpp.py` ;
- `GRAV2XMPP_PYTHON` : interpréteur Python à utiliser.

## 7. Vérification

Crée un article de test dans le blog, puis :

```bash
git add user/pages/01.blog/test-notif/item.md
git commit -m "Test notification XMPP"
tail .git/grav2xmpp.log
```

Le message doit apparaître dans le salon au bout de quelques secondes.
Supprime ensuite l'article de test.

## Dépannage

| Symptôme dans le log | Cause probable |
|----------------------|----------------|
| `Module 'slixmpp' introuvable` | Le venv n'est pas installé, ou le hook utilise le mauvais Python (voir `GRAV2XMPP_PYTHON`) |
| `JID/mot de passe XMPP manquants` | Fichier de configuration absent ou illisible |
| `Authentification XMPP refusée` | Mauvais `jid` ou `password` |
| `Échec de l'envoi XMPP : TimeoutError` | Serveur injoignable, ou salon qui refuse le bot (salon réservé aux membres, pseudo déjà pris) |
| `Commit … : aucun nouvel article détecté.` | Le commit ne fait que modifier des articles existants, ou le fichier n'a pas été reconnu comme un article. Lance le script avec `--dry-run -v` et vérifie `templates`, `blog_folder` et `published` |
| Log vide | Le hook n'est pas exécutable (`chmod +x`) ou n'est pas dans `.git/hooks/` |
