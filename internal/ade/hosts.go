package ade

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"

	"github.com/pelletier/go-toml/v2"
)

type hostSpec struct{ Config, Export, Key, Skills string }

var hostSpecs = map[string]hostSpec{
	"claude":   {".mcp.json", "claude.mcp.json", "mcpServers", ".claude/skills"},
	"codex":    {".codex/config.toml", "codex.toml", "mcp_servers", ".agents/skills"},
	"opencode": {"opencode.json", "opencode.json", "mcp", ".agents/skills"},
}

func parseJSONFile(data []byte) (Object, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return Object{}, nil
	}
	return DecodeJSON(data)
}

func parseToml(data []byte) (map[string]any, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return map[string]any{}, nil
	}
	value := map[string]any{}
	if err := toml.Unmarshal(data, &value); err != nil {
		return nil, err
	}
	return value, nil
}

func Attach(root, workspace, host string, apply bool) (Object, error) {
	spec, ok := hostSpecs[host]
	if !ok {
		return nil, fmt.Errorf("unsupported host: %s", host)
	}
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	workspace, err = filepath.Abs(workspace)
	if err != nil {
		return nil, err
	}
	var result Object
	err = WithTransaction(root, func() error {
		release, err := Generation(root)
		if err != nil {
			return err
		}
		if release == "" {
			return fmt.Errorf("run apply first")
		}
		if host == "opencode" {
			if _, err := os.Stat(filepath.Join(workspace, "opencode.jsonc")); err == nil {
				return fmt.Errorf("opencode.jsonc exists; JSONC merging is not supported")
			}
		}
		target := filepath.Join(workspace, filepath.FromSlash(spec.Config))
		targetParent := filepath.Dir(target)
		resolvedParent, err := filepath.EvalSymlinks(targetParent)
		if err != nil && !os.IsNotExist(err) {
			return err
		}
		if err == nil && !within(workspace, resolvedParent) {
			return fmt.Errorf("host config escapes workspace")
		}
		if info, err := os.Lstat(target); err == nil && info.Mode()&os.ModeSymlink != 0 {
			resolved, err := filepath.EvalSymlinks(target)
			if err != nil {
				return err
			}
			if !within(workspace, resolved) {
				return fmt.Errorf("host config escapes workspace")
			}
		}
		original, readErr := os.ReadFile(target)
		if readErr != nil && !os.IsNotExist(readErr) {
			return readErr
		}
		exists := readErr == nil
		incomingData, err := os.ReadFile(filepath.Join(release, "exports", spec.Export))
		if err != nil {
			return err
		}
		var existingEntries, incomingEntries map[string]any
		var updated string
		if host == "codex" {
			existing, err := parseToml(original)
			if err != nil {
				return err
			}
			incoming, err := parseToml(incomingData)
			if err != nil {
				return err
			}
			existingEntries = table(existing[spec.Key])
			incomingEntries = table(incoming[spec.Key])
			if existingEntries == nil {
				existingEntries = map[string]any{}
			}
			lines := []string{}
			keys := sortedKeys(incomingEntries)
			for _, key := range keys {
				value := incomingEntries[key]
				if old, found := existingEntries[key]; found {
					if !reflect.DeepEqual(normalizeValue(old), normalizeValue(value)) {
						return fmt.Errorf("existing host provider conflicts: %s", key)
					}
					continue
				}
				encoded, err := toml.Marshal(map[string]any{spec.Key: map[string]any{key: value}})
				if err != nil {
					return err
				}
				block := strings.TrimSpace(string(encoded))
				lines = append(lines, block)
			}
			updated = string(original)
			if len(lines) > 0 {
				if updated != "" && !strings.HasSuffix(updated, "\n") {
					updated += "\n"
				}
				if updated != "" {
					updated += "\n"
				}
				updated += strings.Join(lines, "\n\n") + "\n"
			}
		} else {
			existing, err := parseJSONFile(original)
			if err != nil {
				return err
			}
			incoming, err := parseJSONFile(incomingData)
			if err != nil {
				return err
			}
			existingEntries = table(existing[spec.Key])
			incomingEntries = table(incoming[spec.Key])
			if existingEntries == nil {
				existingEntries = map[string]any{}
				existing[spec.Key] = existingEntries
			}
			for key, value := range incomingEntries {
				if old, found := existingEntries[key]; found && !reflect.DeepEqual(normalizeValue(old), normalizeValue(value)) {
					return fmt.Errorf("existing host provider conflicts: %s", key)
				}
				existingEntries[key] = value
			}
			data, err := MarshalPretty(existing)
			if err != nil {
				return err
			}
			updated = string(data)
		}
		pack := filepath.Join(root, "current", "workflow")
		registry, err := ReadJSON(filepath.Join(pack, ".claude-plugin", "plugin.json"))
		if err != nil {
			return fmt.Errorf("invalid Workflow Pack registry: %w", err)
		}
		candidates := []string{}
		if entries, ok := registry["skills"].([]any); ok {
			for _, entry := range entries {
				if text, ok := entry.(string); ok {
					candidates = append(candidates, text)
				}
			}
		}
		for _, name := range []string{"base", "implement", "code-review", "wait-what"} {
			candidates = append(candidates, "skills/"+name)
		}
		links := [][2]string{}
		seen := map[string]bool{}
		for _, relative := range candidates {
			source := filepath.Join(pack, filepath.FromSlash(relative))
			resolved, err := filepath.EvalSymlinks(source)
			if err != nil || !within(pack, resolved) {
				return fmt.Errorf("invalid Workflow Pack registry path")
			}
			if info, err := os.Stat(filepath.Join(source, "SKILL.md")); err != nil || !info.Mode().IsRegular() {
				return fmt.Errorf("invalid Workflow Pack registry path")
			}
			label := filepath.Base(source)
			if seen[label] {
				return fmt.Errorf("duplicate skill name: %s", label)
			}
			seen[label] = true
			destination := filepath.Join(workspace, filepath.FromSlash(spec.Skills), label)
			parent, err := filepath.EvalSymlinks(filepath.Dir(destination))
			if err == nil && !within(workspace, parent) {
				return fmt.Errorf("skills directory escapes workspace")
			}
			if err != nil && !os.IsNotExist(err) {
				return err
			}
			info, err := os.Lstat(destination)
			if err == nil {
				if info.Mode()&os.ModeSymlink != 0 {
					link, err := os.Readlink(destination)
					if err != nil {
						return err
					}
					if link != source {
						return fmt.Errorf("existing skill link conflicts: %s", destination)
					}
					continue
				}
				return fmt.Errorf("existing skill conflicts: %s", destination)
			}
			if !os.IsNotExist(err) {
				return err
			}
			links = append(links, [2]string{destination, source})
		}
		paths := []string{}
		for _, pair := range links {
			paths = append(paths, pair[0])
		}
		result = Object{"host": host, "workspace": workspace, "config": target, "skills_to_link": paths, "applied": apply}
		if !apply {
			return nil
		}
		backup := filepath.Join(root, "attachments", Digest(Object{"workspace": workspace, "host": host}))
		if _, err := os.Stat(backup); os.IsNotExist(err) {
			if err := WriteJSON(backup, Object{"path": target, "original": nil}, 0o600); err != nil {
				return err
			}
			if exists {
				old, _ := ReadJSON(backup)
				old["original"] = string(original)
				if err := WriteJSON(backup, old, 0o600); err != nil {
					return err
				}
			}
		}
		made := []string{}
		rollback := func() {
			for i := len(made) - 1; i >= 0; i-- {
				_ = os.Remove(made[i])
			}
		}
		for _, pair := range links {
			if err := os.MkdirAll(filepath.Dir(pair[0]), 0o755); err != nil {
				rollback()
				return err
			}
			if err := os.Symlink(pair[1], pair[0]); err != nil {
				rollback()
				return err
			}
			made = append(made, pair[0])
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			rollback()
			return err
		}
		latest, err := os.ReadFile(target)
		if err != nil && !os.IsNotExist(err) {
			rollback()
			return err
		}
		if (err == nil) != exists || !bytes.Equal(latest, original) {
			rollback()
			return fmt.Errorf("host config changed during attachment")
		}
		temp, err := os.CreateTemp(filepath.Dir(target), ".ade-attach-")
		if err != nil {
			rollback()
			return err
		}
		staged := temp.Name()
		defer os.Remove(staged)
		if _, err := temp.WriteString(updated); err != nil {
			temp.Close()
			rollback()
			return err
		}
		if err := temp.Chmod(0o600); err != nil {
			temp.Close()
			rollback()
			return err
		}
		if err := temp.Close(); err != nil {
			rollback()
			return err
		}
		if err := os.Rename(staged, target); err != nil {
			rollback()
			return err
		}
		return nil
	})
	return result, err
}

func table(value any) map[string]any {
	result, _ := value.(map[string]any)
	if result != nil {
		return result
	}
	if typed, ok := value.(map[string]interface{}); ok {
		return typed
	}
	return nil
}
func sortedKeys(value map[string]any) []string {
	keys := make([]string, 0, len(value))
	for key := range value {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
func normalizeValue(value any) any {
	switch item := value.(type) {
	case map[string]interface{}:
		result := map[string]any{}
		for k, v := range item {
			result[k] = normalizeValue(v)
		}
		return result
	case []interface{}:
		result := []any{}
		for _, v := range item {
			result = append(result, normalizeValue(v))
		}
		return result
	default:
		return value
	}
}
