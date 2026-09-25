package ade

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const SyncProvider = "plane-linear-sync"
const SyncVersion = "0.1.0"
const sourceMarkerPrefix = "ade:plane-linear-sync"

type PlaneLinearConfig struct {
	PlaneAPIURL, PlaneAPIToken, PlaneWorkspace                                  string
	PlaneAssigneeID, PlaneAssigneeEmail, PlaneIssuesURL, PlaneProjectID         string
	LinearAPIURL, LinearAPIKey, LinearTeamID, LinearAssigneeID, LinearProjectID string
	ProjectMap, StatusMap                                                       map[string]string
	Timeout                                                                     time.Duration
}
type PlaneProject struct{ ID, Name, Identifier, URL string }
type PlaneIssue struct {
	ID, Identifier, Title, Description, StateName, StateGroup, Priority string
	DueDate                                                             *string
	Labels                                                              []string
	Project                                                             *PlaneProject
	AssigneeIDs, AssigneeEmails                                         []string
	URL, UpdatedAt                                                      string
}
type LinearRef struct{ ID, Identifier, Description string }
type LinearAPIInput struct {
	Title, Description    string
	Priority              int
	DueDate               any
	StateID               string
	LabelIDs              []string
	ProjectID, AssigneeID string
}

func envMap(entries []string) map[string]string {
	result := map[string]string{}
	for _, entry := range entries {
		key, value, ok := strings.Cut(entry, "=")
		if ok {
			result[key] = value
		}
	}
	return result
}
func SyncEnvFile(path, repository string) (map[string]string, error) {
	candidate := path
	info, err := os.Lstat(candidate)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return nil, fmt.Errorf("credential env file must be a regular file")
	}
	resolved, err := filepath.Abs(candidate)
	if err != nil {
		return nil, err
	}
	repo, err := filepath.Abs(repository)
	if err != nil {
		return nil, err
	}
	if within(repo, resolved) {
		return nil, fmt.Errorf("credential env file must be outside the repository")
	}
	if info.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("credential env file must be readable by the owner only")
	}
	file, err := os.Open(resolved)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	result := map[string]string{}
	pattern := regexp.MustCompile(`^[A-Z][A-Z0-9_]*$`)
	scanner := bufio.NewScanner(file)
	line := 0
	for scanner.Scan() {
		line++
		value := strings.TrimSpace(scanner.Text())
		if value == "" || strings.HasPrefix(value, "#") {
			continue
		}
		key, raw, ok := strings.Cut(value, "=")
		if !ok || !pattern.MatchString(strings.TrimSpace(key)) {
			return nil, fmt.Errorf("invalid env file entry at line %d", line)
		}
		raw = strings.TrimSpace(raw)
		if strings.HasPrefix(raw, "'") || strings.HasPrefix(raw, "\"") {
			quote := raw[:1]
			if len(raw) < 2 || !strings.HasSuffix(raw, quote) {
				return nil, fmt.Errorf("invalid quoted env value at line %d", line)
			}
			inner := raw[1 : len(raw)-1]
			if quote == "\"" {
				decoded, err := strconv.Unquote(raw)
				if err != nil {
					return nil, fmt.Errorf("invalid quoted env value at line %d", line)
				}
				inner = decoded
			}
			raw = inner
		}
		result[strings.TrimSpace(key)] = raw
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return result, nil
}

