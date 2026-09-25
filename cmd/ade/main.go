package main

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/djpken/agent-dev-environment/internal/ade"
)

type optionSpec struct{ boolean, repeated bool }

func parseOptions(args []string, specs map[string]optionSpec) ([]string, map[string][]string, error) {
	positionals := []string{}
	values := map[string][]string{}
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if !strings.HasPrefix(arg, "--") || arg == "--" {
			positionals = append(positionals, arg)
			continue
		}
		key, value, hasValue := strings.Cut(strings.TrimPrefix(arg, "--"), "=")
		spec, ok := specs[key]
		if !ok {
			return nil, nil, fmt.Errorf("unknown option: --%s", key)
		}
		if spec.boolean {
			if hasValue {
				if value != "true" && value != "false" {
					return nil, nil, fmt.Errorf("--%s expects a boolean", key)
				}
			} else {
				value = "true"
			}
		} else if !hasValue {
			i++
			if i >= len(args) || strings.HasPrefix(args[i], "--") {
				return nil, nil, fmt.Errorf("--%s requires a value", key)
			}
			value = args[i]
		}
		if !spec.repeated && len(values[key]) > 0 {
			return nil, nil, fmt.Errorf("--%s may only be supplied once", key)
		}
		values[key] = append(values[key], value)
	}
	return positionals, values, nil
}

func value(values map[string][]string, name, fallback string) string {
	if items := values[name]; len(items) > 0 {
		return items[len(items)-1]
	}
	return fallback
}

func requiredValue(values map[string][]string, name string) (string, error) {
	result := value(values, name, "")
	if result == "" {
		return "", fmt.Errorf("--%s is required", name)
	}
	return result, nil
}

func enabled(values map[string][]string, name string) bool {
	return value(values, name, "false") == "true"
}

func emitJSON(value any) error {
	data, err := ade.MarshalPretty(value)
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(append(data, '\n'))
	return err
}

func defaultRoot() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ".local/share/ade"
	}
	return filepath.Join(home, ".local/share/ade")
}

func printUsage() {
	fmt.Fprintln(os.Stdout, `Usage: ade [--root PATH] <operation> [options]

Operations: plan, apply, rollback, status, doctor, attach, provider, review,
publish-html, sync, schedule install, environment serve|status|update|authorize|account|enroll|enroll-signed|trending|artifacts|fail-run`)
}

func active(root string) (string, ade.Object, error) {
	release, err := ade.Generation(root)
	if err != nil {
		return "", nil, err
	}
	if release == "" {
		return "", nil, errors.New("run apply first")
	}
	config, err := ade.ReadJSON(filepath.Join(release, "config.json"))
	return release, config, err
}

func providerCommand(provider ade.Object, release string, health bool) []string {
	field := "command"
	if health {
		field = "health"
	}
	command := []string{}
	values, _ := provider[field].([]any)
	for _, raw := range values {
		if item, ok := raw.(string); ok {
			command = append(command, strings.ReplaceAll(item, "{release}", release))
		}
	}
	return command
}

func providerEnv(provider ade.Object, source []string, additions map[string]string) []string {
	filtered := ade.CleanEnvironment(provider, source)
	result := make([]string, 0, len(filtered)+len(additions))
	for _, item := range filtered {
		key, _, _ := strings.Cut(item, "=")
		if _, replaced := additions[key]; !replaced {
			result = append(result, item)
		}
	}
	for key, item := range additions {
		result = append(result, key+"="+item)
	}
	return result
}

func checkProvider(provider ade.Object, release string) error {
	return ade.CheckProviderHealth(provider, release)
}

