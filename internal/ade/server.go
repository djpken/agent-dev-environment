package ade

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const maxJSONRequestBytes = 64 * 1024
const maxArtifactHTTPBytes = MaxArtifactRequestBytes

type APIError struct {
	Status  int
	Message string
}

func (e *APIError) Error() string               { return e.Message }
func apiError(status int, message string) error { return &APIError{Status: status, Message: message} }

type registrationChallenge struct {
	address, message string
	expires          time.Time
	created          time.Time
}
type statusEntry struct {
	expires time.Time
	value   Object
}
type ManagementServer struct {
	Config       *EnvironmentConfig
	UIRoot       string
	trending     *TrendingService
	mu           sync.Mutex
	status       map[string]statusEntry
	registration map[string]registrationChallenge
	failures     map[string][]time.Time
}

func NewManagementServer(config *EnvironmentConfig, uiRoot string) *ManagementServer {
	return &ManagementServer{Config: config, UIRoot: uiRoot, trending: NewTrendingService(), status: map[string]statusEntry{}, registration: map[string]registrationChallenge{}, failures: map[string][]time.Time{}}
}

func (s *ManagementServer) authorization(operation string, payload Object) (Object, error) {
	helper := s.Config.AuthorizationTrigger
	if helper == "" {
		helper = "/usr/local/sbin/agent-environment-authorize"
	}
	request := Object{}
	for key, value := range payload {
		request[key] = value
	}
	request["operation"] = operation
	body, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/usr/bin/sudo", "-n", helper)
	command.Dir = s.Config.SourceRoot
	command.Env = os.Environ()
	command.Stdin = strings.NewReader(string(body))
	var stdout, stderr limitedBuffer
	stdout.limit = 1024 * 1024
	stderr.limit = 1024 * 1024
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		return nil, apiError(503, "wallet authorization helper is unavailable")
	}
	if stdout.overflow {
		return nil, apiError(502, "wallet authorization helper returned too much data")
	}
	result, err := DecodeJSON(stdout.Bytes())
	if err != nil {
		return nil, apiError(502, "wallet authorization helper returned invalid JSON")
	}
	if message, ok := result["error"].(string); ok {
		return nil, apiError(authorizationStatus(message), message)
	}
	return result, nil
}

type limitedBuffer struct {
	data     []byte
	limit    int
	overflow bool
}

func (b *limitedBuffer) Write(data []byte) (int, error) {
	if len(b.data)+len(data) > b.limit {
		b.overflow = true
		remaining := b.limit - len(b.data)
		if remaining > 0 {
			b.data = append(b.data, data[:remaining]...)
		}
		return len(data), nil
	}
	b.data = append(b.data, data...)
	return len(data), nil
}
func (b *limitedBuffer) Bytes() []byte { return b.data }

func authorizationStatus(message string) int {
	if strings.Contains(message, "expired") || strings.Contains(message, "session") || strings.Contains(message, "enrolled") {
		return 401
	}
	if strings.Contains(message, "role") || strings.Contains(message, "origin is not allowed") {
		return 403
	}
	if strings.Contains(message, "registration is already complete") {
		return 409
	}
	return 400
}
func readBearer(request *http.Request) (string, error) {
	value := request.Header.Get("Authorization")
	if !strings.HasPrefix(value, "Bearer ") {
		return "", apiError(401, "wallet session required")
	}
	token := strings.TrimSpace(strings.TrimPrefix(value, "Bearer "))
	if token == "" {
		return "", apiError(401, "wallet session required")
	}
	return token, nil
}
func (s *ManagementServer) requireSession(request *http.Request, role string) (Object, error) {
	token, err := readBearer(request)
	if err != nil {
		return nil, err
	}
	return s.authorization("session", Object{"token": token, "minimum_role": role})
}

