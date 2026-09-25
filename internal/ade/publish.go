package ade

import (
	"encoding/base64"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

const DefaultArtifactRoot = "/var/lib/ade/web-artifacts"
const DefaultArtifactBaseURL = "http://172.16.240.41:80"
const MaxArtifactUploadBytes = 10 * 1024 * 1024
const MaxArtifactUploadFiles = 200
const MaxArtifactRequestBytes = 15 * 1024 * 1024

var artifactNamePattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,63}$`)
var htmlSuffix = map[string]bool{".html": true, ".htm": true}

func ValidateArtifactName(name string) error {
	if !artifactNamePattern.MatchString(name) {
		return fmt.Errorf("invalid artifact name")
	}
	return nil
}
func ValidateEntrypoint(name string) (string, error) {
	if name == "" || strings.HasPrefix(name, "/") || strings.Contains(name, "\\") || strings.IndexByte(name, 0) >= 0 {
		return "", fmt.Errorf("invalid HTML entrypoint")
	}
	parts := strings.Split(name, "/")
	for _, part := range parts {
		if part == "" || part == "." || part == ".." || strings.HasPrefix(part, ".") {
			return "", fmt.Errorf("invalid HTML entrypoint")
		}
	}
	if !htmlSuffix[strings.ToLower(filepath.Ext(name))] {
		return "", fmt.Errorf("HTML entrypoint must end with .html or .htm")
	}
	return strings.Join(parts, "/"), nil
}

func ValidateArtifactBaseURL(raw string) (*url.URL, error) {
	parsed, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("publish URL has an invalid port")
	}
	port := parsed.Port()
	if parsed.Scheme != "http" || parsed.Hostname() == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || (parsed.Path != "" && parsed.Path != "/") || (port != "" && port != "80") {
		return nil, fmt.Errorf("publish URL must be an HTTP port 80 origin")
	}
	return parsed, nil
}
func artifactURL(base, name, entry string) (string, error) {
	parsed, err := ValidateArtifactBaseURL(base)
	if err != nil {
		return "", err
	}
	host := parsed.Hostname()
	if strings.Contains(host, ":") {
		host = "[" + host + "]"
	}
	segments := strings.Split(entry, "/")
	for i, segment := range segments {
		segments[i] = url.PathEscape(segment)
	}
	return "http://" + host + ":80/artifacts/" + url.PathEscape(name) + "/" + strings.Join(segments, "/"), nil
}

func PublisherReady(base string) error {
	parsed, err := ValidateArtifactBaseURL(base)
	if err != nil {
		return err
	}
	host := parsed.Hostname()
	if strings.Contains(host, ":") {
		host = "[" + host + "]"
	}
	request, err := http.NewRequest(http.MethodGet, "http://127.0.0.1:80/healthz", nil)
	if err != nil {
		return err
	}
	request.Host = host
	client := http.Client{Timeout: 2 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("publish blocked: HTTP publisher unavailable at 127.0.0.1:80")
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("publish blocked: HTTP publisher unavailable at 127.0.0.1:80")
	}
	return nil
}

func ArtifactInventory(root, base string) ([]Object, error) {
	if _, err := ValidateArtifactBaseURL(base); err != nil {
		return nil, err
	}
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if _, err := os.Stat(root); os.IsNotExist(err) {
		return []Object{}, nil
	} else if err != nil {
		return nil, err
	}
	items := []Object{}
	err = Lock(filepath.Join(root, ".ade-publish.lock"), true, func() error {
		public := filepath.Join(root, "artifacts")
		info, err := os.Lstat(public)
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("artifact root may not contain symbolic links")
		}
		if !info.IsDir() {
			return nil
		}
		entries, err := os.ReadDir(public)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			if !artifactNamePattern.MatchString(entry.Name()) || entry.Type()&os.ModeSymlink != 0 || !entry.IsDir() {
				continue
			}
			basePath := filepath.Join(public, entry.Name())
			pages := []Object{}
			var size int64
			count := 0
			err := filepath.WalkDir(basePath, func(path string, item fs.DirEntry, walkErr error) error {
				if walkErr != nil {
					return walkErr
				}
				if path == basePath {
					return nil
				}
				info, err := os.Lstat(path)
				if err != nil {
					return err
				}
				if info.Mode()&os.ModeSymlink != 0 {
					return nil
				}
				if info.IsDir() {
					if strings.HasPrefix(item.Name(), ".") {
						return filepath.SkipDir
					}
					return nil
				}
				if !info.Mode().IsRegular() || strings.HasPrefix(item.Name(), ".") {
					return nil
				}
				size += info.Size()
				count++
				relative, err := filepath.Rel(basePath, path)
				if err != nil {
					return err
				}
				relative = filepath.ToSlash(relative)
				if htmlSuffix[strings.ToLower(filepath.Ext(relative))] {
					access, err := artifactURL(base, entry.Name(), relative)
					if err != nil {
						return err
					}
					pages = append(pages, Object{"entrypoint": relative, "access_url": access})
				}
				return nil
			})
			if err != nil {
				return err
			}
			sort.Slice(pages, func(i, j int) bool {
				left := pages[i]["entrypoint"].(string)
				right := pages[j]["entrypoint"].(string)
				if (left == "index.html") != (right == "index.html") {
					return left == "index.html"
				}
				return left < right
			})
			items = append(items, Object{"name": entry.Name(), "pages": pages, "file_count": count, "size_bytes": size})
		}
		return nil
	})
	return items, err
}

func PublishHTML(source, name, root, base, entrypoint string, overwrite bool) (Object, error) {
	if err := ValidateArtifactName(name); err != nil {
		return nil, err
	}
	if entrypoint == "" {
		entrypoint = "index.html"
	}
	entrypoint, err := ValidateEntrypoint(entrypoint)
	if err != nil {
		return nil, err
	}
	sourceInfo, err := os.Lstat(source)
	if err != nil {
		return nil, fmt.Errorf("HTML source does not exist")
	}
	if sourceInfo.Mode()&os.ModeSymlink != 0 {
		return nil, fmt.Errorf("HTML source may not be a symbolic link")
	}
	source, err = filepath.Abs(source)
	if err != nil {
		return nil, err
	}
	root, err = filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if source == root || within(root, source) || within(source, root) {
		return nil, fmt.Errorf("HTML source and artifact root may not overlap")
	}
	access, err := artifactURL(base, name, entrypoint)
	if err != nil {
		return nil, err
	}
	var result Object
	err = Lock(filepath.Join(root, ".ade-publish.lock"), false, func() error {
		if err := os.MkdirAll(root, 0o755); err != nil {
			return err
		}
		_ = os.Chmod(root, 0o755)
		public := filepath.Join(root, "artifacts")
		if info, err := os.Lstat(public); err == nil {
			if info.Mode()&os.ModeSymlink != 0 {
				return fmt.Errorf("artifact root may not contain symbolic links")
			}
			if !info.IsDir() {
				return fmt.Errorf("artifact root artifacts path must be a directory")
			}
		} else if os.IsNotExist(err) {
			if err := os.Mkdir(public, 0o755); err != nil {
				return err
			}
		} else {
			return err
		}
		_ = os.Chmod(public, 0o755)
		destination := filepath.Join(public, name)
		if _, err := os.Lstat(destination); err == nil && !overwrite {
			return fmt.Errorf("artifact already exists; confirm replacement")
		}
		stage, err := os.MkdirTemp(public, "."+name+".stage-")
		if err != nil {
			return err
		}
		defer os.RemoveAll(stage)
		if err := copyHTMLSource(source, stage, entrypoint); err != nil {
			return err
		}
		if err := makePublicTree(stage); err != nil {
			return err
		}
		info, err := os.Stat(filepath.Join(stage, filepath.FromSlash(entrypoint)))
		if err != nil || !info.Mode().IsRegular() {
			return fmt.Errorf("HTML entrypoint does not exist")
		}
		if err := replacePath(destination, stage); err != nil {
			return err
		}
		result = Object{"name": name, "entrypoint": entrypoint, "access_url": access}
		return nil
	})
	return result, err
}

func copyHTMLSource(source, stage, entrypoint string) error {
	info, err := os.Stat(source)
	if err != nil {
		return err
	}
	if info.Mode().IsRegular() {
		if !htmlSuffix[strings.ToLower(filepath.Ext(source))] {
			return fmt.Errorf("source must be an HTML file")
		}
		target := filepath.Join(stage, filepath.FromSlash(entrypoint))
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		data, err := os.ReadFile(source)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, 0o644)
	}
	if !info.IsDir() {
		return fmt.Errorf("HTML bundle may contain regular files only")
	}
	return filepath.WalkDir(source, func(path string, item fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if path == source {
			return nil
		}
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("HTML bundle may not contain symbolic links")
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		target := filepath.Join(stage, relative)
		if info.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("HTML bundle may contain regular files only")
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, 0o644)
	})
}

func makePublicTree(root string) error {
	return filepath.WalkDir(root, func(path string, item fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("artifact tree may not contain symbolic links")
		}
		if info.IsDir() {
			return os.Chmod(path, 0o755)
		}
		if info.Mode().IsRegular() {
			return os.Chmod(path, 0o644)
		}
		return fmt.Errorf("artifact tree may contain regular files only")
	})
}
func replacePath(destination, stage string) error {
	backup := ""
	if _, err := os.Lstat(destination); err == nil {
		backup = filepath.Join(filepath.Dir(destination), "."+filepath.Base(destination)+".old-"+randomID())
		if err := os.Rename(destination, backup); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := os.Rename(stage, destination); err != nil {
		if backup != "" {
			if _, statErr := os.Lstat(destination); os.IsNotExist(statErr) {
				_ = os.Rename(backup, destination)
			}
		}
		return err
	}
	if backup != "" {
		return os.RemoveAll(backup)
	}
	return nil
}
func DeleteHTML(name, root string) (Object, error) {
	if err := ValidateArtifactName(name); err != nil {
		return nil, err
	}
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	err = Lock(filepath.Join(root, ".ade-publish.lock"), false, func() error {
		public := filepath.Join(root, "artifacts")
		if info, err := os.Lstat(public); err == nil && info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("artifact root may not contain symbolic links")
		}
		destination := filepath.Join(public, name)
		if _, err := os.Lstat(destination); os.IsNotExist(err) {
			return fmt.Errorf("artifact not found: %s", name)
		} else if err != nil {
			return err
		}
		return os.RemoveAll(destination)
	})
	if err != nil {
		return nil, err
	}
	return Object{"name": name, "deleted": true}, nil
}

func ArtifactConfiguration(value any, managementOrigins []string) (Object, error) {
	raw, ok := value.(map[string]any)
	if !ok {
		if value == nil {
			raw = Object{}
		} else {
			return nil, fmt.Errorf("artifacts must be an object")
		}
	}
	enabled := false
	if raw["enabled"] != nil {
		var ok bool
		enabled, ok = raw["enabled"].(bool)
		if !ok {
			return nil, fmt.Errorf("artifacts.enabled must be a boolean")
		}
	}
	if !enabled {
		return Object{"enabled": false}, nil
	}
	root := DefaultArtifactRoot
	if raw["artifact_root"] != nil {
		var ok bool
		root, ok = raw["artifact_root"].(string)
		if !ok || !filepath.IsAbs(root) {
			return nil, fmt.Errorf("artifacts.artifact_root must be absolute")
		}
	}
	base := DefaultArtifactBaseURL
	if raw["base_url"] != nil {
		var ok bool
		base, ok = raw["base_url"].(string)
		if !ok {
			return nil, fmt.Errorf("publish URL must be an HTTP port 80 origin")
		}
	}
	parsed, err := ValidateArtifactBaseURL(base)
	if err != nil {
		return nil, err
	}
	for _, origin := range managementOrigins {
		other, parseErr := url.Parse(origin)
		if parseErr == nil && sameOrigin(parsed, other) {
			return nil, fmt.Errorf("artifacts must use a separate origin from the management service")
		}
	}
	return Object{"enabled": true, "artifact_root": root, "base_url": base}, nil
}
func sameOrigin(a, b *url.URL) bool {
	port := func(u *url.URL) string {
		if u.Port() != "" {
			return u.Port()
		}
		if u.Scheme == "https" {
			return "443"
		}
		return "80"
	}
	return strings.EqualFold(a.Scheme, b.Scheme) && strings.EqualFold(a.Hostname(), b.Hostname()) && port(a) == port(b)
}

func ListWebArtifacts(config Object) (Object, error) {
	if !boolValue(config["enabled"]) {
		return Object{"enabled": false, "artifacts": []Object{}}, nil
	}
	ready := true
	if err := PublisherReady(fmt.Sprint(config["base_url"])); err != nil {
		ready = false
	}
	items, err := ArtifactInventory(fmt.Sprint(config["artifact_root"]), fmt.Sprint(config["base_url"]))
	if err != nil {
		return nil, err
	}
	return Object{"enabled": true, "publisher_ready": ready, "max_upload_bytes": MaxArtifactUploadBytes, "max_upload_files": MaxArtifactUploadFiles, "artifacts": items}, nil
}

func UploadWebArtifact(config, payload Object) (Object, error) {
	if !boolValue(config["enabled"]) {
		return nil, fmt.Errorf("web publishing is disabled")
	}
	name, _ := stringValue(payload["name"])
	if err := ValidateArtifactName(name); err != nil {
		return nil, err
	}
	entrypoint, _ := stringValue(payload["entrypoint"])
	if entrypoint == "" {
		entrypoint = "index.html"
	}
	entrypoint, err := ValidateEntrypoint(entrypoint)
	if err != nil {
		return nil, err
	}
	overwrite := boolValue(payload["overwrite"])
	raw, ok := payload["files"].([]any)
	if !ok || len(raw) == 0 || len(raw) > MaxArtifactUploadFiles {
		return nil, fmt.Errorf("upload must contain 1 to %d files", MaxArtifactUploadFiles)
	}
	if err := PublisherReady(fmt.Sprint(config["base_url"])); err != nil {
		return nil, err
	}
	temporary, err := os.MkdirTemp("", "ade-upload-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(temporary)
	seen := map[string]bool{}
	total := 0
	for _, value := range raw {
		item := object(value)
		if item == nil {
			return nil, fmt.Errorf("invalid upload file")
		}
		path, ok := item["path"].(string)
		if !ok || path == "" || len(path) > 1024 || strings.Contains(path, "\\") || strings.IndexByte(path, 0) >= 0 {
			return nil, fmt.Errorf("invalid upload path")
		}
		parts := strings.Split(path, "/")
		for _, part := range parts {
			if part == "" || strings.HasPrefix(part, ".") {
				return nil, fmt.Errorf("invalid upload path")
			}
		}
		if seen[path] {
			return nil, fmt.Errorf("duplicate upload path")
		}
		seen[path] = true
		encoded, ok := item["content_base64"].(string)
		if !ok || len(encoded) > 4*((MaxArtifactUploadBytes+2)/3) {
			return nil, fmt.Errorf("upload exceeds 10 MiB")
		}
		data, err := base64.StdEncoding.Strict().DecodeString(encoded)
		if err != nil {
			return nil, fmt.Errorf("invalid base64 file content")
		}
		total += len(data)
		if total > MaxArtifactUploadBytes {
			return nil, fmt.Errorf("upload exceeds 10 MiB")
		}
		target := filepath.Join(temporary, filepath.FromSlash(path))
		if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
			return nil, fmt.Errorf("conflicting upload paths")
		}
		if err := os.WriteFile(target, data, 0o600); err != nil {
			return nil, fmt.Errorf("conflicting upload paths")
		}
	}
	if !seen[entrypoint] {
		return nil, fmt.Errorf("entrypoint is not included in upload")
	}
	return PublishHTML(temporary, name, fmt.Sprint(config["artifact_root"]), fmt.Sprint(config["base_url"]), entrypoint, overwrite)
}

func RemoveWebArtifact(config Object, name string) (Object, error) {
	if !boolValue(config["enabled"]) {
		return nil, fmt.Errorf("web publishing is disabled")
	}
	return DeleteHTML(name, fmt.Sprint(config["artifact_root"]))
}

func IsNotFound(err error) bool { return errors.Is(err, fs.ErrNotExist) }
