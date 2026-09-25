package ade

import (
	"bytes"
	"encoding/xml"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
)

const defaultScheduleProvider = "plane-linear-sync"
const schedulerService = "ade-plane-linear-sync"
const schedulerLabel = "com.ade.plane-linear-sync"

var defaultSyncTimes = []string{"08:00", "12:00", "17:00"}

func ValidateScheduleTimes(times []string) ([]string, error) {
	if len(times) == 0 {
		return nil, fmt.Errorf("sync schedule must contain unique times")
	}
	seen := map[string]bool{}
	for _, value := range times {
		if !regexp.MustCompile(`^(?:[01][0-9]|2[0-3]):[0-5][0-9]$`).MatchString(value) {
			return nil, fmt.Errorf("sync schedule times must use HH:MM")
		}
		if seen[value] {
			return nil, fmt.Errorf("sync schedule must contain unique times")
		}
		seen[value] = true
	}
	return append([]string(nil), times...), nil
}

func scheduleCommand(root, provider, envFile, executable string) []string {
	if executable == "" {
		executable = os.Getenv("ADE_BIN")
	}
	if executable == "" {
		executable = "/usr/local/bin/ade"
	}
	result := []string{executable, "--root", root, "sync", "--provider", provider}
	if envFile != "" {
		result = append(result, "--env-file", envFile)
	}
	return result
}

func ShellQuote(values []string) string {
	quoted := make([]string, 0, len(values))
	for _, value := range values {
		if value != "" && regexp.MustCompile(`^[A-Za-z0-9_./:=+-]+$`).MatchString(value) {
			quoted = append(quoted, value)
		} else {
			quoted = append(quoted, "'"+strings.ReplaceAll(value, "'", "'\\''")+"'")
		}
	}
	return strings.Join(quoted, " ")
}

func RenderSystemdSchedule(root, provider string, times []string, envFile, executable string) (map[string]string, error) {
	checked, err := ValidateScheduleTimes(times)
	if err != nil {
		return nil, err
	}
	root, err = filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if envFile != "" {
		envFile, err = filepath.Abs(envFile)
		if err != nil {
			return nil, err
		}
	}
	command := ShellQuote(scheduleCommand(root, provider, envFile, executable))
	service := strings.Join([]string{"[Unit]", "Description=ADE Plane to Linear issue synchronization", "", "[Service]", "Type=oneshot", "ExecStart=" + command, ""}, "\n")
	lines := []string{"[Unit]", "Description=Run ADE Plane to Linear synchronization on schedule", "", "[Timer]"}
	for _, time := range checked {
		lines = append(lines, "OnCalendar=*-*-* "+time+":00")
	}
	lines = append(lines, "Persistent=true", "Unit="+schedulerService+".service", "", "[Install]", "WantedBy=timers.target", "")
	return map[string]string{schedulerService + ".service": service, schedulerService + ".timer": strings.Join(lines, "\n")}, nil
}

func RenderLaunchdSchedule(root, provider string, times []string, envFile, executable string) ([]byte, error) {
	checked, err := ValidateScheduleTimes(times)
	if err != nil {
		return nil, err
	}
	root, err = filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if envFile != "" {
		envFile, err = filepath.Abs(envFile)
		if err != nil {
			return nil, err
		}
	}
	args := scheduleCommand(root, provider, envFile, executable)
	var b bytes.Buffer
	b.WriteString("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\">\n<dict>\n")
	writeKeyString(&b, "Label", schedulerLabel)
	b.WriteString("<key>ProgramArguments</key><array>\n")
	for _, arg := range args {
		writeString(&b, arg)
	}
	b.WriteString("</array>\n<key>StartCalendarInterval</key><array>\n")
	for _, value := range checked {
		parts := strings.Split(value, ":")
		hour, _ := strconv.Atoi(parts[0])
		minute, _ := strconv.Atoi(parts[1])
		b.WriteString("<dict>\n")
		writeKeyInt(&b, "Hour", hour)
		writeKeyInt(&b, "Minute", minute)
		b.WriteString("</dict>\n")
	}
	b.WriteString("</array>\n")
	b.WriteString("<key>RunAtLoad</key><false/>\n<key>ProcessType</key><string>Background</string>\n")
	writeKeyString(&b, "StandardOutPath", filepath.Join(root, "state", provider, "scheduler.stdout.log"))
	writeKeyString(&b, "StandardErrorPath", filepath.Join(root, "state", provider, "scheduler.stderr.log"))
	b.WriteString("</dict>\n</plist>\n")
	return b.Bytes(), nil
}

