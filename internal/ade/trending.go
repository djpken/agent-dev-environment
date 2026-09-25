package ade

import (
	"encoding/json"
	"fmt"
	"html"
	"io"
	"math"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type trendCacheEntry struct {
	at    time.Time
	value Object
}
type TrendingService struct {
	mu       sync.Mutex
	cache    map[string]trendCacheEntry
	locks    map[string]*sync.Mutex
	cacheFor time.Duration
	client   *http.Client
}

func NewTrendingService() *TrendingService {
	return &TrendingService{cache: map[string]trendCacheEntry{}, locks: map[string]*sync.Mutex{}, cacheFor: 45 * time.Second, client: &http.Client{Timeout: 20 * time.Second}}
}

var githubArticle = regexp.MustCompile(`(?is)<article class="Box-row"[\s\S]*?</article>`)
var githubHref = regexp.MustCompile(`(?is)<h2\b[\s\S]*?<a\b[^>]*href="/([^"]+)"`)
var githubDescription = regexp.MustCompile(`(?is)<p\b[^>]*class="col-9[^"]*"[^>]*>([\s\S]*?)</p>`)
var githubLanguage = regexp.MustCompile(`(?is)<span\b[^>]*itemprop="programmingLanguage"[^>]*>([\s\S]*?)</span>`)
var githubStars = regexp.MustCompile(`(?is)href="/[^"]+/stargazers"[^>]*>[\s\S]*?([\d,]+)\s*</a>`)
var githubGain = regexp.MustCompile(`(?i)([\d,]+)\s+stars?\s+(?:today|this week|this month)`)
var trendshiftPush = regexp.MustCompile(`self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)`)
var tagsToRemove = regexp.MustCompile(`<[^>]+>`)
var repoPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)

func (service *TrendingService) Get(source, since string) (Object, error) {
	if source != "github" && source != "trendshift" {
		return nil, fmt.Errorf("source must be github or trendshift")
	}
	if since != "daily" && since != "weekly" && since != "monthly" {
		return nil, fmt.Errorf("since must be daily, weekly, or monthly")
	}
	key := source + "/" + since
	service.mu.Lock()
	lock := service.locks[key]
	if lock == nil {
		lock = &sync.Mutex{}
		service.locks[key] = lock
	}
	if cached, ok := service.cache[key]; ok && time.Since(cached.at) < service.cacheFor {
		value := cached.value
		service.mu.Unlock()
		return cloneObject(value)
	}
	service.mu.Unlock()
	lock.Lock()
	defer lock.Unlock()
	service.mu.Lock()
	if cached, ok := service.cache[key]; ok && time.Since(cached.at) < service.cacheFor {
		value := cached.value
		service.mu.Unlock()
		return cloneObject(value)
	}
	service.mu.Unlock()
	address := trendingURL(source, since)
	host := "github.com"
	label := "GitHub Trending（全部語言）"
	if source == "trendshift" {
		host = "trendshift.io"
		label = "Trendshift（熱門榜）"
	}
	document, err := service.fetch(address, host)
	if err != nil {
		return nil, err
	}
	var items []Object
	if source == "github" {
		items = parseGitHubTrending(document)
		if len(items) == 0 {
			return nil, fmt.Errorf("GitHub Trending page structure changed or returned no repositories")
		}
	} else {
		parsed, found := parseTrendshiftTrending(document)
		if !found {
			return nil, fmt.Errorf("Trendshift page structure changed: initialData was not found")
		}
		items = parsed
	}
	result := Object{"source": source, "label": label, "since": since, "sourceUrl": address, "fetchedAt": isoNow(), "items": items}
	service.mu.Lock()
	service.cache[key] = trendCacheEntry{time.Now(), result}
	service.mu.Unlock()
	return result, nil
}

func trendingURL(source, since string) string {
	if source == "github" {
		return "https://github.com/trending?since=" + url.QueryEscape(since)
	}
	if since == "daily" {
		return "https://trendshift.io/"
	}
	return "https://trendshift.io/" + since
}
func (service *TrendingService) fetch(address, host string) (string, error) {
	last := "network request failed"
	for attempt := 0; attempt < 2; attempt++ {
		request, err := http.NewRequest(http.MethodGet, address, nil)
		if err != nil {
			return "", err
		}
		request.Header.Set("User-Agent", "ade-agent-environment/0.1 (read-only GitHub Trending view)")
		request.Header.Set("Accept", "text/html,application/xhtml+xml")
		request.Header.Set("Accept-Language", "en-US,en;q=0.9")
		client := *service.client
		client.CheckRedirect = func(request *http.Request, via []*http.Request) error {
			if request.URL.Scheme != "https" || request.URL.Hostname() != host {
				return fmt.Errorf("upstream attempted to redirect outside its source host")
			}
			if len(via) >= 10 {
				return fmt.Errorf("upstream exceeded the redirect limit")
			}
			return nil
		}
		response, err := client.Do(request)
		if err == nil {
			body, readErr := io.ReadAll(io.LimitReader(response.Body, 5*1024*1024+1))
			response.Body.Close()
			if readErr != nil {
				err = readErr
			} else if len(body) > 5*1024*1024 {
				return "", fmt.Errorf("upstream response exceeded the 5 MiB limit")
			} else if response.StatusCode >= 200 && response.StatusCode < 300 {
				return string(body), nil
			} else {
				last = fmt.Sprintf("HTTP %d %s", response.StatusCode, http.StatusText(response.StatusCode))
				if response.StatusCode != 429 && response.StatusCode < 500 {
					break
				}
				err = fmt.Errorf("%s", last)
			}
		}
		if err != nil {
			last = err.Error()
		}
		if attempt == 0 {
			time.Sleep(500 * time.Millisecond)
		}
	}
	return "", fmt.Errorf("Could not fetch %s: %s", host, last)
}

