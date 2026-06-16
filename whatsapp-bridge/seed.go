package main

// Anchr fork: seed the chats table for allowlist groups that the bridge
// hasn't received any messages from yet. WhatsApp's post-pairing history
// sync only ships chats with recent activity, so cold groups stay
// invisible to `make allowlist` and to the MCP server's allowlist
// resolver until someone posts. This proactively calls GetGroupInfo for
// each @g.us JID in the allowlist that doesn't yet have a chats row,
// closing that gap.

import (
	"context"
	"database/sql"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// SeedAllowlistGroups looks up any allowlist @g.us JID missing from the
// chats table and inserts a row with the group's real name. Existing
// rows are left in place (name is refreshed via upsert, but
// last_message_time is preserved). Safe to call on every reconnection —
// the existence check makes the steady-state cost a single PK lookup
// per allowlist group.
//
// Intended call site: the *events.Connected handler, in a goroutine so
// GetGroupInfo network latency doesn't block other event processing.
//
// Failures (invalid JID, GetGroupInfo error, SQL error) are logged but
// not propagated — a cold group that fails today will be picked up on
// the next reconnection, or when the first organic message arrives.
func (store *MessageStore) SeedAllowlistGroups(ctx context.Context, client *whatsmeow.Client, logger waLog.Logger) {
	if store == nil || store.allowlist == nil || !store.allowlist.enabled {
		return
	}
	jids := store.allowlist.AllowedGroupJIDs()
	if len(jids) == 0 {
		return
	}

	var seeded, skipped, failed int
	for _, jidStr := range jids {
		var existing string
		err := store.db.QueryRow("SELECT jid FROM chats WHERE jid = ?", jidStr).Scan(&existing)
		switch {
		case err == nil:
			skipped++
			continue
		case err != sql.ErrNoRows:
			logger.Warnf("seed: chats lookup failed for %s: %v", jidStr, err)
			failed++
			continue
		}

		jid, parseErr := types.ParseJID(jidStr)
		if parseErr != nil {
			logger.Warnf("seed: invalid JID in allowlist %q: %v", jidStr, parseErr)
			failed++
			continue
		}

		info, infoErr := client.GetGroupInfo(ctx, jid)
		if infoErr != nil {
			logger.Warnf("seed: GetGroupInfo(%s) failed: %v — row will be created when a message arrives", jidStr, infoErr)
			failed++
			continue
		}

		name := info.Name
		if name == "" {
			// Group exists but has no display name set; still worth a row
			// so `make allowlist` doesn't report UNRESOLVED forever.
			name = "Group " + jid.User
		}

		// Delegate to the bridge's canonical chat upsert (StoreChat in
		// main.go). This is deliberate: handing the row insert back to
		// the upstream helper means any schema additions or write-time
		// invariants vgp introduces later (new columns, triggers, etc.)
		// flow through to seeded rows automatically — we don't have to
		// chase their schema with hand-rolled SQL.
		//
		// Pass time.Time{} so the seeded row's last_message_time stays
		// at the zero value until a real message updates it. StoreChat
		// has monotonic last_message_time handling, so a later real
		// message will correctly overwrite the zero time.
		if insErr := store.StoreChat(jidStr, name, time.Time{}); insErr != nil {
			logger.Errorf("seed: failed to insert chat row for %s (%q): %v", jidStr, name, insErr)
			failed++
			continue
		}
		logger.Infof("seed: %s -> %q", jidStr, name)
		seeded++
	}

	switch {
	case seeded == 0 && failed == 0:
		logger.Infof("seed: all %d allowlist group(s) already present in chats", len(jids))
	default:
		logger.Infof("seed summary: %d new, %d already present, %d failed (of %d allowlist groups)",
			seeded, skipped, failed, len(jids))
	}
}
