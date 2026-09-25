package ade

import (
	"encoding/base64"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func testPasswordAuthority(t *testing.T) *EnvironmentAuthority {
	t.Helper()
	root := t.TempDir()
	configPath := filepath.Join(root, "agent-environment.json")
	if err := os.WriteFile(configPath, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	authority, err := NewEnvironmentAuthority(&EnvironmentConfig{Path: configPath, VMID: "vm-test"})
	if err != nil {
		t.Fatal(err)
	}
	salt := []byte("0123456789abcdef")
	hash, err := passwordHash("test-password-long-enough", salt)
	if err != nil {
		t.Fatal(err)
	}
	account := Object{
		"username": "admin", "role": "admin", "credential_id": "credential-v1", "hash_version": 1,
		"salt":          base64.RawStdEncoding.EncodeToString(salt),
		"password_hash": base64.RawStdEncoding.EncodeToString(hash),
	}
	if err := WriteJSON(authority.AccountsPath, Object{"vm_id": "vm-test", "users": Object{"admin": account}}, 0o600); err != nil {
		t.Fatal(err)
	}
	return authority
}

func TestPasswordLoginCreatesHashedBearerSession(t *testing.T) {
	authority := testPasswordAuthority(t)
	login, err := authority.Dispatch(Object{
		"operation": "login", "username": " ADMIN ", "password": "test-password-long-enough",
	})
	if err != nil {
		t.Fatal(err)
	}
	if login["username"] != "admin" || login["role"] != "admin" {
		t.Fatalf("unexpected login result: %#v", login)
	}
	token, ok := login["session"].(string)
	if !ok || len(token) < 32 {
		t.Fatalf("invalid session token: %#v", login["session"])
	}
	session, err := authority.Dispatch(Object{"operation": "session", "token": token})
	if err != nil {
		t.Fatal(err)
	}
	if session["username"] != "admin" || session["role"] != "admin" {
		t.Fatalf("unexpected session: %#v", session)
	}
	state, err := os.ReadFile(authority.StatePath)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(state), token) {
		t.Fatal("session storage contains the bearer token")
	}
}

func TestPasswordLoginUsesGenericInvalidCredentialsError(t *testing.T) {
	authority := testPasswordAuthority(t)
	for _, request := range []Object{
		{"operation": "login", "username": "admin", "password": "incorrect-password"},
		{"operation": "login", "username": "unknown", "password": "test-password-long-enough"},
	} {
		if _, err := authority.Dispatch(request); err == nil || err.Error() != "invalid credentials" {
			t.Fatalf("expected generic invalid credentials error, got %v", err)
		}
	}
}

func TestSecurePasswordRequestTrustsOnlyTLSOrLoopbackHTTPSProxy(t *testing.T) {
	directTLS := httptest.NewRequest("POST", "https://example.test/api/v1/auth/login", nil)
	if !securePasswordRequest(directTLS) {
		t.Fatal("direct TLS request was rejected")
	}
	proxyTLS := httptest.NewRequest("POST", "http://127.0.0.1/api/v1/auth/login", nil)
	proxyTLS.RemoteAddr = "127.0.0.1:6790"
	proxyTLS.Header.Set("X-Forwarded-Proto", "https")
	if !securePasswordRequest(proxyTLS) {
		t.Fatal("loopback HTTPS proxy request was rejected")
	}
	spoofed := httptest.NewRequest("POST", "http://example.test/api/v1/auth/login", nil)
	spoofed.RemoteAddr = "192.0.2.10:6790"
	spoofed.Header.Set("X-Forwarded-Proto", "https")
	if securePasswordRequest(spoofed) {
		t.Fatal("untrusted forwarded-proto header was accepted")
	}
}

func TestLoginRouteRejectsPlainHTTPBeforeReadingCredentials(t *testing.T) {
	server := NewManagementServer(&EnvironmentConfig{VMID: "vm-test"}, "")
	request := httptest.NewRequest("POST", "/api/v1/auth/login", strings.NewReader("{\"username\":\"admin\",\"password\":\"test-password-long-enough\"}"))
	request.RemoteAddr = "192.0.2.10:6790"
	response := httptest.NewRecorder()
	server.ServeHTTP(response, request)
	if response.Code != 400 || !strings.Contains(response.Body.String(), "HTTPS is required") {
		t.Fatalf("expected an HTTPS-required response, got %d: %s", response.Code, response.Body.String())
	}
}
