package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestBridgeStatusHandlerReady(t *testing.T) {
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodGet, "/api/status", nil)
	provider := func() BridgeStatusResponse {
		return BridgeStatusResponse{
			Status:        "ready",
			Ready:         true,
			Connected:     true,
			LoggedIn:      true,
			SendEnabled:   false,
			AccountJID:    "123456789@s.whatsapp.net",
			UptimeSeconds: 12,
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
	if !response.Ready || response.SendEnabled {
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

func TestSendMessageHandlerBlocksWhenDisabled(t *testing.T) {
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/api/send", nil)

	sendMessageHandler(nil, false).ServeHTTP(w, r)

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
