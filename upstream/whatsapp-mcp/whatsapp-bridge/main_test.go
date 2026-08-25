package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
)

func TestBridgeStatusHandlerReady(t *testing.T) {
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodGet, "/api/status", nil)
	provider := func() BridgeStatusResponse {
		return BridgeStatusResponse{
			Status:             "ready",
			Ready:              true,
			Connected:          true,
			LoggedIn:           true,
			SendEnabled:        false,
			TestSendEnabled:    true,
			TestRecipientCount: 1,
			AccountJID:         "123456789@s.whatsapp.net",
			UptimeSeconds:      12,
		}
	}

	bridgeStatusHandler(provider, true).ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d", w.Code)
	}
	var response BridgeStatusResponse
	if err := json.NewDecoder(w.Body).Decode(&response); err != nil {
		t.Fatalf("decode status response: %v", err)
	}
	if !response.Ready || response.SendEnabled || !response.TestSendEnabled || response.TestRecipientCount != 1 {
		t.Fatalf("unexpected status response: %+v", response)
	}
}

func TestBridgeStatusHandlerNotReady(t *testing.T) {
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodGet, "/api/status", nil)
	provider := func() BridgeStatusResponse {
		return BridgeStatusResponse{Status: "not_ready"}
	}

	bridgeStatusHandler(provider, true).ServeHTTP(w, r)

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected status 503, got %d", w.Code)
	}
}

func TestBridgeStatusHandlerRejectsNonGET(t *testing.T) {
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/health", nil)

	bridgeStatusHandler(func() BridgeStatusResponse { return BridgeStatusResponse{} }, false).ServeHTTP(w, r)

	if w.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected status 405, got %d", w.Code)
	}
}

func TestPairingHandlersExposeStateButNeverRawCode(t *testing.T) {
	state := newPairingState(true)
	state.update("waiting_for_scan", true, "secret-whatsapp-pairing-value", time.Now().Add(time.Minute))

	statusWriter := httptest.NewRecorder()
	statusRequest := httptest.NewRequest(http.MethodGet, "/api/pairing", nil)
	pairingStatusHandler(state).ServeHTTP(statusWriter, statusRequest)
	if statusWriter.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d", statusWriter.Code)
	}
	if bytes.Contains(statusWriter.Body.Bytes(), []byte("secret-whatsapp-pairing-value")) {
		t.Fatal("pairing status leaked the raw QR value")
	}
	var response PairingStatusResponse
	if err := json.NewDecoder(statusWriter.Body).Decode(&response); err != nil {
		t.Fatalf("decode pairing status: %v", err)
	}
	if !response.NeedsPairing || !response.HasQR || response.Generation != 1 {
		t.Fatalf("unexpected pairing status: %+v", response)
	}

	qrWriter := httptest.NewRecorder()
	qrRequest := httptest.NewRequest(http.MethodGet, "/api/pairing/qr", nil)
	pairingQRHandler(state).ServeHTTP(qrWriter, qrRequest)
	if qrWriter.Code != http.StatusOK {
		t.Fatalf("expected QR status 200, got %d", qrWriter.Code)
	}
	if qrWriter.Header().Get("Content-Type") != "image/png" {
		t.Fatalf("unexpected QR content type: %s", qrWriter.Header().Get("Content-Type"))
	}
	if !bytes.HasPrefix(qrWriter.Body.Bytes(), []byte("\x89PNG\r\n\x1a\n")) {
		t.Fatal("pairing endpoint did not return a PNG")
	}
}

func TestPairingQRHandlerRejectsExpiredCode(t *testing.T) {
	state := newPairingState(true)
	state.update("waiting_for_scan", true, "expired", time.Now().Add(-time.Second))
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodGet, "/api/pairing/qr", nil)

	pairingQRHandler(state).ServeHTTP(w, r)

	if w.Code != http.StatusNotFound {
		t.Fatalf("expected status 404, got %d", w.Code)
	}
}

func TestNormalizedPairingQRTimeoutUsesServerValue(t *testing.T) {
	want := 60 * time.Second
	if got := normalizedPairingQRTimeout(want); got != want {
		t.Fatalf("expected %s, got %s", want, got)
	}
}

