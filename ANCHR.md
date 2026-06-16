# Anchr fork of `whatsapp-mcp`

This is Anchr's private fork of [`verygoodplugins/whatsapp-mcp`](https://github.com/verygoodplugins/whatsapp-mcp),
which is itself an actively-maintained fork of [`lharries/whatsapp-mcp`](https://github.com/lharries/whatsapp-mcp).
We swapped base from `lharries` → `verygoodplugins` because the latter ships:

- The CDN-auth-token / directPath fix for 403 media downloads on historical
  messages (the only real reason we need a non-upstream client at all).
- LID ↔ phone resolution, full-history-pair, and bearer-token bridge auth.
- Active dependency bumps for `whatsmeow` and Python deps.

Why an Anchr fork at all (vs. just running `verygoodplugins/main`):

1. **Read-only tool surface.** Their MCP server registers `send_message`,
   `send_reaction`, `send_file`, and `send_audio_message`. We strip all four.
   Agents have access to *untrusted message content* + *private chat reads*;
   adding *agent-controlled outbound WhatsApp* completes the
   ["lethal trifecta"](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
   and lets a single prompt injection exfiltrate or impersonate.
2. **Chat allowlist (belt + suspenders).** A `chat-allowlist.txt` at the repo
   root narrows what the bridge writes to disk *and* what the MCP server
   exposes to a connected agent. Enforced in both Go (bridge) and Python (MCP
   server) so an oversight in one layer doesn't open the floodgates.
3. **Configurable bridge port.** Upstream defaults to `8080`, which collides
   with the Casper dev server. We default our local `.env` to `18080`.

Everything else — pairing, sync, search, media downloads — flows straight
through to verygoodplugins' code.

---

## Install runbook

Steps 1–4 are one-time. Steps 5–7 are how you bring it up after a fresh
clone, after a `make update-deps`, or after a reboot.

### 1. Clone and symlink

```bash
git clone git@github.com:ben-anchr/whatsapp-mcp.git ~/Git/whatsapp-mcp
ln -s ~/Git/whatsapp-mcp /path/to/this/casper/workspace/whatsapp-mcp
```

The symlink is so Cursor's project shows the repo without bringing it into
the worktree.

### 2. Toolchain prerequisites

- Go ≥ 1.22 (`brew install go`)
- Python 3.11+ (the MCP server's `.python-version` is authoritative)
- `uv` (`brew install uv`) for the Python venv — see vgp's `README.md`
- `ffmpeg` if you ever expect to play voice notes locally

### 3. Configure environment

```bash
cd ~/Git/whatsapp-mcp
cp .env.example .env
```

Open `.env` and set at least `WHATSAPP_BRIDGE_PORT=18080` (or any free
port). Leave everything else at defaults unless you have a reason.

### 4. Register the MCP server with Cursor

Add the following to your global `mcp.json` (Cursor → Settings → MCP):

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "uv",
      "args": [
        "--directory", "/Users/<you>/Git/whatsapp-mcp/whatsapp-mcp-server",
        "run", "main.py"
      ]
    }
  }
}
```

Restart Cursor. The MCP server will start on its own. The bridge does
**not** auto-start — that's a deliberate choice, see step 6.

### 5. Define your allowlist

```bash
cp chat-allowlist.example.txt chat-allowlist.txt
```

Edit `chat-allowlist.txt` to list the chats the agent is allowed to see.
JIDs are preferred (they survive chat renames); chat names work but resolve
lazily against the bridge's `chats` table. If you don't know a chat's JID,
either:

- Run the bridge once and `make allowlist-resolve NAME="My Chat"` to
  print the JID it found.
- Browse `whatsapp-bridge/store/messages.db` directly with `sqlite3` or
  TablePlus and read the `chats` table.

The file is **gitignored** — your private chat list never enters the public
fork's history.

### 6. Start the bridge

```bash
make bridge
```

This compiles and runs `whatsapp-bridge` in the foreground. On first run
it'll print a QR code; scan it with WhatsApp → Linked Devices → Link a
Device. Subsequent runs reuse the pairing in `whatsapp-bridge/store/`.

To background the bridge instead (e.g. so the terminal can be closed):

```bash
make bridge &
disown
```

Or use vgp's launchd installer at `scripts/install-launchd-macos.sh`
(haven't tested it under Anchr config, YMMV).

### 7. Sanity check

In Cursor, ask the agent:

> List my recent WhatsApp chats.

You should see only the chats you allowlisted. If you see *everything*,
your allowlist isn't being read — check that `chat-allowlist.txt` exists
at the repo root and that the bridge log on startup says
`allowlist active: N JID(s) allowed`.

---

## What's actually different from upstream

| File | Change |
|---|---|
| `whatsapp-bridge/allowlist.go` | **New.** Loads `chat-allowlist.txt` and resolves names → JIDs. |
| `whatsapp-bridge/seed.go` | **New.** On every `*events.Connected`, fetches `GetGroupInfo` for any allowlist `@g.us` JID missing from the `chats` table and inserts a row. Closes the cold-cache gap where WhatsApp's history sync doesn't ship groups with no recent traffic, so `make allowlist` reports the real name immediately instead of `UNRESOLVED`. |
| `whatsapp-bridge/main.go` | `MessageStore` grows an `allowlist` field + `SetAllowlist`; `StoreMessage` short-circuits when the chat isn't allowed; `main()` calls `LoadAllowlist` post-init; `*events.Connected` handler triggers `SeedAllowlistGroups` in a goroutine. |
| `whatsapp-mcp-server/allowlist.py` | **New.** Mirrors the Go loader; also exposes `ChatNotAllowed` and `enforce()`. |
| `whatsapp-mcp-server/main.py` | Imports for `send_*` removed. Four `@mcp.tool()` registrations (`send_message`, `send_reaction`, `send_file`, `send_audio_message`) deleted. `enforce()` / allowlist filter calls added to every read tool that exposes chat content. |
| `chat-allowlist.example.txt` | **New.** Schema + safe placeholders. |
| `chat-allowlist.txt` | **Gitignored.** Your private chat list. |
| `.gitignore` | `chat-allowlist.txt` added. |
| `Makefile` | **New.** Operational ergonomics (see below). |
| `ANCHR.md` | This file. |

The git log keeps upstream verygoodplugins' history intact; our changes
land as squashed Anchr commits on top.

---

## Operations cheat sheet

```bash
make help                # list all targets
make bridge              # foreground bridge (first run = QR pairing)
make kill-bridge         # stop a running bridge cleanly
make restart-bridge      # kill + start
make build               # compile bridge binary without running it
make update-deps         # go get -u whatsmeow + go mod tidy + go build
make reset-pairing       # nuke whatsapp-bridge/store/whatsapp.db (re-pair)
make doctor              # print env vars + bridge state for triage

