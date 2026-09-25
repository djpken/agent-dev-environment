package ade

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"time"
)

var providerID = regexp.MustCompile(`^[a-z][a-z0-9-]{0,63}$`)
var semver = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)
var permissionNames = map[string]bool{
	"workspace-read": true, "workspace-write": true, "local-state": true,
	"llm-network": true, "external-network": true, "listen-loopback": true,
}

func object(value any) Object {
	result, _ := value.(map[string]any)
	return result
}

func stringValue(value any) (string, bool) {
	result, ok := value.(string)
	return result, ok
}

func listValue(value any) []any {
	result, _ := value.([]any)
	return result
}

func boolValue(value any) bool { result, _ := value.(bool); return result }

func cloneObject(value Object) (Object, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	return DecodeJSON(data)
}

func validateStringArray(value any, field string, min int) ([]string, error) {
	items, ok := value.([]any)
	if !ok || len(items) < min {
		return nil, fmt.Errorf("%s must be an array with at least %d item(s)", field, min)
	}
	result := make([]string, 0, len(items))
	seen := map[string]bool{}
	for _, item := range items {
		text, ok := item.(string)
		if !ok || (min > 0 && text == "") {
			return nil, fmt.Errorf("%s items must be non-empty text", field)
		}
		if seen[text] {
			return nil, fmt.Errorf("%s items must be unique", field)
		}
		seen[text] = true
		result = append(result, text)
	}
	return result, nil
}

func ValidateManifest(value Object) error {
	allowed := map[string]bool{"id": true, "version": true, "capabilities": true, "lifecycle": true, "transport": true, "command": true, "health": true, "url": true, "permissions": true, "env_vars": true, "builtin": true, "schedule": true}
	for key := range value {
		if !allowed[key] {
			return fmt.Errorf("invalid provider manifest: additional property %q", key)
		}
	}
	id, _ := stringValue(value["id"])
	version, _ := stringValue(value["version"])
	lifecycle, _ := stringValue(value["lifecycle"])
	transport, _ := stringValue(value["transport"])
	if !providerID.MatchString(id) {
		return fmt.Errorf("invalid provider manifest: id does not match required pattern")
	}
	if !semver.MatchString(version) {
		return fmt.Errorf("invalid provider manifest: version does not match required pattern")
	}
	if _, err := validateStringArray(value["capabilities"], "capabilities", 1); err != nil {
		return fmt.Errorf("invalid provider manifest: %w", err)
	}
	if _, err := validateStringArray(value["command"], "command", 1); err != nil {
		return fmt.Errorf("invalid provider manifest: %w", err)
	}
	if _, err := validateStringArray(value["health"], "health", 1); err != nil {
		return fmt.Errorf("invalid provider manifest: %w", err)
	}
	if _, err := validateStringArray(value["permissions"], "permissions", 0); err != nil {
		return fmt.Errorf("invalid provider manifest: %w", err)
	}
	for _, permission := range listValue(value["permissions"]) {
		text, _ := stringValue(permission)
		if !permissionNames[text] {
			return fmt.Errorf("invalid provider manifest: unsupported permission %q", text)
		}
	}
	if lifecycle != "host-spawned" && lifecycle != "shared-local" {
		return fmt.Errorf("invalid provider manifest: lifecycle is unsupported")
	}
	if transport != "cli" && transport != "stdio" && transport != "http" {
		return fmt.Errorf("invalid provider manifest: transport is unsupported")
	}
	if lifecycle == "shared-local" || transport == "http" {
		u, ok := value["url"].(string)
		if !ok || lifecycle != "shared-local" || transport != "http" {
			return fmt.Errorf("invalid provider manifest: shared-local providers require HTTP transport and url")
		}
		parsed, err := url.Parse(u)
		if err != nil || parsed.Scheme != "http" || parsed.Hostname() != "127.0.0.1" || parsed.Port() == "" || parsed.User != nil || !strings.HasPrefix(parsed.Path, "/") {
			return fmt.Errorf("shared services must use a loopback endpoint")
		}
	}
	if builtin, ok := value["builtin"]; ok {
		if _, valid := builtin.(bool); !valid {
			return fmt.Errorf("invalid provider manifest: builtin must be a boolean")
		}
	}
	if envVars, ok := value["env_vars"]; ok {
		items, err := validateStringArray(envVars, "env_vars", 0)
		if err != nil {
			return fmt.Errorf("invalid provider manifest: %w", err)
		}
		pattern := regexp.MustCompile(`^[A-Z][A-Z0-9_]*$`)
		for _, item := range items {
			if !pattern.MatchString(item) {
				return fmt.Errorf("invalid provider manifest: env_vars item is invalid")
			}
		}
	}
	if scheduleValue, ok := value["schedule"]; ok {
		schedule := object(scheduleValue)
		if schedule == nil {
			return fmt.Errorf("invalid provider manifest: schedule must be an object")
		}
		for key := range schedule {
			if key != "times" && key != "timezone" {
				return fmt.Errorf("invalid provider manifest: schedule contains an unknown property")
			}
		}
		times, err := validateStringArray(schedule["times"], "schedule.times", 1)
		if err != nil {
			return fmt.Errorf("invalid provider manifest: %w", err)
		}
		for _, item := range times {
			if !regexp.MustCompile(`^(?:[01][0-9]|2[0-3]):[0-5][0-9]$`).MatchString(item) {
				return fmt.Errorf("invalid provider manifest: schedule time is invalid")
			}
		}
		if timezone, ok := schedule["timezone"]; ok {
			text, yes := timezone.(string)
			if !yes || text == "" {
				return fmt.Errorf("invalid provider manifest: schedule timezone is invalid")
			}
		}
	}
	return nil
}

