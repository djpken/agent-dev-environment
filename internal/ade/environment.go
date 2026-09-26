package ade

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

const EnvironmentVersion = "1"
const DefaultEnvironmentConfig = "/etc/ade/agent-environment.json"
const fixedEnvironmentPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

var safeIdentifier = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
var systemdUnitName = regexp.MustCompile(`^[A-Za-z0-9_.@:%+-]+$`)
var targetVersionName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$`)
var targetVersionArgument = regexp.MustCompile(`^--[A-Za-z0-9][A-Za-z0-9-]{0,31}$`)
var runIdentifier = regexp.MustCompile(`^[0-9a-f-]{36}$`)
var registrationNonce = regexp.MustCompile(`^[A-Za-z0-9_-]{16,128}$`)

type Component struct {
	ID               string
	Label            string
	Kind             string
	Health           Object
	VersionFile      string
	VersionCommand   []string
	UpdateCommand    []string
	TargetVersionArg string
	RestartCommand   []string
	RollbackCommand  []string
	Dependencies     []string
	Enabled          bool
	Public           bool
	Timeout          time.Duration
}

type EnvironmentConfig struct {
	Path                 string
	VMID                 string
	SourceRoot           string
	ADERoot              string
	StateRoot            string
	LocalDeployRepoRoot  string
	PublicOrigin         string
	AllowedOrigins       []string
	Components           []*Component
	Wallets              []Object
	Schedule             Object
	Snapshot             Object
	Listen               Object
	TLS                  Object
	AllowHTTP            bool
	ManualTrigger        string
	AuthorizationTrigger string
	Artifacts            Object
	Raw                  Object
}

func isoNow() string { return time.Now().UTC().Truncate(time.Second).Format(time.RFC3339) }

func readJSONOr(path string, fallback Object) (Object, error) {
	value, err := ReadJSON(path)
	if os.IsNotExist(err) {
		return fallback, nil
	}
	return value, err
}

func LoadEnvironmentConfig(path string) (*EnvironmentConfig, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	raw, err := ReadJSON(abs)
	if err != nil {
		return nil, fmt.Errorf("environment config does not exist: %s", abs)
	}
	vmID, _ := raw["vm_id"].(string)
	if !safeIdentifier.MatchString(vmID) {
		return nil, fmt.Errorf("vm_id must be a safe identifier")
	}
	source, _ := raw["source_root"].(string)
	if source == "" {
		source = "."
	}
	source, err = filepath.Abs(source)
	if err != nil {
		return nil, err
	}
	adeRoot, _ := raw["ade_root"].(string)
	if adeRoot == "" {
		home, homeErr := os.UserHomeDir()
		if homeErr != nil {
			return nil, fmt.Errorf("could not determine ADE user home")
		}
		adeRoot = filepath.Join(home, ".local", "share", "ade")
	} else if !filepath.IsAbs(adeRoot) {
		return nil, fmt.Errorf("ade_root must be an absolute path")
	}
	adeRoot = filepath.Clean(adeRoot)
	stateRoot, _ := raw["state_root"].(string)
	if stateRoot == "" {
		stateRoot = "/var/lib/ade/agent-environment"
	}
	stateRoot, err = filepath.Abs(stateRoot)
	if err != nil {
		return nil, err
	}
	localDeployment := object(raw["local_deployment"])
	if raw["local_deployment"] != nil && localDeployment == nil {
		return nil, fmt.Errorf("local_deployment must be an object")
	}
	localDeployRepoRoot := ""
	if localDeployment["repo_root"] != nil {
		var ok bool
		localDeployRepoRoot, ok = localDeployment["repo_root"].(string)
		if !ok {
			return nil, fmt.Errorf("local_deployment.repo_root must be a string")
		}
	}
	if localDeployRepoRoot != "" {
		if !filepath.IsAbs(localDeployRepoRoot) {
			return nil, fmt.Errorf("local_deployment.repo_root must be an absolute path")
		}
		localDeployRepoRoot = filepath.Clean(localDeployRepoRoot)
	}
	origin, _ := raw["public_origin"].(string)
	parsedOrigin, err := url.Parse(origin)
	if err != nil || (parsedOrigin.Scheme != "http" && parsedOrigin.Scheme != "https") || parsedOrigin.Hostname() == "" {
		return nil, fmt.Errorf("public_origin must be an HTTP(S) origin")
	}
	origin = strings.TrimRight(origin, "/")
	allowedOrigins := []string{}
	for _, item := range listValue(raw["allowed_origins"]) {
		value, ok := item.(string)
		if !ok {
			return nil, fmt.Errorf("allowed_origins must be a list")
		}
		allowedOrigins = append(allowedOrigins, value)
	}
	componentsRaw, ok := raw["components"].([]any)
	if !ok || len(componentsRaw) == 0 {
		return nil, fmt.Errorf("components must be a non-empty list")
	}
	components := make([]*Component, 0, len(componentsRaw))
	componentIDs := map[string]bool{}
	for _, value := range componentsRaw {
		item, ok := value.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("components must be unique objects")
		}
		component, err := parseComponent(item)
		if err != nil {
			return nil, err
		}
		if componentIDs[component.ID] {
			return nil, fmt.Errorf("components must be unique objects")
		}
		componentIDs[component.ID] = true
		components = append(components, component)
	}
	for _, component := range components {
		for _, dependency := range component.Dependencies {
			if !componentIDs[dependency] {
				return nil, fmt.Errorf("component %s has unknown dependencies: [%s]", component.ID, dependency)
			}
		}
	}
	walletConfig := object(raw["wallets"])
	walletItems := []Object{}
	if walletConfig != nil {
		if values, ok := walletConfig["authorized"].([]any); ok {
			for _, value := range values {
				item := object(value)
				if item == nil {
					return nil, fmt.Errorf("wallet entries require address and viewer/operator/admin role")
				}
				address, err := NormalizeWalletAddress(item["address"])
				if err != nil {
					return nil, fmt.Errorf("wallet entries require address and viewer/operator/admin role")
				}
				role, _ := item["role"].(string)
				if role != "viewer" && role != "operator" && role != "admin" {
					return nil, fmt.Errorf("wallet entries require address and viewer/operator/admin role")
				}
				walletItems = append(walletItems, Object{"address": address, "role": role})
			}
		} else if walletConfig["authorized"] != nil {
			return nil, fmt.Errorf("wallets.authorized must be a list")
		}
	}
	schedule := object(raw["schedule"])
	if schedule == nil {
		schedule = Object{}
	}
	if schedule["time"] != nil && schedule["time"] != "04:00" || schedule["timezone"] != nil && schedule["timezone"] != "UTC+8" {
		return nil, fmt.Errorf("the environment schedule is fixed at 04:00 UTC+8")
	}
	snapshot := object(raw["snapshot"])
	if snapshot == nil {
		snapshot = Object{}
	}
	listen := object(raw["listen"])
	if listen == nil {
		listen = Object{}
	}
	tls := object(raw["tls"])
	if tls == nil {
		tls = Object{}
	}
	manual, _ := raw["manual_trigger"].(string)
	if manual == "" {
		manual = "/usr/local/sbin/agent-environment-trigger"
	}
	authorization, _ := raw["authorization_trigger"].(string)
	if authorization == "" {
		authorization = "/usr/local/sbin/agent-environment-authorize"
	}
	artifacts, err := ArtifactConfiguration(raw["artifacts"], append([]string{origin}, allowedOrigins...))
	if err != nil {
		return nil, err
	}
	return &EnvironmentConfig{Path: abs, VMID: vmID, SourceRoot: source, ADERoot: adeRoot, StateRoot: stateRoot, LocalDeployRepoRoot: localDeployRepoRoot, PublicOrigin: origin, AllowedOrigins: allowedOrigins, Components: components, Wallets: walletItems, Schedule: schedule, Snapshot: snapshot, Listen: listen, TLS: tls, AllowHTTP: boolValue(raw["allow_http"]), ManualTrigger: manual, AuthorizationTrigger: authorization, Artifacts: artifacts, Raw: raw}, nil
}

func parseComponent(value Object) (*Component, error) {
	id, _ := value["id"].(string)
	if !safeIdentifier.MatchString(id) {
		return nil, fmt.Errorf("component.id must be a safe identifier")
	}
	label, _ := value["label"].(string)
	if label == "" {
		label = id
	}
	if strings.TrimSpace(label) == "" {
		return nil, fmt.Errorf("component %s label must be text", id)
	}
	kind, _ := value["kind"].(string)
	if kind == "" {
		kind = "service"
	}
	if strings.TrimSpace(kind) == "" {
		return nil, fmt.Errorf("component %s kind must be text", id)
	}
	health := object(value["health"])
	if health == nil {
		return nil, fmt.Errorf("component %s health must be an object", id)
	}
	healthType, _ := health["type"].(string)
	if healthType != "systemd" && healthType != "command" && healthType != "http" {
		return nil, fmt.Errorf("component %s has unsupported health type", id)
	}
	if healthType == "systemd" {
		unit, _ := health["unit"].(string)
		if !systemdUnitName.MatchString(unit) {
			return nil, fmt.Errorf("component %s has invalid systemd unit", id)
		}
	} else if healthType == "command" {
		if _, err := commandArray(health["command"], "component "+id+" health.command", true); err != nil {
			return nil, err
		}
	} else {
		healthURL, _ := health["url"].(string)
		parsed, err := url.Parse(healthURL)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" {
			return nil, fmt.Errorf("component %s has invalid health URL", id)
		}
	}
	versionFile, _ := value["version_file"].(string)
	if versionFile != "" && !filepath.IsAbs(versionFile) {
		return nil, fmt.Errorf("component %s version_file must be absolute", id)
	}
	versionCommand, err := commandArray(value["version_command"], "component "+id+" version_command", false)
	if err != nil {
		return nil, err
	}
	updateCommand, err := commandArray(value["update_command"], "component "+id+" update_command", false)
	if err != nil {
		return nil, err
	}
	restartCommand, err := commandArray(value["restart_command"], "component "+id+" restart_command", false)
	if err != nil {
		return nil, err
	}
	rollbackCommand, err := commandArray(value["rollback_command"], "component "+id+" rollback_command", false)
	if err != nil {
		return nil, err
	}
	targetArg, _ := value["target_version_arg"].(string)
	if targetArg != "" && !targetVersionArgument.MatchString(targetArg) {
		return nil, fmt.Errorf("component %s target_version_arg is invalid", id)
	}
	dependencies := []string{}
	if value["dependencies"] != nil {
		dependencies, err = validateStringArray(value["dependencies"], "component "+id+" dependencies", 0)
		if err != nil {
			return nil, fmt.Errorf("component %s dependencies must be a list", id)
		}
	}
	for _, dependency := range dependencies {
		if !safeIdentifier.MatchString(dependency) {
			return nil, fmt.Errorf("component %s dependency must be a safe identifier", id)
		}
	}
	timeout := 1800 * time.Second
	if raw := value["timeout_seconds"]; raw != nil {
		seconds, ok := numericFloat(raw)
		if !ok || seconds < 1 || seconds > 3600 || seconds != float64(int(seconds)) {
			return nil, fmt.Errorf("component %s timeout_seconds is invalid", id)
		}
		timeout = time.Duration(seconds) * time.Second
	}
	enabled := true
	if raw, exists := value["enabled"]; exists {
		enabled = boolValue(raw)
	}
	public := true
	if raw, exists := value["public"]; exists {
		public = boolValue(raw)
	}
	return &Component{ID: id, Label: strings.TrimSpace(label), Kind: strings.TrimSpace(kind), Health: health, VersionFile: versionFile, VersionCommand: versionCommand, UpdateCommand: updateCommand, TargetVersionArg: targetArg, RestartCommand: restartCommand, RollbackCommand: rollbackCommand, Dependencies: dependencies, Enabled: enabled, Public: public, Timeout: timeout}, nil
}

func commandArray(value any, field string, required bool) ([]string, error) {
	if value == nil && !required {
		return nil, nil
	}
	items, ok := value.([]any)
	if !ok || len(items) == 0 {
		return nil, fmt.Errorf("%s must be a non-empty argv array", field)
	}
	result := make([]string, 0, len(items))
	for _, item := range items {
		text, ok := item.(string)
		if !ok {
			return nil, fmt.Errorf("%s must be a non-empty argv array", field)
		}
		result = append(result, text)
	}
	if !filepath.IsAbs(result[0]) {
		return nil, fmt.Errorf("%s[0] must be an absolute executable path", field)
	}
	return result, nil
}

func numericFloat(value any) (float64, bool) {
	switch number := value.(type) {
	case int:
		return float64(number), true
	case int64:
		return float64(number), true
	case float64:
		return number, true
	case json.Number:
		parsed, err := number.Float64()
		return parsed, err == nil
	}
	return 0, false
}

func (config *EnvironmentConfig) ComponentMap() map[string]*Component {
	result := map[string]*Component{}
	for _, component := range config.Components {
		result[component.ID] = component
	}
	return result
}
func (config *EnvironmentConfig) PublicConfig() Object {
	return Object{"version": EnvironmentVersion, "vm_id": config.VMID, "public_origin": config.PublicOrigin, "schedule": Object{"time": "04:00", "timezone": "UTC+8", "persistent": true}, "snapshot": Object{"enabled": boolValue(config.Snapshot["enabled"])}}
}

func commandEnvironment() []string { return []string{"PATH=" + fixedEnvironmentPath} }
func runCommand(argv []string, timeout time.Duration, headers map[string]string) (int, string, string, error) {
	if len(argv) == 0 || argv[0] == "" {
		return 1, "", "", fmt.Errorf("command is empty")
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Env = commandEnvironment()
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	if ctx.Err() == context.DeadlineExceeded {
		return 1, stdout.String(), stderr.String(), fmt.Errorf("command timed out after %d seconds", int(timeout.Seconds()))
	}
	if err == nil {
		return 0, stdout.String(), stderr.String(), nil
	}
	if exit, ok := err.(*exec.ExitError); ok {
		return exit.ExitCode(), stdout.String(), stderr.String(), nil
	}
	return 1, stdout.String(), stderr.String(), err
}

func Redact(value string, limit int) string {
	replacements := []*regexp.Regexp{regexp.MustCompile(`(?i)(token|secret|password|api[_-]?key|private[_-]?key)([=:])[^\s,;]+`), regexp.MustCompile(`(?i)bearer\s+[^\s]+`)}
	value = replacements[0].ReplaceAllString(value, "$1$2[redacted]")
	value = replacements[1].ReplaceAllString(value, "Bearer [redacted]")
	value = strings.Join(strings.Fields(value), " ")
	runes := []rune(value)
	if len(runes) > limit {
		runes = runes[len(runes)-limit:]
	}
	return string(runes)
}

func (component *Component) Version() string {
	var value string
	if component.VersionFile != "" {
		data, err := os.ReadFile(component.VersionFile)
		if err != nil {
			return ""
		}
		value = strings.TrimSpace(string(data))
	} else if len(component.VersionCommand) > 0 {
		code, out, stderr, err := runCommand(component.VersionCommand, 15*time.Second, nil)
		if err != nil || code != 0 {
			return ""
		}
		value = strings.TrimSpace(out)
		if value == "" {
			value = strings.TrimSpace(stderr)
		}
		if idx := strings.IndexByte(value, '\n'); idx >= 0 {
			value = value[:idx]
		}
	} else {
		return ""
	}
	return Redact(value, 128)
}

func (component *Component) Probe() Object {
	checked := isoNow()
	healthy := false
	detail := ""
	var err error
	switch component.Health["type"] {
	case "systemd":
		unit, _ := component.Health["unit"].(string)
		code, out, stderr, runErr := runCommand([]string{"/usr/bin/systemctl", "is-active", "--quiet", unit}, 15*time.Second, nil)
		err = runErr
		healthy = code == 0
		if healthy {
			detail = "active"
		} else {
			detail = Redact(firstNonEmpty(out, stderr, "inactive"), 160)
		}
	case "command":
		command, _ := commandArray(component.Health["command"], "health.command", true)
		code, out, stderr, runErr := runCommand(command, 15*time.Second, nil)
		err = runErr
		healthy = code == 0
		if healthy {
			detail = "ok"
		} else {
			detail = Redact(firstNonEmpty(stderr, out, "command failed"), 160)
		}
	case "http":
		endpoint, _ := component.Health["url"].(string)
		request, requestErr := http.NewRequest(http.MethodGet, endpoint, nil)
		if requestErr != nil {
			err = requestErr
			break
		}
		for key, value := range object(component.Health["headers"]) {
			if text, ok := value.(string); ok {
				request.Header.Set(key, text)
			}
		}
		client := http.Client{Timeout: 5 * time.Second}
		response, responseErr := client.Do(request)
		if responseErr != nil {
			err = responseErr
			break
		}
		healthy = response.StatusCode >= 200 && response.StatusCode < 400
		detail = fmt.Sprintf("HTTP %d", response.StatusCode)
		response.Body.Close()
	}
	if err != nil {
		return Object{"status": "unknown", "checked_at": checked, "detail": Redact(err.Error(), 160), "version": nullableString(component.Version())}
	}
	status := "unhealthy"
	if healthy {
		status = "healthy"
	}
	return Object{"status": status, "checked_at": checked, "detail": detail, "version": nullableString(component.Version())}
}

func (component *Component) PublicStatus(probe Object) Object {
	status, _ := probe["status"].(string)
	if status != "healthy" && status != "unhealthy" && status != "unknown" && status != "degraded" && status != "updating" {
		status = "unknown"
	}
	detail := "probe unavailable"
	if status == "healthy" {
		detail = "healthy"
	} else if status == "unhealthy" {
		detail = "unhealthy"
	}
	return Object{"id": component.ID, "label": component.Label, "kind": component.Kind, "status": status, "version": probe["version"], "checked_at": probe["checked_at"], "detail": detail, "update_supported": component.UpdateCommand != nil, "target_version_supported": component.UpdateCommand != nil && component.TargetVersionArg != "", "restart_supported": component.RestartCommand != nil, "rollback_supported": component.RollbackCommand != nil, "dependencies": component.Dependencies, "enabled": component.Enabled}
}
func nullableString(value string) any {
	if value == "" {
		return nil
	}
	return value
}
func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func EnvironmentHealth(config *EnvironmentConfig, includePrivate bool) Object {
	statuses := []Object{}
	hasIssue, hasHealthy := false, false
	for _, component := range config.Components {
		if !component.Enabled || (!includePrivate && !component.Public) {
			continue
		}
		status := component.PublicStatus(component.Probe())
		statuses = append(statuses, status)
		if status["status"] == "unhealthy" || status["status"] == "unknown" {
			hasIssue = true
		}
		if status["status"] == "healthy" {
			hasHealthy = true
		}
	}
	overall := "healthy"
	if hasIssue {
		overall = "unhealthy"
		if hasHealthy {
			overall = "degraded"
		}
	}
	return Object{"service": "agent-environment", "version": EnvironmentVersion, "vm_id": config.VMID, "status": overall, "checked_at": isoNow(), "components": statuses}
}

type RunStore struct{ Root, StatePath, HistoryPath, LockPath string }

func NewRunStore(root string) (*RunStore, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(root, 0o750); err != nil {
		return nil, err
	}
	return &RunStore{Root: root, StatePath: filepath.Join(root, "runs.json"), HistoryPath: filepath.Join(root, "runs.jsonl"), LockPath: filepath.Join(root, ".runs.lock")}, nil
}
func (store *RunStore) read() (Object, error) {
	state, err := readJSONOr(store.StatePath, Object{"runs": []any{}})
	if err != nil {
		return nil, fmt.Errorf("invalid update run state")
	}
	runs, ok := state["runs"].([]any)
	if !ok {
		return nil, fmt.Errorf("invalid update run state")
	}
	state["runs"] = runs
	return state, nil
}
func (store *RunStore) Create(trigger string, componentIDs []string, requestedBy, action string, targetVersions Object, authorization Object) (Object, error) {
	if targetVersions == nil {
		targetVersions = Object{}
	}
	if authorization == nil {
		authorization = Object{}
	}
	run := Object{"id": randomUUID(), "trigger": trigger, "component_ids": componentIDs, "action": action, "target_versions": targetVersions, "authorization": authorization, "requested_by": requestedBy, "status": "queued", "created_at": isoNow(), "started_at": nil, "finished_at": nil, "components": []any{}, "publication": nil, "error": nil}
	err := Lock(store.LockPath, false, func() error {
		state, err := store.read()
		if err != nil {
			return err
		}
		runs := listValue(state["runs"])
		if len(runs) > 99 {
			runs = runs[:99]
		}
		state["runs"] = append([]any{run}, runs...)
		return WriteJSON(store.StatePath, state, 0o640)
	})
	return run, err
}
func (store *RunStore) Get(runID string) (Object, error) {
	if !runIdentifier.MatchString(runID) {
		return nil, nil
	}
	var found Object
	err := Lock(store.LockPath, false, func() error {
		state, err := store.read()
		if err != nil {
			return err
		}
		for _, raw := range listValue(state["runs"]) {
			item := object(raw)
			if item["id"] == runID {
				found = item
				break
			}
		}
		return nil
	})
	return found, err
}
func (store *RunStore) Update(runID string, changes Object) (Object, error) {
	var updated Object
	err := Lock(store.LockPath, false, func() error {
		state, err := store.read()
		if err != nil {
			return err
		}
		runs := listValue(state["runs"])
		for index, raw := range runs {
			item := object(raw)
			if item["id"] != runID {
				continue
			}
			updated = Object{}
			for key, value := range item {
				updated[key] = value
			}
			for key, value := range changes {
				updated[key] = value
			}
			runs[index] = updated
			state["runs"] = runs
			if err := WriteJSON(store.StatePath, state, 0o640); err != nil {
				return err
			}
			status, _ := updated["status"].(string)
			if status == "completed" || status == "partial_failure" || status == "failed" || status == "blocked" {
				data, err := CanonicalJSON(updated, false, false)
				if err != nil {
					return err
				}
				file, err := os.OpenFile(store.HistoryPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o640)
				if err != nil {
					return err
				}
				_, writeErr := file.Write(append(data, '\n'))
				_ = file.Chmod(0o640)
				closeErr := file.Close()
				if writeErr != nil {
					return writeErr
				}
				if closeErr != nil {
					return closeErr
				}
			}
			return nil
		}
		return fmt.Errorf("update run not found: %s", runID)
	})
	return updated, err
}
func (store *RunStore) List(limit int) ([]Object, error) {
	var result []Object
	if limit < 1 {
		limit = 1
	}
	if limit > 100 {
		limit = 100
	}
	err := Lock(store.LockPath, false, func() error {
		state, err := store.read()
		if err != nil {
			return err
		}
		for _, raw := range listValue(state["runs"]) {
			if len(result) >= limit {
				break
			}
			if item := object(raw); item != nil {
				result = append(result, item)
			}
		}
		return nil
	})
	return result, err
}
func (store *RunStore) WritePolicy(value Object) error {
	return WriteJSON(filepath.Join(store.Root, "policy.json"), value, 0o640)
}
func (store *RunStore) ReadPolicy() (Object, error) {
	value, err := readJSONOr(filepath.Join(store.Root, "policy.json"), nil)
	return value, err
}

func ComponentOrder(config *EnvironmentConfig, requested []string) ([]*Component, error) {
	known := config.ComponentMap()
	selected := map[string]bool{}
	var include func(string) error
	include = func(id string) error {
		component, ok := known[id]
		if !ok {
			return fmt.Errorf("unknown component: %s", id)
		}
		if selected[id] {
			return nil
		}
		selected[id] = true
		for _, dependency := range component.Dependencies {
			if err := include(dependency); err != nil {
				return err
			}
		}
		return nil
	}
	for _, id := range requested {
		if err := include(id); err != nil {
			return nil, err
		}
	}
	result := []*Component{}
	visiting, visited := map[string]bool{}, map[string]bool{}
	var visit func(string) error
	visit = func(id string) error {
		if visited[id] {
			return nil
		}
		if visiting[id] {
			return fmt.Errorf("component dependency cycle detected")
		}
		visiting[id] = true
		for _, dependency := range known[id].Dependencies {
			if selected[dependency] {
				if err := visit(dependency); err != nil {
					return err
				}
			}
		}
		delete(visiting, id)
		visited[id] = true
		result = append(result, known[id])
		return nil
	}
	for _, id := range requested {
		if err := visit(id); err != nil {
			return nil, err
		}
	}
	return result, nil
}

func ValidateTargetVersions(config *EnvironmentConfig, componentIDs []string, raw any) (Object, error) {
	if raw == nil {
		return Object{}, nil
	}
	values, ok := raw.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("target_versions must be an object")
	}
	selected := map[string]bool{}
	for _, id := range componentIDs {
		selected[id] = true
	}
	known := config.ComponentMap()
	result := Object{}
	for id, value := range values {
		version, ok := value.(string)
		if !ok || !selected[id] {
			return nil, fmt.Errorf("target_versions must only contain selected components")
		}
		if !targetVersionName.MatchString(version) {
			return nil, fmt.Errorf("target version is invalid: %s", id)
		}
		component := known[id]
		if component == nil || component.TargetVersionArg == "" {
			return nil, fmt.Errorf("component does not support exact target versions: %s", id)
		}
		result[id] = version
	}
	return result, nil
}

func sortedStrings(values []string) []string {
	result := append([]string(nil), values...)
	sort.Strings(result)
	return result
}
func sha256Hex(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}