func (s *ManagementServer) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	setSecurityHeaders(writer)
	path := request.URL.Path
	writer.Header().Set("Cache-Control", "no-store")
	if strings.HasPrefix(path, "/assets/") {
		writer.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
	}
	origin := request.Header.Get("Origin")
	if origin != "" && !s.originAllowed(origin) {
		writeAPIError(writer, 403, "origin is not allowed")
		return
	}
	if origin != "" {
		writer.Header().Set("Access-Control-Allow-Origin", origin)
		writer.Header().Set("Vary", "Origin")
	}
	if request.Method == http.MethodOptions {
		writer.Header().Set("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
		writer.Header().Set("Access-Control-Allow-Headers", "Content-Type,Authorization")
		writer.Header().Set("Access-Control-Max-Age", "600")
		writer.WriteHeader(http.StatusNoContent)
		return
	}
	limit := int64(maxJSONRequestBytes)
	if request.Method == http.MethodPost && path == "/api/v1/artifacts" {
		limit = maxArtifactHTTPBytes
	}
	request.Body = http.MaxBytesReader(writer, request.Body, limit)
	defer func() {
		if recovered := recover(); recovered != nil {
			log.Printf("request panic: %v path=%s", recovered, request.URL.Path)
			writeAPIError(writer, 500, "internal server error")
		}
	}()
	if err := s.dispatch(writer, request); err != nil {
		s.respondError(writer, request, err)
	}
}

func (s *ManagementServer) originAllowed(origin string) bool {
	if origin == s.Config.PublicOrigin {
		return true
	}
	for _, value := range s.Config.AllowedOrigins {
		if value == "*" || value == origin {
			return true
		}
	}
	return false
}
func setSecurityHeaders(writer http.ResponseWriter) {
	writer.Header().Set("X-Content-Type-Options", "nosniff")
	writer.Header().Set("X-Frame-Options", "DENY")
	writer.Header().Set("Referrer-Policy", "no-referrer")
	writer.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://ethereum-rpc.publicnode.com wss://mm-sdk-relay.api.cx.metamask.io; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
}