func Compose(lock, user, workspace Object) (Object, error) {
	version, ok := lock["schema_version"].(json.Number)
	if !ok || version.String() != "1" {
		return nil, fmt.Errorf("unsupported lockfile version")
	}
	for key := range workspace {
		if key != "providers" && key != "prompts" {
			return nil, fmt.Errorf("workspace may only select providers and prompts; grants/endpoints belong to user config")
		}
	}
	for key := range user {
		if key != "providers" && key != "prompts" && key != "grants" && key != "llm_endpoint" && key != "extensions" {
			return nil, fmt.Errorf("unknown user config field")
		}
	}
	providers := Object{}
	locked := listValue(lock["providers"])
	extensions := listValue(user["extensions"])
	for index, raw := range append(append([]any{}, locked...), extensions...) {
		manifest, ok := raw.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("invalid provider manifest: expected object")
		}
		manifest, err := cloneObject(manifest)
		if err != nil {
			return nil, err
		}
		if err := ValidateManifest(manifest); err != nil {
			return nil, err
		}
		if boolValue(manifest["builtin"]) && index >= len(locked) {
			return nil, fmt.Errorf("user extensions may not declare builtin providers")
		}
		id, _ := stringValue(manifest["id"])
		if _, exists := providers[id]; exists {
			return nil, fmt.Errorf("duplicate provider namespace: %s", id)
		}
		providers[id] = manifest
	}
	defaults, ok := lock["defaults"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("lockfile defaults must be an object")
	}
	result, err := cloneObject(defaults)
	if err != nil {
		return nil, err
	}
	if object(result["providers"]) == nil {
		return nil, fmt.Errorf("defaults.providers must be an object")
	}
	for _, layer := range []Object{user, workspace} {
		selected, exists := layer["providers"]
		if exists {
			entries, ok := selected.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("providers must be an object")
			}
			for name, enabled := range entries {
				flag, isBool := enabled.(bool)
				if _, exists := providers[name]; !exists || !isBool {
					return nil, fmt.Errorf("unknown provider or invalid enabled flag")
				}
				object(result["providers"])[name] = flag
			}
		}
		if prompts, exists := layer["prompts"]; exists {
			result["prompts"] = prompts
		}
	}
	prompts, err := validateStringArray(result["prompts"], "prompts", 0)
	if err != nil {
		return nil, fmt.Errorf("prompts must be an array")
	}
	for _, name := range prompts {
		if !regexp.MustCompile(`^[A-Za-z0-9_.-]+\.md$`).MatchString(name) {
			return nil, fmt.Errorf("prompt must be a filename within the Workflow Pack")
		}
	}
	grants, exists := user["grants"]
	if !exists {
		grants = Object{}
	}
	grantMap, ok := grants.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("grants must be an object")
	}
	for name, raw := range grantMap {
		if _, exists := providers[name]; !exists {
			return nil, fmt.Errorf("invalid provider grant")
		}
		if _, err := validateStringArray(raw, "grant", 0); err != nil {
			return nil, fmt.Errorf("invalid provider grant")
		}
	}
	endpoint := user["llm_endpoint"]
	if endpoint != nil {
		text, ok := endpoint.(string)
		if !ok {
			return nil, fmt.Errorf("llm_endpoint must be a URL or null")
		}
		u, err := url.Parse(text)
		if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return nil, fmt.Errorf("invalid LLM endpoint")
		}
		if u.Scheme != "https" && u.Hostname() != "localhost" && u.Hostname() != "127.0.0.1" && u.Hostname() != "::1" {
			return nil, fmt.Errorf("non-local LLM endpoints require HTTPS")
		}
	}
	result["manifests"], result["grants"], result["llm_endpoint"] = providers, grants, endpoint
	return result, nil
}

