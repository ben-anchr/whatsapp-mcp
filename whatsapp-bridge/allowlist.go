package main

// Anchr fork: chat allowlist enforced at the bridge level (belt-and-suspenders
// pairing with the Python MCP server's allowlist). When chat-allowlist.txt
// is present at the repo root, the bridge silently drops messages from any
// chat not on the list before they're written to messages.db. See ANCHR.md.

import (
	"bufio"
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"sync"

	waLog "go.mau.fi/whatsmeow/util/log"
)

// Allowlist gates which chats are written to messages.db.
//
// File format: one entry per line; '#' and blank lines ignored. Each entry
// is either a JID (contains '@') or a chat name (matched case-insensitively
// against the chats table). Names are resolved lazily — if a name in the
// file isn't in chats yet at startup, it'll be promoted to an allowed JID
// the first time a chat with that name appears.
type Allowlist struct {
	mu           sync.RWMutex
	enabled      bool
	allowedJIDs  map[string]bool
	pendingNames map[string]bool // lowercased names not yet resolved
	logger       waLog.Logger
}

// LoadAllowlist reads chat-allowlist.txt from the repo root (one dir up
// from the whatsapp-bridge cwd) and resolves any name entries against the
// chats table that's already populated by prior runs.
//
// If the file is absent, returns a disabled allowlist (allow-all) with a
// loud warning. Use chat-allowlist.example.txt for the schema.
func LoadAllowlist(db *sql.DB, logger waLog.Logger) *Allowlist {
	al := &Allowlist{
		allowedJIDs:  make(map[string]bool),
		pendingNames: make(map[string]bool),
		logger:       logger,
	}

	cwd, err := os.Getwd()
	if err != nil {
		logger.Warnf("allowlist: cannot determine cwd; allow-all: %v", err)
		return al
	}
	path := filepath.Join(filepath.Dir(cwd), "chat-allowlist.txt")

	file, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			logger.Warnf("allowlist: %s not present — bridge will STORE ALL CHATS. "+
				"Create the file (see chat-allowlist.example.txt) to restrict.", path)
			return al
		}
		logger.Errorf("allowlist: cannot read %s: %v — allow-all", path, err)
		return al
	}
	defer file.Close()

	al.enabled = true

	var nameLookups []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.Contains(line, "@") {
			al.allowedJIDs[line] = true
			continue
		}
		nameLookups = append(nameLookups, line)
	}
	if err := scanner.Err(); err != nil {
		logger.Errorf("allowlist: read error on %s: %v", path, err)
	}

	// Resolve name entries against the existing chats table.
	if db != nil && len(nameLookups) > 0 {
		for _, name := range nameLookups {
			var jid sql.NullString
			err := db.QueryRow(
				"SELECT jid FROM chats WHERE name = ? COLLATE NOCASE LIMIT 1",
				name,
			).Scan(&jid)
			if err == sql.ErrNoRows || !jid.Valid {
				al.pendingNames[strings.ToLower(name)] = true
				continue
			}
			if err != nil {
				logger.Warnf("allowlist: name lookup failed for %q: %v", name, err)
				al.pendingNames[strings.ToLower(name)] = true
				continue
			}
			al.allowedJIDs[jid.String] = true
			logger.Infof("allowlist: resolved %q -> %s", name, jid.String)
		}
	} else if len(nameLookups) > 0 {
		for _, name := range nameLookups {
			al.pendingNames[strings.ToLower(name)] = true
		}
	}

	if len(al.pendingNames) > 0 {
		pending := make([]string, 0, len(al.pendingNames))
		for n := range al.pendingNames {
			pending = append(pending, n)
		}
		logger.Warnf("allowlist: %d name(s) not yet seen in chats table "+
			"(will be promoted on first matching message): %s",
			len(al.pendingNames), strings.Join(pending, ", "))
	}

	logger.Infof("allowlist active: %d JID(s) allowed, %d pending name(s)",
		len(al.allowedJIDs), len(al.pendingNames))

	return al
}

// IsAllowed returns true when a chat with the given JID and (optional) name
// should be written to messages.db. Lazily resolves pending names — the
// first time a chat with a pending name comes through, its JID is promoted
// into the allowed set so subsequent messages skip the name lookup.
func (al *Allowlist) IsAllowed(jid, name string) bool {
	if !al.enabled {
		return true
	}

	al.mu.RLock()
	if al.allowedJIDs[jid] {
		al.mu.RUnlock()
		return true
	}
	nameMatchPending := false
	if name != "" {
		if al.pendingNames[strings.ToLower(name)] {
			nameMatchPending = true
		}
	}
	al.mu.RUnlock()

	if nameMatchPending {
		al.mu.Lock()
		al.allowedJIDs[jid] = true
		delete(al.pendingNames, strings.ToLower(name))
		al.mu.Unlock()
		al.logger.Infof("allowlist: lazy-resolved %q -> %s", name, jid)
		return true
	}
	return false
}
