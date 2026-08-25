package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"
	"golang.org/x/net/proxy"
	"rsc.io/qr"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

const (
	whatsAppWebURL           = "https://web.whatsapp.com/"
	whatsAppBrowserUserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
		"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
	whatsAppChromiumBrand = `"Chromium";v="140", "Not=A?Brand";v="24"`
)

func whatsAppBrowserPreflightHeaders() http.Header {
	return http.Header{
		"Accept":                    {"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"},
		"Accept-Encoding":           {"gzip, deflate, br, zstd"},
		"Accept-Language":           {"en-US,en;q=0.9"},
		"Priority":                  {"u=0, i"},
		"Sec-Ch-Ua":                 {whatsAppChromiumBrand},
		"Sec-Ch-Ua-Mobile":          {"?0"},
		"Sec-Ch-Ua-Platform":        {`"Windows"`},
		"Sec-Fetch-Dest":            {"document"},
		"Sec-Fetch-Mode":            {"navigate"},
		"Sec-Fetch-Site":            {"none"},
		"Sec-Fetch-User":            {"?1"},
		"Upgrade-Insecure-Requests": {"1"},
		"User-Agent":                {whatsAppBrowserUserAgent},
	}
}

func whatsAppBrowserWebSocketHeaders() http.Header {
	return http.Header{
		"Accept":             {"*/*"},
		"Accept-Encoding":    {"gzip, deflate, br, zstd"},
		"Accept-Language":    {"en-US,en;q=0.9"},
		"Priority":           {"u=3, i"},
		"Sec-Ch-Ua":          {whatsAppChromiumBrand},
		"Sec-Ch-Ua-Mobile":   {"?0"},
		"Sec-Ch-Ua-Platform": {`"Windows"`},
		"Sec-Fetch-Dest":     {"websocket"},
		"Sec-Fetch-Mode":     {"websocket"},
		"Sec-Fetch-Site":     {"same-origin"},
	}
}

func newWhatsAppWebHTTPClient(proxyAddress string) (*http.Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	proxyAddress = strings.TrimSpace(proxyAddress)
	if proxyAddress != "" {
		parsed, err := url.Parse(proxyAddress)
		if err != nil || parsed.Host == "" {
			return nil, fmt.Errorf("invalid WhatsApp proxy URL")
		}
		switch parsed.Scheme {
		case "http", "https":
			transport.Proxy = http.ProxyURL(parsed)
		case "socks5", "socks5h":
			dialer, err := proxy.FromURL(parsed, &net.Dialer{
				Timeout:   30 * time.Second,
				KeepAlive: 30 * time.Second,
			})
			if err != nil {
				return nil, fmt.Errorf("failed to configure WhatsApp SOCKS5 proxy")
			}
			if contextDialer, ok := dialer.(proxy.ContextDialer); ok {
				transport.DialContext = contextDialer.DialContext
			} else {
				transport.DialContext = func(_ context.Context, network, address string) (net.Conn, error) {
					return dialer.Dial(network, address)
				}
			}
		default:
			return nil, fmt.Errorf("unsupported WhatsApp proxy scheme %q", parsed.Scheme)
		}
	}

	jar, err := cookiejar.New(nil)
	if err != nil {
		return nil, fmt.Errorf("create WhatsApp cookie jar: %w", err)
	}
	return &http.Client{Transport: transport, Jar: jar}, nil
}

func primeWhatsAppWebSession(ctx context.Context, httpClient *http.Client, targetURL string) (int, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, targetURL, nil)
	if err != nil {
		return 0, fmt.Errorf("create WhatsApp browser preflight: %w", err)
	}
	request.Header = whatsAppBrowserPreflightHeaders()

	response, err := httpClient.Do(request)
	if err != nil {
		return 0, fmt.Errorf("WhatsApp browser preflight failed: %w", err)
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 2<<20))
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusBadRequest {
		return 0, fmt.Errorf("WhatsApp browser preflight returned HTTP %d", response.StatusCode)
	}
	if httpClient.Jar == nil {
		return 0, fmt.Errorf("WhatsApp browser preflight has no cookie jar")
	}
	cookies := httpClient.Jar.Cookies(request.URL)
	if len(cookies) == 0 {
		return 0, fmt.Errorf("WhatsApp browser preflight returned no session cookies")
	}
	return len(cookies), nil
}

func configureWhatsAppBrowserTransport(
	ctx context.Context,
	client *whatsmeow.Client,
	proxyAddress string,
) (int, error) {
	httpClient, err := newWhatsAppWebHTTPClient(proxyAddress)
	if err != nil {
		return 0, err
	}
	client.UserAgent = whatsAppBrowserUserAgent
	client.WebSocketHeaders = whatsAppBrowserWebSocketHeaders()
	client.SetPreLoginHTTPClient(httpClient)
	client.SetWebsocketHTTPClient(httpClient)
	return primeWhatsAppWebSession(ctx, httpClient, whatsAppWebURL)
}

// Message represents a chat message for our client
type Message struct {
	Time      time.Time
	Sender    string
	Content   string
	IsFromMe  bool
	MediaType string
	Filename  string
}

// Database handler for storing message history
type MessageStore struct {
	db *sql.DB
}

// Initialize message store
func NewMessageStore() (*MessageStore, error) {
	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		return nil, fmt.Errorf("failed to create store directory: %v", err)
	}

	// Open SQLite database for messages
	db, err := sql.Open("sqlite3", "file:store/messages.db?_foreign_keys=on")
	if err != nil {
		return nil, fmt.Errorf("failed to open message database: %v", err)
	}

	// Create tables if they don't exist
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (
			jid TEXT PRIMARY KEY,
			name TEXT,
			last_message_time TIMESTAMP
		);
		
		CREATE TABLE IF NOT EXISTS messages (
			id TEXT,
			chat_jid TEXT,
			sender TEXT,
			content TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			media_type TEXT,
			filename TEXT,
			url TEXT,
			media_key BLOB,
			file_sha256 BLOB,
			file_enc_sha256 BLOB,
			file_length INTEGER,
			PRIMARY KEY (id, chat_jid),
			FOREIGN KEY (chat_jid) REFERENCES chats(jid)
		);
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create tables: %v", err)
	}

	return &MessageStore{db: db}, nil
}

// Close the database connection
func (store *MessageStore) Close() error {
	return store.db.Close()
}

// Store a chat in the database
func (store *MessageStore) StoreChat(jid, name string, lastMessageTime time.Time) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		jid, name, lastMessageTime,
	)
	return err
}

// Store a message in the database
func (store *MessageStore) StoreMessage(id, chatJID, sender, content string, timestamp time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	// Only store if there's actual content or media
	if content == "" && mediaType == "" {
		return nil
	}

	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO messages 
		(id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length) 
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, content, timestamp, isFromMe, mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength,
	)
	return err
}

