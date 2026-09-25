package ade

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"html"
	"os"
	"os/exec"
	"reflect"
	"strings"
	"time"
)

func ControlMessage(config *EnvironmentConfig, action string, componentIDs []string, targetVersions Object, nonce, issuedAt, expiresAt, policyID string) string {
	ids := sortedStrings(componentIDs)
	targets := Object{}
	for key, value := range targetVersions {
		targets[key] = value
	}
	payload := Object{"action": action, "component_ids": ids, "expires_at": expiresAt, "issued_at": issuedAt, "nonce": nonce, "policy_id": nullableString(policyID), "target_versions": targets, "vm_id": config.VMID}
	encoded, _ := CanonicalJSON(payload, false, false)
	return "ADE-ENVIRONMENT-CONTROL-V1\n" + string(encoded)
}

func ControlPayload(message string) (Object, error) {
	prefix := "ADE-ENVIRONMENT-CONTROL-V1\n"
	if !strings.HasPrefix(message, prefix) {
		return nil, fmt.Errorf("control message has an invalid prefix")
	}
	payload, err := DecodeJSON([]byte(strings.TrimPrefix(message, prefix)))
	if err != nil {
		return nil, fmt.Errorf("control message is not valid JSON")
	}
	required := []string{"action", "component_ids", "expires_at", "issued_at", "nonce", "policy_id", "target_versions", "vm_id"}
	if len(payload) != len(required) {
		return nil, fmt.Errorf("control message fields are invalid")
	}
	for _, key := range required {
		if _, ok := payload[key]; !ok {
			return nil, fmt.Errorf("control message fields are invalid")
		}
	}
	return payload, nil
}

func ReadEnvironmentPolicy(config *EnvironmentConfig) (Object, error) {
	authority, err := NewEnvironmentAuthority(config)
	if err != nil {
		return nil, err
	}
	return authority.ReadPolicy()
}
func verifyManualRun(config *EnvironmentConfig, run Object) (bool, string) {
	authority, err := NewEnvironmentAuthority(config)
	if err != nil {
		return false, err.Error()
	}
	return authority.ConsumeRun(run)
}

type UpdateCoordinator struct {
	Config *EnvironmentConfig
	Store  *RunStore
}

func NewUpdateCoordinator(config *EnvironmentConfig, store *RunStore) *UpdateCoordinator {
	return &UpdateCoordinator{Config: config, Store: store}
}

func (c *UpdateCoordinator) CreateRun(trigger string, componentIDs []string, requestedBy, action string, targetVersions Object, authorization Object) (Object, error) {
	if trigger == "scheduled" && componentIDs == nil {
		policy, err := ReadEnvironmentPolicy(c.Config)
		if err != nil {
			return nil, err
		}
		componentIDs = []string{}
		for _, raw := range listValue(policy["components"]) {
			if id, ok := raw.(string); ok {
				componentIDs = append(componentIDs, id)
			}
		}
		targetVersions = object(policy["target_versions"])
	} else if componentIDs == nil {
		componentIDs = []string{}
		for _, component := range c.Config.Components {
			if component.Enabled && component.UpdateCommand != nil {
				componentIDs = append(componentIDs, component.ID)
			}
		}
		if targetVersions == nil {
			targetVersions = Object{}
		}
	}
	if action == "" {
		action = "update"
	}
	if action != "update" && action != "restart" {
		return nil, fmt.Errorf("unsupported update action")
	}
	known := c.Config.ComponentMap()
	for _, id := range componentIDs {
		component := known[id]
		if component == nil || !component.Enabled {
			return nil, fmt.Errorf("unknown component: %s", id)
		}
		if action == "update" && component.UpdateCommand == nil {
			return nil, fmt.Errorf("component has no update command: %s", id)
		}
		if action == "restart" && component.RestartCommand == nil {
			return nil, fmt.Errorf("component has no restart command: %s", id)
		}
	}
	if _, err := ComponentOrder(c.Config, componentIDs); err != nil {
		return nil, err
	}
	targets, err := ValidateTargetVersions(c.Config, componentIDs, targetVersions)
	if err != nil {
		return nil, err
	}
	return c.Store.Create(trigger, componentIDs, requestedBy, action, targets, authorization)
}