func SyncConfigFromEnvironment(environment map[string]string) (PlaneLinearConfig, error) {
	required := map[string]string{"PLANE_API_URL": environment["PLANE_API_URL"], "PLANE_API_TOKEN": environment["PLANE_API_TOKEN"], "PLANE_WORKSPACE_SLUG": environment["PLANE_WORKSPACE_SLUG"], "LINEAR_API_KEY": environment["LINEAR_API_KEY"], "LINEAR_TEAM_ID": environment["LINEAR_TEAM_ID"]}
	missing := []string{}
	for key, value := range required {
		if value == "" {
			missing = append(missing, key)
		}
	}
	sort.Strings(missing)
	if environment["PLANE_ASSIGNEE_ID"] == "" && environment["PLANE_ASSIGNEE_EMAIL"] == "" {
		missing = append(missing, "PLANE_ASSIGNEE_ID or PLANE_ASSIGNEE_EMAIL")
	}
	if len(missing) > 0 {
		return PlaneLinearConfig{}, fmt.Errorf("missing sync configuration: %s", strings.Join(missing, ", "))
	}
	if !regexp.MustCompile(`^[A-Za-z0-9_-]+$`).MatchString(required["PLANE_WORKSPACE_SLUG"]) {
		return PlaneLinearConfig{}, fmt.Errorf("PLANE_WORKSPACE_SLUG must be a Plane workspace slug")
	}
	timeout := 30.0
	if raw := environment["PLANE_LINEAR_TIMEOUT"]; raw != "" {
		value, err := strconv.ParseFloat(raw, 64)
		if err != nil {
			return PlaneLinearConfig{}, fmt.Errorf("PLANE_LINEAR_TIMEOUT must be a number")
		}
		timeout = value
	}
	if timeout < 1 || timeout > 120 {
		return PlaneLinearConfig{}, fmt.Errorf("PLANE_LINEAR_TIMEOUT must be between 1 and 120 seconds")
	}
	planeURL, err := validateSyncEndpoint(required["PLANE_API_URL"], "PLANE_API_URL")
	if err != nil {
		return PlaneLinearConfig{}, err
	}
	linearURL := environment["LINEAR_API_URL"]
	if linearURL == "" {
		linearURL = "https://api.linear.app/graphql"
	}
	linearURL, err = validateSyncEndpoint(linearURL, "LINEAR_API_URL")
	if err != nil {
		return PlaneLinearConfig{}, err
	}
	issuesURL := environment["PLANE_ISSUES_URL"]
	if issuesURL != "" {
		issuesURL, err = validateSyncEndpoint(issuesURL, "PLANE_ISSUES_URL")
		if err != nil {
			return PlaneLinearConfig{}, err
		}
	}
	projects, err := syncStringMap(environment["PLANE_LINEAR_PROJECT_MAP"], "PLANE_LINEAR_PROJECT_MAP")
	if err != nil {
		return PlaneLinearConfig{}, err
	}
	statuses, err := syncStringMap(environment["PLANE_LINEAR_STATUS_MAP"], "PLANE_LINEAR_STATUS_MAP")
	if err != nil {
		return PlaneLinearConfig{}, err
	}
	return PlaneLinearConfig{PlaneAPIURL: planeURL, PlaneAPIToken: required["PLANE_API_TOKEN"], PlaneWorkspace: required["PLANE_WORKSPACE_SLUG"], PlaneAssigneeID: environment["PLANE_ASSIGNEE_ID"], PlaneAssigneeEmail: environment["PLANE_ASSIGNEE_EMAIL"], PlaneIssuesURL: issuesURL, PlaneProjectID: environment["PLANE_PROJECT_ID"], LinearAPIURL: linearURL, LinearAPIKey: required["LINEAR_API_KEY"], LinearTeamID: required["LINEAR_TEAM_ID"], LinearAssigneeID: environment["LINEAR_ASSIGNEE_ID"], LinearProjectID: environment["LINEAR_PROJECT_ID"], ProjectMap: projects, StatusMap: statuses, Timeout: time.Duration(timeout * float64(time.Second))}, nil
}
func validateSyncEndpoint(value, name string) (string, error) {
	parsed, err := url.Parse(value)
	if err != nil {
		return "", fmt.Errorf("%s must be a valid URL", name)
	}
	local := parsed.Hostname() == "localhost" || parsed.Hostname() == "127.0.0.1" || parsed.Hostname() == "::1"
	if (parsed.Scheme != "https" && parsed.Scheme != "http") || parsed.Hostname() == "" || parsed.User != nil {
		return "", fmt.Errorf("%s must be an HTTPS URL without credentials", name)
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", fmt.Errorf("%s must not contain query or fragment data", name)
	}
	if parsed.Scheme != "https" && !local {
		return "", fmt.Errorf("%s must use HTTPS unless it targets localhost", name)
	}
	return strings.TrimRight(value, "/"), nil
}
func syncStringMap(value, name string) (map[string]string, error) {
	result := map[string]string{}
	if value == "" {
		return result, nil
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(value), &parsed); err != nil || parsed == nil {
		return nil, fmt.Errorf("%s must be a JSON object", name)
	}
	for key, raw := range parsed {
		text, ok := raw.(string)
		if !ok {
			return nil, fmt.Errorf("%s must be a JSON object of strings", name)
		}
		result[key] = text
	}
	return result, nil
}

func normalizeSync(value any) string {
	return strings.ToLower(strings.Join(strings.FieldsFunc(strings.TrimSpace(fmt.Sprint(value)), func(r rune) bool { return r == ' ' || r == '\t' || r == '\n' || r == '_' || r == '-' }), " "))
}
func syncText(value any) string {
	if value == nil {
		return ""
	}
	if item := object(value); item != nil {
		for _, key := range []string{"name", "title", "value", "display_name", "displayName"} {
			if item[key] != nil {
				return strings.TrimSpace(fmt.Sprint(item[key]))
			}
		}
	}
	return strings.TrimSpace(fmt.Sprint(value))
}
func syncEntity(value any, keys ...string) string {
	if item := object(value); item != nil {
		for _, key := range keys {
			if item[key] != nil {
				return strings.TrimSpace(fmt.Sprint(item[key]))
			}
		}
	}
	if value != nil && object(value) == nil {
		return strings.TrimSpace(fmt.Sprint(value))
	}
	return ""
}
func syncDate(value any) (*string, error) {
	if value == nil || value == "" {
		return nil, nil
	}
	text := fmt.Sprint(value)
	if len(text) >= 10 {
		text = text[:10]
	}
	parsed, err := time.Parse("2006-01-02", text)
	if err != nil {
		return nil, fmt.Errorf("Plane due date is not an ISO date")
	}
	date := parsed.Format("2006-01-02")
	return &date, nil
}
func syncPriority(value any) string {
	if item := object(value); item != nil {
		value = item["name"]
		if value == nil {
			value = item["value"]
		}
		if value == nil {
			value = item["priority"]
		}
	}
	if boolean, ok := value.(bool); ok {
		if boolean {
			return "urgent"
		}
		return "none"
	}
	if number, ok := value.(json.Number); ok {
		integer, _ := number.Int64()
		if _, err := number.Int64(); err != nil {
			return "none"
		}
		return mappedPriority(integer)
	}
	if integer, ok := value.(int); ok {
		return mappedPriority(int64(integer))
	}
	normal := normalizeSync(value)
	switch normal {
	case "no priority", "none", "":
		return "none"
	case "urgent", "high", "medium", "low":
		return normal
	}
	return normal
}