// Get messages from a chat
func (store *MessageStore) GetMessages(chatJID string, limit int) ([]Message, error) {
	rows, err := store.db.Query(
		"SELECT sender, content, timestamp, is_from_me, media_type, filename FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?",
		chatJID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var messages []Message
	for rows.Next() {
		var msg Message
		var timestamp time.Time
		err := rows.Scan(&msg.Sender, &msg.Content, &timestamp, &msg.IsFromMe, &msg.MediaType, &msg.Filename)
		if err != nil {
			return nil, err
		}
		msg.Time = timestamp
		messages = append(messages, msg)
	}

	return messages, nil
}

// Get all chats
func (store *MessageStore) GetChats() (map[string]time.Time, error) {
	rows, err := store.db.Query("SELECT jid, last_message_time FROM chats ORDER BY last_message_time DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	chats := make(map[string]time.Time)
	for rows.Next() {
		var jid string
		var lastMessageTime time.Time
		err := rows.Scan(&jid, &lastMessageTime)
		if err != nil {
			return nil, err
		}
		chats[jid] = lastMessageTime
	}

	return chats, nil
}

// Extract text content from a message
func extractTextContent(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}

	// Try to get text content
	if text := msg.GetConversation(); text != "" {
		return text
	} else if extendedText := msg.GetExtendedTextMessage(); extendedText != nil {
		return extendedText.GetText()
	}

	// For now, we're ignoring non-text messages
	return ""
}

// SendMessageResponse represents the response for the send message API
type SendMessageResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
}

// BridgeStatusResponse reports bridge readiness without exposing session secrets.
type BridgeStatusResponse struct {
	Status             string `json:"status"`
	Ready              bool   `json:"ready"`
	Connected          bool   `json:"connected"`
	LoggedIn           bool   `json:"logged_in"`
	SendEnabled        bool   `json:"send_enabled"`
	TestSendEnabled    bool   `json:"test_send_enabled"`
	TestRecipientCount int    `json:"test_recipient_count"`
	AccountJID         string `json:"account_jid,omitempty"`
	UptimeSeconds      int64  `json:"uptime_seconds"`
}

// PairingStatusResponse exposes pairing progress without leaking the QR payload.
type PairingStatusResponse struct {
	State        string `json:"state"`
	NeedsPairing bool   `json:"needs_pairing"`
	HasQR        bool   `json:"has_qr"`
	Generation   int    `json:"generation"`
	UpdatedAt    string `json:"updated_at,omitempty"`
	ExpiresAt    string `json:"expires_at,omitempty"`
}

// PairingState keeps the short-lived QR value only in bridge memory.
type PairingState struct {
	mu           sync.RWMutex
	state        string
	needsPairing bool
	qrCode       string
	generation   int
	updatedAt    time.Time
	expiresAt    time.Time
}

const (
	pairingConnectTimeout    = 30 * time.Second
	connectionReadyTimeout   = 30 * time.Second
	pairingFirstEventTimeout = 45 * time.Second
	pairingFallbackQRTimeout = 20 * time.Second
	pairingMaxQRTimeout      = 2 * time.Minute
	connectionStallTimeout   = 2 * time.Minute
)

func normalizedPairingQRTimeout(value time.Duration) time.Duration {
	if value <= 0 || value > pairingMaxQRTimeout {
		return pairingFallbackQRTimeout
	}
	return value
}

func normalizedWhatsAppLogLevel(raw string) string {
	switch strings.ToUpper(strings.TrimSpace(raw)) {
	case "DEBUG":
		return "DEBUG"
	case "WARN":
		return "WARN"
	case "ERROR":
		return "ERROR"
	default:
		return "INFO"
	}
}

func resetTimer(timer *time.Timer, duration time.Duration) {
	if !timer.Stop() {
		select {
		case <-timer.C:
		default:
		}
	}
	timer.Reset(duration)
}

func newPairingState(needsPairing bool) *PairingState {
	state := "not_required"
	if needsPairing {
		state = "starting"
	}
	return &PairingState{
		state:        state,
		needsPairing: needsPairing,
		updatedAt:    time.Now().UTC(),
	}
}

func (state *PairingState) update(name string, needsPairing bool, code string, expiresAt time.Time) {
	state.mu.Lock()
	defer state.mu.Unlock()
	state.state = name
	state.needsPairing = needsPairing
	state.qrCode = code
	state.updatedAt = time.Now().UTC()
	state.expiresAt = expiresAt.UTC()
	if code != "" {
		state.generation++
	}
}

func (state *PairingState) snapshot() PairingStatusResponse {
	state.mu.RLock()
	defer state.mu.RUnlock()
	hasQR := state.qrCode != "" && (state.expiresAt.IsZero() || time.Now().Before(state.expiresAt))
	response := PairingStatusResponse{
		State:        state.state,
		NeedsPairing: state.needsPairing,
		HasQR:        hasQR,
		Generation:   state.generation,
		UpdatedAt:    state.updatedAt.Format(time.RFC3339),
	}
	if !state.expiresAt.IsZero() {
		response.ExpiresAt = state.expiresAt.Format(time.RFC3339)
	}
	return response
}

func (state *PairingState) qrPNG() ([]byte, error) {
	state.mu.RLock()
	codeValue := state.qrCode
	expiresAt := state.expiresAt
	state.mu.RUnlock()
	if codeValue == "" || (!expiresAt.IsZero() && time.Now().After(expiresAt)) {
		return nil, fmt.Errorf("no active pairing QR")
	}
	code, err := qr.Encode(codeValue, qr.L)
	if err != nil {
		return nil, fmt.Errorf("encode pairing QR: %w", err)
	}
	code.Scale = 8
	return code.PNG(), nil
}

func pairingStatusHandler(state *PairingState) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_ = json.NewEncoder(w).Encode(state.snapshot())
	}
}

func pairingQRHandler(state *PairingState) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		png, err := state.qrPNG()
		if err != nil {
			http.Error(w, "No active pairing QR", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "image/png")
		w.Header().Set("Cache-Control", "no-store, max-age=0")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		_, _ = w.Write(png)
	}
}

// SendMessageRequest represents the request body for the send message API
type SendMessageRequest struct {
	Recipient string `json:"recipient"`
	Message   string `json:"message"`
	MediaPath string `json:"media_path,omitempty"`
}

// Function to send a WhatsApp message
func mediaStoreRoot() string {
	if configured := strings.TrimSpace(os.Getenv("WHATSAPP_MEDIA_ROOT")); configured != "" {
		return configured
	}
	return "store"
}

func sanitizePathComponent(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" || value == "." || value == ".." || strings.ContainsAny(value, "/\\\x00") {
		return "", fmt.Errorf("invalid path component")
	}

	var sanitized strings.Builder
	for _, character := range value {
		if character >= 'a' && character <= 'z' ||
			character >= 'A' && character <= 'Z' ||
			character >= '0' && character <= '9' ||
			strings.ContainsRune("@._-", character) {
			sanitized.WriteRune(character)
		} else {
			sanitized.WriteByte('_')
		}
	}

	result := strings.Trim(sanitized.String(), ".")
	if result == "" || result == "." || result == ".." {
		return "", fmt.Errorf("invalid path component")
	}
	return result, nil
}

