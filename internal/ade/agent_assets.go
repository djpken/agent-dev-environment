package ade

import (
	"context"
	"crypto/sha1"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const agentAssetRequestTimeout = 30 * time.Second
const agentAssetResponseLimit = 16 << 20

type agentSkillFiles map[string]string

type githubTreeEntry struct {
	Path string `json:"path"`
	Mode string `json:"mode"`
	Type string `json:"type"`
	SHA  string `json:"sha"`
}

type githubTreeResponse struct {
	Truncated bool              `json:"truncated"`
	Tree      []githubTreeEntry `json:"tree"`
}

type agentSkillStatus struct {
	ID           string `json:"id"`
	Status       string `json:"status"`
	Installed    bool   `json:"installed"`
	FileCount    int    `json:"file_count"`
	ChangedFiles int    `json:"changed_files"`
}

type agentMCPStatus struct {
	ID               string `json:"id"`
	Label            string `json:"label"`
	Transport        string `json:"transport"`
	Enabled          bool   `json:"enabled"`
	Installed        bool   `json:"installed"`
	InstalledVersion string `json:"installed_version,omitempty"`
	LatestVersion    string `json:"latest_version,omitempty"`
	UpdateStatus     string `json:"update_status"`
	ConnectionStatus string `json:"connection_status"`
}

type agentAssetSummary struct {
	Skills                int `json:"skills"`
	SkillUpdatesAvailable int `json:"skill_updates_available"`
	SkillsAvailable       int `json:"skills_available"`
	MCPServers            int `json:"mcp_servers"`
	MCPUpdatesAvailable   int `json:"mcp_updates_available"`
	MCPEnabled            int `json:"mcp_enabled"`
}

type agentAssetReport struct {
	CheckedAt    string             `json:"checked_at"`
	Repository   string             `json:"repository"`
	SourceRef    string             `json:"source_ref"`
	Installation string             `json:"installation"`
	Skills       []agentSkillStatus `json:"skills"`
	MCPServers   []agentMCPStatus   `json:"mcp_servers"`
	Summary      agentAssetSummary  `json:"summary"`
}

type githubRepository struct {
	DefaultBranch string `json:"default_branch"`
}

type githubBlob struct {
	Encoding string `json:"encoding"`
	Content  string `json:"content"`
}

// checkAgentAssetUpdates reads the installed ADE generation and compares its
// Workflow Pack and locked MCP provider versions with the repository's current
// default branch. It does not modify the installation or the source checkout.
func checkAgentAssetUpdates(parent context.Context, config *EnvironmentConfig) (agentAssetReport, error) {
	if parent == nil {
		parent = context.Background()
	}
	ctx, cancel := context.WithTimeout(parent, agentAssetRequestTimeout)
	defer cancel()

	localLock, err := ReadJSON(filepath.Join(config.SourceRoot, "ade.lock.json"))
	if err != nil {
		return agentAssetReport{}, fmt.Errorf("could not read the ADE source lockfile")
	}
	workflow := object(localLock["workflow"])
	repository, _ := workflow["repository"].(string)
	owner, repo, err := parseGitHubRepository(repository)
	if err != nil {
		return agentAssetReport{}, err
	}

	client := &http.Client{Timeout: 12 * time.Second}
	apiBase := "https://api.github.com/repos/" + url.PathEscape(owner) + "/" + url.PathEscape(repo)
	var repositoryInfo githubRepository
	if err := githubAPIGet(ctx, client, apiBase, &repositoryInfo); err != nil {
		return agentAssetReport{}, err
	}
	if repositoryInfo.DefaultBranch == "" || strings.ContainsAny(repositoryInfo.DefaultBranch, "?#") {
		return agentAssetReport{}, fmt.Errorf("GitHub repository has no usable default branch")
	}

	var tree githubTreeResponse
	treeURL := apiBase + "/git/trees/" + url.PathEscape(repositoryInfo.DefaultBranch) + "?recursive=1"
	if err := githubAPIGet(ctx, client, treeURL, &tree); err != nil {
		return agentAssetReport{}, err
	}
	if tree.Truncated {
		return agentAssetReport{}, fmt.Errorf("GitHub repository tree is too large to inspect completely")
	}
	entries := make(map[string]githubTreeEntry, len(tree.Tree))
	for _, entry := range tree.Tree {
		if entry.Type != "blob" || !safeRepositoryPath(entry.Path) {
			continue
		}
		entries[entry.Path] = entry
	}
	lockEntry, ok := entries["ade.lock.json"]
	if !ok || lockEntry.SHA == "" {
		return agentAssetReport{}, fmt.Errorf("GitHub repository has no ADE lockfile")
	}
	var remoteBlob githubBlob
	if err := githubAPIGet(ctx, client, apiBase+"/git/blobs/"+url.PathEscape(lockEntry.SHA), &remoteBlob); err != nil {
		return agentAssetReport{}, err
	}
	if remoteBlob.Encoding != "base64" {
		return agentAssetReport{}, fmt.Errorf("GitHub ADE lockfile has an unsupported encoding")
	}
	remoteLockBytes, err := base64.StdEncoding.DecodeString(strings.Join(strings.Fields(remoteBlob.Content), ""))
	if err != nil {
		return agentAssetReport{}, fmt.Errorf("GitHub ADE lockfile could not be decoded")
	}
	remoteLock, err := DecodeJSON(remoteLockBytes)
	if err != nil {
		return agentAssetReport{}, fmt.Errorf("GitHub ADE lockfile is invalid")
	}

	release, err := Generation(config.ADERoot)
	if err != nil {
		return agentAssetReport{}, fmt.Errorf("could not inspect the active ADE generation")
	}
	installedSkillFiles := map[string]agentSkillFiles{}
	installedProviders := map[string]Object{}
	enabledProviders := map[string]bool{}
	installation := "not_installed"
	if release != "" {
		installation = "installed"
		installedSkillFiles, err = readSkillFiles(filepath.Join(release, "workflow", "skills"))
		if err != nil {
			return agentAssetReport{}, fmt.Errorf("could not inspect installed Skills")
		}
		installedConfig, err := ReadJSON(filepath.Join(release, "config.json"))
		if err != nil {
			return agentAssetReport{}, fmt.Errorf("could not read installed ADE provider metadata")
		}
		for id, raw := range object(installedConfig["manifests"]) {
			if manifest := object(raw); manifest != nil {
				installedProviders[id] = manifest
			}
		}
		for id, raw := range object(installedConfig["providers"]) {
			enabledProviders[id] = boolValue(raw)
		}
	}

	remoteSkillFiles, err := skillFilesFromGitHub(ctx, client, apiBase, entries)
	if err != nil {
		return agentAssetReport{}, fmt.Errorf("could not compare Skills in the GitHub source")
	}
	skills := compareSkillFiles(installedSkillFiles, remoteSkillFiles, installation == "installed")
	remoteProviders := providerManifests(remoteLock)
	providers := compareMCPProviders(installedProviders, enabledProviders, remoteProviders)

	report := agentAssetReport{
		CheckedAt:    isoNow(),
		Repository:   "https://github.com/" + owner + "/" + repo,
		SourceRef:    repositoryInfo.DefaultBranch,
		Installation: installation,
		Skills:       skills,
		MCPServers:   providers,
	}
	for _, skill := range skills {
		report.Summary.Skills++
		if skill.Status == "update_available" {
			report.Summary.SkillUpdatesAvailable++
		}
		if skill.Status == "available" {
			report.Summary.SkillsAvailable++
		}
	}
	for _, provider := range providers {
		report.Summary.MCPServers++
		if provider.Enabled {
			report.Summary.MCPEnabled++
		}
		if provider.UpdateStatus == "update_available" {
			report.Summary.MCPUpdatesAvailable++
		}
	}
	return report, nil
}

func parseGitHubRepository(value string) (string, string, error) {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "https" || parsed.Host != "github.com" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", "", fmt.Errorf("ADE source repository must be an HTTPS GitHub repository")
	}
	parts := strings.Split(strings.Trim(parsed.Path, "/"), "/")
	if len(parts) != 2 {
		return "", "", fmt.Errorf("ADE source repository must identify a GitHub owner and repository")
	}
	repo := strings.TrimSuffix(parts[1], ".git")
	for _, part := range []string{parts[0], repo} {
		if part == "" || strings.ContainsAny(part, "\\\x00") || part == "." || part == ".." {
			return "", "", fmt.Errorf("ADE source repository is invalid")
		}
	}
	return parts[0], repo, nil
}