func mappedPriority(value int64) string {
	if mapped, exists := map[int64]string{0: "none", 1: "urgent", 2: "high", 3: "medium", 4: "low"}[value]; exists {
		return mapped
	}
	return "none"
}
func syncStatusType(value any) string {
	normal := normalizeSync(value)
	aliases := map[string]string{"backlog": "backlog", "unstarted": "unstarted", "todo": "unstarted", "to do": "unstarted", "open": "unstarted", "started": "started", "in progress": "started", "doing": "started", "active": "started", "completed": "completed", "complete": "completed", "done": "completed", "closed": "completed", "resolved": "completed", "canceled": "canceled", "cancelled": "canceled", "rejected": "canceled"}
	if value, ok := aliases[normal]; ok {
		return value
	}
	return normal
}

func ParsePlaneIssue(payload Object, baseURL string) (PlaneIssue, error) {
	id := syncEntity(payload, "id", "issue_id", "issueId")
	title := syncEntity(payload, "name", "title")
	if id == "" || title == "" {
		return PlaneIssue{}, fmt.Errorf("Plane issue must contain id and title")
	}
	identifier := syncEntity(payload, "identifier", "sequence_id", "sequenceId")
	if identifier == "" {
		identifier = id
	}
	state := payload["state_detail"]
	if state == nil {
		state = payload["state"]
	}
	if state == nil {
		state = payload["status"]
	}
	stateName := syncEntity(state, "name", "title", "value")
	if stateName == "" {
		stateName = syncText(state)
	}
	stateGroup := syncEntity(state, "group", "type", "group_name", "groupName")
	projectValue := payload["project_detail"]
	if projectValue == nil {
		projectValue = payload["project"]
	}
	var project *PlaneProject
	if p := object(projectValue); p != nil {
		value := PlaneProject{ID: syncEntity(p, "id", "project_id", "projectId"), Name: syncEntity(p, "name", "title"), Identifier: syncEntity(p, "identifier", "key", "project_identifier")}
		value.URL, _ = p["url"].(string)
		if value.ID != "" || value.Name != "" || value.Identifier != "" {
			project = &value
		}
	} else if projectValue != nil && projectValue != "" {
		project = &PlaneProject{ID: syncText(projectValue)}
	}
	assignee := payload["assignee_detail"]
	if assignee == nil {
		assignee = payload["assignee"]
	}
	assigneesRaw := payload["assignees"]
	if assignee != nil {
		assigneesRaw = []any{assignee}
	} else if object(assigneesRaw) != nil {
		assigneesRaw = []any{assigneesRaw}
	}
	assignees := listValue(assigneesRaw)
	ids, emails := []string{}, []string{}
	for _, item := range assignees {
		if p := object(item); p != nil {
			id := syncEntity(p, "id", "user_id", "userId")
			if id != "" {
				ids = append(ids, id)
			}
			email := syncEntity(p, "email")
			if email != "" {
				emails = append(emails, email)
			}
		} else if item != nil {
			text := strings.TrimSpace(fmt.Sprint(item))
			if text != "" {
				ids = append(ids, text)
			}
		}
	}
	labelsRaw := payload["label_details"]
	if labelsRaw == nil {
		labelsRaw = payload["labels"]
	}
	labels := []string{}
	if text, ok := labelsRaw.(string); ok {
		labelsRaw = []any{text}
	} else if object(labelsRaw) != nil {
		labelsRaw = []any{labelsRaw}
	}
	for _, item := range listValue(labelsRaw) {
		text := syncText(item)
		if text != "" && !containsString(labels, text) {
			labels = append(labels, text)
		}
	}
	sort.Strings(labels)
	issueURL := syncEntity(payload, "url", "issue_url", "issueUrl")
	if issueURL == "" {
		issueURL = strings.TrimRight(baseURL, "/") + "/issues/" + url.PathEscape(id) + "/"
	}
	description := syncEntity(payload, "description_str", "description", "description_html")
	due, err := syncDate(firstAny(payload["due_date"], payload["dueDate"], payload["target_date"], payload["targetDate"]))
	if err != nil {
		return PlaneIssue{}, err
	}
	return PlaneIssue{ID: id, Identifier: identifier, Title: title, Description: description, StateName: stateName, StateGroup: stateGroup, Priority: syncPriority(payload["priority"]), DueDate: due, Labels: labels, Project: project, AssigneeIDs: ids, AssigneeEmails: emails, URL: issueURL, UpdatedAt: syncEntity(payload, "updated_at", "updatedAt")}, nil
}
func firstAny(values ...any) any {
	for _, value := range values {
		if value != nil && value != "" {
			return value
		}
	}
	return nil
}
func containsString(values []string, value string) bool {
	for _, item := range values {
		if item == value {
			return true
		}
	}
	return false
}
func (issue PlaneIssue) SourceKey(workspace string) string {
	return "plane:" + workspace + ":" + issue.ID
}
func (issue PlaneIssue) Matches(config PlaneLinearConfig) bool {
	if len(issue.AssigneeIDs) > 0 && config.PlaneAssigneeID != "" {
		return containsString(issue.AssigneeIDs, config.PlaneAssigneeID)
	}
	if len(issue.AssigneeEmails) > 0 && config.PlaneAssigneeEmail != "" {
		for _, email := range issue.AssigneeEmails {
			if strings.EqualFold(email, config.PlaneAssigneeEmail) {
				return true
			}
		}
		return false
	}
	return true
}

type SyncHTTP struct {
	client  *http.Client
	timeout time.Duration
}