func resolvePathWithinRoot(root, candidate string, mustExist bool) (string, error) {
	rootPath, err := filepath.Abs(root)
	if err != nil {
		return "", fmt.Errorf("resolve media root: %w", err)
	}
	if err := os.MkdirAll(rootPath, 0755); err != nil {
		return "", fmt.Errorf("create media root: %w", err)
	}
	rootPath, err = filepath.EvalSymlinks(rootPath)
	if err != nil {
		return "", fmt.Errorf("resolve media root links: %w", err)
	}

	resolved := candidate
	if !filepath.IsAbs(resolved) {
		resolved = filepath.Join(rootPath, resolved)
	}
	resolved, err = filepath.Abs(resolved)
	if err != nil {
		return "", fmt.Errorf("resolve media path: %w", err)
	}
	if mustExist {
		resolved, err = filepath.EvalSymlinks(resolved)
		if err != nil {
			return "", fmt.Errorf("resolve media path links: %w", err)
		}
	} else {
		// Resolve the nearest existing ancestor before appending a new filename.
		// This prevents a pre-existing directory symlink from redirecting writes
		// outside the configured media root.
		existing := resolved
		unresolvedTail := []string{}
		for {
			_, statErr := os.Lstat(existing)
			if statErr == nil {
				break
			}
			if !os.IsNotExist(statErr) {
				return "", fmt.Errorf("inspect media path: %w", statErr)
			}
			parent := filepath.Dir(existing)
			if parent == existing {
				return "", fmt.Errorf("media path has no existing ancestor")
			}
			unresolvedTail = append([]string{filepath.Base(existing)}, unresolvedTail...)
			existing = parent
		}
		existing, err = filepath.EvalSymlinks(existing)
		if err != nil {
			return "", fmt.Errorf("resolve media path ancestor links: %w", err)
		}
		resolved = filepath.Join(append([]string{existing}, unresolvedTail...)...)
	}

	relative, err := filepath.Rel(rootPath, resolved)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("media path is outside the configured root")
	}
	return resolved, nil
}

func sendWhatsAppMessage(client *whatsmeow.Client, recipient string, message string, mediaPath string) (bool, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp"
	}

	// Create JID for recipient
	var recipientJID types.JID
	var err error

	// Check if recipient is a JID
	isJID := strings.Contains(recipient, "@")

	if isJID {
		// Parse the JID string
		recipientJID, err = types.ParseJID(recipient)
		if err != nil {
			return false, fmt.Sprintf("Error parsing JID: %v", err)
		}
	} else {
		// Create JID from phone number
		recipientJID = types.JID{
			User:   recipient,
			Server: "s.whatsapp.net", // For personal chats
		}
	}

	msg := &waProto.Message{}

	// Check if we have media to send
	if mediaPath != "" {
		resolvedMediaPath, err := resolvePathWithinRoot(mediaStoreRoot(), mediaPath, true)
		if err != nil {
			return false, fmt.Sprintf("Invalid media path: %v", err)
		}

		// Read media file
		mediaData, err := os.ReadFile(resolvedMediaPath)
		if err != nil {
			return false, fmt.Sprintf("Error reading media file: %v", err)
		}

		// Determine media type and mime type based on file extension
		fileExt := strings.TrimPrefix(strings.ToLower(filepath.Ext(resolvedMediaPath)), ".")
		var mediaType whatsmeow.MediaType
		var mimeType string

		// Handle different media types
		switch fileExt {
		// Image types
		case "jpg", "jpeg":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/jpeg"
		case "png":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/png"
		case "gif":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/gif"
		case "webp":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/webp"

		// Audio types
		case "ogg":
			mediaType = whatsmeow.MediaAudio
			mimeType = "audio/ogg; codecs=opus"

		// Video types
		case "mp4":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/mp4"
		case "avi":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/avi"
		case "mov":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/quicktime"

		// Document types (for any other file type)
		default:
			mediaType = whatsmeow.MediaDocument
			mimeType = "application/octet-stream"
		}

		// Upload media to WhatsApp servers
		resp, err := client.Upload(context.Background(), mediaData, mediaType)
		if err != nil {
			return false, fmt.Sprintf("Error uploading media: %v", err)
		}

		fmt.Println("Media upload completed")

		// Create the appropriate message type based on media type
		switch mediaType {
		case whatsmeow.MediaImage:
			msg.ImageMessage = &waProto.ImageMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaAudio:
			// Handle ogg audio files
			var seconds uint32 = 30 // Default fallback
			var waveform []byte = nil

			// Try to analyze the ogg file
			if strings.Contains(mimeType, "ogg") {
				analyzedSeconds, analyzedWaveform, err := analyzeOggOpus(mediaData)
				if err == nil {
					seconds = analyzedSeconds
					waveform = analyzedWaveform
				} else {
					return false, fmt.Sprintf("Failed to analyze Ogg Opus file: %v", err)
				}
			} else {
				fmt.Printf("Not an Ogg Opus file: %s\n", mimeType)
			}

			msg.AudioMessage = &waProto.AudioMessage{
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				Seconds:       proto.Uint32(seconds),
				PTT:           proto.Bool(true),
				Waveform:      waveform,
			}
		case whatsmeow.MediaVideo:
			msg.VideoMessage = &waProto.VideoMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaDocument:
			msg.DocumentMessage = &waProto.DocumentMessage{
				Title:         proto.String(filepath.Base(resolvedMediaPath)),
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		}
	} else {
		msg.Conversation = proto.String(message)
	}

	// Send message
	_, err = client.SendMessage(context.Background(), recipientJID, msg)

	if err != nil {
		return false, fmt.Sprintf("Error sending message: %v", err)
	}

	return true, fmt.Sprintf("Message sent to %s", recipient)
}

func whatsappSendEnabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv("OLLUM_ALLOW_WHATSAPP_SEND")), "true")
}

func normalizePolicyRecipient(recipient string) string {
	trimmed := strings.TrimSpace(recipient)
	if trimmed == "" {
		return ""
	}
	if strings.Contains(trimmed, "@") {
		jid, err := types.ParseJID(trimmed)
		if err != nil || jid.Server != types.DefaultUserServer {
			return ""
		}
		trimmed = jid.User
	}

	var digits strings.Builder
	for _, character := range trimmed {
		if character >= '0' && character <= '9' {
			digits.WriteRune(character)
		}
	}
	normalized := strings.TrimPrefix(digits.String(), "00")
	if len(normalized) == 11 && strings.HasPrefix(normalized, "8") {
		normalized = "7" + normalized[1:]
	}
	if len(normalized) < 7 || len(normalized) > 15 || strings.Trim(normalized, "0") == "" {
		return ""
	}
	return normalized
}

func whatsappTestRecipients() map[string]struct{} {
	recipients := make(map[string]struct{})
	for _, rawRecipient := range strings.FieldsFunc(
		os.Getenv("OLLUM_WHATSAPP_TEST_RECIPIENTS"),
		func(character rune) bool {
			return character == ',' || character == ';' || character == '\n' || character == '\r'
		},
	) {
		normalized := normalizePolicyRecipient(rawRecipient)
		if normalized != "" {
			recipients[normalized] = struct{}{}
		}
	}
	return recipients
}

func testRecipientAllowed(recipient string, allowedRecipients map[string]struct{}) bool {
	normalized := normalizePolicyRecipient(recipient)
	if normalized == "" {
		return false
	}
	_, allowed := allowedRecipients[normalized]
	return allowed
}

func currentBridgeStatus(
	client *whatsmeow.Client,
	startedAt time.Time,
	testRecipients map[string]struct{},
) BridgeStatusResponse {
	connected := client.IsConnected()
	loggedIn := client.IsLoggedIn()
	ready := connected && loggedIn
	status := "not_ready"
	if ready {
		status = "ready"
	}

	accountJID := ""
	if client.Store != nil && client.Store.ID != nil {
		accountJID = client.Store.ID.String()
	}

	uptimeSeconds := int64(time.Since(startedAt).Seconds())
	if uptimeSeconds < 0 {
		uptimeSeconds = 0
	}

	return BridgeStatusResponse{
		Status:             status,
		Ready:              ready,
		Connected:          connected,
		LoggedIn:           loggedIn,
		SendEnabled:        whatsappSendEnabled(),
		TestSendEnabled:    len(testRecipients) > 0,
		TestRecipientCount: len(testRecipients),
		AccountJID:         accountJID,
		UptimeSeconds:      uptimeSeconds,
	}
}