func safeRepositoryPath(value string) bool {
	return value != "" && !strings.Contains(value, "\\") && !strings.ContainsRune(value, 0) && path.Clean(value) == value && !strings.HasPrefix(value, "../") && !strings.HasPrefix(value, "/")
}

func githubAPIGet(ctx context.Context, client *http.Client, endpoint string, target any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return fmt.Errorf("could not prepare GitHub update check")
	}
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("User-Agent", "agent-development-environment")
	request.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("GitHub update check request failed")
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("GitHub update check returned HTTP %d", response.StatusCode)
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, agentAssetResponseLimit))
	if err := decoder.Decode(target); err != nil {
		return fmt.Errorf("GitHub update check returned invalid data")
	}
	return nil
}

func skillFilesFromGitHub(ctx context.Context, client *http.Client, apiBase string, entries map[string]githubTreeEntry) (map[string]agentSkillFiles, error) {
	ids := map[string]bool{}
	for name := range entries {
		if name == "skills/SKILL.md" || strings.HasPrefix(name, "skills/") && strings.HasSuffix(name, "/SKILL.md") {
			id := path.Dir(name)
			if id == "skills" {
				id = "skills"
			} else {
				id = strings.TrimPrefix(id, "skills/")
			}
			ids[id] = true
		}
	}
	result := map[string]agentSkillFiles{}
	linkContent := map[string][]byte{}
	for id := range ids {
		prefix := "skills/" + id + "/"
		if id == "skills" {
			prefix = "skills/"
		}
		files := agentSkillFiles{}
		for name, entry := range entries {
			if !strings.HasPrefix(name, prefix) || entry.SHA == "" {
				continue
			}
			resolved, ok, err := resolveGitHubSkillFile(ctx, client, apiBase, entries, "skills", entry, 0, linkContent)
			if err != nil {
				return nil, err
			}
			if !ok {
				continue
			}
			files[strings.TrimPrefix(name, prefix)] = resolved.SHA + ":" + resolved.Mode
		}
		result[id] = files
	}
	return result, nil
}