func NewSyncHTTP(timeout time.Duration) *SyncHTTP {
	return &SyncHTTP{client: &http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}, timeout: timeout}
}
func (httpClient *SyncHTTP) Request(address, method string, headers map[string]string, body any) (any, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(data)
	}
	request, err := http.NewRequest(method, address, reader)
	if err != nil {
		return nil, err
	}
	for key, value := range headers {
		request.Header.Set(key, value)
	}
	ctx, cancel := context.WithTimeout(context.Background(), httpClient.timeout)
	defer cancel()
	request = request.WithContext(ctx)
	response, err := httpClient.client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("provider request failed for %s: %v", address, err)
	}
	defer response.Body.Close()
	if response.StatusCode >= 300 && response.StatusCode < 400 {
		return nil, fmt.Errorf("provider redirect refused")
	}
	if response.StatusCode == 404 {
		return nil, fmt.Errorf("provider returned HTTP 404 for %s", address)
	}
	if response.StatusCode >= 400 {
		return nil, fmt.Errorf("provider returned HTTP %d for %s", response.StatusCode, address)
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, 20*1024*1024+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > 20*1024*1024 {
		return nil, fmt.Errorf("provider response is too large")
	}
	if len(raw) == 0 {
		return Object{}, nil
	}
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return nil, fmt.Errorf("provider returned invalid JSON for %s", address)
	}
	return value, nil
}

func pageItems(payload any) ([]Object, any, error) {
	if list, ok := payload.([]any); ok {
		result := []Object{}
		for _, raw := range list {
			if item := object(raw); item != nil {
				result = append(result, item)
			}
		}
		return result, nil, nil
	}
	item := object(payload)
	if item == nil {
		return nil, nil, fmt.Errorf("provider returned an unexpected list response")
	}
	var entries any
	for _, key := range []string{"results", "issues", "items"} {
		if value, ok := item[key]; ok {
			entries = value
			break
		}
	}
	if nested := object(entries); nested != nil {
		entries = nested["nodes"]
		if entries == nil {
			entries = nested["results"]
		}
	}
	list, ok := entries.([]any)
	if !ok {
		return nil, nil, fmt.Errorf("provider returned an unexpected issue collection")
	}
	result := []Object{}
	for _, raw := range list {
		if value := object(raw); value != nil {
			result = append(result, value)
		}
	}
	continuation := firstAny(item["next"], item["next_page"], item["next_cursor"])
	if continuation == nil && boolValue(item["next_page_results"]) {
		return nil, nil, fmt.Errorf("Plane indicated another page without a continuation cursor")
	}
	return result, continuation, nil
}

func sameOriginURL(left, right string) bool {
	a, err := url.Parse(left)
	if err != nil {
		return false
	}
	b, err := url.Parse(right)
	if err != nil {
		return false
	}
	port := func(value *url.URL) string {
		if value.Port() != "" {
			return value.Port()
		}
		if value.Scheme == "https" {
			return "443"
		}
		return "80"
	}
	return a.Scheme == b.Scheme && strings.EqualFold(a.Hostname(), b.Hostname()) && port(a) == port(b)
}
func addQuery(address string, values map[string]string) string {
	parsed, err := url.Parse(address)
	if err != nil {
		return address
	}
	query := parsed.Query()
	for key, value := range values {
		query.Add(key, value)
	}
	parsed.RawQuery = query.Encode()
	return parsed.String()
}
func planeContinuation(value any, base string, endpoint string, params map[string]string) (string, bool, error) {
	if value == nil || value == "" || value == false {
		return "", false, nil
	}
	if item := object(value); item != nil {
		value = firstAny(item["url"], item["next"])
	}
	text, ok := value.(string)
	if !ok {
		text = fmt.Sprint(value)
	}
	if strings.HasPrefix(text, "http://") || strings.HasPrefix(text, "https://") {
		if !sameOriginURL(text, endpoint) {
			return "", false, fmt.Errorf("Plane pagination pointed to a different origin")
		}
		return text, true, nil
	}
	if strings.HasPrefix(text, "/") {
		parsed, _ := url.Parse(base)
		relative, _ := url.Parse(text)
		return parsed.ResolveReference(relative).String(), true, nil
	}
	next := map[string]string{}
	for k, v := range params {
		next[k] = v
	}
	next["cursor"] = text
	return addQuery(endpoint, next), true, nil
}

type PlaneAPIClient struct {
	Config PlaneLinearConfig
	HTTP   *SyncHTTP
}