func syncEnvironment(provider ade.Object, envFile string) (map[string]string, error) {
	result := map[string]string{}
	for _, entry := range os.Environ() {
		key, val, ok := strings.Cut(entry, "=")
		if ok {
			result[key] = val
		}
	}
	if envFile != "" {
		file, err := ade.SyncEnvFile(envFile, mustWorkingDirectory())
		if err != nil {
			return nil, err
		}
		for key, val := range file {
			result[key] = val
		}
	}
	allowed := map[string]bool{}
	if raw, ok := provider["env_vars"].([]any); ok {
		for _, item := range raw {
			if key, ok := item.(string); ok {
				allowed[key] = true
			}
		}
	}
	for key := range result {
		if key != "PATH" && key != "LANG" && key != "LC_ALL" && key != "TMPDIR" && key != "SYSTEMROOT" && key != "HOME" && !allowed[key] {
			delete(result, key)
		}
	}
	return result, nil
}

func mustWorkingDirectory() string {
	working, err := os.Getwd()
	if err != nil {
		return "."
	}
	return working
}

func runSync(root, providerName, envFile string) (int, error) {
	_, config, err := active(root)
	if err != nil {
		return 1, err
	}
	provider := objectAt(config, "manifests", providerName)
	if provider == nil {
		return 1, fmt.Errorf("unknown sync provider: %s", providerName)
	}
	if _, err := ade.Authorize(config, providerName); err != nil {
		return 1, err
	}
	environment, err := syncEnvironment(provider, envFile)
	if err != nil {
		return 1, err
	}
	result, err := ade.RunSync(root, environment)
	if err != nil {
		return 1, err
	}
	if err := emitJSON(result); err != nil {
		return 1, err
	}
	if result["status"] == "completed" {
		return 0, nil
	}
	return 1, nil
}

func objectAt(root ade.Object, key, child string) ade.Object {
	parent, _ := root[key].(map[string]any)
	if parent == nil {
		return nil
	}
	result, _ := parent[child].(map[string]any)
	return result
}

func supervise(provider ade.Object, release string) (int, error) {
	if provider["lifecycle"] != "shared-local" {
		return 1, errors.New("provider is not shared-local")
	}
	endpoint, _ := provider["url"].(string)
	parsed, err := url.Parse(endpoint)
	if err != nil || parsed.Hostname() == "" {
		return 1, errors.New("shared provider URL is invalid")
	}
	address := parsed.Host
	if _, _, err := net.SplitHostPort(address); err != nil {
		port := parsed.Port()
		if port == "" {
			if parsed.Scheme == "https" {
				port = "443"
			} else {
				port = "80"
			}
		}
		address = net.JoinHostPort(parsed.Hostname(), port)
	}
	conn, err := net.DialTimeout("tcp", address, 2*time.Second)
	if err == nil {
		conn.Close()
		return 1, errors.New("shared endpoint is already occupied")
	}
	command := providerCommand(provider, release, false)
	if len(command) == 0 {
		return 1, errors.New("provider has no command")
	}
	child := exec.Command(command[0], command[1:]...)
	child.Env = ade.CleanEnvironment(provider, os.Environ())
	child.Stdout, child.Stderr = os.Stdout, os.Stderr
	child.Stdin = os.Stdin
	child.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := child.Start(); err != nil {
		return 1, err
	}
	wait := make(chan error, 1)
	go func() { wait <- child.Wait() }()
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	deadline := time.NewTimer(20 * time.Second)
	ticker := time.NewTicker(100 * time.Millisecond)
	defer deadline.Stop()
	defer ticker.Stop()
	client := &http.Client{Timeout: time.Second}
	ready := false
	for !ready {
		select {
		case err := <-wait:
			return exitCode(err), errors.New("shared provider exited before readiness")
		case <-signals:
			_ = syscall.Kill(-child.Process.Pid, syscall.SIGTERM)
			return exitCode(<-wait), nil
		case <-deadline.C:
			_ = syscall.Kill(-child.Process.Pid, syscall.SIGTERM)
			select {
			case err := <-wait:
				return exitCode(err), fmt.Errorf("shared provider failed readiness check: %w", err)
			case <-time.After(5 * time.Second):
				_ = syscall.Kill(-child.Process.Pid, syscall.SIGKILL)
				<-wait
				return 1, errors.New("shared provider failed readiness check")
			}
		case <-ticker.C:
			response, err := client.Get(endpoint)
			if err == nil {
				response.Body.Close()
				fmt.Fprintln(os.Stderr, "shared provider ready: "+fmt.Sprint(provider["id"]))
				ready = true
			}
		}
	}
	select {
	case err := <-wait:
		return exitCode(err), nil
	case <-signals:
		_ = syscall.Kill(-child.Process.Pid, syscall.SIGTERM)
		return exitCode(<-wait), nil
	}
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode()
	}
	return 1
}