func TestNormalizedPairingQRTimeoutFallsBackForInvalidValues(t *testing.T) {
	for _, value := range []time.Duration{0, -time.Second, pairingMaxQRTimeout + time.Second} {
		if got := normalizedPairingQRTimeout(value); got != pairingFallbackQRTimeout {
			t.Fatalf("expected fallback %s for %s, got %s", pairingFallbackQRTimeout, value, got)
		}
	}
}

func TestNormalizedWhatsAppLogLevel(t *testing.T) {
	tests := map[string]string{
		"":        "INFO",
		"info":    "INFO",
		" debug ": "DEBUG",
		"warn":    "WARN",
		"ERROR":   "ERROR",
		"trace":   "INFO",
	}
	for input, expected := range tests {
		if got := normalizedWhatsAppLogLevel(input); got != expected {
			t.Fatalf("expected %s for %q, got %s", expected, input, got)
		}
	}
}

func TestWhatsAppBrowserHeadersMatchNavigationAndWebSocketContexts(t *testing.T) {
	preflight := whatsAppBrowserPreflightHeaders()
	websocket := whatsAppBrowserWebSocketHeaders()
	if preflight.Get("User-Agent") != whatsAppBrowserUserAgent {
		t.Fatal("browser preflight must use the configured browser user agent")
	}
	if preflight.Get("Sec-Fetch-Dest") != "document" || preflight.Get("Sec-Fetch-Site") != "none" {
		t.Fatalf("unexpected browser navigation headers: %v", preflight)
	}
	if websocket.Get("Sec-Fetch-Dest") != "websocket" || websocket.Get("Sec-Fetch-Site") != "same-origin" {
		t.Fatalf("unexpected browser websocket headers: %v", websocket)
	}
	if websocket.Get("Priority") != "u=3, i" {
		t.Fatalf("unexpected websocket priority: %q", websocket.Get("Priority"))
	}
}

func TestPrimeWhatsAppWebSessionSeedsCookieJar(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") != whatsAppBrowserUserAgent {
			t.Errorf("unexpected user agent: %q", r.Header.Get("User-Agent"))
		}
		if r.Header.Get("Sec-Fetch-Dest") != "document" {
			t.Errorf("unexpected fetch destination: %q", r.Header.Get("Sec-Fetch-Dest"))
		}
		http.SetCookie(w, &http.Cookie{Name: "wa_ul", Value: "test", Path: "/", HttpOnly: true})
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}))
	defer server.Close()

	httpClient, err := newWhatsAppWebHTTPClient("")
	if err != nil {
		t.Fatalf("create browser HTTP client: %v", err)
	}
	cookieCount, err := primeWhatsAppWebSession(context.Background(), httpClient, server.URL)
	if err != nil {
		t.Fatalf("prime browser session: %v", err)
	}
	if cookieCount != 1 {
		t.Fatalf("expected one browser session cookie, got %d", cookieCount)
	}
}

func TestNewWhatsAppWebHTTPClientRejectsUnsupportedProxy(t *testing.T) {
	if _, err := newWhatsAppWebHTTPClient("ftp://proxy.example.test:21"); err == nil {
		t.Fatal("expected unsupported proxy scheme to fail")
	}
}

func TestConnectionReadyTimeoutAllowsInitialAutoReconnect(t *testing.T) {
	if connectionReadyTimeout < pairingConnectTimeout {
		t.Fatalf(
			"connection readiness timeout %s must not be shorter than connect timeout %s",
			connectionReadyTimeout,
			pairingConnectTimeout,
		)
	}
	if connectionReadyTimeout >= connectionStallTimeout {
		t.Fatalf(
			"connection readiness timeout %s must leave time for the %s stall watchdog",
			connectionReadyTimeout,
			connectionStallTimeout,
		)
	}
}

func TestSendMessageHandlerBlocksWhenDisabled(t *testing.T) {
	sendCalled := false
	w := httptest.NewRecorder()
	r := httptest.NewRequest(
		http.MethodPost,
		"/api/send",
		strings.NewReader(`{"recipient":"79990000000","message":"hello"}`),
	)

	sendMessageHandler(
		nil,
		false,
		map[string]struct{}{"79779335513": {}},
		func(_ *whatsmeow.Client, _, _, _ string) (bool, string) {
			sendCalled = true
			return true, "sent"
		},
	).ServeHTTP(w, r)

	if w.Code != http.StatusForbidden {
		t.Fatalf("expected status 403, got %d", w.Code)
	}
	var response SendMessageResponse
	if err := json.NewDecoder(w.Body).Decode(&response); err != nil {
		t.Fatalf("decode send response: %v", err)
	}
	if response.Success {
		t.Fatal("disabled bridge unexpectedly allowed sending")
	}
	if sendCalled {
		t.Fatal("disabled bridge called the WhatsApp client for a non-allowlisted recipient")
	}
}