func (c *UpdateCoordinator) Execute(runID, expectedTrigger string) (Object, error) {
	run, err := c.Store.Get(runID)
	if err != nil {
		return nil, err
	}
	if run == nil {
		return nil, fmt.Errorf("update run not found: %s", runID)
	}
	if expectedTrigger != "" && run["trigger"] != expectedTrigger {
		return nil, fmt.Errorf("update run trigger does not match the privileged invocation")
	}
	trigger, _ := run["trigger"].(string)
	if trigger != "scheduled" && trigger != "manual" {
		return nil, fmt.Errorf("unsupported update trigger")
	}
	if run["status"] != "queued" {
		return nil, fmt.Errorf("update run has already started")
	}
	if trigger == "scheduled" {
		policy, err := ReadEnvironmentPolicy(c.Config)
		if err != nil {
			return nil, err
		}
		valid, reason := VerifyPolicy(c.Config, policy)
		if !valid {
			return c.blocked(runID, reason)
		}
		if !sameStringSet(listStrings(run["component_ids"]), listStrings(policy["components"])) || run["action"] != "update" || !reflect.DeepEqual(object(run["target_versions"]), object(policy["target_versions"])) {
			return c.blocked(runID, "scheduled run does not match the approved policy")
		}
	}
	if trigger == "manual" {
		valid, reason := verifyManualRun(c.Config, run)
		if !valid {
			return c.blocked(runID, reason)
		}
	}
	if _, err := c.Store.Update(runID, Object{"status": "running", "started_at": isoNow()}); err != nil {
		return nil, err
	}
	ordered, err := ComponentOrder(c.Config, listStrings(run["component_ids"]))
	if err != nil {
		return nil, err
	}
	action, _ := run["action"].(string)
	targets := object(run["target_versions"])
	results := []any{}
	failed := map[string]bool{}
	for _, component := range ordered {
		dependencyFailed := false
		for _, dependency := range component.Dependencies {
			if failed[dependency] {
				dependencyFailed = true
			}
		}
		if dependencyFailed {
			results = append(results, Object{"id": component.ID, "status": "blocked", "detail": "dependency failed"})
			failed[component.ID] = true
			continue
		}
		command := component.UpdateCommand
		if action == "restart" {
			command = component.RestartCommand
		}
		if len(command) == 0 {
			results = append(results, Object{"id": component.ID, "status": "skipped", "detail": "no " + action + " command declared"})
			continue
		}
		selected := append([]string(nil), command...)
		target := targets[component.ID]
		if action == "update" && target != nil {
			version, ok := target.(string)
			if !ok || component.TargetVersionArg == "" {
				results = append(results, Object{"id": component.ID, "status": "failed", "action": action, "detail": "component does not support exact target versions"})
				failed[component.ID] = true
				continue
			}
			selected = append(selected, component.TargetVersionArg, version)
		}
		code, stdout, stderr, runErr := runCommand(selected, component.Timeout, nil)
		probe := component.Probe()
		ok := runErr == nil && code == 0 && probe["status"] == "healthy"
		var rollback any
		if !ok && action == "update" && component.RollbackCommand != nil {
			rollbackCode, _, _, rollbackErr := runCommand(component.RollbackCommand, component.Timeout, nil)
			if rollbackErr == nil && rollbackCode == 0 {
				rollback = "completed"
			} else {
				rollback = "failed"
			}
			probe = component.Probe()
		}
		status := "failed"
		detail := firstNonEmpty(stderr, stdout, fmt.Sprint(probe["detail"]), "update failed")
		if ok {
			status = "completed"
			detail = "ok"
		}
		item := Object{"id": component.ID, "status": status, "action": action, "returncode": code, "version": probe["version"], "health": probe["status"], "rollback": rollback, "detail": Redact(detail, 280)}
		results = append(results, item)
		if !ok {
			failed[component.ID] = true
		}
	}
	failedCount := len(failed)
	status := "completed"
	errorValue := any(nil)
	if failedCount > 0 {
		completed := false
		for _, value := range results {
			if object(value)["status"] == "completed" {
				completed = true
			}
		}
		status = "failed"
		if completed {
			status = "partial_failure"
		}
		errorValue = "one or more components failed"
	}
	finished, err := c.Store.Update(runID, Object{"status": status, "finished_at": isoNow(), "components": results, "error": errorValue})
	if err != nil {
		return nil, err
	}
	health := EnvironmentHealth(c.Config, false)
	publication := PublishSnapshot(c.Config, health, finished)
	if publication != nil {
		return c.Store.Update(runID, Object{"publication": publication})
	}
	return finished, nil
}

func (c *UpdateCoordinator) blocked(runID, reason string) (Object, error) {
	blocked, err := c.Store.Update(runID, Object{"status": "blocked", "finished_at": isoNow(), "error": reason})
	if err != nil {
		return nil, err
	}
	publication := PublishSnapshot(c.Config, EnvironmentHealth(c.Config, false), blocked)
	if publication != nil {
		return c.Store.Update(runID, Object{"publication": publication})
	}
	return blocked, nil
}