type bridgeStatusProvider func() BridgeStatusResponse

func bridgeStatusHandler(provider bridgeStatusProvider, requireReady bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		status := provider()
		w.Header().Set("Content-Type", "application/json")
		if requireReady && !status.Ready {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
		if err := json.NewEncoder(w).Encode(status); err != nil {
			fmt.Printf("Failed to encode bridge status: %v\n", err)
		}
	}
}

type sendMessageFunc func(
	client *whatsmeow.Client,
	recipient string,
	message string,
	mediaPath string,
) (bool, string)

func sendMessageHandler(
	client *whatsmeow.Client,
	sendEnabled bool,
	testRecipients map[string]struct{},
	send sendMessageFunc,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		var req SendMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		if req.Recipient == "" {
			http.Error(w, "Recipient is required", http.StatusBadRequest)
			return
		}
		if req.Message == "" && req.MediaPath == "" {
			http.Error(w, "Message or media path is required", http.StatusBadRequest)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		testRecipient := testRecipientAllowed(req.Recipient, testRecipients)
		if !sendEnabled && !testRecipient {
			w.WriteHeader(http.StatusForbidden)
			_ = json.NewEncoder(w).Encode(SendMessageResponse{
				Success: false,
				Message: "WhatsApp sending is disabled for this recipient by bridge policy",
			})
			return
		}
		if !sendEnabled && testRecipient && req.MediaPath != "" {
			w.WriteHeader(http.StatusForbidden)
			_ = json.NewEncoder(w).Encode(SendMessageResponse{
				Success: false,
				Message: "Test-recipient policy permits text messages only",
			})
			return
		}

		success, message := send(client, req.Recipient, req.Message, req.MediaPath)
		fmt.Println(sendAuditLine(success))
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}
		_ = json.NewEncoder(w).Encode(SendMessageResponse{
			Success: success,
			Message: message,
		})
	}
}

func sendAuditLine(success bool) string {
	return fmt.Sprintf("WhatsApp send request completed: success=%t", success)
}

// Extract media info from a message
func extractMediaInfo(msg *waProto.Message) (mediaType string, filename string, url string, mediaKey []byte, fileSHA256 []byte, fileEncSHA256 []byte, fileLength uint64) {
	if msg == nil {
		return "", "", "", nil, nil, nil, 0
	}

	// Check for image message
	if img := msg.GetImageMessage(); img != nil {
		return "image", "image_" + time.Now().Format("20060102_150405") + ".jpg",
			img.GetURL(), img.GetMediaKey(), img.GetFileSHA256(), img.GetFileEncSHA256(), img.GetFileLength()
	}

	// Check for video message
	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", "video_" + time.Now().Format("20060102_150405") + ".mp4",
			vid.GetURL(), vid.GetMediaKey(), vid.GetFileSHA256(), vid.GetFileEncSHA256(), vid.GetFileLength()
	}

	// Check for audio message
	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", "audio_" + time.Now().Format("20060102_150405") + ".ogg",
			aud.GetURL(), aud.GetMediaKey(), aud.GetFileSHA256(), aud.GetFileEncSHA256(), aud.GetFileLength()
	}

	// Check for document message
	if doc := msg.GetDocumentMessage(); doc != nil {
		filename := doc.GetFileName()
		if filename == "" {
			filename = "document_" + time.Now().Format("20060102_150405")
		}
		return "document", filename,
			doc.GetURL(), doc.GetMediaKey(), doc.GetFileSHA256(), doc.GetFileEncSHA256(), doc.GetFileLength()
	}

	return "", "", "", nil, nil, nil, 0
}

// Handle regular incoming messages with media support
func handleMessage(client *whatsmeow.Client, messageStore *MessageStore, msg *events.Message, logger waLog.Logger) {
	// Save message to database
	chatJID := msg.Info.Chat.String()
	sender := msg.Info.Sender.User

	// Get appropriate chat name (pass nil for conversation since we don't have one for regular messages)
	name := GetChatName(client, messageStore, msg.Info.Chat, chatJID, nil, sender, logger)

	// Update chat in database with the message timestamp (keeps last message time updated)
	err := messageStore.StoreChat(chatJID, name, msg.Info.Timestamp)
	if err != nil {
		logger.Warnf("Failed to store chat: %v", err)
	}

	// Extract text content
	content := extractTextContent(msg.Message)

	// Extract media info
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(msg.Message)

	// Skip if there's no content and no media
	if content == "" && mediaType == "" {
		return
	}

	// Store message in database
	err = messageStore.StoreMessage(
		msg.Info.ID,
		chatJID,
		sender,
		content,
		msg.Info.Timestamp,
		msg.Info.IsFromMe,
		mediaType,
		filename,
		url,
		mediaKey,
		fileSHA256,
		fileEncSHA256,
		fileLength,
	)

	if err != nil {
		logger.Warnf("Failed to store message: %v", err)
	} else {
		// Log message reception
		timestamp := msg.Info.Timestamp.Format("2006-01-02 15:04:05")
		direction := "←"
		if msg.Info.IsFromMe {
			direction = "→"
		}

		// Never place private message content, contact identifiers, or filenames in logs.
		fmt.Printf("[%s] %s message stored (media=%t)\n", timestamp, direction, mediaType != "")
	}
}

// DownloadMediaRequest represents the request body for the download media API
type DownloadMediaRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
}

// DownloadMediaResponse represents the response for the download media API
type DownloadMediaResponse struct {
	Success  bool   `json:"success"`
	Message  string `json:"message"`
	Filename string `json:"filename,omitempty"`
	Path     string `json:"path,omitempty"`
}

// Store additional media info in the database
func (store *MessageStore) StoreMediaInfo(id, chatJID, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	_, err := store.db.Exec(
		"UPDATE messages SET url = ?, media_key = ?, file_sha256 = ?, file_enc_sha256 = ?, file_length = ? WHERE id = ? AND chat_jid = ?",
		url, mediaKey, fileSHA256, fileEncSHA256, fileLength, id, chatJID,
	)
	return err
}

// Get media info from the database
func (store *MessageStore) GetMediaInfo(id, chatJID string) (string, string, string, []byte, []byte, []byte, uint64, error) {
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64

	err := store.db.QueryRow(
		"SELECT media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	).Scan(&mediaType, &filename, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength)

	return mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err
}

// MediaDownloader implements the whatsmeow.DownloadableMessage interface
type MediaDownloader struct {
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileLength    uint64
	FileSHA256    []byte
	FileEncSHA256 []byte
	MediaType     whatsmeow.MediaType
}

// GetDirectPath implements the DownloadableMessage interface
func (d *MediaDownloader) GetDirectPath() string {
	return d.DirectPath
}

// GetURL implements the DownloadableMessage interface
func (d *MediaDownloader) GetURL() string {
	return d.URL
}

// GetMediaKey implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaKey() []byte {
	return d.MediaKey
}

// GetFileLength implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileLength() uint64 {
	return d.FileLength
}

// GetFileSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileSHA256() []byte {
	return d.FileSHA256
}

// GetFileEncSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileEncSHA256() []byte {
	return d.FileEncSHA256
}

// GetMediaType implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType {
	return d.MediaType
}

// Function to download media from a message
func downloadMedia(client *whatsmeow.Client, messageStore *MessageStore, messageID, chatJID string) (bool, string, string, string, error) {
	// Query the database for the message
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64
	var err error

	// Get media info from the database
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err = messageStore.GetMediaInfo(messageID, chatJID)

	if err != nil {
		// Try to get basic info if extended info isn't available
		err = messageStore.db.QueryRow(
			"SELECT media_type, filename FROM messages WHERE id = ? AND chat_jid = ?",
			messageID, chatJID,
		).Scan(&mediaType, &filename)

		if err != nil {
			return false, "", "", "", fmt.Errorf("failed to find message: %v", err)
		}
	}

	// Check if this is a media message
	if mediaType == "" {
		return false, "", "", "", fmt.Errorf("not a media message")
	}
	if strings.TrimSpace(filename) == "" {
		filename = messageID
	}

	chatComponent, err := sanitizePathComponent(chatJID)
	if err != nil {
		return false, "", "", "", fmt.Errorf("invalid chat JID: %w", err)
	}
	fileComponent, err := sanitizePathComponent(filename)
	if err != nil {
		return false, "", "", "", fmt.Errorf("invalid media filename: %w", err)
	}
	localPath, err := resolvePathWithinRoot(
		mediaStoreRoot(),
		filepath.Join(chatComponent, fileComponent),
		false,
	)
	if err != nil {
		return false, "", "", "", fmt.Errorf("invalid local media path: %w", err)
	}
	chatDir := filepath.Dir(localPath)

	// Create directory for the chat if it doesn't exist
	if err := os.MkdirAll(chatDir, 0755); err != nil {
		return false, "", "", "", fmt.Errorf("failed to create chat directory: %v", err)
	}

	// Check if file already exists
	if _, err := os.Stat(localPath); err == nil {
		// File exists, return it
		return true, mediaType, fileComponent, localPath, nil
	}

	// If we don't have all the media info we need, we can't download
	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 || fileLength == 0 {
		return false, "", "", "", fmt.Errorf("incomplete media information for download")
	}

	fmt.Println("Attempting to download stored media")

	// Extract direct path from URL
	directPath := extractDirectPathFromURL(url)

	// Create a downloader that implements DownloadableMessage
	var waMediaType whatsmeow.MediaType
	switch mediaType {
	case "image":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "document":
		waMediaType = whatsmeow.MediaDocument
	default:
		return false, "", "", "", fmt.Errorf("unsupported media type: %s", mediaType)
	}

	downloader := &MediaDownloader{
		URL:           url,
		DirectPath:    directPath,
		MediaKey:      mediaKey,
		FileLength:    fileLength,
		FileSHA256:    fileSHA256,
		FileEncSHA256: fileEncSHA256,
		MediaType:     waMediaType,
	}

	// Download the media using whatsmeow client
	mediaData, err := client.Download(context.Background(), downloader)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to download media: %v", err)
	}

	// Save the downloaded media to file
	if err := os.WriteFile(localPath, mediaData, 0644); err != nil {
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}

	fmt.Printf("Successfully downloaded media (%d bytes)\n", len(mediaData))
	return true, mediaType, fileComponent, localPath, nil
}

// Extract direct path from a WhatsApp media URL
func extractDirectPathFromURL(url string) string {
	// The direct path is typically in the URL, we need to extract it
	// Example URL: https://mmg.whatsapp.net/v/t62.7118-24/13812002_698058036224062_3424455886509161511_n.enc?ccb=11-4&oh=...

	// Find the path part after the domain
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url // Return original URL if parsing fails
	}

	pathPart := parts[1]

	// Remove query parameters
	pathPart = strings.SplitN(pathPart, "?", 2)[0]

	// Create proper direct path format
	return "/" + pathPart
}

// Start a REST API server to expose the WhatsApp client functionality
func startRESTServer(client *whatsmeow.Client, messageStore *MessageStore, pairingState *PairingState, port int) {
	serverStartedAt := time.Now()
	testRecipients := whatsappTestRecipients()
	statusProvider := func() BridgeStatusResponse {
		return currentBridgeStatus(client, serverStartedAt, testRecipients)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/health", bridgeStatusHandler(statusProvider, false))
	mux.HandleFunc("/api/status", bridgeStatusHandler(statusProvider, true))
	mux.HandleFunc("/api/pairing", pairingStatusHandler(pairingState))
	mux.HandleFunc("/api/pairing/qr", pairingQRHandler(pairingState))
	mux.HandleFunc(
		"/api/send",
		sendMessageHandler(
			client,
			whatsappSendEnabled(),
			testRecipients,
			sendWhatsAppMessage,
		),
	)

	// Handler for downloading media
	mux.HandleFunc("/api/download", func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "Message ID and Chat JID are required", http.StatusBadRequest)
			return
		}

		// Download the media
		success, mediaType, filename, path, err := downloadMedia(client, messageStore, req.MessageID, req.ChatJID)

		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Handle download result
		if !success || err != nil {
			errMsg := "Unknown error"
			if err != nil {
				errMsg = err.Error()
			}

			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(DownloadMediaResponse{
				Success: false,
				Message: fmt.Sprintf("Failed to download media: %s", errMsg),
			})
			return
		}

		// Send successful response
		json.NewEncoder(w).Encode(DownloadMediaResponse{
			Success:  true,
			Message:  fmt.Sprintf("Successfully downloaded %s media", mediaType),
			Filename: filename,
			Path:     path,
		})
	})

	// Start the server
	serverAddr := fmt.Sprintf(":%d", port)
	fmt.Printf("Starting REST API server on %s...\n", serverAddr)

	// Run server in a goroutine so it doesn't block
	go func() {
		if err := http.ListenAndServe(serverAddr, mux); err != nil {
			fmt.Printf("REST API server error: %v\n", err)
		}
	}()
}

