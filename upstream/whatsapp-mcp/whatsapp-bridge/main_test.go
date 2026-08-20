package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
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