func Authorize(config Object, name string) (Object, error) {
	providers := object(config["providers"])
	manifests := object(config["manifests"])
	grants := object(config["grants"])
	if !boolValue(providers[name]) {
		return nil, fmt.Errorf("provider disabled: %s", name)
	}
	provider := object(manifests[name])
	if provider == nil {
		return nil, fmt.Errorf("unknown provider: %s", name)
	}
	allowed := map[string]bool{}
	for _, raw := range listValue(grants[name]) {
		if text, ok := raw.(string); ok {
			allowed[text] = true
		}
	}
	missing := []string{}
	for _, raw := range listValue(provider["permissions"]) {
		text, _ := raw.(string)
		if !allowed[text] {
			missing = append(missing, text)
		}
	}
	sort.Strings(missing)
	if len(missing) > 0 {
		return nil, fmt.Errorf("missing user grants for %s: %s", name, strings.Join(missing, ", "))
	}
	for _, raw := range listValue(provider["permissions"]) {
		if raw == "llm-network" && config["llm_endpoint"] == nil {
			return nil, fmt.Errorf("set an explicit llm_endpoint in user config before running %s", name)
		}
	}
	return provider, nil
}

func Generation(root string) (string, error) {
	pointer := filepath.Join(root, "current")
	info, err := os.Lstat(pointer)
	if os.IsNotExist(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	if info.Mode()&os.ModeSymlink == 0 {
		return "", fmt.Errorf("current must be an ADE-managed symlink")
	}
	target, err := filepath.EvalSymlinks(pointer)
	if err != nil {
		return "", err
	}
	releases, err := filepath.EvalSymlinks(filepath.Join(root, "releases"))
	if err != nil {
		return "", fmt.Errorf("invalid generation pointer")
	}
	if filepath.Dir(target) != releases {
		return "", fmt.Errorf("invalid generation pointer")
	}
	if stat, err := os.Stat(target); err != nil || !stat.IsDir() {
		return "", fmt.Errorf("invalid generation pointer")
	}
	return target, nil
}

func WithTransaction(root string, fn func() error) error {
	if err := os.MkdirAll(root, 0o700); err != nil {
		return err
	}
	return Lock(filepath.Join(root, "update.lock"), false, fn)
}

func switchGeneration(root, target string) (string, error) {
	old, err := Generation(root)
	if err != nil {
		return "", err
	}
	temporary := filepath.Join(root, ".current-"+randomID())
	if err := os.Symlink(filepath.Join("releases", filepath.Base(target)), temporary); err != nil {
		return "", err
	}
	if err := os.Rename(temporary, filepath.Join(root, "current")); err != nil {
		os.Remove(temporary)
		return "", err
	}
	return old, nil
}

type BundleFile struct {
	Data       []byte
	Executable bool
}

func Bundle(repository string) (map[string]BundleFile, error) {
	root, err := filepath.EvalSymlinks(repository)
	if err != nil {
		return nil, err
	}
	paths := []string{".claude-plugin/plugin.json", "LICENSE"}
	for _, folder := range []string{"skills", "prompt"} {
		base := filepath.Join(root, folder)
		info, err := os.Lstat(base)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return nil, fmt.Errorf("missing or linked Workflow Pack directory: %s", folder)
		}
		err = filepath.WalkDir(base, func(path string, entry os.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			info, err := os.Lstat(path)
			if err != nil {
				return err
			}
			if info.Mode()&os.ModeSymlink != 0 {
				resolved, err := filepath.EvalSymlinks(path)
				if err != nil {
					return err
				}
				targetInfo, err := os.Stat(path)
				if err != nil {
					return err
				}
				if targetInfo.IsDir() {
					return fmt.Errorf("linked Workflow Pack directories are not supported")
				}
				if !within(base, resolved) {
					return fmt.Errorf("Workflow Pack link escapes its directory")
				}
				paths = append(paths, path)
			} else if entry.Type().IsRegular() {
				paths = append(paths, path)
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	sort.Strings(paths)
	result := map[string]BundleFile{}
	for _, path := range paths {
		resolved, err := filepath.EvalSymlinks(path)
		if err != nil {
			return nil, err
		}
		if !within(root, resolved) {
			return nil, fmt.Errorf("invalid Workflow Pack file: %s", path)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, err
		}
		info, err := os.Stat(path)
		if err != nil {
			return nil, err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return nil, err
		}
		result[filepath.ToSlash(rel)] = BundleFile{Data: data, Executable: info.Mode()&0o111 != 0}
	}
	return result, nil
}

func within(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func BundleID(files map[string]BundleFile) string {
	value := Object{}
	for name, file := range files {
		sum := sha256.Sum256(file.Data)
		value[name] = []any{hex.EncodeToString(sum[:]), file.Executable}
	}
	return Digest(value)
}

func Plan(lock, config Object, root, repository string) (Object, error) {
	active, err := Generation(root)
	if err != nil {
		return nil, err
	}
	workflow := object(lock["workflow"])
	var revision string
	if workflow["source"] == "bundled" {
		captured, err := Bundle(repository)
		if err != nil {
			return nil, err
		}
		revision = BundleID(captured)
	} else {
		revision, _ = stringValue(workflow["revision"])
	}
	selected := []string{}
	blocked := Object{}
	providers := object(config["providers"])
	keys := make([]string, 0, len(providers))
	for name := range providers {
		keys = append(keys, name)
	}
	sort.Strings(keys)
	for _, name := range keys {
		if !boolValue(providers[name]) {
			continue
		}
		selected = append(selected, name)
		if _, err := Authorize(config, name); err != nil {
			blocked[name] = err.Error()
		}
	}
	planInput := Object{"lock": lock, "config": config, "current": nil, "workflow": revision}
	if active != "" {
		planInput["current"] = active
	}
	planID := Digest(planInput)
	var current any
	if active != "" {
		current = active
	}
	return Object{"plan_id": planID, "current": current, "workflow_revision": revision, "providers": selected, "blocked_until_configured": blocked, "actions": []string{"stage Workflow Pack", "verify pinned artifacts", "write host exports", "switch current atomically"}}, nil
}

func extractArchive(data []byte, destination string) error {
	reader := tar.NewReader(bytes.NewReader(data))
	for {
		header, err := reader.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		name := filepath.Clean(filepath.FromSlash(header.Name))
		if filepath.IsAbs(name) || name == ".." || strings.HasPrefix(name, ".."+string(filepath.Separator)) {
			return fmt.Errorf("workflow archive path escapes destination")
		}
		target := filepath.Join(destination, name)
		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
		case tar.TypeReg, tar.TypeRegA:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			file, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, os.FileMode(header.Mode)&0o755)
			if err != nil {
				return err
			}
			_, copyErr := io.Copy(file, reader)
			closeErr := file.Close()
			if copyErr != nil {
				return copyErr
			}
			if closeErr != nil {
				return closeErr
			}
		case tar.TypeSymlink:
			link := filepath.Clean(filepath.FromSlash(header.Linkname))
			if filepath.IsAbs(link) || link == ".." || strings.HasPrefix(link, ".."+string(filepath.Separator)) {
				return fmt.Errorf("workflow archive link escapes destination")
			}
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			if err := os.Symlink(header.Linkname, target); err != nil {
				return err
			}
		default:
			return fmt.Errorf("workflow archive may not contain devices")
		}
	}
}

func Snapshot(repository, revision, destination string) error {
	if !regexp.MustCompile(`^[0-9a-f]{40}$`).MatchString(revision) {
		return fmt.Errorf("workflow revision must be a full commit SHA")
	}
	command := exec.Command("git", "-C", repository, "archive", revision)
	data, err := command.Output()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(destination, 0o755); err != nil {
		return err
	}
	return extractArchive(data, destination)
}

func Artifact(lock Object, name string) (string, string, error) {
	item := object(object(lock["artifacts"])[name])
	if item == nil {
		return "", "", fmt.Errorf("unknown artifact: %s", name)
	}
	arch := runtime.GOARCH
	if arch == "amd64" {
		arch = "x86_64"
	}
	if arch == "arm64" {
		arch = "aarch64"
	}
	platform := runtime.GOOS + "-" + arch
	pinned := object(object(item["platforms"])[platform])
	if pinned == nil {
		return "", "", fmt.Errorf("unsupported artifact platform: %s", platform)
	}
	file, _ := stringValue(pinned["file"])
	checksum, _ := stringValue(pinned["sha256"])
	if !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(checksum) {
		return "", "", fmt.Errorf("invalid SHA-256")
	}
	base, _ := stringValue(item["base_url"])
	parsed, err := url.Parse(base + file)
	if err != nil || parsed.Scheme != "https" {
		return "", "", fmt.Errorf("artifact downloads require HTTPS")
	}
	return parsed.String(), checksum, nil
}

func DownloadArtifact(address, checksum, path string) error {
	client := &http.Client{Timeout: 60 * time.Second, CheckRedirect: func(request *http.Request, via []*http.Request) error {
		if request.URL.Scheme != "https" {
			return fmt.Errorf("insecure artifact redirect")
		}
		return nil
	}}
	response, err := client.Get(address)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("artifact download returned HTTP %d", response.StatusCode)
	}
	if response.Request.URL.Scheme != "https" {
		return fmt.Errorf("insecure artifact redirect")
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(io.MultiWriter(file, hash), response.Body); err != nil {
		return err
	}
	if hex.EncodeToString(hash.Sum(nil)) != checksum {
		return fmt.Errorf("artifact checksum mismatch")
	}
	return file.Chmod(0o755)
}

func providerArgv(provider Object, release string, health bool) []string {
	field := "command"
	if health {
		field = "health"
	}
	result := []string{}
	for _, value := range listValue(provider[field]) {
		text, _ := value.(string)
		result = append(result, strings.ReplaceAll(text, "{release}", release))
	}
	return result
}

func CleanEnvironment(provider Object, source []string) []string {
	allowed := map[string]bool{"PATH": true, "LANG": true, "LC_ALL": true, "TMPDIR": true, "SYSTEMROOT": true, "HOME": true}
	for _, value := range listValue(provider["env_vars"]) {
		if text, ok := value.(string); ok {
			allowed[text] = true
		}
	}
	result := []string{}
	for _, entry := range source {
		key, _, _ := strings.Cut(entry, "=")
		if allowed[key] {
			result = append(result, entry)
		}
	}
	return result
}

func CheckProviderHealth(provider Object, release string) error {
	if boolValue(provider["builtin"]) {
		return nil
	}
	command := providerArgv(provider, release, true)
	if len(command) == 0 {
		return fmt.Errorf("provider has no health command")
	}
	cmd := exec.Command(command[0], command[1:]...)
	cmd.Env = CleanEnvironment(provider, os.Environ())
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("health command failed: %s", provider["id"])
	}
	version, _ := stringValue(provider["version"])
	pattern := regexp.MustCompile(`(?m)(?:^|[^0-9.])` + regexp.QuoteMeta(version) + `(?:$|[^0-9.])`)
	if !pattern.Match(output) {
		return fmt.Errorf("installed version does not match manifest: %s", provider["id"])
	}
	return nil
}

func providerExports(config Object, release string) (map[string]any, error) {
	claude, opencode := Object{}, Object{}
	codex := []string{}
	skipped := Object{}
	providers := object(config["providers"])
	manifests := object(config["manifests"])
	grants := object(config["grants"])
	keys := make([]string, 0, len(providers))
	for name := range providers {
		keys = append(keys, name)
	}
	sort.Strings(keys)
	for _, name := range keys {
		if !boolValue(providers[name]) {
			continue
		}
		provider, err := Authorize(config, name)
		if err != nil {
			skipped[name] = err.Error()
			continue
		}
		transport, _ := stringValue(provider["transport"])
		if transport == "cli" {
			continue
		}
		key := "ade-" + name
		if transport == "http" {
			rawURL := provider["url"]
			claude[key] = Object{"type": "http", "url": rawURL}
			opencode[key] = Object{"type": "remote", "url": rawURL, "enabled": true}
			codex = append(codex, "[mcp_servers."+key+"]", "url = "+jsonStringValue(rawURL), "")
		} else {
			binary := os.Getenv("ADE_BIN")
			if binary == "" {
				binary = "/usr/local/bin/ade"
			}
			command := []string{binary, "--root", filepath.Dir(filepath.Dir(release)), "provider", name}
			claude[key] = Object{"type": "stdio", "command": command[0], "args": command[1:]}
			opencode[key] = Object{"type": "local", "command": command, "enabled": true}
			args, _ := json.Marshal(command[1:])
			codex = append(codex, "[mcp_servers."+key+"]", "command = "+jsonStringValue(command[0]), "args = "+string(args), "")
		}
	}
	_ = manifests
	_ = grants
	return map[string]any{"claude.mcp.json": Object{"mcpServers": claude}, "opencode.json": Object{"mcp": opencode}, "codex.toml": strings.Join(codex, "\n"), "blocked.json": skipped}, nil
}

func jsonStringValue(value any) string { data, _ := json.Marshal(value); return string(data) }

func Install(lock, config Object, root, repository, expectedPlan string) (Object, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	var result Object
	err = WithTransaction(root, func() error {
		proposed, err := Plan(lock, config, root, repository)
		if err != nil {
			return err
		}
		if expectedPlan != "" && proposed["plan_id"] != expectedPlan {
			return fmt.Errorf("stale plan; run plan again")
		}
		releases := filepath.Join(root, "releases")
		if err := os.MkdirAll(releases, 0o700); err != nil {
			return err
		}
		stage, err := os.MkdirTemp(releases, ".stage-")
		if err != nil {
			return err
		}
		defer os.RemoveAll(stage)
		final := filepath.Join(releases, randomID())
		workflow := object(lock["workflow"])
		if workflow["source"] == "bundled" {
			files, err := Bundle(repository)
			if err != nil {
				return err
			}
			for name, file := range files {
				target := filepath.Join(stage, "workflow", filepath.FromSlash(name))
				if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
					return err
				}
				mode := os.FileMode(0o644)
				if file.Executable {
					mode = 0o755
				}
				if err := os.WriteFile(target, file.Data, mode); err != nil {
					return err
				}
			}
		} else {
			revision, _ := stringValue(workflow["revision"])
			if err := Snapshot(repository, revision, filepath.Join(stage, "workflow")); err != nil {
				return err
			}
		}
		if err := os.MkdirAll(filepath.Join(stage, "bin"), 0o755); err != nil {
			return err
		}
		for name, enabled := range object(config["providers"]) {
			if !boolValue(enabled) {
				continue
			}
			provider := object(object(config["manifests"])[name])
			artifacts := object(lock["artifacts"])
			if artifact, ok := artifacts[name]; ok {
				item := object(artifact)
				version, _ := stringValue(item["version"])
				if provider["version"] != version {
					return fmt.Errorf("artifact version mismatch")
				}
				address, checksum, err := Artifact(lock, name)
				if err != nil {
					return err
				}
				target := filepath.Join(stage, "bin", name)
				if err := DownloadArtifact(address, checksum, target); err != nil {
					return err
				}
				if err := CheckProviderHealth(provider, stage); err != nil {
					return err
				}
			} else {
				if _, err := Authorize(config, name); err != nil {
					return err
				}
				if !boolValue(provider["builtin"]) {
					if err := CheckProviderHealth(provider, stage); err != nil {
						return err
					}
				}
			}
		}
		parts := []string{}
		for _, raw := range listValue(config["prompts"]) {
			name, _ := raw.(string)
			path := filepath.Join(stage, "workflow", "prompt", name)
			data, err := os.ReadFile(path)
			if err != nil {
				return fmt.Errorf("unknown Workflow Pack prompt: %s", name)
			}
			parts = append(parts, string(data))
		}
		if err := os.WriteFile(filepath.Join(stage, "personal-prompt.md"), []byte(strings.Join(parts, "\n\n---\n\n")), 0o644); err != nil {
			return err
		}
		if err := WriteJSON(filepath.Join(stage, "lock.json"), lock, 0o644); err != nil {
			return err
		}
		if err := WriteJSON(filepath.Join(stage, "workflow-source.json"), Object{"digest_or_revision": proposed["workflow_revision"]}, 0o644); err != nil {
			return err
		}
		if err := WriteJSON(filepath.Join(stage, "config.json"), config, 0o644); err != nil {
			return err
		}
		exports, err := providerExports(config, final)
		if err != nil {
			return err
		}
		for name, value := range exports {
			path := filepath.Join(stage, "exports", name)
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				return err
			}
			if data, ok := value.(string); ok {
				if err := os.WriteFile(path, []byte(data), 0o644); err != nil {
					return err
				}
			} else if err := WriteJSON(path, value, 0o644); err != nil {
				return err
			}
		}
		if err := WriteJSON(filepath.Join(stage, "previous.json"), Object{"generation": proposed["current"]}, 0o644); err != nil {
			return err
		}
		if err := os.Rename(stage, final); err != nil {
			return err
		}
		old, err := switchGeneration(root, final)
		if err != nil {
			return err
		}
		result = Object{"generation": final, "previous": nil, "blocked_until_configured": proposed["blocked_until_configured"]}
		if old != "" {
			result["previous"] = old
		}
		return nil
	})
	return result, err
}

func Rollback(root string) (Object, error) {
	var result Object
	err := WithTransaction(root, func() error {
		current, err := Generation(root)
		if err != nil {
			return err
		}
		if current == "" {
			return fmt.Errorf("no active generation")
		}
		previous, err := ReadJSON(filepath.Join(current, "previous.json"))
		if err != nil {
			return err
		}
		target, _ := stringValue(previous["generation"])
		if target == "" {
			return fmt.Errorf("no previous generation")
		}
		if filepath.Dir(target) != filepath.Join(root, "releases") {
			return fmt.Errorf("invalid previous generation")
		}
		if _, err := os.Stat(target); err != nil {
			return fmt.Errorf("invalid previous generation")
		}
		if _, err := switchGeneration(root, target); err != nil {
			return err
		}
		result = Object{"generation": target}
		return nil
	})
	return result, err
}
