package main

// Anchr fork: on-demand media-retry-receipt fallback for download_media.
//
// When client.Download returns a 403/404/410 from WhatsApp's CDN — which
// happens for media we received in history-sync (no decryption key) or
// for older media whose signed URL has expired — we ask the sender's
// phone to re-upload via SendMediaRetryReceipt. The response arrives as
// an *events.MediaRetry through the main event handler; we deliver it
// to the waiting download attempt via this package's registry, decrypt
// the notification with the original media key, update the message's
// stored directPath, and retry the download once.
//
// All of this is silent on the sender's side — it's the same protocol
// WhatsApp uses internally when their own web client encounters expired
// media URLs. See ANCHR.md for the broader rationale.

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// mediaRetryWaitTimeout bounds how long we wait for the sender's phone
// to respond. Online phones typically respond in 1-3 seconds; offline
// phones queue the receipt server-side and respond when they next sync.
// 30s is a balance between "give a stale-but-online phone time" and
// "don't hang the agent's tool call".
const mediaRetryWaitTimeout = 30 * time.Second

// shouldRetryDownloadErr returns true for the three error classes that
// retry receipts can actually fix: expired CDN tokens (403) and the two
// "media is gone from the CDN" responses (404, 410). Other errors —
// decryption failures, connection drops, malformed descriptors — won't
// be helped by a retry, so we don't burn the timeout on them.
func shouldRetryDownloadErr(err error) bool {
	if err == nil {
		return false
	}
	return errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith403) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410)
}

// MediaRetryRegistry tracks pending retry receipts so the *events.MediaRetry
// case in the main event handler can wake up the right download attempt.
// Lifetime is the bridge process; entries are scoped to a single in-flight
// request.
type MediaRetryRegistry struct {
	mu      sync.Mutex
	waiters map[string]chan *events.MediaRetry
}

func NewMediaRetryRegistry() *MediaRetryRegistry {
	return &MediaRetryRegistry{waiters: make(map[string]chan *events.MediaRetry)}
}

// Register reserves a slot for the given messageID and returns a buffered
// channel that Deliver writes into. The caller MUST Unregister, even on
// timeout, to free the slot.
func (r *MediaRetryRegistry) Register(messageID string) chan *events.MediaRetry {
	ch := make(chan *events.MediaRetry, 1)
	r.mu.Lock()
	r.waiters[messageID] = ch
	r.mu.Unlock()
	return ch
}

func (r *MediaRetryRegistry) Unregister(messageID string) {
	r.mu.Lock()
	delete(r.waiters, messageID)
	r.mu.Unlock()
}

// Deliver routes an incoming *events.MediaRetry to a waiting Register
// caller. Called from the main event handler. No-op if nobody's waiting
// — that happens if the requesting goroutine timed out and exited
// before the phone responded.
func (r *MediaRetryRegistry) Deliver(evt *events.MediaRetry) {
	r.mu.Lock()
	ch, ok := r.waiters[string(evt.MessageID)]
	r.mu.Unlock()
	if !ok {
		return
	}
	select {
	case ch <- evt:
	default:
		// Channel buffer already has an event — caller will pick the
		// first one, this is a duplicate retry response. Discard.
	}
}

// mediaRetryRegistry is the bridge-wide singleton. main.go's
// *events.MediaRetry handler calls Deliver here; requestMediaRetry below
// calls Register/Unregister.
var mediaRetryRegistry = NewMediaRetryRegistry()