func listStrings(value any) []string {
	result := []string{}
	for _, item := range listValue(value) {
		if text, ok := item.(string); ok {
			result = append(result, text)
		}
	}
	return result
}
func sameStringSet(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	a := sortedStrings(left)
	b := sortedStrings(right)
	return reflect.DeepEqual(a, b)
}

func PublishSnapshot(config *EnvironmentConfig, health Object, run Object) Object {
	if !boolValue(config.Snapshot["enabled"]) {
		return nil
	}
	name, _ := config.Snapshot["name"].(string)
	if name == "" {
		name = "environment-" + config.VMID
	}
	root, _ := config.Snapshot["artifact_root"].(string)
	if root == "" {
		root = DefaultArtifactRoot
	}
	base, _ := config.Snapshot["base_url"].(string)
	if base == "" {
		base = DefaultArtifactBaseURL
	}
	file, err := os.CreateTemp("", "agent-environment-snapshot-*.html")
	if err != nil {
		return Object{"status": "blocked", "error": "publish blocked: " + Redact(err.Error(), 280)}
	}
	path := file.Name()
	defer os.Remove(path)
	if _, err := file.WriteString(renderSnapshot(config, health, run)); err != nil {
		file.Close()
		return Object{"status": "blocked", "error": "publish blocked: " + Redact(err.Error(), 280)}
	}
	_ = file.Chmod(0o644)
	_ = file.Close()
	executable := os.Getenv("ADE_BIN")
	if executable == "" {
		executable = "/usr/local/bin/ade"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/usr/sbin/runuser", "-u", "orca", "--", "/usr/bin/env", "HOME=/home/orca", "ADE_WEB_ARTIFACT_ROOT="+root, "ADE_WEB_ARTIFACT_BASE_URL="+base, executable, "publish-html", "publish", path, "--name", name, "--artifact-root", root, "--base-url", base)
	command.Env = append(os.Environ(), "ADE_WEB_ARTIFACT_ROOT="+root, "ADE_WEB_ARTIFACT_BASE_URL="+base)
	var stdout, stderr strings.Builder
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		return Object{"status": "blocked", "error": "publish blocked: " + Redact(firstNonEmpty(stderr.String(), stdout.String(), "publisher unavailable"), 280)}
	}
	receipt, err := DecodeJSON([]byte(stdout.String()))
	if err != nil {
		return Object{"status": "blocked", "error": "publish blocked: invalid publisher receipt"}
	}
	result := Object{"status": "published"}
	for key, value := range receipt {
		result[key] = value
	}
	return result
}

func renderSnapshot(config *EnvironmentConfig, health Object, run Object) string {
	rows := []string{}
	for _, raw := range listValue(health["components"]) {
		item := object(raw)
		status := html.EscapeString(fmt.Sprint(item["status"]))
		label := html.EscapeString(firstNonEmpty(fmt.Sprint(item["label"]), fmt.Sprint(item["id"])))
		version := html.EscapeString(firstNonEmpty(fmt.Sprint(item["version"]), "unknown"))
		checked := html.EscapeString(fmt.Sprint(item["checked_at"]))
		rows = append(rows, "<tr><td>"+label+"</td><td><span class=\"status "+status+"\">"+status+"</span></td><td>"+version+"</td><td>"+checked+"</td></tr>")
	}
	runText := "No update run recorded"
	if run != nil {
		runText = html.EscapeString(fmt.Sprint(run["status"])) + " · " + html.EscapeString(firstNonEmpty(fmt.Sprint(run["finished_at"]), fmt.Sprint(run["created_at"])))
	}
	vm := html.EscapeString(config.VMID)
	status := html.EscapeString(fmt.Sprint(health["status"]))
	return "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>ADES · " + vm + "</title><style>body{font:15px system-ui,sans-serif;background:#101318;color:#e8edf2;margin:0;padding:32px}main{max-width:960px;margin:auto}h1{font-size:28px;margin:0 0 8px}p{color:#aab5c1}table{width:100%;border-collapse:collapse;background:#171c23;border:1px solid #2a3440;border-radius:12px;overflow:hidden}th,td{padding:13px 15px;text-align:left;border-bottom:1px solid #2a3440}th{color:#aab5c1;font-size:12px;text-transform:uppercase;letter-spacing:.08em}.status{border-radius:999px;padding:4px 9px;font-size:12px}.healthy{background:#123d2a;color:#7ce2a5}.degraded,.unknown{background:#433514;color:#ffd37a}.unhealthy{background:#4b1e27;color:#ff9aa8}code{color:#9fd4ff}</style></head><body><main><h1>ADES</h1><p>Agent Development Environment Service</p><p>VM <code>" + vm + "</code> · overall <strong>" + status + "</strong></p><table><thead><tr><th>Component</th><th>Health</th><th>Version</th><th>Checked</th></tr></thead><tbody>" + strings.Join(rows, "") + "</tbody></table><p>Last update run: " + runText + "</p></main></body></html>"
}