func runReview(values map[string][]string, provider, config ade.Object, release, workspace string) (int, error) {
	model, err := requiredValue(values, "model")
	if err != nil {
		return 1, errors.New("review requires --model")
	}
	base, err := requiredValue(values, "base")
	if err != nil {
		return 1, err
	}
	head := value(values, "head", "HEAD")
	protocol := value(values, "protocol", "openai")
	if protocol != "anthropic" && protocol != "openai" && protocol != "openai-responses" {
		return 1, errors.New("--protocol must be anthropic, openai, or openai-responses")
	}
	keyEnv := value(values, "key-env", "OCR_LLM_TOKEN")
	token, ok := os.LookupEnv(keyEnv)
	if !ok {
		return 1, fmt.Errorf("missing credential environment variable: %s", keyEnv)
	}
	if err := checkProvider(provider, release); err != nil {
		return 1, err
	}
	revisions := []string{}
	for _, ref := range []string{base, head} {
		cmd := exec.Command("git", "-C", workspace, "rev-parse", "--verify", "--end-of-options", ref+"^{commit}")
		output, err := cmd.Output()
		if err != nil {
			return 1, err
		}
		revisions = append(revisions, strings.TrimSpace(string(output)))
	}
	ancestor := exec.Command("git", "-C", workspace, "merge-base", "--is-ancestor", revisions[0], revisions[1])
	if err := ancestor.Run(); err != nil {
		return 1, errors.New("base must be an ancestor of head")
	}
	command := providerCommand(provider, release, false)
	command = append(command, "review", "--from", revisions[0], "--to", revisions[1], "--format", "json")
	if len(command) == 0 {
		return 1, errors.New("OCR provider has no command")
	}
	cmd := exec.Command(command[0], command[1:]...)
	cmd.Dir = workspace
	endpoint, _ := config["llm_endpoint"].(string)
	cmd.Env = providerEnv(provider, os.Environ(), map[string]string{"OCR_LLM_URL": endpoint, "OCR_LLM_MODEL": model, "OCR_LLM_TOKEN": token, "OCR_LLM_PROTOCOL": protocol})
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	err = cmd.Run()
	if err == nil {
		return 0, nil
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode(), nil
	}
	return 1, err
}

func readLimited(reader io.Reader, limit int64) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return nil, err
	}
	if len(data) == 0 || int64(len(data)) > limit {
		return nil, errors.New("request is empty or too large")
	}
	return data, nil
}

func decodeObject(data []byte, message string) (ade.Object, error) {
	value, err := ade.DecodeJSON(data)
	if err != nil {
		return nil, errors.New(message)
	}
	return value, nil
}

func readHiddenPassword(prompt string) (string, error) {
	info, err := os.Stdin.Stat()
	if err != nil || info.Mode()&os.ModeCharDevice == 0 {
		return "", errors.New("password setup requires an interactive terminal")
	}
	fmt.Fprint(os.Stderr, prompt)
	command := exec.Command("stty", "-echo")
	command.Stdin = os.Stdin
	if err := command.Run(); err != nil {
		return "", errors.New("could not disable terminal echo")
	}
	defer func() {
		restore := exec.Command("stty", "echo")
		restore.Stdin = os.Stdin
		_ = restore.Run()
		fmt.Fprintln(os.Stderr)
	}()
	line, err := bufio.NewReader(os.Stdin).ReadString('\n')
	if err != nil && len(line) == 0 {
		return "", err
	}
	line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
	return line, nil
}