// requestMediaRetry asks the sender's phone to re-upload media for a
// given message and waits up to mediaRetryWaitTimeout for the response.
// On SUCCESS, returns the new directPath; the caller is expected to
// rebuild the URL and persist the updated descriptor before retrying
// client.Download.
//
// Failure modes returned as plain errors:
//   - client disconnected
//   - message not found in store
//   - missing mediaKey (can't construct the receipt)
//   - SendMediaRetryReceipt RPC failure
//   - notification carried a MediaRetryError code
//   - notification decrypted but Result != SUCCESS
//   - timeout (phone offline / not responding)
func requestMediaRetry(ctx context.Context, client *whatsmeow.Client, store *MessageStore, messageID, chatJID string, logger waLog.Logger) (newDirectPath string, err error) {
	if client == nil || !client.IsConnected() {
		return "", fmt.Errorf("client is not connected")
	}

	var sender string
	var isFromMe bool
	var mediaKey []byte
	var timestamp time.Time
	err = store.db.QueryRow(
		"SELECT sender, is_from_me, media_key, timestamp FROM messages WHERE id = ? AND chat_jid = ?",
		messageID, chatJID,
	).Scan(&sender, &isFromMe, &mediaKey, &timestamp)
	if errors.Is(err, sql.ErrNoRows) {
		return "", fmt.Errorf("message %s in chat %s not found in store", messageID, chatJID)
	}
	if err != nil {
		return "", fmt.Errorf("failed to load message metadata: %w", err)
	}
	if len(mediaKey) == 0 {
		return "", fmt.Errorf("message has no mediaKey; cannot construct retry receipt")
	}

	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return "", fmt.Errorf("invalid chat JID %q: %w", chatJID, err)
	}
	isGroup := chat.Server == types.GroupServer

	var senderJID types.JID
	if sender != "" {
		senderJID, err = types.ParseJID(sender)
		if err != nil {
			return "", fmt.Errorf("invalid sender JID %q: %w", sender, err)
		}
	}

	info := &types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:     chat,
			Sender:   senderJID,
			IsFromMe: isFromMe,
			IsGroup:  isGroup,
		},
		ID:        messageID,
		Timestamp: timestamp,
	}

	// Register BEFORE sending so a fast phone response can't race us
	// (we'd otherwise miss the event and wait the full timeout).
	ch := mediaRetryRegistry.Register(messageID)
	defer mediaRetryRegistry.Unregister(messageID)

	logger.Infof("media-retry: requesting re-upload for message %s in chat %s", messageID, chatJID)
	if err := client.SendMediaRetryReceipt(ctx, info, mediaKey); err != nil {
		return "", fmt.Errorf("SendMediaRetryReceipt failed: %w", err)
	}

	select {
	case evt := <-ch:
		if evt == nil {
			return "", fmt.Errorf("media retry returned empty event")
		}
		if evt.Error != nil {
			// Code 2 = media not available on phone (sender cleared the
			// chat / app data). Other codes are protocol errors.
			return "", fmt.Errorf("media retry error code %d", evt.Error.Code)
		}
		notif, decErr := whatsmeow.DecryptMediaRetryNotification(evt, mediaKey)
		if decErr != nil {
			return "", fmt.Errorf("decrypt media retry notification: %w", decErr)
		}
		if notif.GetResult() != waMmsRetry.MediaRetryNotification_SUCCESS {
			return "", fmt.Errorf("media retry result was %s, not SUCCESS", notif.GetResult())
		}
		directPath := notif.GetDirectPath()
		if directPath == "" {
			return "", fmt.Errorf("media retry succeeded but DirectPath was empty")
		}
		logger.Infof("media-retry: success for message %s", messageID)
		return directPath, nil
	case <-time.After(mediaRetryWaitTimeout):
		return "", fmt.Errorf("media retry timed out after %s (sender phone likely offline)", mediaRetryWaitTimeout)
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

// rebuildMediaURL constructs a WhatsApp CDN URL from a directPath
// fragment so it round-trips through extractDirectPathFromURL in
// main.go (which slices on ".net/"). The reconstructed URL won't carry
// the original CDN auth query params, but that's fine: whatsmeow's
// Download path attaches its own auth hash via the directPath the
// downloader exposes.
func rebuildMediaURL(directPath string) string {
	if directPath == "" {
		return ""
	}
	if strings.HasPrefix(directPath, "/") {
		return "https://mmg.whatsapp.net" + directPath
	}
	return "https://mmg.whatsapp.net/" + directPath
}