func NewPlaneAPIClient(config PlaneLinearConfig) *PlaneAPIClient {
	return &PlaneAPIClient{Config: config, HTTP: NewSyncHTTP(config.Timeout)}
}
func (p *PlaneAPIClient) headers() map[string]string {
	return map[string]string{"Accept": "application/json", "X-API-Key": p.Config.PlaneAPIToken}
}
func (p *PlaneAPIClient) appendIssues(items []Object, issues *[]PlaneIssue, seen map[string]bool) error {
	for _, item := range items {
		issue, err := ParsePlaneIssue(item, p.Config.PlaneAPIURL)
		if err != nil {
			return err
		}
		if seen[issue.ID] || !issue.Matches(p.Config) {
			continue
		}
		seen[issue.ID] = true
		*issues = append(*issues, issue)
	}
	return nil
}
func (p *PlaneAPIClient) listGet(endpoint string, params map[string]string) ([]PlaneIssue, error) {
	next := addQuery(endpoint, params)
	seenURL := map[string]bool{}
	seenIssue := map[string]bool{}
	issues := []PlaneIssue{}
	for next != "" {
		if !strings.HasPrefix(next, "http://") && !strings.HasPrefix(next, "https://") {
			next = strings.TrimRight(p.Config.PlaneAPIURL, "/") + "/" + strings.TrimLeft(next, "/")
		}
		if !sameOriginURL(next, endpoint) {
			return nil, fmt.Errorf("Plane pagination pointed to a different origin")
		}
		if seenURL[next] {
			return nil, fmt.Errorf("Plane pagination repeated the same page")
		}
		seenURL[next] = true
		payload, err := p.HTTP.Request(next, http.MethodGet, p.headers(), nil)
		if err != nil {
			return nil, err
		}
		items, continuation, err := pageItems(payload)
		if err != nil {
			return nil, err
		}
		if err := p.appendIssues(items, &issues, seenIssue); err != nil {
			return nil, err
		}
		next, _, err = planeContinuation(continuation, p.Config.PlaneAPIURL, endpoint, params)
		if err != nil {
			return nil, err
		}
	}
	return issues, nil
}
func (p *PlaneAPIClient) listAdvanced() ([]PlaneIssue, error) {
	if p.Config.PlaneAssigneeID == "" {
		return nil, fmt.Errorf("PLANE_ASSIGNEE_ID is required for workspace-wide Plane search")
	}
	endpoint := strings.TrimRight(p.Config.PlaneAPIURL, "/") + "/api/v1/workspaces/" + url.PathEscape(p.Config.PlaneWorkspace) + "/work-items/advanced-search/"
	body := Object{"filters": Object{"assignees": []string{p.Config.PlaneAssigneeID}}, "limit": 100, "workspace_search": true}
	issues := []PlaneIssue{}
	seen := map[string]bool{}
	cursors := map[string]bool{}
	for {
		payload, err := p.HTTP.Request(endpoint, http.MethodPost, map[string]string{"Accept": "application/json", "X-API-Key": p.Config.PlaneAPIToken, "Content-Type": "application/json"}, body)
		if err != nil {
			return nil, err
		}
		items, continuation, err := pageItems(payload)
		if err != nil {
			return nil, err
		}
		if err := p.appendIssues(items, &issues, seen); err != nil {
			return nil, err
		}
		if value := object(continuation); value != nil {
			continuation = firstAny(value["cursor"], value["next_cursor"], value["url"])
		}
		if continuation == nil || continuation == "" {
			return issues, nil
		}
		cursor := fmt.Sprint(continuation)
		if cursors[cursor] {
			return nil, fmt.Errorf("Plane pagination repeated the same cursor")
		}
		cursors[cursor] = true
		body["cursor"] = continuation
	}
}
func (p *PlaneAPIClient) ListAssignedIssues() ([]PlaneIssue, error) {
	if p.Config.PlaneIssuesURL != "" {
		assignee := p.Config.PlaneAssigneeID
		if assignee == "" {
			assignee = p.Config.PlaneAssigneeEmail
		}
		params := map[string]string{"assignee": assignee, "per_page": "100"}
		if p.Config.PlaneProjectID != "" {
			params["project"] = p.Config.PlaneProjectID
		}
		return p.listGet(p.Config.PlaneIssuesURL, params)
	}
	if p.Config.PlaneProjectID != "" {
		endpoint := strings.TrimRight(p.Config.PlaneAPIURL, "/") + "/api/v1/workspaces/" + url.PathEscape(p.Config.PlaneWorkspace) + "/projects/" + url.PathEscape(p.Config.PlaneProjectID) + "/work-items/"
		return p.listGet(endpoint, map[string]string{"per_page": "100", "expand": "assignees"})
	}
	return p.listAdvanced()
}

type LinearAPIClient struct {
	Config         PlaneLinearConfig
	HTTP           *SyncHTTP
	states, labels []Object
	viewer         string
	sourceIndex    map[string]LinearRef
}

func NewLinearAPIClient(config PlaneLinearConfig) *LinearAPIClient {
	return &LinearAPIClient{Config: config, HTTP: NewSyncHTTP(config.Timeout)}
}
func (l *LinearAPIClient) graphql(query string, variables Object) (Object, error) {
	payload, err := l.HTTP.Request(l.Config.LinearAPIURL, http.MethodPost, map[string]string{"Accept": "application/json", "Content-Type": "application/json", "Authorization": l.Config.LinearAPIKey}, Object{"query": query, "variables": variables})
	if err != nil {
		return nil, err
	}
	result := object(payload)
	if result == nil {
		return nil, fmt.Errorf("Linear returned no GraphQL data")
	}
	if errorsRaw, ok := result["errors"].([]any); ok && len(errorsRaw) > 0 {
		messages := []string{}
		for _, raw := range errorsRaw {
			item := object(raw)
			if item != nil && item["message"] != nil {
				messages = append(messages, fmt.Sprint(item["message"]))
			}
		}
		message := strings.Join(messages, "; ")
		if strings.Contains(strings.ToLower(message), "not found") {
			return nil, fmt.Errorf("Linear issue was not found")
		}
		if message != "" {
			return nil, fmt.Errorf("Linear GraphQL request failed: %s", message)
		}
		return nil, fmt.Errorf("Linear GraphQL request failed")
	}
	data := object(result["data"])
	if data == nil {
		return nil, fmt.Errorf("Linear returned no GraphQL data")
	}
	return data, nil
}