func resolveGitHubSkillFile(ctx context.Context, client *http.Client, apiBase string, entries map[string]githubTreeEntry, skillRoot string, entry githubTreeEntry, depth int, linkContent map[string][]byte) (githubTreeEntry, bool, error) {
	if entry.Mode == "100644" || entry.Mode == "100755" {
		return entry, true, nil
	}
	if entry.Mode != "120000" || depth >= 16 {
		return githubTreeEntry{}, false, nil
	}
	content, ok := linkContent[entry.SHA]
	if !ok {
		var blob githubBlob
		if err := githubAPIGet(ctx, client, apiBase+"/git/blobs/"+url.PathEscape(entry.SHA), &blob); err != nil {
			return githubTreeEntry{}, false, err
		}
		if blob.Encoding != "base64" {
			return githubTreeEntry{}, false, fmt.Errorf("GitHub link has an unsupported encoding")
		}
		decoded, err := base64.StdEncoding.DecodeString(strings.Join(strings.Fields(blob.Content), ""))
		if err != nil {
			return githubTreeEntry{}, false, fmt.Errorf("GitHub link could not be decoded")
		}
		content = decoded
		linkContent[entry.SHA] = content
	}
	target := string(content)
	if target == "" || path.IsAbs(target) || strings.Contains(target, "\\") {
		return githubTreeEntry{}, false, nil
	}
	targetPath := path.Clean(path.Join(path.Dir(entry.Path), target))
	if !safeRepositoryPath(targetPath) || targetPath == skillRoot || !strings.HasPrefix(targetPath, skillRoot+"/") {
		return githubTreeEntry{}, false, nil
	}
	targetEntry, ok := entries[targetPath]
	if !ok {
		return githubTreeEntry{}, false, nil
	}
	return resolveGitHubSkillFile(ctx, client, apiBase, entries, skillRoot, targetEntry, depth+1, linkContent)
}