func cleanHTML(value string) string {
	return strings.TrimSpace(strings.Join(strings.Fields(html.UnescapeString(tagsToRemove.ReplaceAllString(value, ""))), " "))
}
func integerValue(value any) int {
	switch number := value.(type) {
	case json.Number:
		floatValue, err := number.Float64()
		if err == nil {
			return int(floatValue)
		}
		return 0
	case int:
		return number
	case int64:
		return int(number)
	case float64:
		return int(number)
	case string:
		text := strings.ToLower(strings.ReplaceAll(strings.TrimSpace(number), ",", ""))
		multiplier := 1.0
		if strings.HasSuffix(text, "k") {
			multiplier = 1000
			text = strings.TrimSuffix(text, "k")
		} else if strings.HasSuffix(text, "m") {
			multiplier = 1_000_000
			text = strings.TrimSuffix(text, "m")
		}
		value, err := strconv.ParseFloat(text, 64)
		if err != nil {
			return 0
		}
		if multiplier > 1 {
			return int(math.RoundToEven(value * multiplier))
		}
		return int(value)
	default:
		return 0
	}
}
func trendCandidate(name string, language any, today, total int, description, category, source string) Object {
	name = strings.Trim(strings.TrimSpace(html.UnescapeString(name)), "/")
	if !repoPattern.MatchString(name) {
		return nil
	}
	parts := strings.SplitN(name, "/", 2)
	var languageValue any
	if text, ok := language.(string); ok && text != "" {
		languageValue = strings.TrimSpace(html.UnescapeString(text))
	}
	return Object{"fullName": name, "name": parts[1], "author": parts[0], "url": "https://github.com/" + name, "language": languageValue, "starsToday": today, "starsTotal": total, "description": strings.TrimSpace(html.UnescapeString(description)), "category": category, "source": source}
}
func regexCapture(pattern *regexp.Regexp, value string) string {
	match := pattern.FindStringSubmatch(value)
	if len(match) > 1 {
		return match[1]
	}
	return ""
}
func parseGitHubTrending(document string) []Object {
	items := []Object{}
	for _, article := range githubArticle.FindAllString(document, -1) {
		href := regexCapture(githubHref, article)
		if href == "" {
			continue
		}
		name, err := url.PathUnescape(strings.TrimSuffix(href, "/"))
		if err != nil {
			continue
		}
		description := cleanHTML(regexCapture(githubDescription, article))
		language := cleanHTML(regexCapture(githubLanguage, article))
		stars := integerValue(regexCapture(githubStars, article))
		gain := integerValue(regexCapture(githubGain, article))
		var languageValue any
		if language != "" {
			languageValue = language
		}
		item := trendCandidate(name, languageValue, gain, stars, description, "All Languages", "trending")
		if item != nil {
			items = append(items, item)
		}
	}
	return items
}

func parseTrendshiftTrending(document string) ([]Object, bool) {
	for _, match := range trendshiftPush.FindAllStringSubmatch(document, -1) {
		var decoded string
		if err := json.Unmarshal([]byte("\""+match[1]+"\""), &decoded); err != nil {
			continue
		}
		index := strings.Index(decoded, `"initialData":`)
		if index < 0 {
			continue
		}
		start := strings.Index(decoded[index:], "[")
		if start < 0 {
			continue
		}
		start += index
		var rows []any
		decoder := json.NewDecoder(strings.NewReader(decoded[start:]))
		decoder.UseNumber()
		if err := decoder.Decode(&rows); err != nil {
			continue
		}
		ranked := []struct {
			rank, index int
			item        Object
		}{}
		for index, raw := range rows {
			row := object(raw)
			if row == nil {
				continue
			}
			name, _ := row["full_name"].(string)
			language := row["repository_language"]
			if language == nil || language == "" {
				language = row["language"]
			}
			description, _ := row["repository_description"].(string)
			item := trendCandidate(name, language, integerValue(row["repository_stars_gained"]), integerValue(row["repository_stars"]), description, "Trendshift", "trendshift")
			if item == nil {
				continue
			}
			rank := integerValue(row["rank"])
			if rank == 0 {
				rank = index + 1
			}
			ranked = append(ranked, struct {
				rank, index int
				item        Object
			}{rank, index, item})
		}
		sort.SliceStable(ranked, func(i, j int) bool { return ranked[i].rank < ranked[j].rank })
		items := []Object{}
		for _, entry := range ranked {
			items = append(items, entry.item)
		}
		return items, true
	}
	return nil, false
}