func environmentCommand(args []string) (int, error) {
	if len(args) == 0 {
		return 1, errors.New("environment operation is required")
	}
	operation := args[0]
	values := map[string][]string{}
	positionals := []string{}
	var err error
	switch operation {
	case "serve", "status", "trending", "authorize", "account", "enroll", "enroll-signed", "fail-run", "artifacts", "update":
		options := map[string]optionSpec{"config": {}, "host": {}, "port": {}, "source": {}, "since": {}, "address": {}, "username": {}, "role": {}, "request-json": {}, "run-id": {}, "error": {}, "trigger": {}, "requested-by": {}, "component": {repeated: true}}
		positionals, values, err = parseOptions(args[1:], options)
	default:
		return 1, fmt.Errorf("unknown environment operation: %s", operation)
	}
	if err != nil {
		return 1, err
	}
	if operation == "account" && (len(positionals) != 1 || positionals[0] != "set") {
		return 1, errors.New("account requires set")
	}
	if operation != "artifacts" && operation != "account" && len(positionals) > 0 {
		return 1, fmt.Errorf("unexpected argument: %s", positionals[0])
	}
	configPath := value(values, "config", ade.DefaultEnvironmentConfig)
	if operation == "serve" {
		port := 0
		if raw := value(values, "port", ""); raw != "" {
			_, err := fmt.Sscanf(raw, "%d", &port)
			if err != nil || port < 1 || port > 65535 {
				return 1, errors.New("--port must be between 1 and 65535")
			}
		}
		config, err := ade.LoadEnvironmentConfig(configPath)
		if err != nil {
			return 1, err
		}
		return 1, ade.StartManagementServer(config, value(values, "host", ""), port)
	}
	config, err := ade.LoadEnvironmentConfig(configPath)
	if err != nil {
		return 1, err
	}
	switch operation {
	case "account":
		info, err := os.Stat(config.Path)
		if err != nil {
			return 1, errors.New("account setup requires root and a root-owned config")
		}
		owner, ownerOK := info.Sys().(*syscall.Stat_t)
		if os.Geteuid() != 0 || !ownerOK || owner.Uid != 0 || info.Mode().Perm()&0o022 != 0 {
			return 1, errors.New("account setup requires root and a root-owned config")
		}
		username, err := requiredValue(values, "username")
		if err != nil {
			return 1, err
		}
		password, err := readHiddenPassword("New password: ")
		if err != nil {
			return 1, err
		}
		confirmation, err := readHiddenPassword("Confirm password: ")
		if err != nil {
			return 1, err
		}
		if password != confirmation {
			return 1, errors.New("passwords do not match")
		}
		authority, err := ade.NewEnvironmentAuthority(config)
		if err != nil {
			return 1, err
		}
		role := value(values, "role", "admin")
		if err := authority.SetAccount(username, password, role); err != nil {
			return 1, err
		}
		return 0, emitJSON(ade.Object{"account_set": true, "username": strings.ToLower(strings.TrimSpace(username)), "role": role})
	case "authorize":
		info, err := os.Stat(config.Path)
		if err != nil {
			return 1, errors.New("environment authorization requires root and a root-owned config")
		}
		owner, ownerOK := info.Sys().(*syscall.Stat_t)
		if os.Geteuid() != 0 || !ownerOK || owner.Uid != 0 || info.Mode().Perm()&0o022 != 0 {
			return 1, errors.New("environment authorization requires root and a root-owned config")
		}
		requestData, readErr := readLimited(os.Stdin, 64*1024)
		if readErr != nil {
			return 0, emitJSON(ade.Object{"error": "authorization request is empty or too large"})
		}
		request, parseErr := decodeObject(requestData, "authorization request must be valid JSON")
		if parseErr != nil {
			return 0, emitJSON(ade.Object{"error": parseErr.Error()})
		}
		authority, err := ade.NewEnvironmentAuthority(config)
		if err != nil {
			return 0, emitJSON(ade.Object{"error": err.Error()})
		}
		result, err := authority.Dispatch(request)
		if err != nil {
			return 0, emitJSON(ade.Object{"error": err.Error()})
		}
		return 0, emitJSON(result)
	case "status":
		result, err := ade.EnvironmentStatus(config)
		if err != nil {
			return 1, err
		}
		return 0, emitJSON(result)
	case "trending":
		result, err := ade.NewTrendingService().Get(value(values, "source", "github"), value(values, "since", "daily"))
		if err != nil {
			return 1, err
		}
		return 0, emitJSON(result)
	case "artifacts":
		if len(positionals) != 1 || (positionals[0] != "list" && positionals[0] != "upload" && positionals[0] != "delete") {
			return 1, errors.New("artifacts requires list, upload, or delete")
		}
		action := positionals[0]
		var result ade.Object
		if action == "list" {
			result, err = ade.ListWebArtifacts(config.Artifacts)
		} else if action == "upload" {
			var raw []byte
			raw, err = readLimited(os.Stdin, ade.MaxArtifactRequestBytes)
			if err == nil {
				var payload ade.Object
				payload, err = decodeObject(raw, "artifact request must be valid JSON")
				if err == nil {
					result, err = ade.UploadWebArtifact(config.Artifacts, payload)
				}
			}
		} else {
			name, nameErr := requiredValue(values, "name")
			if nameErr != nil {
				return 1, nameErr
			}
			result, err = ade.RemoveWebArtifact(config.Artifacts, name)
		}
		if err != nil {
			status := 400
			message := err.Error()
			if action == "list" || strings.HasPrefix(message, "publish blocked:") || strings.Contains(message, "storage unavailable") {
				status = 503
			}
			if action == "delete" && strings.HasPrefix(message, "artifact not found:") {
				status = 404
			}
			_ = emitJSON(ade.Object{"error": message, "status_code": status})
			return 1, nil
		}
		return 0, emitJSON(result)
	case "fail-run":
		runID, err := requiredValue(values, "run-id")
		if err != nil {
			return 1, err
		}
		message, err := requiredValue(values, "error")
		if err != nil {
			return 1, err
		}
		result, err := ade.FailRun(config.StateRoot, runID, message)
		if err != nil {
			return 1, err
		}
		return 0, emitJSON(result)
	case "enroll":
		address, err := requiredValue(values, "address")
		if err != nil {
			return 1, err
		}
		result, err := ade.EnrollWallet(config, address, value(values, "role", "admin"))
		if err != nil {
			return 1, err
		}
		return 0, emitJSON(result)
	case "enroll-signed":
		if os.Geteuid() != 0 {
			return 1, errors.New("signed wallet enrollment must run as root")
		}
		raw, err := requiredValue(values, "request-json")
		if err != nil {
			return 1, err
		}
		request, err := decodeObject([]byte(raw), "signed enrollment request must be valid JSON")
		if err != nil {
			return 1, err
		}
		if len(request) != 3 {
			return 1, errors.New("signed enrollment request fields are invalid")
		}
		address, addressOK := request["address"].(string)
		message, messageOK := request["message"].(string)
		signature, signatureOK := request["signature"].(string)
		if !addressOK || !messageOK || !signatureOK {
			return 1, errors.New("signed enrollment request values are invalid")
		}
		result, err := ade.EnrollSignedWallet(config, address, message, signature)
		if err != nil {
			return 1, err
		}
		return 0, emitJSON(result)
	case "update":
		store, err := ade.NewRunStore(config.StateRoot)
		if err != nil {
			return 1, err
		}
		coordinator := ade.NewUpdateCoordinator(config, store)
		trigger := value(values, "trigger", "scheduled")
		var run ade.Object
		if id := value(values, "run-id", ""); id != "" {
			run, err = store.Get(id)
		} else {
			run, err = coordinator.CreateRun(trigger, values["component"], value(values, "requested-by", "system"), "update", ade.Object{}, ade.Object{})
		}
		if err != nil {
			return 1, err
		}
		if run == nil {
			return 1, fmt.Errorf("update run not found: %s", value(values, "run-id", ""))
		}
		id, _ := run["id"].(string)
		result, err := coordinator.Execute(id, trigger)
		if err != nil {
			return 1, err
		}
		if err := emitJSON(result); err != nil {
			return 1, err
		}
		if result["status"] == "completed" || result["status"] == "blocked" {
			return 0, nil
		}
		return 1, nil
	}
	return 1, fmt.Errorf("unknown environment operation: %s", operation)
}