func main() {
	// Set up logger
	logLevel := normalizedWhatsAppLogLevel(os.Getenv("WHATSAPP_LOG_LEVEL"))
	logger := waLog.Stdout("Client", logLevel, true)
	logger.Infof("Starting WhatsApp client...")

	// Create database connection for storing session data
	dbLog := waLog.Stdout("Database", "INFO", true)

	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		logger.Errorf("Failed to create store directory: %v", err)
		return
	}

	container, err := sqlstore.New(context.Background(), "sqlite3", "file:store/whatsapp.db?_foreign_keys=on", dbLog)
	if err != nil {
		logger.Errorf("Failed to connect to database: %v", err)
		return
	}

	// Get device store - This contains session information
	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		if err == sql.ErrNoRows {
			// No device exists, create one
			deviceStore = container.NewDevice()
			logger.Infof("Created new device")
		} else {
			logger.Errorf("Failed to get device: %v", err)
			return
		}
	}

	// Create client instance
	client := whatsmeow.NewClient(deviceStore, logger)
	if client == nil {
		logger.Errorf("Failed to create WhatsApp client")
		return
	}
	proxyAddress := strings.TrimSpace(os.Getenv("WHATSAPP_PROXY_URL"))
	if proxyAddress != "" {
		if err := client.SetProxyAddress(proxyAddress); err != nil {
			logger.Errorf("Failed to configure WhatsApp proxy: %v", err)
			return
		}
		logger.Infof("WhatsApp proxy configured")
	}
	preflightContext, cancelPreflight := context.WithTimeout(context.Background(), 30*time.Second)
	cookieCount, preflightErr := configureWhatsAppBrowserTransport(preflightContext, client, proxyAddress)
	cancelPreflight()
	if preflightErr != nil {
		logger.Errorf("Failed to initialize browser-compatible WhatsApp transport: %v", preflightErr)
		return
	}
	logger.Infof("WhatsApp browser session initialized with %d cookie(s)", cookieCount)

	// Initialize message store
	messageStore, err := NewMessageStore()
	if err != nil {
		logger.Errorf("Failed to initialize message store: %v", err)
		return
	}
	defer messageStore.Close()
	pairingState := newPairingState(client.Store.ID == nil)

	// The private REST API starts before authentication so the dashboard can
	// display and refresh the QR code without restarting the bridge.
	startRESTServer(client, messageStore, pairingState, 8080)
	exitChan := make(chan os.Signal, 1)
	signal.Notify(exitChan, syscall.SIGINT, syscall.SIGTERM)

	// Setup event handling for messages and history sync
	client.AddEventHandler(func(evt interface{}) {
		switch v := evt.(type) {
		case *events.Message:
			// Process regular messages
			handleMessage(client, messageStore, v, logger)

		case *events.HistorySync:
			// Process history sync events
			handleHistorySync(client, messageStore, v, logger)

		case *events.Connected:
			pairingState.update("connected", false, "", time.Time{})
			logger.Infof("Connected to WhatsApp")

		case *events.Disconnected:
			pairingState.update("reconnecting", client.Store.ID == nil, "", time.Time{})
			logger.Warnf("WhatsApp connection interrupted; automatic reconnect is running")

		case *events.ConnectFailure:
			pairingState.update("connection_failed", client.Store.ID == nil, "", time.Time{})
			logger.Warnf("WhatsApp connection rejected; reason=%v", v.Reason)

		case *events.StreamError:
			pairingState.update("stream_error", client.Store.ID == nil, "", time.Time{})
			logger.Warnf("WhatsApp stream error; code=%s", v.Code)

		case *events.StreamReplaced:
			pairingState.update("session_replaced", false, "", time.Time{})
			logger.Warnf("WhatsApp session was replaced by another active bridge")

		case *events.KeepAliveTimeout:
			pairingState.update("reconnecting", client.Store.ID == nil, "", time.Time{})
			logger.Warnf("WhatsApp keepalive timed out; consecutive_errors=%d", v.ErrorCount)

		case *events.LoggedOut:
			pairingState.update("restarting", true, "", time.Time{})
			logger.Warnf("Device logged out; restarting bridge to request a fresh QR code")
			select {
			case exitChan <- syscall.SIGTERM:
			default:
			}
		}
	})

	// Connect to WhatsApp
	if client.Store.ID == nil {
		pairingTimeout := 10 * time.Minute
		if rawTimeout := strings.TrimSpace(os.Getenv("WHATSAPP_PAIRING_TIMEOUT")); rawTimeout != "" {
			parsedTimeout, parseErr := time.ParseDuration(rawTimeout)
			if parseErr != nil || parsedTimeout < 3*time.Minute || parsedTimeout > 30*time.Minute {
				logger.Errorf("WHATSAPP_PAIRING_TIMEOUT must be a duration between 3m and 30m")
				return
			}
			pairingTimeout = parsedTimeout
		}

		pairingContext, cancelPairing := context.WithTimeout(context.Background(), pairingTimeout)
		defer cancelPairing()

		paired := false
		for !paired {
			// Whatsmeow emits a finite batch of rotating QR codes. When that batch
			// expires, reconnect and request a fresh batch until our operator window
			// closes. This keeps pairing interactive without restarting other services.
			batchContext, cancelBatch := context.WithCancel(pairingContext)
			qrChan, qrErr := client.GetQRChannel(batchContext)
			if qrErr != nil {
				cancelBatch()
				pairingState.update("failed", true, "", time.Time{})
				logger.Errorf("Failed to initialize QR pairing: %v", qrErr)
				return
			}

			connectContext, cancelConnect := context.WithTimeout(
				pairingContext,
				pairingConnectTimeout,
			)
			err = client.ConnectContext(connectContext)
			cancelConnect()
			if err != nil {
				cancelBatch()
				client.Disconnect()
				pairingState.update("refreshing", true, "", time.Time{})
				logger.Warnf("QR pairing connection attempt failed; retrying: %v", err)
				select {
				case <-time.After(2 * time.Second):
				case <-pairingContext.Done():
					pairingState.update("timed_out", true, "", time.Time{})
					logger.Errorf("Timeout waiting for QR code scan")
					return
				}
				continue
			}

			retryWithFreshBatch := false
			batchFinished := false
			batchTimer := time.NewTimer(pairingFirstEventTimeout)
			for !batchFinished {
				select {
				case <-pairingContext.Done():
					batchFinished = true
				case <-batchTimer.C:
					pairingState.update("refreshing", true, "", time.Time{})
					retryWithFreshBatch = true
					batchFinished = true
					logger.Warnf("QR channel produced no fresh event; requesting a new batch")
				case evt, open := <-qrChan:
					if !open {
						batchFinished = true
						break
					}
					switch evt.Event {
					case "code":
						qrTimeout := normalizedPairingQRTimeout(evt.Timeout)
						pairingState.update(
							"waiting_for_scan",
							true,
							evt.Code,
							time.Now().Add(qrTimeout),
						)
						resetTimer(batchTimer, qrTimeout+15*time.Second)
						fmt.Println("\nScan this QR code with your WhatsApp app:")
						qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
					case "success":
						pairingState.update("paired", false, "", time.Time{})
						paired = true
						batchFinished = true
					case "timeout":
						pairingState.update("refreshing", true, "", time.Time{})
						retryWithFreshBatch = true
						batchFinished = true
						logger.Infof("QR batch expired; requesting a fresh batch")
					case "error", "err-unexpected-state", "err-client-outdated", "err-scanned-without-multidevice":
						pairingState.update("failed", true, "", time.Time{})
						logger.Errorf("QR pairing failed: %s", evt.Event)
						if !batchTimer.Stop() {
							select {
							case <-batchTimer.C:
							default:
							}
						}
						cancelBatch()
						return
					default:
						logger.Infof("QR pairing event: %s", evt.Event)
					}
				}
			}
			if !batchTimer.Stop() {
				select {
				case <-batchTimer.C:
				default:
				}
			}
			cancelBatch()

			if paired {
				break
			}
			if pairingContext.Err() != nil {
				pairingState.update("timed_out", true, "", time.Time{})
				logger.Errorf("Timeout waiting for QR code scan")
				return
			}
			if !retryWithFreshBatch {
				pairingState.update("failed", true, "", time.Time{})
				logger.Errorf("QR pairing channel closed without a terminal result")
				return
			}

			client.Disconnect()
			select {
			case <-time.After(2 * time.Second):
			case <-pairingContext.Done():
				pairingState.update("timed_out", true, "", time.Time{})
				logger.Errorf("Timeout waiting for QR code scan")
				return
			}
		}

		fmt.Println("\nSuccessfully connected and authenticated!")
	} else {
		pairingState.update("not_required", false, "", time.Time{})
		// Already logged in, just connect
		connectContext, cancelConnect := context.WithTimeout(
			context.Background(),
			pairingConnectTimeout,
		)
		err = client.ConnectContext(connectContext)
		cancelConnect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}
	}

	// ConnectContext may return nil after scheduling Whatsmeow's initial
	// automatic reconnect. Keep the REST API and QR surface alive while that
	// reconnect runs instead of exiting after a brittle two-second snapshot.
	if client.WaitForConnection(connectionReadyTimeout) {
		pairingState.update("connected", false, "", time.Time{})
	} else {
		pairingState.update("reconnecting", client.Store.ID == nil, "", time.Time{})
		logger.Warnf("Initial WhatsApp connection is still unavailable; keeping bridge online while automatic reconnect continues")
	}

	// Whatsmeow normally reconnects on its own. If the bridge remains detached
	// for too long, exit cleanly so Docker's unless-stopped policy can recreate
	// only this service while preserving the authenticated session volume.
	go func() {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		disconnectedSince := time.Time{}
		for range ticker.C {
			if client.IsConnected() {
				disconnectedSince = time.Time{}
				continue
			}
			if disconnectedSince.IsZero() {
				disconnectedSince = time.Now()
				continue
			}
			if time.Since(disconnectedSince) < connectionStallTimeout {
				continue
			}
			pairingState.update("restarting", client.Store.ID == nil, "", time.Time{})
			logger.Warnf("WhatsApp connection remained unavailable; restarting bridge")
			select {
			case exitChan <- syscall.SIGTERM:
			default:
			}
			return
		}
	}()

	if client.IsConnected() && client.IsLoggedIn() {
		fmt.Println("\n✓ Connected to WhatsApp! Type 'help' for commands.")
	} else {
		fmt.Println("\nBridge API is online; WhatsApp reconnect is still in progress.")
	}

	fmt.Println("REST server is running. Press Ctrl+C to disconnect and exit.")

	// Wait for termination signal
	<-exitChan

	fmt.Println("Disconnecting...")
	// Disconnect client
	client.Disconnect()
}