func readSkillFiles(root string) (map[string]agentSkillFiles, error) {
	result := map[string]agentSkillFiles{}
	if _, err := os.Stat(root); os.IsNotExist(err) {
		return result, nil
	} else if err != nil {
		return nil, err
	}
	ids := map[string]bool{}
	err := filepath.WalkDir(root, func(current string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.Type()&os.ModeSymlink != 0 {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if !entry.IsDir() && entry.Name() == "SKILL.md" {
			dir, err := filepath.Rel(root, filepath.Dir(current))
			if err != nil {
				return err
			}
			id := filepath.ToSlash(dir)
			if id == "." {
				id = "skills"
			}
			ids[id] = true
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	for id := range ids {
		base := root
		if id != "skills" {
			base = filepath.Join(root, filepath.FromSlash(id))
		}
		files := agentSkillFiles{}
		err := filepath.WalkDir(base, func(current string, entry os.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if entry.Type()&os.ModeSymlink != 0 {
				if entry.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
			if entry.IsDir() {
				return nil
			}
			info, err := entry.Info()
			if err != nil || !info.Mode().IsRegular() {
				return err
			}
			data, err := os.ReadFile(current)
			if err != nil {
				return err
			}
			rel, err := filepath.Rel(base, current)
			if err != nil {
				return err
			}
			mode := "100644"
			if info.Mode()&0o111 != 0 {
				mode = "100755"
			}
			files[filepath.ToSlash(rel)] = gitBlobSHA(data) + ":" + mode
			return nil
		})
		if err != nil {
			return nil, err
		}
		result[id] = files
	}
	return result, nil
}

func gitBlobSHA(data []byte) string {
	header := []byte("blob " + strconv.Itoa(len(data)) + "\x00")
	hash := sha1.Sum(append(header, data...))
	return hex.EncodeToString(hash[:])
}

func compareSkillFiles(installed, latest map[string]agentSkillFiles, hasInstallation bool) []agentSkillStatus {
	ids := map[string]bool{}
	for id := range installed {
		ids[id] = true
	}
	for id := range latest {
		ids[id] = true
	}
	ordered := make([]string, 0, len(ids))
	for id := range ids {
		ordered = append(ordered, id)
	}
	sort.Strings(ordered)
	result := make([]agentSkillStatus, 0, len(ordered))
	for _, id := range ordered {
		local, localExists := installed[id]
		remote, remoteExists := latest[id]
		item := agentSkillStatus{ID: id, Installed: localExists, FileCount: len(remote)}
		switch {
		case !hasInstallation || !localExists:
			item.Status = "available"
		case !remoteExists:
			item.Status = "removed_upstream"
			item.FileCount = len(local)
		case sameSkillFiles(local, remote):
			item.Status = "current"
			item.FileCount = len(local)
		default:
			item.Status = "update_available"
			item.ChangedFiles = changedSkillFileCount(local, remote)
		}
		result = append(result, item)
	}
	return result
}

func sameSkillFiles(left, right agentSkillFiles) bool {
	return changedSkillFileCount(left, right) == 0
}

func changedSkillFileCount(left, right agentSkillFiles) int {
	changed := 0
	seen := map[string]bool{}
	for name, value := range left {
		seen[name] = true
		if other, ok := right[name]; !ok || other != value {
			changed++
		}
	}
	for name := range right {
		if !seen[name] {
			changed++
		}
	}
	return changed
}

func providerManifests(lock Object) map[string]Object {
	result := map[string]Object{}
	for _, raw := range listValue(lock["providers"]) {
		manifest := object(raw)
		if manifest == nil {
			continue
		}
		id, _ := manifest["id"].(string)
		if id != "" {
			result[id] = manifest
		}
	}
	return result
}

func compareMCPProviders(installed map[string]Object, enabled map[string]bool, latest map[string]Object) []agentMCPStatus {
	ids := map[string]bool{}
	for id, manifest := range installed {
		if isMCPManifest(manifest) {
			ids[id] = true
		}
	}
	for id, manifest := range latest {
		if isMCPManifest(manifest) {
			ids[id] = true
		}
	}
	ordered := make([]string, 0, len(ids))
	for id := range ids {
		ordered = append(ordered, id)
	}
	sort.Strings(ordered)
	result := make([]agentMCPStatus, 0, len(ordered))
	for _, id := range ordered {
		manifest, isInstalled := installed[id]
		latestManifest, hasLatest := latest[id]
		candidate := manifest
		if !isInstalled {
			candidate = latestManifest
		}
		label, _ := candidate["label"].(string)
		if label == "" {
			label = id
		}
		transport, _ := candidate["transport"].(string)
		item := agentMCPStatus{ID: id, Label: label, Transport: transport, Enabled: enabled[id], Installed: isInstalled && enabled[id], ConnectionStatus: "not_checked"}
		version, _ := manifest["version"].(string)
		latestVersion, _ := latestManifest["version"].(string)
		if item.Installed {
			item.InstalledVersion = version
		}
		item.LatestVersion = latestVersion
		switch {
		case !isInstalled:
			item.UpdateStatus = "available"
		case !item.Enabled:
			item.UpdateStatus = "disabled"
		case !hasLatest:
			if boolValue(manifest["builtin"]) {
				item.UpdateStatus = "source_missing"
			} else {
				item.UpdateStatus = "untracked"
			}
		case !semver.MatchString(version) || !semver.MatchString(latestVersion):
			item.UpdateStatus = "unknown"
		default:
			switch compareSemanticVersions(version, latestVersion) {
			case -1:
				item.UpdateStatus = "update_available"
			case 0:
				item.UpdateStatus = "current"
			default:
				item.UpdateStatus = "ahead"
			}
		}
		result = append(result, item)
	}
	return result
}

func isMCPManifest(manifest Object) bool {
	transport, _ := manifest["transport"].(string)
	return transport == "stdio" || transport == "http"
}

func compareSemanticVersions(installed, latest string) int {
	left := strings.Split(installed, ".")
	right := strings.Split(latest, ".")
	for index := range left {
		leftNumber, leftErr := strconv.Atoi(left[index])
		rightNumber, rightErr := strconv.Atoi(right[index])
		if leftErr != nil || rightErr != nil {
			return 0
		}
		if leftNumber < rightNumber {
			return -1
		}
		if leftNumber > rightNumber {
			return 1
		}
	}
	return 0
}
