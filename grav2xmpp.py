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

DEFAULT_CONFIG_PATHS = [
    os.environ.get("GRAV2XMPP_CONFIG", ""),
    os.path.expanduser("~/.config/grav2xmpp.ini"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "grav2xmpp.ini"),
]

ZERO_SHA = "0" * 40
log = logging.getLogger("grav2xmpp")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    jid: str
    password: str
    room: str = "hacktechdev@conference.tarentule.us"
    nick: str = "GravBot"
    base_url: str = ""
    # Noms de template considérés comme des articles (item.md, item.fr.md…)
    templates: tuple[str, ...] = ("item",)
    # Si renseigné, seules les pages sous ce dossier sont prises en compte
    # (ex. "blog" pour user/pages/01.blog/...)
    blog_folder: str = ""
    message: str = "📝 Nouvel article : {title}\n{url}"

    @classmethod
    def load(cls, path: str | None) -> "Config":
        parser = configparser.ConfigParser(interpolation=None)
        paths = [path] if path else [p for p in DEFAULT_CONFIG_PATHS if p]
        read = parser.read(paths, encoding="utf-8")
        sect = parser["xmpp"] if parser.has_section("xmpp") else {}
        grav = parser["grav"] if parser.has_section("grav") else {}
        if read:
            log.debug("Configuration lue depuis %s", read)

        def get(section, key, env, default=""):
            return os.environ.get(env) or section.get(key, default)

        room = get(sect, "room", "GRAV2XMPP_ROOM", cls.room)
        room = room.removeprefix("xmpp:").split("?")[0]
        templates = get(grav, "templates", "GRAV2XMPP_TEMPLATES", "item")
        message = get(grav, "message", "GRAV2XMPP_MESSAGE", cls.message)

        return cls(
            jid=get(sect, "jid", "GRAV2XMPP_JID"),
            password=get(sect, "password", "GRAV2XMPP_PASSWORD"),
            room=room,
            nick=get(sect, "nick", "GRAV2XMPP_NICK", cls.nick),
            base_url=get(grav, "base_url", "GRAV2XMPP_BASE_URL").rstrip("/"),
            templates=tuple(t.strip() for t in templates.split(",") if t.strip()),
            blog_folder=get(grav, "blog_folder", "GRAV2XMPP_BLOG_FOLDER"),
            message=message.replace("\\n", "\n"),
        )


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #
def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def added_files(rev_range: str) -> list[tuple[str, str]]:
    """Retourne [(commit, chemin)] des fichiers ajoutés dans la plage donnée."""
    if ".." in rev_range:
        commits = git("rev-list", "--reverse", rev_range).split()
    else:
        commits = [git("rev-parse", rev_range).strip()]

    result = []
    for commit in commits:
        out = git(
            "diff-tree", "--root", "--no-commit-id", "-r", "-M",
            "--name-only", "--diff-filter=A", commit,
        )
        result += [(commit, line) for line in out.splitlines() if line]
    return result


def read_blob(commit: str, path: str) -> str:
    return git("show", f"{commit}:{path}")


# --------------------------------------------------------------------------- #
# Grav
# --------------------------------------------------------------------------- #
PREFIX_RE = re.compile(r"^\d+\.")  # "01.blog" -> "blog"


@dataclass
class Article:
    title: str
    url: str
    path: str
    commit: str


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    if not m:
        return {}
    raw = m.group(1)
    try:
        import yaml  # PyYAML, optionnel

        data = yaml.safe_load(raw)
        return data if isinstance(data, dict) else {}
    except ImportError:
        # Repli minimal : clés de premier niveau "cle: valeur"
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
    """Si `path` est une page article Grav, retourne (segments d'URL, langue)."""
    p = PurePosixPath(path)
    parts = p.parts
    if "pages" not in parts:
        return None
    # Nom de fichier : <template>.md ou <template>.<lang>.md
    fm = re.match(r"^([\w-]+?)(?:\.([a-z]{2}(?:-[A-Za-z]{2})?))?\.md$", p.name)
    if not fm or fm.group(1) not in cfg.templates:
        return None

    idx = len(parts) - 1 - parts[::-1].index("pages")
    folders = list(parts[idx + 1:-1])
    if not folders:
        return None
    segments = [PREFIX_RE.sub("", f) for f in folders]
    if cfg.blog_folder and cfg.blog_folder not in segments[:-1]:
        return None
    return segments, fm.group(2) or ""