func (s *ManagementServer) dispatch(writer http.ResponseWriter, request *http.Request) error {
	path := request.URL.Path
	method := request.Method
	switch {
	case method == "GET" && path == "/healthz":
		if _, err := s.requireSession(request, "viewer"); err != nil {
			return err
		}
		return writeJSONResponse(writer, 200, Object{"status": "ok", "service": "agent-environment", "vm_id": s.Config.VMID, "registration_required": len(s.Config.Wallets) == 0})
	case method == "GET" && path == "/api/v1/trending":
		for key, values := range request.URL.Query() {
			if (key != "source" && key != "since") || len(values) != 1 {
				return apiError(400, "only one source and one since value are allowed")
			}
		}
		source := "github"
		since := "daily"
		if values := request.URL.Query()["source"]; len(values) == 1 && values[0] != "" {
			source = values[0]
		}
		if values := request.URL.Query()["since"]; len(values) == 1 && values[0] != "" {
			since = values[0]
		}
		if source != "github" && source != "trendshift" || since != "daily" && since != "weekly" && since != "monthly" {
			return apiError(400, "source or since is unsupported")
		}
		result, err := s.trending.Get(source, since)
		if err != nil {
			return apiError(502, "trending source is unavailable")
		}
		return writeJSONResponse(writer, 200, result)
	case method == "GET" && path == "/api/v1/registration/challenge":
		address, err := queryExactlyOnce(request, "address", true)
		if err != nil {
			return err
		}
		result, err := s.registrationChallenge(address)
		if err != nil {
			return err
		}
		return writeJSONResponse(writer, 200, result)
	case method == "POST" && path == "/api/v1/registration":
		body, err := readObjectBody(request, maxJSONRequestBytes)
		if err != nil {
			return err
		}
		if err := validateSignatureBody(body); err != nil {
			return err
		}
		result, err := s.registerWallet(body)
		if err != nil {
			return err
		}
		return writeJSONResponse(writer, 200, result)
	case method == "GET" && path == "/api/v1/auth/challenge":
		address, err := queryExactlyOnce(request, "address", true)
		if err != nil {
			return err
		}
		origin, err := queryExactlyOnce(request, "origin", false)
		if err != nil {
			return err
		}
		payload := Object{"address": address}
		if origin != "" {
			payload["origin"] = origin
		}
		result, err := s.authorization("challenge", payload)
		if err != nil {
			return err
		}
		return writeJSONResponse(writer, 200, result)
	case method == "POST" && path == "/api/v1/auth/verify":
		body, err := readObjectBody(request, maxJSONRequestBytes)
		if err != nil {
			return err
		}
		if err := validateSignatureBody(body); err != nil {
			return err
		}
		result, err := s.verifyAuth(body, requestIP(request))
		if err != nil {
			return err
		}
		return writeJSONResponse(writer, 200, result)
	case method == "POST" && path == "/api/v1/auth/logout":
		token, err := readBearer(request)
		if err != nil {
			return err
		}
		result, err := s.authorization("logout", Object{"token": token})
		if err != nil {
			return err
		}
		return writeJSONResponse(writer, 200, result)
	case method == "GET" && path == "/api/v1/auth/session":
		result, err := s.requireSession(request, "viewer")
		if err != nil {
			return err
		}
		return writeJSONResponse(writer, 200, result)
	case method == "GET" && path == "/api/v1/registration/status":
		if _, err := s.requireSession(request, "viewer"); err != nil {
			return err
		}
		return writeJSONResponse(writer, 200, Object{"required": len(s.Config.Wallets) == 0, "vm_id": s.Config.VMID, "role": nullableString(map[bool]string{true: "admin"}[len(s.Config.Wallets) == 0])})
	case method == "GET" && (path == "/api/v1/health" || path == "/api/v1/components" || path == "/api/v1/runs" || path == "/api/v1/schedule" || path == "/api/v1/policy"):
		if _, err := s.requireSession(request, "viewer"); err != nil {
			return err
		}
		token, err := readBearer(request)
		if err != nil {
			return err
		}
		status, err := s.loadStatus(token)
		if err != nil {
			return err
		}
		switch path {
		case "/api/v1/health":
			return writeJSONResponse(writer, 200, status["health"])
		case "/api/v1/components":
			health := object(status["health"])
			return writeJSONResponse(writer, 200, Object{"vm_id": s.Config.VMID, "components": health["components"]})
		case "/api/v1/runs":
			return writeJSONResponse(writer, 200, Object{"runs": status["runs"]})
		case "/api/v1/policy":
			return writeJSONResponse(writer, 200, status["policy"])
		default:
			result := Object{}
			for key, value := range object(status["config"]) {
				result[key] = value
			}
			policy := object(status["policy"])
			result["policy_valid"] = boolValue(policy["valid"])
			reason, _ := policy["reason"].(string)
			if reason == "" {
				reason = "daily updates are not enabled"
			}
			result["policy_reason"] = reason
			return writeJSONResponse(writer, 200, result)
		}
	case method == "GET" && path == "/api/v1/artifacts":
		if _, err := s.requireSession(request, "viewer"); err != nil {
			return err
		}
		result, err := ListWebArtifacts(s.Config.Artifacts)
		if err != nil {
			return apiError(503, "web artifact storage is unavailable")
		}
		return writeJSONResponse(writer, 200, result)
	case method == "POST" && path == "/api/v1/artifacts":
		if _, err := s.requireSession(request, "operator"); err != nil {
			return err
		}
		body, err := readObjectBody(request, maxArtifactHTTPBytes)
		if err != nil {
			return err
		}
		if err := validateArtifactBody(body); err != nil {
			return err
		}
		result, err := UploadWebArtifact(s.Config.Artifacts, body)
		if err != nil {
			return mapArtifactError(err)
		}
		return writeJSONResponse(writer, 201, result)
	case method == "DELETE" && strings.HasPrefix(path, "/api/v1/artifacts/"):
		if _, err := s.requireSession(request, "operator"); err != nil {
			return err
		}
		name, err := url.PathUnescape(strings.TrimPrefix(path, "/api/v1/artifacts/"))
		if err != nil || name == "" || strings.Contains(name, "/") {
			return apiError(400, "invalid artifact name")
		}
		result, err := RemoveWebArtifact(s.Config.Artifacts, name)
		if err != nil {
			return mapArtifactError(err)
		}
		return writeJSONResponse(writer, 200, result)
	case method == "POST" && path == "/api/v1/update-runs":
		body, err := readObjectBody(request, maxJSONRequestBytes)
		if err != nil {
			return err
		}
		if err := validateUpdateBody(body); err != nil {
			return err
		}
		token, err := readBearer(request)
		if err != nil {
			return err
		}
		action := "update"
		if value, ok := body["action"].(string); ok {
			action = value
		}
		ids := listStrings(body["component_ids"])
		targets := object(body["target_versions"])
		run, err := s.authorization("authorize_run", Object{"token": token, "action": action, "component_ids": ids, "target_versions": defaultObject(targets)})
		if err != nil {
			return err
		}
		id, _ := run["id"].(string)
		if !runIdentifier.MatchString(id) {
			return apiError(502, "wallet authorization helper returned an invalid run")
		}
		trigger := s.Config.ManualTrigger
		if trigger == "" {
			trigger = "/usr/local/sbin/agent-environment-trigger"
		}
		command := exec.Command("/usr/bin/sudo", "-n", trigger, id)
		command.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
		if err := command.Start(); err != nil {
			_, _ = FailRun(s.Config.StateRoot, id, "privileged updater trigger could not start")
			return apiError(400, "update run was queued but could not start")
		}
		_ = command.Process.Release()
		return writeJSONResponse(writer, 202, run)
	case method == "PUT" && path == "/api/v1/policy":
		if _, err := s.requireSession(request, "admin"); err != nil {
			return err
		}
		body, err := readObjectBody(request, maxJSONRequestBytes)
		if err != nil {
			return err
		}
		if err := validatePolicyBody(body); err != nil {
			return err
		}
		token, err := readBearer(request)
		if err != nil {
			return err
		}
		payload := Object{"token": token}
		for _, field := range []string{"enabled", "components", "target_versions", "allow_restart", "release_channel", "expires_at"} {
			if value, ok := body[field]; ok {
				payload[field] = value
			}
		}
		result, err := s.authorization("save_policy", payload)
		if err != nil {
			return err
		}
		s.mu.Lock()
		s.status = map[string]statusEntry{}
		s.mu.Unlock()
		return writeJSONResponse(writer, 200, result)
	default:
		if method == "GET" && !strings.HasPrefix(path, "/api/") {
			s.serveUI(writer, request)
			return nil
		}
		return apiError(404, "route not found")
	}
}