// GetChatName determines the appropriate name for a chat based on JID and other info
func GetChatName(client *whatsmeow.Client, messageStore *MessageStore, jid types.JID, chatJID string, conversation interface{}, sender string, logger waLog.Logger) string {
	// First, check if chat already exists in database with a name
	var existingName string
	err := messageStore.db.QueryRow("SELECT name FROM chats WHERE jid = ?", chatJID).Scan(&existingName)
	if err == nil && existingName != "" {
		// Chat exists with a name, use that
		logger.Infof("Using an existing stored chat name")
		return existingName
	}

	// Need to determine chat name
	var name string

	if jid.Server == "g.us" {
		// This is a group chat
		logger.Infof("Resolving a group chat name")

		// Use conversation data if provided (from history sync)
		if conversation != nil {
			// Extract name from conversation if available
			// This uses type assertions to handle different possible types
			var displayName, convName *string
			// Try to extract the fields we care about regardless of the exact type
			v := reflect.ValueOf(conversation)
			if v.Kind() == reflect.Ptr && !v.IsNil() {
				v = v.Elem()

				// Try to find DisplayName field
				if displayNameField := v.FieldByName("DisplayName"); displayNameField.IsValid() && displayNameField.Kind() == reflect.Ptr && !displayNameField.IsNil() {
					dn := displayNameField.Elem().String()
					displayName = &dn
				}

				// Try to find Name field
				if nameField := v.FieldByName("Name"); nameField.IsValid() && nameField.Kind() == reflect.Ptr && !nameField.IsNil() {
					n := nameField.Elem().String()
					convName = &n
				}
			}

			// Use the name we found
			if displayName != nil && *displayName != "" {
				name = *displayName
			} else if convName != nil && *convName != "" {
				name = *convName
			}
		}

		// If we didn't get a name, try group info
		if name == "" {
			groupInfo, err := client.GetGroupInfo(context.Background(), jid)
			if err == nil && groupInfo.Name != "" {
				name = groupInfo.Name
			} else {
				// Fallback name for groups
				name = fmt.Sprintf("Group %s", jid.User)
			}
		}

		logger.Infof("Resolved a group chat name")
	} else {
		// This is an individual contact
		logger.Infof("Resolving a contact name")

		// Just use contact info (full name)
		contact, err := client.Store.Contacts.GetContact(context.Background(), jid)
		if err == nil && contact.FullName != "" {
			name = contact.FullName
		} else if sender != "" {
			// Fallback to sender
			name = sender
		} else {
			// Last fallback to JID
			name = jid.User
		}

		logger.Infof("Resolved a contact name")
	}

	return name
}

// Handle history sync events
func handleHistorySync(client *whatsmeow.Client, messageStore *MessageStore, historySync *events.HistorySync, logger waLog.Logger) {
	fmt.Printf("Received history sync event with %d conversations\n", len(historySync.Data.Conversations))

	syncedCount := 0
	for _, conversation := range historySync.Data.Conversations {
		// Parse JID from the conversation
		if conversation.ID == nil {
			continue
		}

		chatJID := *conversation.ID

		// Try to parse the JID
		jid, err := types.ParseJID(chatJID)
		if err != nil {
			logger.Warnf("Failed to parse a history-sync JID")
			continue
		}

		// Get appropriate chat name by passing the history sync conversation directly
		name := GetChatName(client, messageStore, jid, chatJID, conversation, "", logger)

		// Process messages
		messages := conversation.Messages
		if len(messages) > 0 {
			// Update chat with latest message timestamp
			latestMsg := messages[0]
			if latestMsg == nil || latestMsg.Message == nil {
				continue
			}

			// Get timestamp from message info
			timestamp := time.Time{}
			if ts := latestMsg.Message.GetMessageTimestamp(); ts != 0 {
				timestamp = time.Unix(int64(ts), 0)
			} else {
				continue
			}

			messageStore.StoreChat(chatJID, name, timestamp)

			// Store messages
			for _, msg := range messages {
				if msg == nil || msg.Message == nil {
					continue
				}

				// Extract text content
				var content string
				if msg.Message.Message != nil {
					if conv := msg.Message.Message.GetConversation(); conv != "" {
						content = conv
					} else if ext := msg.Message.Message.GetExtendedTextMessage(); ext != nil {
						content = ext.GetText()
					}
				}

				// Extract media info
				var mediaType, filename, url string
				var mediaKey, fileSHA256, fileEncSHA256 []byte
				var fileLength uint64

				if msg.Message.Message != nil {
					mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength = extractMediaInfo(msg.Message.Message)
				}

				// Skip messages with no content and no media
				if content == "" && mediaType == "" {
					continue
				}

				// Determine sender
				var sender string
				isFromMe := false
				if msg.Message.Key != nil {
					if msg.Message.Key.FromMe != nil {
						isFromMe = *msg.Message.Key.FromMe
					}
					if !isFromMe && msg.Message.Key.Participant != nil && *msg.Message.Key.Participant != "" {
						sender = *msg.Message.Key.Participant
					} else if isFromMe {
						sender = client.Store.ID.User
					} else {
						sender = jid.User
					}
				} else {
					sender = jid.User
				}

				// Store message
				msgID := ""
				if msg.Message.Key != nil && msg.Message.Key.ID != nil {
					msgID = *msg.Message.Key.ID
				}

				// Get message timestamp
				timestamp := time.Time{}
				if ts := msg.Message.GetMessageTimestamp(); ts != 0 {
					timestamp = time.Unix(int64(ts), 0)
				} else {
					continue
				}

				err = messageStore.StoreMessage(
					msgID,
					chatJID,
					sender,
					content,
					timestamp,
					isFromMe,
					mediaType,
					filename,
					url,
					mediaKey,
					fileSHA256,
					fileEncSHA256,
					fileLength,
				)
				if err != nil {
					logger.Warnf("Failed to store history message: %v", err)
				} else {
					syncedCount++
					logger.Infof("Stored a history message (media=%t)", mediaType != "")
				}
			}
		}
	}

	fmt.Printf("History sync complete. Stored %d messages.\n", syncedCount)
}

