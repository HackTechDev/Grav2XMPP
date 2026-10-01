#!/usr/bin/env python3
"""
grav2xmpp — Notifie un salon XMPP (MUC) lorsqu'un nouvel article Grav est commité.

Utilisation typique : appelé par un hook git `post-commit` (ou `post-receive`
sur un dépôt distant). Le script détecte les fichiers de page Grav ajoutés
(ex. user/pages/01.blog/mon-article/item.md), lit leur en-tête YAML
(title, slug, published…) et envoie un message dans le salon configuré.

Exemples :
    grav2xmpp.py                      # analyse le dernier commit (HEAD)
    grav2xmpp.py --rev abc123         # analyse un commit précis
    grav2xmpp.py --range A..B         # analyse une plage de commits
    grav2xmpp.py --post-receive       # lit "old new ref" sur stdin (hook serveur)
    grav2xmpp.py --dry-run            # affiche le message sans l'envoyer
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath

# Emplacements où chercher le fichier de configuration, par ordre de priorité.
# Les chemins inexistants sont simplement ignorés par ConfigParser.read().
DEFAULT_CONFIG_PATHS = [
    os.environ.get("GRAV2XMPP_CONFIG", ""),
    os.path.expanduser("~/.config/grav2xmpp.ini"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "grav2xmpp.ini"),
]

# SHA « nul » utilisé par git dans les hooks post-receive : en ancienne valeur,
# il signale la création d'une branche ; en nouvelle valeur, sa suppression.
ZERO_SHA = "0" * 40

# Logger du script. Les modules slixmpp écrivent aussi dans le logger racine,
# configuré dans main() : leurs messages apparaissent avec --verbose.
log = logging.getLogger("grav2xmpp")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    """Paramètres du script, lus depuis le fichier INI et l'environnement."""

    # Compte XMPP du bot (obligatoires, sauf en --dry-run)
    jid: str
    password: str
    # Salon cible et pseudo utilisé pour le rejoindre
    room: str = "hacktechdev@conference.tarentule.us"
    nick: str = "GravBot"
    # URL publique du site, sans « / » final ; vide → liens relatifs
    base_url: str = ""
    # Noms de template considérés comme des articles (item.md, item.fr.md…)
    templates: tuple[str, ...] = ("item",)
    # Si renseigné, seules les pages sous ce dossier sont prises en compte
    # (ex. "blog" pour user/pages/01.blog/...)
    blog_folder: str = ""
    # Modèle du message ; variables : {title} {url} {path} {commit}
    message: str = "📝 Nouvel article : {title}\n{url}"

    @classmethod
    def load(cls, path: str | None) -> "Config":
        """Construit la configuration.

        Si `path` est fourni, seul ce fichier est lu ; sinon on essaie
        DEFAULT_CONFIG_PATHS. Une variable d'environnement GRAV2XMPP_*
        l'emporte toujours sur la valeur du fichier.
        """
        # interpolation=None : un « % » dans le mot de passe ne doit pas être
        # interprété comme une référence à une autre clé.
        parser = configparser.ConfigParser(interpolation=None)
        paths = [path] if path else [p for p in DEFAULT_CONFIG_PATHS if p]
        read = parser.read(paths, encoding="utf-8")
        # Une section absente est remplacée par un dict vide, pour que get()
        # retombe sur les valeurs par défaut.
        sect = parser["xmpp"] if parser.has_section("xmpp") else {}
        grav = parser["grav"] if parser.has_section("grav") else {}
        if read:
            log.debug("Configuration lue depuis %s", read)

        def get(section, key, env, default=""):
            """Valeur de `key` : variable d'environnement, puis fichier, puis défaut."""
            return os.environ.get(env) or section.get(key, default)

        # Accepte aussi la forme URI « xmpp:salon@serveur?join ».
        room = get(sect, "room", "GRAV2XMPP_ROOM", cls.room)
        room = room.removeprefix("xmpp:").split("?")[0]
        templates = get(grav, "templates", "GRAV2XMPP_TEMPLATES", "item")
        message = get(grav, "message", "GRAV2XMPP_MESSAGE", cls.message)

        return cls(
            jid=get(sect, "jid", "GRAV2XMPP_JID"),
            password=get(sect, "password", "GRAV2XMPP_PASSWORD"),
            room=room,
            nick=get(sect, "nick", "GRAV2XMPP_NICK", cls.nick),
            # Le « / » final est retiré pour éviter un « // » dans les URL.
            base_url=get(grav, "base_url", "GRAV2XMPP_BASE_URL").rstrip("/"),
            # "item, post" → ("item", "post")
            templates=tuple(t.strip() for t in templates.split(",") if t.strip()),
            blog_folder=get(grav, "blog_folder", "GRAV2XMPP_BLOG_FOLDER"),
            # Dans un fichier INI, le saut de ligne s'écrit littéralement « \n ».
            message=message.replace("\\n", "\n"),
        )


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #
def git(*args: str) -> str:
    """Lance une commande git dans le dépôt courant et renvoie sa sortie.

    Lève subprocess.CalledProcessError si git échoue (géré dans main()).
    """
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def added_files(rev_range: str) -> list[tuple[str, str]]:
    """Retourne [(commit, chemin)] des fichiers ajoutés dans la plage donnée.

    `rev_range` est soit un commit unique (« HEAD », un SHA…), soit une plage
    « A..B ». Chaque fichier est associé au commit qui l'a ajouté, pour pouvoir
    relire son contenu tel qu'il était à ce moment-là.
    """
    if ".." in rev_range:
        # Commits de la plage, du plus ancien au plus récent
        commits = git("rev-list", "--reverse", rev_range).split()
    else:
        commits = [git("rev-parse", rev_range).strip()]

    result = []
    for commit in commits:
        # --root          : fonctionne aussi pour le tout premier commit du dépôt
        # -r              : descend dans les sous-dossiers
        # -M              : détecte les renommages, qui ne comptent pas comme ajouts
        # --diff-filter=A : ne garde que les fichiers ajoutés
        out = git(
            "diff-tree", "--root", "--no-commit-id", "-r", "-M",
            "--name-only", "--diff-filter=A", commit,
        )
        result += [(commit, line) for line in out.splitlines() if line]
    return result


