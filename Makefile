# Anchr fork operations for verygoodplugins/whatsapp-mcp.
# See ANCHR.md for context.

SHELL := bash
PYTHON ?= python3

BRIDGE_DIR := whatsapp-bridge
MCP_DIR := whatsapp-mcp-server
ALLOWLIST_FILE := chat-allowlist.txt
ALLOWLIST_PY := $(MCP_DIR)/allowlist.py

# Auto-source .env for bridge subprocesses so $WHATSAPP_BRIDGE_PORT (and
# anything else in there) flows into `go run`. vgp's main.go reads the
# OS env but doesn't load .env itself. Wrap the run command with a shell
# snippet that sources .env if it exists.
RUN_WITH_ENV := set -a; [ -f ../.env ] && . ../.env; set +a;

# Match any bridge binary by its name `whatsapp-client` — covers both
# `go run .` (which compiles to a temp exe of that name) and any
# `make build` binary at whatsapp-bridge/whatsapp-client, no matter where
# Go's build cache stashed it (~/Library/Caches/go-build/..., /var/folders/...,
# etc.). The [w] bracket trick is the classic pgrep self-match dodge:
# the regex matches literal "whatsapp-client", but pgrep's OWN argv
# contains the literal "[w]hatsapp-client" (brackets included) which the
# regex won't match. Avoids the kind of orphan pileup that triggered a
# stream-replaced ping-pong during the vgp rebase.
BRIDGE_PGREP := pgrep -f '[w]hatsapp-client'

.PHONY: help bridge kill-bridge restart-bridge build update-deps \
        reset-pairing doctor \
        allowlist allowlist-resolve allowlist-cleanup allowlist-cleanup-dry

help:
	@echo "Anchr WhatsApp MCP — Makefile targets"
	@echo ""
	@echo "  make bridge                  start bridge in foreground (QR on first run)"
	@echo "  make kill-bridge             kill any running bridge"
	@echo "  make restart-bridge          kill + start"
	@echo "  make build                   compile bridge binary (no run)"
	@echo "  make update-deps             bump go deps + rebuild"
	@echo "  make reset-pairing           drop whatsmeow store; force re-pair"
	@echo "  make doctor                  print env + bridge state for triage"
	@echo ""
	@echo "  make allowlist               show current allowlist"
	@echo "  make allowlist-resolve NAME='Chat Name'"
	@echo "                               look up the JID for a chat name"
	@echo "  make allowlist-cleanup       delete stored messages for chats NOT on the allowlist"
	@echo "  make allowlist-cleanup-dry   preview what cleanup would delete"

bridge:
	cd $(BRIDGE_DIR) && $(RUN_WITH_ENV) go run .

kill-bridge:
	@pids="$$($(BRIDGE_PGREP) 2>/dev/null | tr '\n' ' ')"; \
	if [ -z "$$pids" ]; then \
		echo "no bridge processes found"; \
	else \
		echo "Killing bridge (pids:$$pids)"; \
		kill $$pids 2>/dev/null; \
		sleep 1; \
		stragglers="$$($(BRIDGE_PGREP) 2>/dev/null | tr '\n' ' ')"; \
		if [ -n "$$stragglers" ]; then \
			echo "force-killing stragglers (pids:$$stragglers)"; \
			kill -9 $$stragglers 2>/dev/null; \
		fi; \
	fi

restart-bridge: kill-bridge
	@sleep 1
	$(MAKE) -s bridge

build:
	cd $(BRIDGE_DIR) && go build -o whatsapp-bridge .

update-deps:
	cd $(BRIDGE_DIR) && go get -u go.mau.fi/whatsmeow@latest && go mod tidy && go build -o whatsapp-bridge .

reset-pairing:
	@echo "This will require re-scanning a QR code on next bridge start."
	@read -p "Continue? [y/N] " ans && [ "$$ans" = "y" ] || (echo "aborted" && exit 1)
	rm -f $(BRIDGE_DIR)/store/whatsapp.db
	rm -f $(BRIDGE_DIR)/store/whatsapp.db-shm
	rm -f $(BRIDGE_DIR)/store/whatsapp.db-wal
	@echo "Pairing reset. Run 'make bridge' to re-pair."

doctor:
	@echo "=== env ==="
	@if [ -f .env ]; then \
		grep -E '^(WHATSAPP_|WEBHOOK_)' .env || echo "(no WHATSAPP_/WEBHOOK_ vars in .env)"; \
	else \
		echo "(no .env — using upstream defaults)"; \
	fi
	@echo ""
	@echo "=== allowlist ==="
	@if [ -f $(ALLOWLIST_FILE) ]; then \
		echo "$(ALLOWLIST_FILE) present ($$(grep -v '^#' $(ALLOWLIST_FILE) | grep -v '^[[:space:]]*$$' | wc -l | tr -d ' ') entries)"; \
	else \
		echo "$(ALLOWLIST_FILE) MISSING — bridge + MCP will allow all chats"; \
	fi
	@echo ""
	@echo "=== bridge state ==="
	@pids="$$($(BRIDGE_PGREP) 2>/dev/null | tr '\n' ' ')"; \
	if [ -z "$$pids" ]; then echo "no bridge running"; else echo "running (pids:$$pids)"; fi
	@echo ""
	@echo "=== store ==="
	@ls -lh $(BRIDGE_DIR)/store 2>/dev/null || echo "(no store/ yet — bridge hasn't started)"

allowlist:
	cd $(MCP_DIR) && $(PYTHON) allowlist.py show

allowlist-resolve:
	@if [ -z "$(NAME)" ]; then echo "usage: make allowlist-resolve NAME='Chat Name'"; exit 1; fi
	cd $(MCP_DIR) && $(PYTHON) allowlist.py resolve "$(NAME)"

allowlist-cleanup:
	@pids="$$($(BRIDGE_PGREP) 2>/dev/null | tr '\n' ' ')"; \
	if [ -n "$$pids" ]; then \
		echo "ERROR: bridge is running (pids:$$pids). Stop it first ('make kill-bridge') so cleanup doesn't race writes."; \
		exit 1; \
	fi
	cd $(MCP_DIR) && $(PYTHON) allowlist.py cleanup

allowlist-cleanup-dry:
	cd $(MCP_DIR) && $(PYTHON) allowlist.py cleanup --dry-run