def build_article(commit: str, path: str, cfg: Config) -> Article | None:
    matched = match_article(path, cfg)
    if not matched:
        return None
    segments, lang = matched

    header = parse_frontmatter(read_blob(commit, path))
    if header.get("published") is False:
        log.info("Ignoré (published: false) : %s", path)
        return None
    if header.get("visible") is False and header.get("published") is None:
        log.debug("Page non visible, mais publiée : %s", path)

    if header.get("slug"):
        segments[-1] = str(header["slug"])
    title = str(header.get("title") or segments[-1].replace("-", " ").capitalize())

    url_path = "/".join(segments)
    if lang:
        url_path = f"{lang}/{url_path}"
    url = f"{cfg.base_url}/{url_path}" if cfg.base_url else f"/{url_path}"
    return Article(title=title, url=url, path=path, commit=commit)


def find_articles(rev_range: str, cfg: Config) -> list[Article]:
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
    import slixmpp

    class Bot(slixmpp.ClientXMPP):
        def __init__(self):
            super().__init__(cfg.jid, cfg.password)
            self.register_plugin("xep_0030")  # Service discovery
            self.register_plugin("xep_0045")  # MUC
            self.register_plugin("xep_0199")  # Ping
            self.done = asyncio.get_running_loop().create_future()
            self.add_event_handler("session_start", self.on_start)
            self.add_event_handler("failed_auth", self.on_fail)
            self.add_event_handler("disconnected", self.on_disconnect)

        async def on_start(self, _):
            try:
                await self.get_roster()
                self.send_presence()
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
            if not self.done.done():
                self.done.set_exception(RuntimeError("Authentification XMPP refusée"))
            self.disconnect()

        def on_disconnect(self, _):
            if not self.done.done():
                self.done.set_exception(RuntimeError("Déconnecté du serveur XMPP"))

    bot = Bot()
    bot.connect()
    await asyncio.wait_for(bot.done, timeout=timeout)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def ranges_from_post_receive(stdin) -> list[str]:
    ranges = []
    for line in stdin:
        try:
            old, new, ref = line.split()
        except ValueError:
            continue
        if new == ZERO_SHA:  # suppression de branche
            continue
        ranges.append(new if old == ZERO_SHA else f"{old}..{new}")
    return ranges


def main() -> int:
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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[grav2xmpp] %(levelname)s %(message)s",
    )
    cfg = Config.load(args.config)

    if args.post_receive:
        ranges = ranges_from_post_receive(sys.stdin)
    else:
        ranges = [args.range or args.rev]

    try:
        articles = [a for r in ranges for a in find_articles(r, cfg)]
    except subprocess.CalledProcessError as exc:
        log.error("Erreur git : %s", exc.stderr.strip())
        return 1

    if not articles:
        log.debug("Aucun nouvel article détecté.")
        return 0

    messages = [cfg.message.format(title=a.title, url=a.url, path=a.path,
                                   commit=a.commit[:8]) for a in articles]
    for msg in messages:
        log.info("Message → %s :\n%s", cfg.room, msg)

    if args.dry_run:
        return 0
    if not cfg.jid or not cfg.password:
        log.error("JID/mot de passe XMPP manquants (voir grav2xmpp.ini.example).")
        return 1

    try:
        asyncio.run(send_to_muc(cfg, messages))
    except ImportError:
        log.error("Module 'slixmpp' introuvable : pip install slixmpp")
        return 1
    except Exception as exc:
        log.error("Échec de l'envoi XMPP : %s", exc or type(exc).__name__)
        return 1

    log.info("%d notification(s) envoyée(s).", len(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