func (s *ManagementServer) registrationChallenge(addressValue string) (Object, error) {
	if len(s.Config.Wallets) > 0 {
		return nil, apiError(409, "registration is already complete")
	}
	address, err := NormalizeWalletAddress(addressValue)
	if err != nil {
		return nil, apiError(400, err.Error())
	}
	s.cleanRegistrationChallenges()
	issued := isoNow()
	expires := time.Now().UTC().Add(10 * time.Minute).Format(time.RFC3339)
	id := randomUUID()
	nonce := UseRandomNonce(24)
	payload := Object{"action": "register", "address": address, "expires_at": expires, "issued_at": issued, "nonce": nonce, "role": "admin", "vm_binding": sha256Hex(s.Config.VMID)}
	encoded, _ := CanonicalJSON(payload, false, false)
	message := "ADE-ENVIRONMENT-REGISTRATION-V1\n" + string(encoded)
	s.mu.Lock()
	s.registration[id] = registrationChallenge{address: address, message: message, expires: time.Now().Add(10 * time.Minute), created: time.Now()}
	s.mu.Unlock()
	s.cleanRegistrationChallenges()
	return Object{"challenge_id": id, "address": address, "role": "admin", "message": message, "issued_at": issued, "expires_at": expires}, nil
}

func (s *ManagementServer) cleanRegistrationChallenges() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	for id, challenge := range s.registration {
		if !challenge.expires.After(now) {
			delete(s.registration, id)
		}
	}
	for len(s.registration) > 32 {
		oldest := ""
		var created time.Time
		for id, challenge := range s.registration {
			if oldest == "" || challenge.created.Before(created) {
				oldest = id
				created = challenge.created
			}
		}
		delete(s.registration, oldest)
	}
}

func (s *ManagementServer) registerWallet(body Object) (Object, error) {
	address, err := NormalizeWalletAddress(body["address"])
	if err != nil {
		return nil, apiError(400, err.Error())
	}
	id, _ := body["challenge_id"].(string)
	message, _ := body["message"].(string)
	signature, _ := body["signature"].(string)
	s.mu.Lock()
	challenge, ok := s.registration[id]
	s.mu.Unlock()
	if !ok || challenge.address != address || challenge.message != message || !challenge.expires.After(time.Now()) {
		return nil, apiError(400, "registration challenge is invalid")
	}
	helper := s.Config.RegistrationTrigger
	if helper == "" {
		helper = "/usr/local/sbin/agent-environment-enroll"
	}
	request := Object{"address": address, "message": message, "signature": signature}
	data, _ := json.Marshal(request)
	ctx, cancel := context.WithTimeout(context.Background(), 65*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/usr/bin/sudo", "-n", helper)
	command.Dir = s.Config.SourceRoot
	command.Stdin = strings.NewReader(string(data))
	var stdout, stderr limitedBuffer
	stdout.limit = 1024 * 1024
	stderr.limit = 1024 * 1024
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		return nil, apiError(400, "wallet registration failed")
	}
	if _, err := DecodeJSON(stdout.Bytes()); err != nil {
		return nil, apiError(502, "wallet registration helper returned invalid JSON")
	}
	s.mu.Lock()
	delete(s.registration, id)
	s.status = map[string]statusEntry{}
	s.mu.Unlock()
	fresh, err := LoadEnvironmentConfig(s.Config.Path)
	if err == nil {
		s.Config = fresh
	}
	return Object{"registered": true, "address": address, "role": "admin", "login_required": true}, nil
}