make allowlist           # show current allowlist (resolved + pending)
make allowlist-resolve NAME="Some Chat"
                         # look up a chat's JID for the allowlist
make allowlist-cleanup   # delete already-stored messages for chats not on
                         # the list (use after tightening the allowlist)
make allowlist-cleanup-dry
                         # preview what cleanup would delete
```

---

## Troubleshooting

### `Client outdated (405)` on QR scan

Bump `whatsmeow`: `make update-deps`. We're on a `verygoodplugins` base so
this should be rare — they keep `go.mod` reasonably fresh — but if a couple
of months pass between pairings, the WhatsApp server may reject older
clients.

### Bridge can't bind to port 8080

Edit `.env` to set `WHATSAPP_BRIDGE_PORT=18080` (or another free port).
Restart the bridge *and* restart Cursor so the MCP server picks up the new
port. Verify with `make doctor`.

### MCP returns *everything*, ignoring the allowlist

Check the bridge log at startup. If you see:

```
allowlist: …/chat-allowlist.txt not present — bridge will STORE ALL CHATS.
```

…the file isn't at the repo root, or `chat-allowlist.example.txt` was
copied to a different name. The MCP server logs the same warning.

### MCP returns empty / `ChatNotAllowed`

Your allowlist *is* enforcing, but the chat you asked for isn't on it. Add
the chat (by JID, ideally), restart the MCP via Cursor's MCP panel, and
retry. Restart is required because the Python module caches `ALLOWLIST`
on import.

### `download_media` returns 403

Should not happen on this base — vgp fixed it in
[#132](https://github.com/verygoodplugins/whatsapp-mcp/pull/132). If you
still see it, the message likely predates your pairing *and* the phone
isn't online to re-upload. Open the chat on phone briefly and retry.

### `make allowlist` lists a name as `UNRESOLVED`

For **group** entries (`@g.us`): shouldn't happen after the bridge has
connected once — `SeedAllowlistGroups` (`whatsapp-bridge/seed.go`) fires
on every `*events.Connected` and proactively populates the `chats` row
via `GetGroupInfo`. If you see UNRESOLVED, check the bridge log around
startup for a `seed:` line — `GetGroupInfo` might have failed (wrong
JID? group you've been removed from?) and there'll be a clear error.

For **direct chat** entries (`@s.whatsapp.net`): no proactive seed; the
row is created when the first message in either direction is observed.
Send one message and `make allowlist` will pick it up.

If the name in your file doesn't match the name in `chats.name` exactly
(Unicode apostrophes are a classic foot-gun), `make allowlist-resolve
NAME='…'` falls back to a fuzzy `LIKE` lookup so you can see the actual
stored name. Prefer JID entries for stability.

---

## Updating from upstream

```bash
git fetch verygoodplugins main
git rebase verygoodplugins/main           # or merge, depending on the squash level you want
make update-deps && make build            # confirm Go still compiles
make bridge                               # smoke test
```

If upstream re-introduces `send_*` tool registrations in `main.py`, strip
them again (see the top of that file for the deliberate omission comment).