def read_blob(commit: str, path: str) -> str:
    """Contenu du fichier `path` tel qu'il était dans `commit`."""
    return git("show", f"{commit}:{path}")


# --------------------------------------------------------------------------- #
# Grav
# --------------------------------------------------------------------------- #
# Préfixe numérique d'ordre des dossiers Grav, absent des URL : "01.blog" -> "blog"
PREFIX_RE = re.compile(r"^\d+\.")


@dataclass
class Article:
    """Nouvel article détecté, prêt à être annoncé."""

    title: str
    url: str
    path: str    # chemin du fichier dans le dépôt
    commit: str  # SHA complet du commit qui l'a ajouté


def parse_frontmatter(text: str) -> dict:
    """Extrait l'en-tête YAML (entre deux lignes « --- ») d'une page Grav.

    Renvoie un dict vide si la page n'a pas d'en-tête ou s'il est illisible.
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    if not m:
        return {}
    raw = m.group(1)
    try:
        import yaml  # PyYAML, optionnel

        data = yaml.safe_load(raw)
        # Un en-tête qui n'est pas un mapping (liste, texte…) est ignoré.
        return data if isinstance(data, dict) else {}
    except ImportError:
        # Repli minimal sans PyYAML : clés de premier niveau "cle: valeur".
        # Les listes et sous-clés sont ignorées ; les guillemets autour de la
        # valeur sont retirés, et true/false sont convertis en booléens.
        data = {}
        for line in raw.splitlines():
            km = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
            if km and km.group(2):
                val = km.group(2).strip().strip("'\"")
                data[km.group(1)] = {"true": True, "false": False}.get(val.lower(), val)
        return data
    except Exception as exc:  # YAML invalide
        log.warning("En-tête YAML illisible : %s", exc)
        return {}


def match_article(path: str, cfg: Config) -> tuple[list[str], str] | None:
    """Si `path` est une page article Grav, retourne (segments d'URL, langue).

    Exemple : "user/pages/01.blog/mon-article/item.fr.md"
              → (["blog", "mon-article"], "fr")
    Renvoie None si le fichier n'est pas un article à annoncer.
    """
    p = PurePosixPath(path)
    parts = p.parts
    if "pages" not in parts:
        return None
    # Nom de fichier : <template>.md ou <template>.<lang>.md
    # (langue sur deux lettres, éventuellement suivie d'une région : fr, pt-BR)
    fm = re.match(r"^([\w-]+?)(?:\.([a-z]{2}(?:-[A-Za-z]{2})?))?\.md$", p.name)
    if not fm or fm.group(1) not in cfg.templates:
        return None

    # Position du dernier « pages » du chemin : le dépôt peut être la racine
    # de Grav (user/pages/...) ou le dossier user/ seul (pages/...).
    idx = len(parts) - 1 - parts[::-1].index("pages")
    # Dossiers entre « pages » et le fichier ; une page directement à la
    # racine de pages/ n'est pas un article.
    folders = list(parts[idx + 1:-1])
    if not folders:
        return None
    segments = [PREFIX_RE.sub("", f) for f in folders]
    # Le dossier du blog doit être un parent de l'article, pas l'article lui-même
    # (segments[:-1]) : ainsi la page de liste du blog n'est pas annoncée.
    if cfg.blog_folder and cfg.blog_folder not in segments[:-1]:
        return None
    return segments, fm.group(2) or ""


def build_article(commit: str, path: str, cfg: Config) -> Article | None:
    """Construit l'Article correspondant à `path`, ou None s'il faut l'ignorer."""
    matched = match_article(path, cfg)
    if not matched:
        return None
    segments, lang = matched

    # L'en-tête est lu dans le commit qui a ajouté le fichier, pas dans HEAD.
    header = parse_frontmatter(read_blob(commit, path))
    # Brouillon : « published: false » dans l'en-tête
    if header.get("published") is False:
        log.info("Ignoré (published: false) : %s", path)
        return None
    # Une page « visible: false » est absente des menus mais reste publiée :
    # elle est quand même annoncée.
    if header.get("visible") is False and header.get("published") is None:
        log.debug("Page non visible, mais publiée : %s", path)

    # Le slug de l'en-tête remplace le nom du dossier dans l'URL.
    if header.get("slug"):
        segments[-1] = str(header["slug"])
    # Sans titre, on déduit un titre lisible du dossier : "mon-article" → "Mon article"
    title = str(header.get("title") or segments[-1].replace("-", " ").capitalize())

    # Les pages multilingues ont la langue en préfixe : /fr/blog/mon-article
    url_path = "/".join(segments)
    if lang:
        url_path = f"{lang}/{url_path}"
    url = f"{cfg.base_url}/{url_path}" if cfg.base_url else f"/{url_path}"
    return Article(title=title, url=url, path=path, commit=commit)


def find_articles(rev_range: str, cfg: Config) -> list[Article]:
    """Liste les nouveaux articles à annoncer dans un commit ou une plage."""
    articles = []
    for commit, path in added_files(rev_range):
        art = build_article(commit, path, cfg)
        if art:
            articles.append(art)
    return articles


# --------------------------------------------------------------------------- #
# XMPP
# --------------------------------------------------------------------------- #
async def send_to_muc(cfg: Config, messages: list[str], timeout: float = 30) -> None:
    """Se connecte, rejoint le salon, envoie `messages` puis se déconnecte.

    Lève une exception en cas d'échec (authentification, salon, réseau) ou si
    l'ensemble dépasse `timeout` secondes (asyncio.TimeoutError).
    """
    # Import tardif : --dry-run fonctionne même sans slixmpp installé.
    import slixmpp

    class Bot(slixmpp.ClientXMPP):
        """Client XMPP éphémère : une connexion pour un seul envoi."""

        def __init__(self):
            super().__init__(cfg.jid, cfg.password)
            self.register_plugin("xep_0030")  # Service discovery
            self.register_plugin("xep_0045")  # MUC
            self.register_plugin("xep_0199")  # Ping
            # Future résolue quand l'envoi est terminé (ou échoué) : c'est elle
            # qu'attend send_to_muc(). Chaque gestionnaire vérifie done() car
            # une future ne peut être résolue qu'une seule fois.
            self.done = asyncio.get_running_loop().create_future()
            self.add_event_handler("session_start", self.on_start)
            self.add_event_handler("failed_auth", self.on_fail)
            self.add_event_handler("disconnected", self.on_disconnect)

        async def on_start(self, _):
            """Session ouverte : rejoint le salon et envoie les messages."""
            try:
                # Étapes usuelles d'ouverture de session XMPP
                await self.get_roster()
                self.send_presence()
                # Attend la confirmation du salon ; maxstanzas=0 évite de
                # recevoir l'historique des messages déjà postés.
                await self.plugin["xep_0045"].join_muc_wait(
                    cfg.room, cfg.nick, maxstanzas=0, timeout=timeout
                )
                for body in messages:
                    self.send_message(mto=cfg.room, mbody=body, mtype="groupchat")
                # Laisse le temps aux stanzas de partir avant de fermer
                await asyncio.sleep(1)
                if not self.done.done():
                    self.done.set_result(None)
            except Exception as exc:
                if not self.done.done():
                    self.done.set_exception(exc)
            finally:
                self.disconnect()

        def on_fail(self, _):
            """Le serveur a refusé le JID ou le mot de passe."""
            if not self.done.done():
                self.done.set_exception(RuntimeError("Authentification XMPP refusée"))
            self.disconnect()

        def on_disconnect(self, _):
            """Déconnexion avant la fin de l'envoi : considérée comme un échec.

            Après un envoi réussi, `done` est déjà résolue et rien ne change.
            """
            if not self.done.done():
                self.done.set_exception(RuntimeError("Déconnecté du serveur XMPP"))

    bot = Bot()
    bot.connect()
    # Le délai global couvre la connexion, l'authentification et l'envoi.
    await asyncio.wait_for(bot.done, timeout=timeout)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def ranges_from_post_receive(stdin) -> list[str]:
    """Convertit l'entrée d'un hook post-receive en commits ou plages à analyser.

    Git envoie une ligne « <ancien SHA> <nouveau SHA> <référence> » par branche
    ou tag mis à jour par le push.
    """
    ranges = []
    for line in stdin:
        try:
            old, new, ref = line.split()
        except ValueError:
            # Ligne vide ou mal formée
            continue
        if new == ZERO_SHA:  # suppression de branche
            continue
        # Nouvelle branche : il n'y a pas d'ancien commit, on analyse seulement
        # le dernier. Sinon, tous les commits reçus : « ancien..nouveau ».
        ranges.append(new if old == ZERO_SHA else f"{old}..{new}")
    return ranges


def main() -> int:
    """Point d'entrée : renvoie le code de sortie du script (0 ou 1)."""
    # --- Arguments de la ligne de commande
    # La description reprend le premier paragraphe de la docstring du module.
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--rev", default="HEAD", help="commit à analyser (défaut : HEAD)")
    g.add_argument("--range", help="plage de commits, ex. A..B")
    g.add_argument("--post-receive", action="store_true",
                   help="lit les lignes 'old new ref' sur stdin (hook post-receive)")
    ap.add_argument("-c", "--config", help="fichier de configuration INI")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="affiche les messages sans les envoyer")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # --- Journalisation (sortie redirigée vers .git/grav2xmpp.log par le hook)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [grav2xmpp] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = Config.load(args.config)

    # --- Commits à analyser
    if args.post_receive:
        ranges = ranges_from_post_receive(sys.stdin)
    else:
        ranges = [args.range or args.rev]

    # --- Détection des nouveaux articles
    try:
        articles = [a for r in ranges for a in find_articles(r, cfg)]
    except subprocess.CalledProcessError as exc:
        log.error("Erreur git : %s", exc.stderr.strip())
        return 1

    if not articles:
        # Toujours écrire une ligne, pour voir dans le log que le hook a tourné.
        # Les SHA sont raccourcis, y compris aux deux bouts d'une plage A..B.
        revs = ["..".join(git("rev-parse", "--short", r).strip() for r in rng.split(".."))
                for rng in ranges]
        log.info("Commit %s : aucun nouvel article détecté.",
                 ", ".join(revs) or "(aucun)")
        return 0

    # --- Préparation des messages (un par article)
    messages = [cfg.message.format(title=a.title, url=a.url, path=a.path,
                                   commit=a.commit[:8]) for a in articles]
    for msg in messages:
        log.info("Message → %s :\n%s", cfg.room, msg)

    if args.dry_run:
        return 0
    # Les identifiants ne sont vérifiés qu'ici : --dry-run n'en a pas besoin.
    if not cfg.jid or not cfg.password:
        log.error("JID/mot de passe XMPP manquants (voir grav2xmpp.ini.example).")
        return 1

    # --- Envoi
    try:
        asyncio.run(send_to_muc(cfg, messages))
    except ImportError:
        log.error("Module 'slixmpp' introuvable : pip install slixmpp")
        return 1
    except Exception as exc:
        # Certaines exceptions (TimeoutError…) n'ont pas de message : on
        # affiche alors leur nom.
        log.error("Échec de l'envoi XMPP : %s", exc or type(exc).__name__)
        return 1

    log.info("%d notification(s) envoyée(s).", len(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