func (s *ManagementServer) verifyAuth(body Object, ip string) (Object, error) {
	address, err := NormalizeWalletAddress(body["address"])
	if err != nil {
		return nil, apiError(400, err.Error())
	}
	if !s.authFailureAllowed(ip) {
		return nil, apiError(429, "too many authentication failures")
	}
	result, err := s.authorization("login", Object{"challenge_id": body["challenge_id"], "address": address, "message": body["message"], "signature": body["signature"]})
	if err != nil {
		s.noteAuthFailure(ip)
		return nil, err
	}
	return result, nil
}
func (s *ManagementServer) authFailureAllowed(ip string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	cutoff := time.Now().Add(-5 * time.Minute)
	recent := []time.Time{}
	for _, when := range s.failures[ip] {
		if when.After(cutoff) {
			recent = append(recent, when)
		}
	}
	s.failures[ip] = recent
	return len(recent) < 10
}
func (s *ManagementServer) noteAuthFailure(ip string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failures[ip] = append(s.failures[ip], time.Now())
}

func (s *ManagementServer) loadStatus(token string) (Object, error) {
	s.mu.Lock()
	if cached, ok := s.status[token]; ok && cached.expires.After(time.Now()) {
		value := cached.value
		s.mu.Unlock()
		clone, _ := cloneObject(value)
		return clone, nil
	}
	s.mu.Unlock()
	health := EnvironmentHealth(s.Config, false)
	store, err := NewRunStore(s.Config.StateRoot)
	if err != nil {
		return nil, apiError(502, "ADE status could not be loaded")
	}
	runs, err := store.List(50)
	if err != nil {
		return nil, apiError(502, "ADE status could not be loaded")
	}
	publicRuns := []Object{}
	for _, run := range runs {
		publicRuns = append(publicRuns, RunPublic(run, s.Config))
	}
	policy, err := s.authorization("policy_status", Object{"token": token})
	if err != nil {
		return nil, err
	}
	result := Object{"config": s.Config.PublicConfig(), "health": health, "runs": publicRuns, "policy": policy}
	s.mu.Lock()
	s.status[token] = statusEntry{expires: time.Now().Add(time.Second), value: result}
	s.mu.Unlock()
	return result, nil
}

func (s *ManagementServer) serveUI(writer http.ResponseWriter, request *http.Request) {
	root := s.UIRoot
	if root == "" {
		root = filepath.Join(s.Config.SourceRoot, "web", "dist", "ui")
	}
	clean := filepath.Clean("/" + request.URL.Path)
	candidate := filepath.Join(root, filepath.FromSlash(strings.TrimPrefix(clean, "/")))
	resolved, err := filepath.Abs(candidate)
	base, baseErr := filepath.Abs(root)
	if err == nil && baseErr == nil && within(base, resolved) {
		if info, statErr := os.Stat(resolved); statErr == nil && info.Mode().IsRegular() {
			if strings.Contains(request.URL.Path, "/assets/") {
				writer.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
			}
			http.ServeFile(writer, request, resolved)
			return
		}
	}
	index := filepath.Join(root, "index.html")
	if _, err := os.Stat(index); err != nil {
		writeAPIError(writer, 503, "dashboard build is missing")
		return
	}
	http.ServeFile(writer, request, index)
}