func writeString(out *bytes.Buffer, value string) {
	out.WriteString("<string>")
	_ = xml.EscapeText(out, []byte(value))
	out.WriteString("</string>\n")
}
func writeKeyString(out *bytes.Buffer, key, value string) {
	out.WriteString("<key>")
	_ = xml.EscapeText(out, []byte(key))
	out.WriteString("</key>")
	writeString(out, value)
}
func writeKeyInt(out *bytes.Buffer, key string, value int) {
	fmt.Fprintf(out, "<key>%s</key><integer>%d</integer>\n", key, value)
}

func RenderSchedule(platform, root, provider string, times []string, envFile, executable string) (map[string][]byte, error) {
	if platform == "linux" {
		values, err := RenderSystemdSchedule(root, provider, times, envFile, executable)
		if err != nil {
			return nil, err
		}
		result := map[string][]byte{}
		for name, value := range values {
			result[name] = []byte(value)
		}
		return result, nil
	}
	if platform == "darwin" {
		value, err := RenderLaunchdSchedule(root, provider, times, envFile, executable)
		if err != nil {
			return nil, err
		}
		return map[string][]byte{schedulerLabel + ".plist": value}, nil
	}
	return nil, fmt.Errorf("ADE scheduling supports Linux systemd and macOS launchd")
}

func ManifestSchedule(manifest Object) ([]string, error) {
	rawSchedule, exists := manifest["schedule"]
	if !exists {
		return append([]string(nil), defaultSyncTimes...), nil
	}
	schedule, ok := rawSchedule.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("provider schedule must be an object")
	}
	raw, exists := schedule["times"]
	if !exists {
		return append([]string(nil), defaultSyncTimes...), nil
	}
	times, err := validateStringArray(raw, "schedule.times", 1)
	if err != nil {
		return nil, err
	}
	return ValidateScheduleTimes(times)
}

func InstallSchedule(root, platform, provider string, times []string, envFile, executable, destination string, enable bool) (Object, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	rendered, err := RenderSchedule(platform, root, provider, times, envFile, executable)
	if err != nil {
		return nil, err
	}
	state := filepath.Join(root, "state", provider)
	if err := os.MkdirAll(state, 0o700); err != nil {
		return nil, err
	}
	_ = os.Chmod(state, 0o700)
	var directory string
	if destination != "" {
		directory, err = filepath.Abs(destination)
		if err != nil {
			return nil, err
		}
	} else if platform == "linux" {
		home, err := os.UserHomeDir()
		if err != nil {
			return nil, err
		}
		directory = filepath.Join(home, ".config/systemd/user")
	} else {
		home, err := os.UserHomeDir()
		if err != nil {
			return nil, err
		}
		directory = filepath.Join(home, "Library/LaunchAgents")
	}
	paths := Object{}
	for name, contents := range rendered {
		path := filepath.Join(directory, name)
		if err := atomicFile(path, contents, 0o600); err != nil {
			return nil, err
		}
		paths[name] = path
	}
	if enable && destination == "" && platform == "linux" {
		if err := exec.Command("systemctl", "--user", "daemon-reload").Run(); err != nil {
			return nil, err
		}
		if err := exec.Command("systemctl", "--user", "enable", "--now", schedulerService+".timer").Run(); err != nil {
			return nil, err
		}
	}
	if enable && destination == "" && platform == "darwin" {
		uid := os.Getuid()
		path, _ := paths[schedulerLabel+".plist"].(string)
		if err := exec.Command("launchctl", "bootstrap", fmt.Sprintf("gui/%d", uid), path).Run(); err != nil {
			return nil, err
		}
	}
	return Object{"platform": platform, "provider": provider, "times": times, "files": paths, "enabled": enable}, nil
}

func CurrentPlatform() string { return runtime.GOOS }

func atomicFile(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".ade-write-")
	if err != nil {
		return err
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err := file.Chmod(mode); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}