func TestSendMessageHandlerAllowsTextForExactTestRecipient(t *testing.T) {
	var sentRecipient string
	w := httptest.NewRecorder()
	r := httptest.NewRequest(
		http.MethodPost,
		"/api/send",
		strings.NewReader(`{"recipient":"+7 (977) 933-55-13","message":"hello"}`),
	)

	sendMessageHandler(
		nil,
		false,
		map[string]struct{}{"79779335513": {}},
		func(_ *whatsmeow.Client, recipient, _, _ string) (bool, string) {
			sentRecipient = recipient
			return true, "sent"
		},
	).ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d", w.Code)
	}
	if sentRecipient != "+7 (977) 933-55-13" {
		t.Fatalf("unexpected recipient passed to sender: %q", sentRecipient)
	}
}

func TestPolicyRecipientNormalizationMatchesPythonBoundary(t *testing.T) {
	for input, expected := range map[string]string{
		"+7 (977) 933-55-13":              "79779335513",
		"8 (977) 933-55-13":               "79779335513",
		"0079779335513@s.whatsapp.net":    "79779335513",
		"0@s.whatsapp.net":                "",
		"123":                             "",
		"1234567890123456@s.whatsapp.net": "",
	} {
		if actual := normalizePolicyRecipient(input); actual != expected {
			t.Fatalf("normalize %q: expected %q, got %q", input, expected, actual)
		}
	}
}

func TestSendMessageHandlerBlocksMediaForTestRecipient(t *testing.T) {
	sendCalled := false
	w := httptest.NewRecorder()
	r := httptest.NewRequest(
		http.MethodPost,
		"/api/send",
		strings.NewReader(`{"recipient":"79779335513","media_path":"private.jpg"}`),
	)

	sendMessageHandler(
		nil,
		false,
		map[string]struct{}{"79779335513": {}},
		func(_ *whatsmeow.Client, _, _, _ string) (bool, string) {
			sendCalled = true
			return true, "sent"
		},
	).ServeHTTP(w, r)

	if w.Code != http.StatusForbidden {
		t.Fatalf("expected status 403, got %d", w.Code)
	}
	if sendCalled {
		t.Fatal("test-recipient media request reached the WhatsApp client")
	}
}

func TestSendAuditLineContainsOnlyOutcome(t *testing.T) {
	line := sendAuditLine(true)
	if line != "WhatsApp send request completed: success=true" {
		t.Fatalf("unexpected audit line: %q", line)
	}
	for _, secret := range []string{"recipient@s.whatsapp.net", "private message", "/private/media.jpg"} {
		if strings.Contains(line, secret) {
			t.Fatalf("audit line leaked sensitive value %q", secret)
		}
	}
}

func TestSendMessageHandlerValidatesRequestsBeforeUsingClient(t *testing.T) {
	tests := []struct {
		name   string
		method string
		body   string
		status int
	}{
		{name: "method", method: http.MethodGet, status: http.StatusMethodNotAllowed},
		{name: "invalid json", method: http.MethodPost, body: "{", status: http.StatusBadRequest},
		{name: "recipient", method: http.MethodPost, body: `{"message":"hello"}`, status: http.StatusBadRequest},
		{name: "content", method: http.MethodPost, body: `{"recipient":"123@s.whatsapp.net"}`, status: http.StatusBadRequest},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			r := httptest.NewRequest(test.method, "/api/send", strings.NewReader(test.body))

			sendMessageHandler(
				nil,
				true,
				nil,
				func(_ *whatsmeow.Client, _, _, _ string) (bool, string) {
					return true, "sent"
				},
			).ServeHTTP(w, r)

			if w.Code != test.status {
				t.Fatalf("expected status %d, got %d", test.status, w.Code)
			}
		})
	}
}