// Request history sync from the server
func requestHistorySync(client *whatsmeow.Client) {
	if client == nil {
		fmt.Println("Client is not initialized. Cannot request history sync.")
		return
	}

	if !client.IsConnected() {
		fmt.Println("Client is not connected. Please ensure you are connected to WhatsApp first.")
		return
	}

	if client.Store.ID == nil {
		fmt.Println("Client is not logged in. Please scan the QR code first.")
		return
	}

	// Build and send a history sync request
	historyMsg := client.BuildHistorySyncRequest(nil, 100)
	if historyMsg == nil {
		fmt.Println("Failed to build history sync request.")
		return
	}

	_, err := client.SendMessage(context.Background(), types.JID{
		Server: "s.whatsapp.net",
		User:   "status",
	}, historyMsg)

	if err != nil {
		fmt.Printf("Failed to request history sync: %v\n", err)
	} else {
		fmt.Println("History sync requested. Waiting for server response...")
	}
}

// analyzeOggOpus tries to extract duration and generate a simple waveform from an Ogg Opus file
func analyzeOggOpus(data []byte) (duration uint32, waveform []byte, err error) {
	// Try to detect if this is a valid Ogg file by checking for the "OggS" signature
	// at the beginning of the file
	if len(data) < 4 || string(data[0:4]) != "OggS" {
		return 0, nil, fmt.Errorf("not a valid Ogg file (missing OggS signature)")
	}

	// Parse Ogg pages to find the last page with a valid granule position
	var lastGranule uint64
	var sampleRate uint32 = 48000 // Default Opus sample rate
	var preSkip uint16 = 0
	var foundOpusHead bool

	// Scan through the file looking for Ogg pages
	for i := 0; i < len(data); {
		// Check if we have enough data to read Ogg page header
		if i+27 >= len(data) {
			break
		}

		// Verify Ogg page signature
		if string(data[i:i+4]) != "OggS" {
			// Skip until next potential page
			i++
			continue
		}

		// Extract header fields
		granulePos := binary.LittleEndian.Uint64(data[i+6 : i+14])
		pageSeqNum := binary.LittleEndian.Uint32(data[i+18 : i+22])
		numSegments := int(data[i+26])

		// Extract segment table
		if i+27+numSegments >= len(data) {
			break
		}
		segmentTable := data[i+27 : i+27+numSegments]

		// Calculate page size
		pageSize := 27 + numSegments
		for _, segLen := range segmentTable {
			pageSize += int(segLen)
		}

		// Check if we're looking at an OpusHead packet (should be in first few pages)
		if !foundOpusHead && pageSeqNum <= 1 {
			// Look for "OpusHead" marker in this page
			pageData := data[i : i+pageSize]
			headPos := bytes.Index(pageData, []byte("OpusHead"))
			if headPos >= 0 && headPos+12 < len(pageData) {
				// Found OpusHead, extract sample rate and pre-skip
				// OpusHead format: Magic(8) + Version(1) + Channels(1) + PreSkip(2) + SampleRate(4) + ...
				headPos += 8 // Skip "OpusHead" marker
				// PreSkip is 2 bytes at offset 10
				if headPos+12 <= len(pageData) {
					preSkip = binary.LittleEndian.Uint16(pageData[headPos+10 : headPos+12])
					sampleRate = binary.LittleEndian.Uint32(pageData[headPos+12 : headPos+16])
					foundOpusHead = true
					fmt.Printf("Found OpusHead: sampleRate=%d, preSkip=%d\n", sampleRate, preSkip)
				}
			}
		}

		// Keep track of last valid granule position
		if granulePos != 0 {
			lastGranule = granulePos
		}

		// Move to next page
		i += pageSize
	}

	if !foundOpusHead {
		fmt.Println("Warning: OpusHead not found, using default values")
	}

	// Calculate duration based on granule position
	if lastGranule > 0 {
		// Formula for duration: (lastGranule - preSkip) / sampleRate
		durationSeconds := float64(lastGranule-uint64(preSkip)) / float64(sampleRate)
		duration = uint32(math.Ceil(durationSeconds))
		fmt.Printf("Calculated Opus duration from granule: %f seconds (lastGranule=%d)\n",
			durationSeconds, lastGranule)
	} else {
		// Fallback to rough estimation if granule position not found
		fmt.Println("Warning: No valid granule position found, using estimation")
		durationEstimate := float64(len(data)) / 2000.0 // Very rough approximation
		duration = uint32(durationEstimate)
	}

	// Make sure we have a reasonable duration (at least 1 second, at most 300 seconds)
	if duration < 1 {
		duration = 1
	} else if duration > 300 {
		duration = 300
	}

	// Generate waveform
	waveform = placeholderWaveform(duration)

	fmt.Printf("Ogg Opus analysis: size=%d bytes, calculated duration=%d sec, waveform=%d bytes\n",
		len(data), duration, len(waveform))

	return duration, waveform, nil
}

// min returns the smaller of x or y
func min(x, y int) int {
	if x < y {
		return x
	}
	return y
}

// placeholderWaveform generates a synthetic waveform for WhatsApp voice messages
// that appears natural with some variability based on the duration
func placeholderWaveform(duration uint32) []byte {
	// WhatsApp expects a 64-byte waveform for voice messages
	const waveformLength = 64
	waveform := make([]byte, waveformLength)

	// Seed the random number generator for consistent results with the same duration
	rand.Seed(int64(duration))

	// Create a more natural looking waveform with some patterns and variability
	// rather than completely random values

	// Base amplitude and frequency - longer messages get faster frequency
	baseAmplitude := 35.0
	frequencyFactor := float64(min(int(duration), 120)) / 30.0

	for i := range waveform {
		// Position in the waveform (normalized 0-1)
		pos := float64(i) / float64(waveformLength)

		// Create a wave pattern with some randomness
		// Use multiple sine waves of different frequencies for more natural look
		val := baseAmplitude * math.Sin(pos*math.Pi*frequencyFactor*8)
		val += (baseAmplitude / 2) * math.Sin(pos*math.Pi*frequencyFactor*16)

		// Add some randomness to make it look more natural
		val += (rand.Float64() - 0.5) * 15

		// Add some fade-in and fade-out effects
		fadeInOut := math.Sin(pos * math.Pi)
		val = val * (0.7 + 0.3*fadeInOut)

		// Center around 50 (typical voice baseline)
		val = val + 50

		// Ensure values stay within WhatsApp's expected range (0-100)
		if val < 0 {
			val = 0
		} else if val > 100 {
			val = 100
		}

		waveform[i] = byte(val)
	}

	return waveform
}