func (l *LinearAPIClient) FindBySourceKey(sourceKey string) (*LinearRef, error) {
	if l.sourceIndex == nil {
		l.sourceIndex = map[string]LinearRef{}
		after := ""
		seen := map[string]bool{}
		query := `query FindPlaneSyncIssues($teamId: ID!, $after: String) { team(id: $teamId) { issues(first: 100, after: $after) { nodes { id identifier description } pageInfo { hasNextPage endCursor } } } }`
		for {
			data, err := l.graphql(query, Object{"teamId": l.Config.LinearTeamID, "after": nullableString(after)})
			if err != nil {
				return nil, err
			}
			team := object(data["team"])
			issues := object(team["issues"])
			for _, raw := range listValue(issues["nodes"]) {
				item := object(raw)
				if item == nil {
					continue
				}
				description := syncText(item["description"])
				match := regexp.MustCompile(`<!-- ` + regexp.QuoteMeta(sourceMarkerPrefix) + ` source=([^ ]+) -->`).FindStringSubmatch(description)
				if len(match) > 1 && item["id"] != nil {
					ref := LinearRef{ID: syncText(item["id"]), Identifier: syncText(item["identifier"]), Description: description}
					if previous, ok := l.sourceIndex[match[1]]; ok && previous.ID != ref.ID {
						return nil, fmt.Errorf("multiple Linear issues contain the same Plane source marker")
					}
					l.sourceIndex[match[1]] = ref
				}
			}
			page := object(issues["pageInfo"])
			if !boolValue(page["hasNextPage"]) {
				break
			}
			after = syncText(page["endCursor"])
			if after == "" || seen[after] {
				return nil, fmt.Errorf("Linear pagination repeated the same cursor")
			}
			seen[after] = true
		}
	}
	if ref, ok := l.sourceIndex[sourceKey]; ok {
		return &ref, nil
	}
	return nil, nil
}

func inputObject(config PlaneLinearConfig, value LinearAPIInput) Object {
	result := Object{"title": value.Title, "description": value.Description, "priority": value.Priority, "dueDate": value.DueDate, "stateId": value.StateID, "labelIds": value.LabelIDs}
	if value.ProjectID != "" {
		result["projectId"] = value.ProjectID
	}
	if value.AssigneeID != "" {
		result["assigneeId"] = value.AssigneeID
	}
	return result
}
func (l *LinearAPIClient) createIssue(value LinearAPIInput) (LinearRef, error) {
	input := inputObject(l.Config, value)
	input["teamId"] = l.Config.LinearTeamID
	query := `mutation CreatePlaneSyncIssue($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier description } } }`
	data, err := l.graphql(query, Object{"input": input})
	if err != nil {
		return LinearRef{}, err
	}
	result := object(data["issueCreate"])
	issue := object(result["issue"])
	if !boolValue(result["success"]) || issue == nil || issue["id"] == nil {
		return LinearRef{}, fmt.Errorf("Linear did not create the issue")
	}
	return LinearRef{ID: syncText(issue["id"]), Identifier: syncText(issue["identifier"]), Description: syncText(issue["description"])}, nil
}
func (l *LinearAPIClient) updateIssue(id string, value LinearAPIInput) (LinearRef, error) {
	query := `mutation UpdatePlaneSyncIssue($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success issue { id identifier description } } }`
	data, err := l.graphql(query, Object{"id": id, "input": inputObject(l.Config, value)})
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "not found") {
			return LinearRef{}, errLinearNotFound
		}
		return LinearRef{}, err
	}
	result := object(data["issueUpdate"])
	issue := object(result["issue"])
	if !boolValue(result["success"]) || issue == nil || issue["id"] == nil {
		return LinearRef{}, fmt.Errorf("Linear did not update the issue")
	}
	return LinearRef{ID: syncText(issue["id"]), Identifier: syncText(issue["identifier"]), Description: syncText(issue["description"])}, nil
}
func (l *LinearAPIClient) workflowStates() ([]Object, error) {
	if l.states != nil {
		return l.states, nil
	}
	query := `query PlaneSyncWorkflowStates($teamId: ID!) { workflowStates(filter: {team: {id: {eq: $teamId}}}, first: 100) { nodes { id name type } } }`
	data, err := l.graphql(query, Object{"teamId": l.Config.LinearTeamID})
	if err != nil {
		return nil, err
	}
	l.states = []Object{}
	for _, raw := range listValue(object(data["workflowStates"])["nodes"]) {
		if item := object(raw); item != nil {
			l.states = append(l.states, item)
		}
	}
	return l.states, nil
}
func (l *LinearAPIClient) resolveState(name, group string) (string, error) {
	states, err := l.workflowStates()
	if err != nil {
		return "", err
	}
	mapping := map[string]string{}
	for key, value := range l.Config.StatusMap {
		mapping[normalizeSync(key)] = value
	}
	normalized := normalizeSync(name)
	sourceGroup := syncStatusType(firstNonEmpty(group, name))
	target := mapping[normalized]
	if target == "" {
		target = mapping[sourceGroup]
	}
	if target != "" {
		for _, state := range states {
			if syncText(state["id"]) == target || normalizeSync(state["name"]) == normalizeSync(target) {
				return syncText(state["id"]), nil
			}
		}
		if strings.Contains(" backlog unstarted started completed canceled ", " "+normalizeSync(target)+" ") {
			candidates := []Object{}
			for _, state := range states {
				if syncStatusType(state["type"]) == normalizeSync(target) {
					candidates = append(candidates, state)
				}
			}
			if len(candidates) == 1 {
				return syncText(candidates[0]["id"]), nil
			}
		}
	}
	for _, state := range states {
		if normalized != "" && normalizeSync(state["name"]) == normalized {
			return syncText(state["id"]), nil
		}
	}
	candidates := []Object{}
	for _, state := range states {
		if syncStatusType(state["type"]) == sourceGroup {
			candidates = append(candidates, state)
		}
	}
	sort.SliceStable(candidates, func(i, j int) bool {
		left, right := normalizeSync(candidates[i]["name"]), normalizeSync(candidates[j]["name"])
		if sourceGroup == "started" {
			preferred := map[string]bool{"in progress": true, "started": true, "doing": true}
			if preferred[left] != preferred[right] {
				return preferred[left]
			}
		}
		return left < right
	})
	if len(candidates) > 0 && syncText(candidates[0]["id"]) != "" {
		return syncText(candidates[0]["id"]), nil
	}
	return "", fmt.Errorf("no Linear workflow state matches Plane state %q", firstNonEmpty(name, group))
}
func (l *LinearAPIClient) issueLabels() ([]Object, error) {
	if l.labels != nil {
		return l.labels, nil
	}
	query := `query PlaneSyncIssueLabels($teamId: ID!) { issueLabels(filter: {team: {id: {eq: $teamId}}}, first: 250) { nodes { id name } } }`
	data, err := l.graphql(query, Object{"teamId": l.Config.LinearTeamID})
	if err != nil {
		return nil, err
	}
	l.labels = []Object{}
	for _, raw := range listValue(object(data["issueLabels"])["nodes"]) {
		if item := object(raw); item != nil {
			l.labels = append(l.labels, item)
		}
	}
	return l.labels, nil
}
func (l *LinearAPIClient) resolveLabels(names []string) ([]string, error) {
	labels, err := l.issueLabels()
	if err != nil {
		return nil, err
	}
	lookup := map[string]string{}
	for _, item := range labels {
		lookup[normalizeSync(item["name"])] = syncText(item["id"])
	}
	result := []string{}
	for _, name := range names {
		if id := lookup[normalizeSync(name)]; id != "" && !containsString(result, id) {
			result = append(result, id)
		}
	}
	sort.Strings(result)
	return result, nil
}
func (l *LinearAPIClient) viewerID() (string, error) {
	if l.viewer != "" {
		return l.viewer, nil
	}
	data, err := l.graphql("query PlaneSyncViewer { viewer { id } }", Object{})
	if err != nil {
		return "", err
	}
	l.viewer = syncText(object(data["viewer"])["id"])
	if l.viewer == "" {
		return "", fmt.Errorf("Linear viewer response did not contain an id")
	}
	return l.viewer, nil
}