func TestMessageStoreRoundTrip(t *testing.T) {
	t.Chdir(t.TempDir())
	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("create message store: %v", err)
	}
	t.Cleanup(func() {
		if err := store.Close(); err != nil {
			t.Errorf("close message store: %v", err)
		}
	})

	chatJID := "123@s.whatsapp.net"
	timestamp := time.Date(2026, time.August, 22, 10, 0, 0, 0, time.UTC)
	if err := store.StoreChat(chatJID, "Test chat", timestamp); err != nil {
		t.Fatalf("store chat: %v", err)
	}
	if err := store.StoreMessage(
		"message-1", chatJID, "456@s.whatsapp.net", "hello", timestamp, false,
		"", "", "", nil, nil, nil, 0,
	); err != nil {
		t.Fatalf("store message: %v", err)
	}
	if err := store.StoreMessage(
		"empty-message", chatJID, "456@s.whatsapp.net", "", timestamp, false,
		"", "", "", nil, nil, nil, 0,
	); err != nil {
		t.Fatalf("skip empty message: %v", err)
	}

	messages, err := store.GetMessages(chatJID, 10)
	if err != nil {
		t.Fatalf("get messages: %v", err)
	}
	if len(messages) != 1 {
		t.Fatalf("expected one stored message, got %d", len(messages))
	}
	if messages[0].Sender != "456@s.whatsapp.net" || messages[0].Content != "hello" || !messages[0].Time.Equal(timestamp) {
		t.Fatalf("unexpected message: %+v", messages[0])
	}

	chats, err := store.GetChats()
	if err != nil {
		t.Fatalf("get chats: %v", err)
	}
	storedTime, ok := chats[chatJID]
	if !ok || !storedTime.Equal(timestamp) {
		t.Fatalf("unexpected chats: %+v", chats)
	}
}

func TestSanitizePathComponentRejectsTraversal(t *testing.T) {
	for _, value := range []string{"", ".", "..", "../secret", `..\secret`, "chat/name"} {
		if _, err := sanitizePathComponent(value); err == nil {
			t.Fatalf("expected %q to be rejected", value)
		}
	}

	got, err := sanitizePathComponent("123:45@s.whatsapp.net")
	if err != nil {
		t.Fatalf("sanitize valid JID: %v", err)
	}
	if got != "123_45@s.whatsapp.net" {
		t.Fatalf("unexpected sanitized JID: %q", got)
	}
}

func TestResolvePathWithinRootRejectsEscapeAndSymlink(t *testing.T) {
	root := t.TempDir()
	insideDir := filepath.Join(root, "chat")
	if err := os.MkdirAll(insideDir, 0755); err != nil {
		t.Fatalf("create inside dir: %v", err)
	}
	inside := filepath.Join(insideDir, "voice.ogg")
	if err := os.WriteFile(inside, []byte("audio"), 0644); err != nil {
		t.Fatalf("create inside file: %v", err)
	}

	resolved, err := resolvePathWithinRoot(root, inside, true)
	if err != nil {
		t.Fatalf("resolve allowed file: %v", err)
	}
	if resolved != inside {
		t.Fatalf("expected %q, got %q", inside, resolved)
	}

	outsideDir := t.TempDir()
	outside := filepath.Join(outsideDir, "secret.txt")
	if err := os.WriteFile(outside, []byte("secret"), 0644); err != nil {
		t.Fatalf("create outside file: %v", err)
	}
	if _, err := resolvePathWithinRoot(root, outside, true); err == nil {
		t.Fatal("outside absolute path was accepted")
	}
	if _, err := resolvePathWithinRoot(root, filepath.Join("..", "secret.txt"), false); err == nil {
		t.Fatal("relative traversal was accepted")
	}

	link := filepath.Join(root, "outside-link")
	if err := os.Symlink(outside, link); err == nil {
		if _, err := resolvePathWithinRoot(root, link, true); err == nil {
			t.Fatal("symlink escape was accepted")
		}
	}

	linkedDirectory := filepath.Join(root, "linked-directory")
	if err := os.Symlink(outsideDir, linkedDirectory); err == nil {
		candidate := filepath.Join(linkedDirectory, "new-media.bin")
		if _, err := resolvePathWithinRoot(root, candidate, false); err == nil {
			t.Fatal("write through a symlinked parent directory was accepted")
		}
	}
}
