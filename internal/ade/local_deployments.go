package ade

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

var localDeploymentSHAPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)

type LocalDeploymentRecord struct {
	SHA        string `json:"sha"`
	Subject    string `json:"subject,omitempty"`
	Status     string `json:"status"`
	Stage      string `json:"stage,omitempty"`
	QueuedAt   string `json:"queued_at"`
	StartedAt  string `json:"started_at,omitempty"`
	FinishedAt string `json:"finished_at,omitempty"`
	Error      string `json:"error,omitempty"`
}

func ListLocalDeployments(stateRoot string) ([]LocalDeploymentRecord, error) {
	directory := filepath.Join(stateRoot, "local-deployments")
	entries, err := os.ReadDir(directory)
	if errors.Is(err, os.ErrNotExist) {
		return []LocalDeploymentRecord{}, nil
	}
	if err != nil {
		return nil, err
	}

	records := make([]LocalDeploymentRecord, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		sha := strings.TrimSuffix(entry.Name(), ".json")
		if !localDeploymentSHAPattern.MatchString(sha) {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			return nil, err
		}
		if !info.Mode().IsRegular() {
			continue
		}
		if info.Size() > 16*1024 {
			return nil, errors.New("local deployment record is too large")
		}
		file, err := os.Open(filepath.Join(directory, entry.Name()))
		if err != nil {
			return nil, err
		}
		var record LocalDeploymentRecord
		decodeErr := json.NewDecoder(io.LimitReader(file, 16*1024)).Decode(&record)
		closeErr := file.Close()
		if decodeErr != nil {
			return nil, decodeErr
		}
		if closeErr != nil {
			return nil, closeErr
		}
		if record.SHA != sha {
			return nil, errors.New("local deployment record SHA does not match its filename")
		}
		records = append(records, record)
	}

	sort.Slice(records, func(i, j int) bool {
		if records[i].QueuedAt == records[j].QueuedAt {
			return records[i].SHA > records[j].SHA
		}
		return records[i].QueuedAt > records[j].QueuedAt
	})
	if len(records) > 50 {
		records = records[:50]
	}
	return records, nil
}