var errLinearNotFound = errors.New("Linear issue was not found")

type SyncStateStore struct{ Directory, MappingsPath, RunsDirectory, LockPath string }

func NewSyncStateStore(root string) (*SyncStateStore, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	directory := filepath.Join(root, "state", SyncProvider)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return nil, err
	}
	_ = os.Chmod(directory, 0o700)
	return &SyncStateStore{Directory: directory, MappingsPath: filepath.Join(directory, "mappings.json"), RunsDirectory: filepath.Join(directory, "runs"), LockPath: filepath.Join(directory, "run.lock")}, nil
}
func (store *SyncStateStore) readMappings() (Object, error) {
	state, err := readJSONOr(store.MappingsPath, nil)
	if os.IsNotExist(err) {
		return Object{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("Plane/Linear mapping state is invalid JSON")
	}
	if state == nil {
		return Object{}, nil
	}
	if state["version"] != json.Number("1") || object(state["mappings"]) == nil {
		return nil, fmt.Errorf("unsupported Plane/Linear mapping state")
	}
	return object(state["mappings"]), nil
}
func (store *SyncStateStore) appendRun(value Object) error {
	if err := os.MkdirAll(store.RunsDirectory, 0o700); err != nil {
		return err
	}
	_ = os.Chmod(store.RunsDirectory, 0o700)
	path := filepath.Join(store.RunsDirectory, time.Now().UTC().Format("2006-01-02")+".jsonl")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	_ = file.Chmod(0o600)
	data, err := CanonicalJSON(value, false, false)
	if err == nil {
		_, err = file.Write(append(data, '\n'))
	}
	closeErr := file.Close()
	if err != nil {
		return err
	}
	return closeErr
}
func (store *SyncStateStore) saveMapping(mappings Object, key string, value Object) error {
	mappings[key] = value
	return WriteJSON(store.MappingsPath, Object{"version": 1, "mappings": mappings}, 0o600)
}

func sourceMarker(key string) string { return "<!-- " + sourceMarkerPrefix + " source=" + key + " -->" }
func sourceDescription(issue PlaneIssue, key string) string {
	project := ""
	if issue.Project != nil {
		project = firstNonEmpty(issue.Project.Identifier, issue.Project.Name, issue.Project.ID)
	}
	lines := []string{}
	if strings.TrimSpace(issue.Description) != "" {
		lines = append(lines, strings.TrimSpace(issue.Description))
	}
	lines = append(lines, "", "---", "來源追溯")
	if issue.URL != "" {
		lines = append(lines, "Plane issue：["+issue.Identifier+"]("+issue.URL+")")
	} else {
		lines = append(lines, "Plane issue："+issue.Identifier)
	}
	if project != "" {
		lines = append(lines, "Plane project："+project)
	} else {
		lines = append(lines, "Plane project：未提供")
	}
	lines = append(lines, sourceMarker(key))
	return strings.TrimSpace(strings.Join(lines, "\n"))
}
func linearPriority(value string) int {
	switch normalizeSync(value) {
	case "urgent":
		return 1
	case "high":
		return 2
	case "medium":
		return 3
	case "low":
		return 4
	}
	return 0
}

func (l *LinearAPIClient) inputForIssue(issue PlaneIssue, key string, config PlaneLinearConfig) (LinearAPIInput, error) {
	stateID, err := l.resolveState(issue.StateName, issue.StateGroup)
	if err != nil {
		return LinearAPIInput{}, err
	}
	labels, err := l.resolveLabels(issue.Labels)
	if err != nil {
		return LinearAPIInput{}, err
	}
	assignee := config.LinearAssigneeID
	if assignee == "" {
		assignee, err = l.viewerID()
		if err != nil {
			return LinearAPIInput{}, err
		}
	}
	project := config.LinearProjectID
	if project == "" && issue.Project != nil {
		for _, candidate := range []string{issue.Project.ID, issue.Project.Identifier, issue.Project.Name} {
			if mapped := config.ProjectMap[candidate]; candidate != "" && mapped != "" {
				project = mapped
				break
			}
		}
	}
	var due any
	if issue.DueDate != nil {
		due = *issue.DueDate
	}
	return LinearAPIInput{Title: issue.Title, Description: sourceDescription(issue, key), Priority: linearPriority(issue.Priority), DueDate: due, StateID: stateID, LabelIDs: labels, ProjectID: project, AssigneeID: assignee}, nil
}

func RunSync(root string, environment map[string]string) (Object, error) {
	config, err := SyncConfigFromEnvironment(environment)
	if err != nil {
		return nil, err
	}
	state, err := NewSyncStateStore(root)
	if err != nil {
		return nil, err
	}
	plane := NewPlaneAPIClient(config)
	linear := NewLinearAPIClient(config)
	result := Object{"run_id": randomUUID(), "provider": SyncProvider, "started_at": time.Now().UTC().Format(time.RFC3339), "status": "failed", "discovered": 0, "created": 0, "updated": 0, "skipped": 0, "errors": []any{}, "items": []any{}}
	err = Lock(state.LockPath, false, func() error {
		_ = os.Chmod(state.LockPath, 0o600)
		sourceIssues, err := plane.ListAssignedIssues()
		if err != nil {
			result["errors"] = append(listValue(result["errors"]), syncError("list", nil, err, config))
			return finishSync(state, result)
		}
		result["discovered"] = len(sourceIssues)
		seen := map[string]bool{}
		mappings, err := state.readMappings()
		if err != nil {
			result["errors"] = append(listValue(result["errors"]), syncError("list", nil, err, config))
			return finishSync(state, result)
		}
		for _, issue := range sourceIssues {
			key := issue.SourceKey(config.PlaneWorkspace)
			if seen[key] {
				result["skipped"] = numberAsInt(result["skipped"]) + 1
				continue
			}
			seen[key] = true
			item, action, err := syncOne(issue, key, config, linear, state, mappings)
			if err != nil {
				result["errors"] = append(listValue(result["errors"]), syncError("sync", &key, err, config))
				continue
			}
			result["items"] = append(listValue(result["items"]), item)
			result[action] = numberAsInt(result[action]) + 1
		}
		return finishSync(state, result)
	})
	return result, err
}

func syncOne(issue PlaneIssue, key string, config PlaneLinearConfig, linear *LinearAPIClient, state *SyncStateStore, mappings Object) (Object, string, error) {
	input, err := linear.inputForIssue(issue, key, config)
	if err != nil {
		return nil, "", err
	}
	mapping := object(mappings[key])
	var target *LinearRef
	action := "updated"
	if mapping != nil && mapping["linear_id"] != nil {
		targetID := fmt.Sprint(mapping["linear_id"])
		ref, err := linear.updateIssue(targetID, input)
		if err == nil {
			target = &ref
		} else if errors.Is(err, errLinearNotFound) || strings.Contains(strings.ToLower(err.Error()), "returned http 404") {
			target = nil
		} else {
			return nil, "", err
		}
	}
	if target == nil {
		found, err := linear.FindBySourceKey(key)
		if err != nil {
			return nil, "", err
		}
		if found != nil {
			ref, err := linear.updateIssue(found.ID, input)
			if err != nil {
				return nil, "", err
			}
			target = &ref
		} else {
			ref, err := linear.createIssue(input)
			if err != nil {
				return nil, "", err
			}
			target = &ref
			action = "created"
		}
	}
	mapping = Object{"linear_id": target.ID, "linear_identifier": target.Identifier, "plane_updated_at": nullableString(issue.UpdatedAt), "synced_at": time.Now().UTC().Format(time.RFC3339)}
	if err := state.saveMapping(mappings, key, mapping); err != nil {
		return nil, "", err
	}
	return Object{"source_key": key, "linear_id": target.ID, "linear_identifier": target.Identifier, "action": action}, action, nil
}

func finishSync(state *SyncStateStore, result Object) error {
	result["finished_at"] = time.Now().UTC().Format(time.RFC3339)
	errs := listValue(result["errors"])
	if len(errs) > 0 {
		if numberAsInt(result["created"]) > 0 || numberAsInt(result["updated"]) > 0 {
			result["status"] = "partial"
		} else {
			result["status"] = "failed"
		}
	} else {
		result["status"] = "completed"
	}
	return state.appendRun(result)
}
func syncError(stage string, key *string, err error, config PlaneLinearConfig) Object {
	message := err.Error()
	for _, secret := range []string{config.PlaneAPIToken, config.LinearAPIKey} {
		if secret != "" {
			message = strings.ReplaceAll(message, secret, "<redacted>")
		}
	}
	var source any
	if key != nil {
		source = *key
	}
	kind := fmt.Sprintf("%T", err)
	if index := strings.LastIndex(kind, "."); index >= 0 {
		kind = kind[index+1:]
	}
	return Object{"stage": stage, "source_key": source, "error": kind, "message": message}
}
func numberAsInt(value any) int {
	switch n := value.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case json.Number:
		number, err := n.Int64()
		if err == nil {
			return int(number)
		}
	}
	return 0
}
