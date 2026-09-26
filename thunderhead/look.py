"""How THUNDERHEAD looks in Discord: one set of colors and card builders for every message.

The side bar's color says who's talking (the lead, a supervisor, a dev) or what kind of message it
is (needs you, an approval, an outcome, agents talking, lifecycle).
"""
import io
from datetime import datetime, timezone

import discord

from . import db as store

LEAD = 0xF1C40F        # gold: The Thunderhead
SUPERVISOR = 0x9B59B6  # purple: team supervisors, and team events
DEV = 0x5865F2         # blurple: devs and sessions without a team
NEEDS_YOU = 0xE67E22   # amber: waiting on the human
APPROVAL = 0xF39C12    # orange: a decision to make
GOOD = 0x2ECC71        # green: approved, done
BAD = 0xE74C3C         # red: denied, rejected, failed
AGENTS = 0x1ABC9C      # teal: sessions talking to each other
QUIET = 0x95A5A6       # grey: lifecycle notices
BOARD = 0x2B2D31       # near-black: the board

ICONS = {"starting": "⏳", "working": "🟢", "listening": "🔵", "idle": "⚪", "needs_you": "🔴",
         "waking": "⏰", "sleeping": "💤", "stopped": "⏹️", "ended": "⚫", "gone": "⚫", "deleted": "🗑️"}

DESC_LIMIT = 4000   # Discord allows 4096 in a description; leave room
MESSAGE_LIMIT = 5800  # and 6000 across all embeds in one message


def now():
    return datetime.now(timezone.utc)


def role_color(conn, name: str) -> int:
    role, _ = store.rank(conn, name)
    return {"lead": LEAD, "supervisor": SUPERVISOR}.get(role, DEV)


def who(conn, name: str) -> str:
    """How a session is named in a card's header: its name, plus where it sits."""
    role, team = store.rank(conn, name)
    return {"lead": f"👑 {name}", "supervisor": f"🧭 {name} · {team} supervisor",
            "dev": f"🛠️ {name} · {team}"}.get(role, f"🛠️ {name}")


def card(text: str = "", *, color: int = QUIET, title: str | None = None, author: str | None = None,
         fields=(), footer: str | None = None, stamp: bool = True, url: str | None = None) -> discord.Embed:
    e = discord.Embed(description=text[:DESC_LIMIT] or None, color=color, title=title,
                      timestamp=now() if stamp else None, url=url)
    if author:
        e.set_author(name=author[:250])
    for name, value, *inline in fields:
        e.add_field(name=name[:250], value=(value or "—")[:1024], inline=bool(inline and inline[0]))
    if footer:
        e.set_footer(text=footer[:2000])
    return e


def note(text: str, color: int = QUIET) -> discord.Embed:
    """A one-line notice with no title or timestamp: lifecycle events and the like."""
    return card(text, color=color, stamp=False)


def split(text: str, limit: int = DESC_LIMIT) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + ([text] if text else [])


def long_card(text: str, **kw) -> tuple[list[discord.Embed], discord.File | None]:
    """Embeds for text of any length: one card, a card plus continuation, or a preview plus a file."""
    if len(text) <= DESC_LIMIT:
        return [card(text, **kw)], None
    if len(text) <= MESSAGE_LIMIT - 400:
        first, *rest = split(text)
        cont = {k: v for k, v in kw.items() if k == "color"}
        return [card(first, **{**kw, "stamp": False})] + [card(p, **cont) for p in rest], None
    preview = text[:3500].rsplit("\n", 1)[0] + "\n\n*… full text attached*"
    return [card(preview, **kw)], discord.File(io.BytesIO(text.encode()), filename="full-text.md")


async def send_card(channel, text: str, view=None, content: str | None = None, **kw):
    embeds, file = long_card(text, **kw)
    extra = {"file": file} if file else {}
    if view is not None:
        extra["view"] = view
    return await channel.send(content=content, embeds=embeds, **extra)