func (s *ManagementServer) respondError(writer http.ResponseWriter, request *http.Request, err error) {
	var api *APIError
	if errors.As(err, &api) {
		writeAPIError(writer, api.Status, api.Message)
		return
	}
	var tooLarge *http.MaxBytesError
	if errors.As(err, &tooLarge) {
		writeAPIError(writer, 413, "request body is too large")
		return
	}
	log.Printf("request failed: %v path=%s", err, request.URL.Path)
	writeAPIError(writer, 500, "internal server error")
}
func mapArtifactError(err error) error {
	message := err.Error()
	status := 400
	if strings.HasPrefix(message, "publish blocked:") {
		status = 503
	} else if strings.HasPrefix(message, "artifact not found:") {
		status = 404
	}
	if strings.Contains(message, "storage") || strings.Contains(message, "unavailable") {
		status = 503
	}
	return apiError(status, message)
}

func StartManagementServer(config *EnvironmentConfig, hostOverride string, portOverride int) error {
	host := hostOverride
	if host == "" {
		host, _ = config.Listen["host"].(string)
	}
	if host == "" {
		return fmt.Errorf("listen.host is required")
	}
	port := portOverride
	if port == 0 {
		number, ok := numericFloat(config.Listen["port"])
		if !ok || number < 1 || number > 65535 {
			return fmt.Errorf("listen.host and listen.port are required")
		}
		port = int(number)
	}
	cert, _ := config.TLS["cert"].(string)
	key, _ := config.TLS["key"].(string)
	if (cert == "") != (key == "") {
		return fmt.Errorf("tls.cert and tls.key must be supplied together")
	}
	if cert == "" && !config.AllowHTTP && !isLoopbackHost(host) {
		return fmt.Errorf("TLS is required when the environment service is not loopback-only")
	}
	ui := filepath.Join(config.SourceRoot, "web", "dist", "ui")
	server := &http.Server{Addr: net.JoinHostPort(host, strconv.Itoa(port)), Handler: NewManagementServer(config, ui), ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 1 << 20}
	if cert != "" {
		tlsConfig, err := loadTLSConfig(cert, key)
		if err != nil {
			return err
		}
		server.TLSConfig = tlsConfig
		fmt.Printf("ADES Go server listening at https://%s\n", server.Addr)
		return server.ListenAndServeTLS(cert, key)
	}
	fmt.Printf("ADES Go server listening at http://%s\n", server.Addr)
	return server.ListenAndServe()
}
func isLoopbackHost(host string) bool {
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
func loadTLSConfig(cert, key string) (*tls.Config, error) {
	if _, err := os.Stat(cert); err != nil {
		return nil, err
	}
	if _, err := os.Stat(key); err != nil {
		return nil, err
	}
	return &tls.Config{MinVersion: tls.VersionTLS12}, nil
}

func queryExactlyOnce(request *http.Request, key string, required bool) (string, error) {
	values := request.URL.Query()[key]
	if len(values) > 1 || required && len(values) != 1 {
		return "", apiError(400, key+" must be specified once")
	}
	if len(values) == 0 {
		return "", nil
	}
	return values[0], nil
}
func requestIP(request *http.Request) string {
	host, _, err := net.SplitHostPort(request.RemoteAddr)
	if err != nil {
		host = request.RemoteAddr
	}
	ip := net.ParseIP(host)
	if ip != nil && ip.IsLoopback() {
		forwarded := request.Header.Get("X-Forwarded-For")
		if forwarded != "" {
			parts := strings.Split(forwarded, ",")
			return strings.TrimSpace(parts[0])
		}
	}
	return host
}

func readObjectBody(request *http.Request, limit int64) (Object, error) {
	if request.Body == nil {
		return nil, apiError(400, "request body is invalid")
	}
	data, err := io.ReadAll(io.LimitReader(request.Body, limit+1))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			return nil, apiError(413, "request body is too large")
		}
		return nil, apiError(400, "request body is invalid")
	}
	if int64(len(data)) > limit {
		return nil, apiError(413, "request body is too large")
	}
	result, err := DecodeJSON(data)
	if err != nil {
		return nil, apiError(400, "request body is invalid")
	}
	return result, nil
}
func validateAllowedFields(body Object, allowed ...string) error {
	set := map[string]bool{}
	for _, field := range allowed {
		set[field] = true
	}
	for key := range body {
		if !set[key] {
			return apiError(400, "request body is invalid")
		}
	}
	return nil
}
func validateSignatureBody(body Object) error {
	if err := validateAllowedFields(body, "challenge_id", "address", "message", "signature"); err != nil {
		return err
	}
	id, _ := body["challenge_id"].(string)
	address, _ := body["address"].(string)
	message, _ := body["message"].(string)
	signature, _ := body["signature"].(string)
	if len(id) < 1 || len(id) > 128 || !walletAddressPattern.MatchString(address) || len(message) < 1 || len(message) > 4096 || len(signature) < 1 || len(signature) > 512 {
		return apiError(400, "request body is invalid")
	}
	return nil
}
func validateUpdateBody(body Object) error {
	if err := validateAllowedFields(body, "action", "component_ids", "target_versions"); err != nil {
		return err
	}
	if action, ok := body["action"]; ok && action != "update" && action != "restart" {
		return apiError(400, "request body is invalid")
	}
	ids, err := validateStringArray(body["component_ids"], "component_ids", 1)
	if err != nil {
		return apiError(400, "request body is invalid")
	}
	seen := map[string]bool{}
	for _, id := range ids {
		if len(id) == 0 || seen[id] {
			return apiError(400, "request body is invalid")
		}
		seen[id] = true
	}
	if raw, ok := body["target_versions"]; ok {
		values, ok := raw.(map[string]any)
		if !ok {
			return apiError(400, "request body is invalid")
		}
		for _, value := range values {
			if _, ok := value.(string); !ok {
				return apiError(400, "request body is invalid")
			}
		}
	}
	return nil
}
func validatePolicyBody(body Object) error {
	if err := validateAllowedFields(body, "enabled", "components", "target_versions", "allow_restart", "release_channel", "expires_at"); err != nil {
		return err
	}
	if _, ok := body["enabled"].(bool); !ok {
		return apiError(400, "request body is invalid")
	}
	if raw, ok := body["components"]; ok {
		items, err := validateStringArray(raw, "components", 1)
		if err != nil {
			return apiError(400, "request body is invalid")
		}
		seen := map[string]bool{}
		for _, value := range items {
			if value == "" || seen[value] {
				return apiError(400, "request body is invalid")
			}
			seen[value] = true
		}
	}
	if raw, ok := body["target_versions"]; ok {
		values, ok := raw.(map[string]any)
		if !ok {
			return apiError(400, "request body is invalid")
		}
		for _, value := range values {
			if _, ok := value.(string); !ok {
				return apiError(400, "request body is invalid")
			}
		}
	}
	if raw, ok := body["allow_restart"]; ok {
		if _, ok := raw.(bool); !ok {
			return apiError(400, "request body is invalid")
		}
	}
	if raw, ok := body["release_channel"]; ok && raw != "stable" {
		return apiError(400, "request body is invalid")
	}
	if raw, ok := body["expires_at"]; ok && raw != nil {
		if _, ok := raw.(string); !ok {
			return apiError(400, "request body is invalid")
		}
	}
	return nil
}
func validateArtifactBody(body Object) error {
	if err := validateAllowedFields(body, "name", "entrypoint", "overwrite", "files"); err != nil {
		return err
	}
	if _, ok := body["name"].(string); !ok {
		return apiError(400, "request body is invalid")
	}
	if _, ok := body["entrypoint"].(string); !ok {
		return apiError(400, "request body is invalid")
	}
	if value, ok := body["overwrite"]; ok {
		if _, ok := value.(bool); !ok {
			return apiError(400, "request body is invalid")
		}
	}
	files, ok := body["files"].([]any)
	if !ok || len(files) == 0 || len(files) > MaxArtifactUploadFiles {
		return apiError(400, "request body is invalid")
	}
	for _, raw := range files {
		item := object(raw)
		if item == nil {
			return apiError(400, "request body is invalid")
		}
		if err := validateAllowedFields(item, "path", "content_base64"); err != nil {
			return err
		}
		if _, ok := item["path"].(string); !ok {
			return apiError(400, "request body is invalid")
		}
		if _, ok := item["content_base64"].(string); !ok {
			return apiError(400, "request body is invalid")
		}
	}
	return nil
}

func writeJSONResponse(writer http.ResponseWriter, status int, value any) error {
	data, err := MarshalPretty(value)
	if err != nil {
		return err
	}
	writer.Header().Set("Content-Type", "application/json; charset=utf-8")
	writer.WriteHeader(status)
	_, err = writer.Write(data)
	return err
}
func writeAPIError(writer http.ResponseWriter, status int, message string) {
	_ = writeJSONResponse(writer, status, Object{"error": message})
}
func defaultObject(value Object) Object {
	if value == nil {
		return Object{}
	}
	return value
}