func RunPublic(run Object, config *EnvironmentConfig) Object {
	public := map[string]bool{}
	for _, component := range config.Components {
		if component.Public {
			public[component.ID] = true
		}
	}
	result := Object{}
	for _, field := range []string{"id", "trigger", "action", "status", "created_at", "started_at", "finished_at", "error", "publication", "target_versions"} {
		if value, ok := run[field]; ok {
			result[field] = value
		}
	}
	components := []any{}
	for _, raw := range listValue(run["components"]) {
		item := object(raw)
		id, _ := item["id"].(string)
		if !public[id] {
			continue
		}
		safe := Object{}
		for _, field := range []string{"id", "status", "action", "returncode", "version", "health", "rollback", "detail"} {
			if value, ok := item[field]; ok {
				safe[field] = value
			}
		}
		components = append(components, safe)
	}
	ids := []any{}
	for _, raw := range listValue(run["component_ids"]) {
		id, _ := raw.(string)
		if public[id] {
			ids = append(ids, id)
		}
	}
	result["component_ids"] = ids
	result["components"] = components
	return result
}

func EnvironmentStatus(config *EnvironmentConfig) (Object, error) {
	store, err := NewRunStore(config.StateRoot)
	if err != nil {
		return nil, err
	}
	runs, err := store.List(50)
	if err != nil {
		return nil, err
	}
	policy, err := ReadEnvironmentPolicy(config)
	if err != nil {
		return nil, err
	}
	publicRuns := []Object{}
	for _, run := range runs {
		publicRuns = append(publicRuns, RunPublic(run, config))
	}
	return Object{"config": config.PublicConfig(), "health": EnvironmentHealth(config, false), "runs": publicRuns, "policy": PolicyPublic(policy, config)}, nil
}

func EnrollWallet(config *EnvironmentConfig, address, role string) (Object, error) {
	if os.Geteuid() != 0 {
		return nil, fmt.Errorf("environment wallet enrollment must run as root")
	}
	address, err := NormalizeWalletAddress(address)
	if err != nil {
		return nil, err
	}
	if role != "viewer" && role != "operator" && role != "admin" {
		return nil, fmt.Errorf("wallet role is invalid")
	}
	raw, err := ReadJSON(config.Path)
	if err != nil {
		return nil, err
	}
	wallets := object(raw["wallets"])
	if wallets == nil {
		wallets = Object{}
		raw["wallets"] = wallets
	}
	entries := listValue(wallets["authorized"])
	if wallets["authorized"] != nil && entries == nil {
		return nil, fmt.Errorf("wallets.authorized must be a list")
	}
	filtered := []any{}
	for _, entry := range entries {
		item := object(entry)
		if item == nil {
			filtered = append(filtered, entry)
			continue
		}
		existing, _ := item["address"].(string)
		if strings.ToLower(existing) != address {
			filtered = append(filtered, entry)
		}
	}
	filtered = append(filtered, Object{"address": address, "role": role})
	wallets["authorized"] = filtered
	info, err := os.Stat(config.Path)
	if err != nil {
		return nil, err
	}
	mode := info.Mode().Perm()
	if mode == 0 {
		mode = 0o640
	}
	if err := WriteJSON(config.Path, raw, mode); err != nil {
		return nil, err
	}
	return Object{"address": address, "role": role, "config": config.Path}, nil
}

func FailRun(root, runID, message string) (Object, error) {
	if !runIdentifier.MatchString(runID) {
		return nil, fmt.Errorf("invalid update run id")
	}
	store, err := NewRunStore(root)
	if err != nil {
		return nil, err
	}
	return store.Update(runID, Object{"status": "failed", "finished_at": isoNow(), "error": Redact(message, 280)})
}

func UseRandomNonce(size int) string {
	value := make([]byte, size)
	_, _ = rand.Read(value)
	return base64.RawURLEncoding.EncodeToString(value)
}