func run(args []string) (int, error) {
	if len(args) == 0 || args[0] == "--help" || args[0] == "-h" || args[0] == "help" {
		printUsage()
		return 0, nil
	}
	root := defaultRoot()
	filtered := []string{}
	for i := 0; i < len(args); i++ {
		if args[i] == "--root" {
			if i+1 >= len(args) {
				return 2, errors.New("--root requires a value")
			}
			root = args[i+1]
			i++
		} else if strings.HasPrefix(args[i], "--root=") {
			root = strings.TrimPrefix(args[i], "--root=")
		} else {
			filtered = append(filtered, args[i])
		}
	}
	if len(filtered) == 0 {
		return 2, errors.New("operation is required")
	}
	operation := filtered[0]
	args = filtered[1:]
	switch operation {
	case "environment":
		return environmentCommand(args)
	case "serve":
		return environmentCommand(append([]string{"serve"}, args...))
	case "plan", "apply":
		specs := map[string]optionSpec{"lock": {}, "user": {}, "workspace-config": {}, "workflow-repo": {}, "plan-id": {}}
		positionals, values, err := parseOptions(args, specs)
		if err != nil {
			return 2, err
		}
		if len(positionals) != 0 {
			return 2, fmt.Errorf("unexpected argument: %s", positionals[0])
		}
		lockPath, err := filepath.Abs(value(values, "lock", "ade.lock.json"))
		if err != nil {
			return 1, err
		}
		lock, err := ade.ReadJSON(lockPath)
		if err != nil {
			return 1, err
		}
		user := ade.Object{}
		if path := value(values, "user", ""); path != "" {
			user, err = ade.ReadJSON(path)
			if err != nil {
				return 1, err
			}
		}
		workspace := ade.Object{}
		if path := value(values, "workspace-config", ""); path != "" {
			workspace, err = ade.ReadJSON(path)
			if err != nil {
				return 1, err
			}
		}
		config, err := ade.Compose(lock, user, workspace)
		if err != nil {
			return 1, err
		}
		repository := value(values, "workflow-repo", filepath.Dir(lockPath))
		var result ade.Object
		if operation == "plan" {
			result, err = ade.Plan(lock, config, root, repository)
		} else {
			planID, requiredErr := requiredValue(values, "plan-id")
			if requiredErr != nil {
				return 2, requiredErr
			}
			result, err = ade.Install(lock, config, root, repository, planID)
		}
		if err != nil {
			return 1, err
		}
		return 0, emitJSON(result)
	case "rollback":
		if len(args) != 0 {
			return 2, errors.New("rollback accepts no arguments")
		}
		result, err := ade.Rollback(root)
		if err != nil {
			return 1, err
		}
		return 0, emitJSON(result)
	case "status":
		if len(args) != 0 {
			return 2, errors.New("status accepts no arguments")
		}
		release, err := ade.Generation(root)
		if err != nil {
			return 1, err
		}
		var value any = release
		if release == "" {
			value = "None"
		}
		return 0, emitJSON(ade.Object{"generation": value})
	case "attach":
		positionals, values, err := parseOptions(args, map[string]optionSpec{"host": {}, "workspace": {}, "apply": {boolean: true}})
		if err != nil {
			return 2, err
		}
		if len(positionals) != 0 {
			return 2, fmt.Errorf("unexpected argument: %s", positionals[0])
		}
		host, err := requiredValue(values, "host")
		if err != nil {
			return 2, err
		}
		workspace, err := requiredValue(values, "workspace")
		if err != nil {
			return 2, err
		}
		result, err := ade.Attach(root, workspace, host, enabled(values, "apply"))
		if err != nil {
			return 1, err
		}
		return 0, emitJSON(result)
	case "doctor":
		_, values, err := parseOptions(args, map[string]optionSpec{"env-file": {}})
		if err != nil {
			return 2, err
		}
		release, config, err := active(root)
		if err != nil {
			return 1, err
		}
		results := ade.Object{}
		providers, _ := config["providers"].(map[string]any)
		manifests, _ := config["manifests"].(map[string]any)
		for name, raw := range providers {
			activeProvider, _ := raw.(bool)
			if !activeProvider {
				results[name] = ade.Object{"status": "disabled"}
				continue
			}
			provider, _ := manifests[name].(map[string]any)
			if provider == nil {
				results[name] = ade.Object{"status": "blocked", "reason": "provider manifest is missing"}
				continue
			}
			if name == "plane-linear-sync" {
				environment, loadErr := syncEnvironment(provider, value(values, "env-file", ""))
				if loadErr == nil {
					_, loadErr = ade.SyncConfigFromEnvironment(environment)
				}
				if loadErr != nil {
					results[name] = ade.Object{"status": "blocked", "reason": loadErr.Error()}
				} else {
					results[name] = ade.Object{"status": "ready", "network": "not checked"}
				}
				continue
			}
			if _, err := ade.Authorize(config, name); err != nil {
				results[name] = ade.Object{"status": "blocked", "reason": err.Error()}
			} else if err := checkProvider(provider, release); err != nil {
				results[name] = ade.Object{"status": "blocked", "reason": err.Error()}
			} else {
				results[name] = ade.Object{"status": "ready"}
			}
		}
		_ = emitJSON(results)
		for _, result := range results {
			if object, ok := result.(map[string]any); ok && object["status"] == "blocked" {
				return 1, nil
			}
		}
		return 0, nil
	case "sync":
		_, values, err := parseOptions(args, map[string]optionSpec{"provider": {}, "env-file": {}})
		if err != nil {
			return 2, err
		}
		return runSync(root, value(values, "provider", ade.SyncProvider), value(values, "env-file", ""))
	case "schedule":
		if len(args) == 0 || args[0] != "install" {
			return 2, errors.New("schedule requires install")
		}
		_, values, err := parseOptions(args[1:], map[string]optionSpec{"provider": {}, "env-file": {}, "enable": {boolean: true}})
		if err != nil {
			return 2, err
		}
		providerName := value(values, "provider", ade.SyncProvider)
		_, config, err := active(root)
		if err != nil {
			return 1, err
		}
		provider := objectAt(config, "manifests", providerName)
		if provider == nil {
			return 1, fmt.Errorf("unknown sync provider: %s", providerName)
		}
		if _, err := ade.Authorize(config, providerName); err != nil {
			return 1, err
		}
		if envFile := value(values, "env-file", ""); envFile != "" {
			if _, err := ade.SyncEnvFile(envFile, mustWorkingDirectory()); err != nil {
				return 1, err
			}
		}
		times, err := ade.ManifestSchedule(provider)
		if err != nil {
			return 1, err
		}
		executable, err := os.Executable()
		if err != nil {
			return 1, err
		}
		result, err := ade.InstallSchedule(root, ade.CurrentPlatform(), providerName, times, value(values, "env-file", ""), executable, "", enabled(values, "enable"))
		if err != nil {
			return 1, err
		}
		return 0, emitJSON(result)
	case "publish-html":
		if len(args) == 0 {
			return 2, errors.New("publish-html requires publish or delete")
		}
		subcommand := args[0]
		switch subcommand {
		case "publish":
			positionals, values, err := parseOptions(args[1:], map[string]optionSpec{"name": {}, "entrypoint": {}, "artifact-root": {}, "base-url": {}})
			if err != nil {
				return 2, err
			}
			if len(positionals) != 1 {
				return 2, errors.New("publish requires a source path")
			}
			name, err := requiredValue(values, "name")
			if err != nil {
				return 2, err
			}
			rootPath := value(values, "artifact-root", os.Getenv("ADE_WEB_ARTIFACT_ROOT"))
			if rootPath == "" {
				rootPath = "/var/lib/ade/web-artifacts"
			}
			base := value(values, "base-url", os.Getenv("ADE_WEB_ARTIFACT_BASE_URL"))
			if base == "" {
				base = "http://172.16.240.41:80"
			}
			if _, err := ade.ValidateArtifactBaseURL(base); err != nil {
				return 1, fmt.Errorf("publish blocked: %w", err)
			}
			if err := ade.PublisherReady(base); err != nil {
				return 1, fmt.Errorf("publish blocked: %w", err)
			}
			result, err := ade.PublishHTML(positionals[0], name, rootPath, base, value(values, "entrypoint", "index.html"), false)
			if err != nil {
				if !strings.HasPrefix(err.Error(), "publish blocked:") {
					return 1, fmt.Errorf("publish blocked: %w", err)
				}
				return 1, err
			}
			return 0, emitJSON(result)
		case "delete":
			positionals, values, err := parseOptions(args[1:], map[string]optionSpec{"artifact-root": {}})
			if err != nil {
				return 2, err
			}
			if len(positionals) != 1 {
				return 2, errors.New("delete requires an artifact name")
			}
			rootPath := value(values, "artifact-root", os.Getenv("ADE_WEB_ARTIFACT_ROOT"))
			if rootPath == "" {
				rootPath = "/var/lib/ade/web-artifacts"
			}
			result, err := ade.DeleteHTML(positionals[0], rootPath)
			if err != nil {
				return 1, err
			}
			return 0, emitJSON(result)
		default:
			return 2, fmt.Errorf("unknown publish-html operation: %s", subcommand)
		}
	case "provider", "review":
		positionals, values, err := parseOptions(args, map[string]optionSpec{"shared": {boolean: true}, "workspace": {}, "base": {}, "head": {}, "model": {}, "protocol": {}, "key-env": {}})
		if err != nil {
			return 2, err
		}
		_, config, err := active(root)
		if err != nil {
			return 1, err
		}
		if operation == "review" {
			if len(positionals) != 0 {
				return 2, fmt.Errorf("unexpected argument: %s", positionals[0])
			}
			release, _ := ade.Generation(root)
			provider, err := ade.Authorize(config, "ocr")
			if err != nil {
				return 1, err
			}
			workspace := value(values, "workspace", mustWorkingDirectory())
			return runReview(values, provider, config, release, workspace)
		}
		if len(positionals) != 1 {
			return 2, errors.New("provider requires a name")
		}
		release, _ := ade.Generation(root)
		provider, err := ade.Authorize(config, positionals[0])
		if err != nil {
			return 1, err
		}
		if err := checkProvider(provider, release); err != nil {
			return 1, err
		}
		if enabled(values, "shared") {
			return supervise(provider, release)
		}
		if provider["builtin"] == true {
			return runSync(root, positionals[0], "")
		}
		if provider["lifecycle"] != "host-spawned" {
			return 1, errors.New("use --shared for ADE-owned services")
		}
		if provider["transport"] == "cli" {
			return 1, errors.New("use the typed review command for OCR")
		}
		command := providerCommand(provider, release, false)
		if len(command) == 0 {
			return 1, errors.New("provider has no command")
		}
		executable, err := exec.LookPath(command[0])
		if err != nil {
			return 1, err
		}
		command[0] = executable
		if err := syscall.Exec(executable, command, ade.CleanEnvironment(provider, os.Environ())); err != nil {
			return 1, err
		}
		return 0, nil
	default:
		return 2, fmt.Errorf("unknown operation: %s", operation)
	}
}

func main() {
	code, err := run(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "ade: "+err.Error())
		if code == 0 {
			code = 1
		}
	}
	os.Exit(code)
}
