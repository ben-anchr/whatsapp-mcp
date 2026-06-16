"""Anchr fork: chat allowlist for the Python MCP server.

Pairs with the Go bridge's allowlist (whatsapp-bridge/allowlist.go) as the
"suspenders" half of belt-and-suspenders enforcement: the bridge drops
disallowed chats before they're persisted, and this module filters them
again in every read tool the MCP server exposes. Both layers read the
same file (../chat-allowlist.txt), so they stay consistent.

When chat-allowlist.txt is absent, the MCP server logs a warning and
falls back to upstream behavior (all chats exposed). See ANCHR.md for
the lethal-trifecta rationale.

Plain-text format (one entry per line, '#' comments + blank lines OK):
    # comment
    Engineering Standup        ← matched against chats.name (case-insensitive)
    120363999999999999@g.us    ← JID (contains '@'); preferred for stability
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALLOWLIST_FILE = _REPO_ROOT / "chat-allowlist.txt"

# Respect the verygoodplugins WHATSAPP_DB_PATH env override (set via .env)
# if present; otherwise fall back to the default location the bridge writes.
_MESSAGES_DB = Path(
    os.environ.get(
        "WHATSAPP_DB_PATH",
        str(_REPO_ROOT / "whatsapp-bridge" / "store" / "messages.db"),
    )
)


def _parse_entries(path: Path) -> list[str]:
    entries: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def _resolve_names_to_jids(name_entries: list[str]) -> tuple[set[str], list[str]]:
    """Look up each name against the chats table and return (resolved, unresolved)."""
    resolved: set[str] = set()
    unresolved: list[str] = []

    if not _MESSAGES_DB.exists():
        logger.error(
            "allowlist: bridge messages DB not found at %s — name-based entries cannot resolve. "
            "Start the bridge and let it sync before relying on names.",
            _MESSAGES_DB,
        )
        return resolved, name_entries[:]

    conn = sqlite3.connect(f"file:{_MESSAGES_DB}?mode=ro", uri=True)
    try:
        for name in name_entries:
            row = conn.execute(
                "SELECT jid FROM chats WHERE name = ? COLLATE NOCASE LIMIT 1",
                (name,),
            ).fetchone()
            if row and row[0]:
                resolved.add(row[0])
            else:
                unresolved.append(name)
    finally:
        conn.close()
    return resolved, unresolved


class Allowlist:
    def __init__(self, enabled: bool, allowed_jids: set[str]):
        self.enabled = enabled
        self.allowed_jids = allowed_jids

    @classmethod
    def load(cls) -> "Allowlist":
        if not _ALLOWLIST_FILE.exists():
            logger.warning(
                "allowlist: %s not present — MCP server has access to ALL chats. "
                "Create the file (see chat-allowlist.example.txt) to restrict.",
                _ALLOWLIST_FILE,
            )
            return cls(enabled=False, allowed_jids=set())

        entries = _parse_entries(_ALLOWLIST_FILE)
        if not entries:
            logger.warning(
                "allowlist: %s is empty — enforcement disabled (allow-all).", _ALLOWLIST_FILE,
            )
            return cls(enabled=False, allowed_jids=set())

        jid_entries = [e for e in entries if "@" in e]
        name_entries = [e for e in entries if "@" not in e]

        allowed_jids: set[str] = set(jid_entries)
        if name_entries:
            resolved, unresolved = _resolve_names_to_jids(name_entries)
            allowed_jids.update(resolved)
            if unresolved:
                logger.warning(
                    "allowlist: %d name(s) not found in chats table "
                    "(chat not synced yet, or name mismatch): %s. "
                    "Restart the MCP server after the bridge has synced these chats.",
                    len(unresolved), ", ".join(unresolved),
                )

        if not allowed_jids:
            logger.error(
                "allowlist: file present but resolves to ZERO chats. "
                "All MCP tool calls will return empty / refuse. "
                "Check chat names / JIDs."
            )
        else:
            logger.info(
                "allowlist active: %d chat(s) allowed: %s",
                len(allowed_jids), ", ".join(sorted(allowed_jids)),
            )
        return cls(enabled=True, allowed_jids=allowed_jids)

    def is_allowed(self, jid: Optional[str]) -> bool:
        if not self.enabled:
            return True
        if not jid:
            return False
        return jid in self.allowed_jids

    def filter_chat_jids(self, jids: Iterable[Optional[str]]) -> list[str]:
        if not self.enabled:
            return [j for j in jids if j]
        return [j for j in jids if j and j in self.allowed_jids]


# Module-level singleton; reload by restarting the MCP server (Cursor restart).
ALLOWLIST = Allowlist.load()


class ChatNotAllowed(Exception):
    """Raised when a tool call references a chat outside the allowlist."""


def enforce(jid: Optional[str]) -> None:
    """Raise ChatNotAllowed if the JID is not in the allowlist."""
    if not ALLOWLIST.is_allowed(jid):
        raise ChatNotAllowed(
            f"Chat {jid!r} is not in the MCP allowlist. "
            "Edit chat-allowlist.txt to grant access."
        )


# ---------------------------------------------------------------------------
# CLI: `python allowlist.py {show|resolve|cleanup}` — invoked via the Makefile
# ---------------------------------------------------------------------------

def _cli_show() -> int:
    if not _ALLOWLIST_FILE.exists():
        print(f"{_ALLOWLIST_FILE}: not present (allow-all)")
        return 0
    entries = _parse_entries(_ALLOWLIST_FILE)
    jid_entries = [e for e in entries if "@" in e]
    name_entries = [e for e in entries if "@" not in e]
    print(f"{_ALLOWLIST_FILE}: {len(entries)} entr{'y' if len(entries)==1 else 'ies'}")
    if jid_entries:
        print("  JIDs:")
        for j in jid_entries:
            print(f"    {j}")
    if name_entries:
        resolved, unresolved = _resolve_names_to_jids(name_entries)
        print("  Names:")
        for n in name_entries:
            row_jid = None
            if _MESSAGES_DB.exists():
                conn = sqlite3.connect(f"file:{_MESSAGES_DB}?mode=ro", uri=True)
                try:
                    r = conn.execute(
                        "SELECT jid FROM chats WHERE name = ? COLLATE NOCASE LIMIT 1",
                        (n,),
                    ).fetchone()
                    if r and r[0]:
                        row_jid = r[0]
                finally:
                    conn.close()
            if row_jid:
                print(f"    {n!r} -> {row_jid}")
            else:
                print(f"    {n!r} -> UNRESOLVED")
        if unresolved:
            print(f"  ({len(unresolved)} name(s) not yet seen in chats table)")
    return 0


def _cli_resolve(name: str) -> int:
    if not _MESSAGES_DB.exists():
        print(f"messages.db not found at {_MESSAGES_DB}; start the bridge first.")
        return 2
    conn = sqlite3.connect(f"file:{_MESSAGES_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT jid, name FROM chats WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchall()
        if not rows:
            # Fall back to a fuzzy LIKE so trailing whitespace / Unicode
            # apostrophe differences don't silently leave names unresolved.
            rows = conn.execute(
                "SELECT jid, name FROM chats WHERE name LIKE ? COLLATE NOCASE LIMIT 5",
                (f"%{name}%",),
            ).fetchall()
            if rows:
                print(f"No exact match for {name!r}. Fuzzy candidates:")
            else:
                print(f"No chat found matching {name!r}.")
                return 1
        for jid, n in rows:
            print(f"  {n!r} -> {jid}")
    finally:
        conn.close()
    return 0


def _cli_cleanup(dry_run: bool) -> int:
    if not _MESSAGES_DB.exists():
        print(f"messages.db not found at {_MESSAGES_DB}; nothing to clean.")
        return 0
    al = Allowlist.load()
    if not al.enabled or not al.allowed_jids:
        print("Allowlist is not enabled or has zero resolved JIDs; refusing to wipe everything.")
        return 2
    conn = sqlite3.connect(_MESSAGES_DB)
    try:
        placeholders = ",".join("?" for _ in al.allowed_jids)
        params = list(al.allowed_jids)
        # Count first so we can preview / log impact.
        msg_count = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE chat_jid NOT IN ({placeholders})",
            params,
        ).fetchone()[0]
        chat_count = conn.execute(
            f"SELECT COUNT(*) FROM chats WHERE jid NOT IN ({placeholders})",
            params,
        ).fetchone()[0]
        if dry_run:
            print(f"[dry-run] would delete {msg_count} message(s) across {chat_count} chat(s).")
            return 0
        if msg_count == 0 and chat_count == 0:
            print("Nothing to clean — store is already aligned with the allowlist.")
            return 0
        conn.execute(
            f"DELETE FROM messages WHERE chat_jid NOT IN ({placeholders})",
            params,
        )
        conn.execute(
            f"DELETE FROM chats WHERE jid NOT IN ({placeholders})",
            params,
        )
        conn.commit()
        print(f"Deleted {msg_count} message(s) and {chat_count} chat row(s).")
    finally:
        conn.close()
    return 0


def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="allowlist.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print current allowlist + resolution status")
    resolve = sub.add_parser("resolve", help="look up a chat's JID by name")
    resolve.add_argument("name", help="chat name (exact, case-insensitive; falls back to fuzzy)")
    cleanup = sub.add_parser("cleanup", help="delete stored messages for chats not on the allowlist")
    cleanup.add_argument("--dry-run", action="store_true", help="preview without deleting")
    args = parser.parse_args(argv)

    if args.cmd == "show":
        return _cli_show()
    if args.cmd == "resolve":
        return _cli_resolve(args.name)
    if args.cmd == "cleanup":
        return _cli_cleanup(args.dry_run)
    return 2


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_cli(sys.argv[1:]))
