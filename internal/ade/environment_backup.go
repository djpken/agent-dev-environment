package ade

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/tls"
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"time"
)

const (
	environmentBackupFormat      = "ades-environment-backup"
	environmentBackupVersion     = 1
	environmentBackupMaxBytes    = 8 << 20
	environmentBackupPassMinSize = 12
	webDAVTimeout                = 30 * time.Second
)

var environmentBackupAAD = []byte("ADES-ENVIRONMENT-BACKUP-V1")

func encodeEnvironmentBackup(contents Object, passphrase string) ([]byte, error) {
	if len(passphrase) < environmentBackupPassMinSize || len(passphrase) > 1024 {
		return nil, fmt.Errorf("backup passphrase must be between 12 and 1024 bytes")
	}
	plain, err := MarshalPretty(contents)
	if err != nil {
		return nil, fmt.Errorf("environment backup could not be encoded")
	}
	if len(plain) > environmentBackupMaxBytes {
		return nil, fmt.Errorf("environment backup is too large")
	}
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return nil, fmt.Errorf("environment backup could not be encrypted")
	}
	key, err := passwordHash(passphrase, salt)
	if err != nil {
		return nil, fmt.Errorf("environment backup could not be encrypted")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("environment backup could not be encrypted")
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("environment backup could not be encrypted")
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, fmt.Errorf("environment backup could not be encrypted")
	}
	ciphertext := aead.Seal(nil, nonce, plain, environmentBackupAAD)
	envelope, err := MarshalPretty(Object{
		"format":     environmentBackupFormat,
		"version":    environmentBackupVersion,
		"kdf":        "scrypt-n32768-r8-p1",
		"salt":       base64.RawStdEncoding.EncodeToString(salt),
		"nonce":      base64.RawStdEncoding.EncodeToString(nonce),
		"ciphertext": base64.RawStdEncoding.EncodeToString(ciphertext),
	})
	if err != nil || len(envelope) > environmentBackupMaxBytes {
		return nil, fmt.Errorf("environment backup could not be encoded")
	}
	return envelope, nil
}

func decodeEnvironmentBackup(envelopeBytes []byte, passphrase string) (Object, error) {
	if len(passphrase) < environmentBackupPassMinSize || len(passphrase) > 1024 || len(envelopeBytes) == 0 || len(envelopeBytes) > environmentBackupMaxBytes {
		return nil, fmt.Errorf("backup file is invalid or its passphrase is incorrect")
	}
	envelope, err := DecodeJSON(envelopeBytes)
	if err != nil || len(envelope) != 6 || envelope["format"] != environmentBackupFormat || fmt.Sprint(envelope["version"]) != "1" || envelope["kdf"] != "scrypt-n32768-r8-p1" {
		return nil, fmt.Errorf("backup file is invalid or its passphrase is incorrect")
	}
	salt, saltErr := base64.RawStdEncoding.DecodeString(fmt.Sprint(envelope["salt"]))
	nonce, nonceErr := base64.RawStdEncoding.DecodeString(fmt.Sprint(envelope["nonce"]))
	ciphertext, ciphertextErr := base64.RawStdEncoding.DecodeString(fmt.Sprint(envelope["ciphertext"]))
	if saltErr != nil || len(salt) != 16 || nonceErr != nil || nonceErr == nil && len(nonce) != 12 || ciphertextErr != nil || len(ciphertext) < 16 {
		return nil, fmt.Errorf("backup file is invalid or its passphrase is incorrect")
	}
	key, err := passwordHash(passphrase, salt)
	if err != nil {
		return nil, fmt.Errorf("backup file is invalid or its passphrase is incorrect")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("backup file is invalid or its passphrase is incorrect")
	}
	aead, err := cipher.NewGCM(block)
	if err != nil || len(nonce) != aead.NonceSize() {
		return nil, fmt.Errorf("backup file is invalid or its passphrase is incorrect")
	}
	plain, err := aead.Open(nil, nonce, ciphertext, environmentBackupAAD)
	if err != nil {
		return nil, fmt.Errorf("backup file is invalid or its passphrase is incorrect")
	}
	contents, err := DecodeJSON(plain)
	if err != nil {
		return nil, fmt.Errorf("backup file is invalid or its passphrase is incorrect")
	}
	return contents, nil
}

func webDAVURL(value any) (*url.URL, error) {
	textValue, ok := value.(string)
	if !ok || len(textValue) == 0 || len(textValue) > 4096 {
		return nil, fmt.Errorf("WebDAV file URL is invalid")
	}
	parsed, err := url.Parse(textValue)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil || parsed.Fragment != "" || parsed.Opaque != "" {
		return nil, fmt.Errorf("WebDAV file URL must use HTTPS and must not contain credentials or a fragment")
	}
	return parsed, nil
}

func webDAVCredentials(request Object) (string, string, error) {
	username, userOK := request["webdav_username"].(string)
	password, passwordOK := request["webdav_password"].(string)
	if !userOK || !passwordOK || len(username) > 1024 || len(password) > 1024 || (username == "") != (password == "") {
		return "", "", fmt.Errorf("WebDAV username and password must both be provided or both be empty")
	}
	return username, password, nil
}

func newWebDAVClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	return &http.Client{
		Timeout:   webDAVTimeout,
		Transport: transport,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

func downloadWebDAVBackup(request Object) ([]byte, error) {
	endpoint, err := webDAVURL(request["webdav_url"])
	if err != nil {
		return nil, err
	}
	username, password, err := webDAVCredentials(request)
	if err != nil {
		return nil, err
	}
	webRequest, err := http.NewRequest(http.MethodGet, endpoint.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("WebDAV request could not be created")
	}
	webRequest.Header.Set("Accept", "application/octet-stream")
	webRequest.Header.Set("User-Agent", "ADES/1")
	if username != "" {
		webRequest.SetBasicAuth(username, password)
	}
	response, err := newWebDAVClient().Do(webRequest)
	if err != nil {
		return nil, fmt.Errorf("WebDAV download failed; check the HTTPS URL and credentials")
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("WebDAV download failed with HTTP status %d", response.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, environmentBackupMaxBytes+1))
	if err != nil || len(data) == 0 || len(data) > environmentBackupMaxBytes {
		return nil, fmt.Errorf("WebDAV backup file is empty, unreadable, or too large")
	}
	return data, nil
}

func uploadWebDAVBackup(request Object, contents []byte) error {
	endpoint, err := webDAVURL(request["webdav_url"])
	if err != nil {
		return err
	}
	username, password, err := webDAVCredentials(request)
	if err != nil {
		return err
	}
	webRequest, err := http.NewRequest(http.MethodPut, endpoint.String(), bytes.NewReader(contents))
	if err != nil {
		return fmt.Errorf("WebDAV request could not be created")
	}
	webRequest.Header.Set("Content-Type", "application/octet-stream")
	webRequest.Header.Set("User-Agent", "ADES/1")
	if username != "" {
		webRequest.SetBasicAuth(username, password)
	}
	response, err := newWebDAVClient().Do(webRequest)
	if err != nil {
		return fmt.Errorf("WebDAV upload failed; check the HTTPS URL and credentials")
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("WebDAV upload failed with HTTP status %d", response.StatusCode)
	}
	return nil
}

func (a *EnvironmentAuthority) createEnvironmentBackup(passphrase string) ([]byte, error) {
	manifest, err := ReadJSON(a.Config.Path)
	if err != nil {
		return nil, fmt.Errorf("environment manifest could not be read")
	}
	accountStore, err := ReadJSON(a.AccountsPath)
	if err != nil {
		return nil, fmt.Errorf("account storage could not be read")
	}
	if _, err := validateAccountStore(accountStore, a.Config.VMID); err != nil {
		return nil, err
	}
	policy, err := a.ReadPolicy()
	if err != nil {
		return nil, err
	}
	var sessionPolicy, legacyPolicy Object
	if policy != nil {
		if policy["authorization"] == "session" {
			sessionPolicy = policy
		} else {
			legacyPolicy = policy
		}
	}
	contents := Object{
		"version":        1,
		"created_at":     isoNow(),
		"vm_id":          a.Config.VMID,
		"manifest":       manifest,
		"accounts":       accountStore,
		"session_policy": sessionPolicy,
		"legacy_policy":  legacyPolicy,
	}
	return encodeEnvironmentBackup(contents, passphrase)
}

func (a *EnvironmentAuthority) decodeAndValidateEnvironmentBackup(envelope []byte, passphrase string) (Object, Object, Object, Object, *EnvironmentConfig, error) {
	contents, err := decodeEnvironmentBackup(envelope, passphrase)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	if len(contents) != 7 || fmt.Sprint(contents["version"]) != "1" || contents["vm_id"] != a.Config.VMID {
		return nil, nil, nil, nil, nil, fmt.Errorf("backup belongs to another VM or has an unsupported format")
	}
	manifest := object(contents["manifest"])
	accounts := object(contents["accounts"])
	sessionPolicy := object(contents["session_policy"])
	legacyPolicy := object(contents["legacy_policy"])
	if manifest == nil || accounts == nil || contents["session_policy"] != nil && sessionPolicy == nil || contents["legacy_policy"] != nil && legacyPolicy == nil {
		return nil, nil, nil, nil, nil, fmt.Errorf("backup is missing required environment settings")
	}
	config, err := a.validateBackupManifest(manifest)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	users, err := validateAccountStore(accounts, config.VMID)
	if err != nil || len(users) == 0 {
		return nil, nil, nil, nil, nil, fmt.Errorf("backup account data is invalid")
	}
	adminFound := false
	for _, raw := range users {
		if account := object(raw); account != nil && account["role"] == "admin" {
			adminFound = true
			break
		}
	}
	if !adminFound {
		return nil, nil, nil, nil, nil, fmt.Errorf("backup must include at least one admin account")
	}
	if err := validateSessionPolicySnapshot(sessionPolicy, config); err != nil {
		return nil, nil, nil, nil, nil, err
	}
	if legacyPolicy != nil && legacyPolicy["vm_id"] != nil && legacyPolicy["vm_id"] != config.VMID {
		return nil, nil, nil, nil, nil, fmt.Errorf("backup update policy belongs to another VM")
	}
	return manifest, accounts, sessionPolicy, legacyPolicy, config, nil
}

func (a *EnvironmentAuthority) validateBackupManifest(manifest Object) (*EnvironmentConfig, error) {
	if manifest["vm_id"] != a.Config.VMID {
		return nil, fmt.Errorf("backup belongs to another VM")
	}
	temporary, err := os.CreateTemp(filepath.Dir(a.Config.Path), ".ade-backup-config-")
	if err != nil {
		return nil, fmt.Errorf("backup environment settings could not be validated")
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	data, err := MarshalPretty(manifest)
	if err != nil {
		_ = temporary.Close()
		return nil, fmt.Errorf("backup environment settings could not be validated")
	}
	if _, err := temporary.Write(data); err != nil {
		_ = temporary.Close()
		return nil, fmt.Errorf("backup environment settings could not be validated")
	}
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return nil, fmt.Errorf("backup environment settings could not be validated")
	}
	if err := temporary.Close(); err != nil {
		return nil, fmt.Errorf("backup environment settings could not be validated")
	}
	config, err := LoadEnvironmentConfig(temporaryPath)
	if err != nil || config.VMID != a.Config.VMID || config.SourceRoot != a.Config.SourceRoot || config.StateRoot != a.Config.StateRoot {
		return nil, fmt.Errorf("backup environment settings do not match this VM installation")
	}
	config.Path = a.Config.Path
	return config, nil
}

func validateSessionPolicySnapshot(policy Object, config *EnvironmentConfig) error {
	if policy == nil {
		return nil
	}
	if policy["authorization"] != "session" || policy["vm_id"] != config.VMID {
		return fmt.Errorf("backup session policy is invalid")
	}
	enabled, ok := policy["enabled"].(bool)
	if !ok {
		return fmt.Errorf("backup session policy is invalid")
	}
	components := listValue(policy["components"])
	targets := object(policy["target_versions"])
	if targets == nil {
		return fmt.Errorf("backup session policy is invalid")
	}
	if !enabled {
		if len(components) != 0 || len(targets) != 0 {
			return fmt.Errorf("disabled backup session policy must not contain update targets")
		}
		return nil
	}
	ids := []string{}
	seen := map[string]bool{}
	for _, raw := range components {
		id, ok := raw.(string)
		if !ok || seen[id] {
			return fmt.Errorf("backup session policy components are invalid")
		}
		seen[id] = true
		ids = append(ids, id)
	}
	if len(ids) == 0 {
		return fmt.Errorf("enabled backup session policy has no components")
	}
	ordered, err := ComponentOrder(config, ids)
	if err != nil {
		return fmt.Errorf("backup session policy contains an unsupported component")
	}
	for _, component := range ordered {
		if !component.Enabled || component.UpdateCommand == nil {
			return fmt.Errorf("backup session policy contains an unsupported component")
		}
	}
	if _, err := ValidateTargetVersions(config, ids, targets); err != nil {
		return fmt.Errorf("backup session policy targets are invalid")
	}
	return nil
}

type restoreFile struct {
	path   string
	data   []byte
	mode   os.FileMode
	remove bool
}

type fileImage struct {
	path   string
	data   []byte
	mode   os.FileMode
	uid    int
	gid    int
	exists bool
}

func captureFile(path string) (fileImage, error) {
	image := fileImage{path: path}
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return image, nil
	}
	if err != nil || !info.Mode().IsRegular() {
		return image, fmt.Errorf("environment settings path is invalid")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return image, fmt.Errorf("environment settings ownership cannot be verified")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return image, fmt.Errorf("environment settings could not be read")
	}
	image.data = data
	image.mode = info.Mode().Perm()
	image.uid = int(stat.Uid)
	image.gid = int(stat.Gid)
	image.exists = true
	return image, nil
}

func writeAtomicBytes(path string, data []byte, mode os.FileMode, uid, gid int) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".ade-restore-")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if _, err := temporary.Write(data); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Chown(uid, gid); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Chmod(mode); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}
	directory, err := os.Open(filepath.Dir(path))
	if err == nil {
		_ = directory.Sync()
		_ = directory.Close()
	}
	return nil
}

func applyRestoreFiles(changes []restoreFile) error {
	originals := make([]fileImage, 0, len(changes))
	for _, change := range changes {
		image, err := captureFile(change.path)
		if err != nil {
			return err
		}
		originals = append(originals, image)
	}
	for index, change := range changes {
		if change.remove {
			if err := os.Remove(change.path); err != nil && !os.IsNotExist(err) {
				return rollbackRestoreFiles(originals[:index+1], err)
			}
			continue
		}
		owner, group := 0, 0
		mode := change.mode
		if original := originals[index]; original.exists {
			owner, group, mode = original.uid, original.gid, original.mode
		}
		if err := writeAtomicBytes(change.path, change.data, mode, owner, group); err != nil {
			return rollbackRestoreFiles(originals[:index+1], err)
		}
	}
	return nil
}

func rollbackRestoreFiles(originals []fileImage, cause error) error {
	var rollbackError error
	for index := len(originals) - 1; index >= 0; index-- {
		image := originals[index]
		var err error
		if image.exists {
			err = writeAtomicBytes(image.path, image.data, image.mode, image.uid, image.gid)
		} else {
			err = os.Remove(image.path)
			if os.IsNotExist(err) {
				err = nil
			}
		}
		if err != nil && rollbackError == nil {
			rollbackError = err
		}
	}
	if rollbackError != nil {
		return fmt.Errorf("environment restore failed: %v; rollback was incomplete: %v", cause, rollbackError)
	}
	return fmt.Errorf("environment restore failed: %v; previous files were restored", cause)
}

func (a *EnvironmentAuthority) restoreEnvironmentBackup(manifest, accounts, sessionPolicy, legacyPolicy Object, state Object) (Object, error) {
	store, err := NewRunStore(a.Config.StateRoot)
	if err != nil {
		return nil, fmt.Errorf("environment update state could not be read")
	}
	runs, err := store.List(100)
	if err != nil {
		return nil, fmt.Errorf("environment update state could not be read")
	}
	for _, run := range runs {
		if run["status"] == "queued" || run["status"] == "running" {
			return nil, fmt.Errorf("environment settings cannot be imported while an update run is active")
		}
	}
	currentManifest, err := ReadJSON(a.Config.Path)
	if err != nil {
		return nil, fmt.Errorf("current environment manifest could not be read")
	}
	restartRequired := !reflect.DeepEqual(currentManifest, manifest)
	state["vm_id"] = a.Config.VMID
	state["challenges"] = Object{}
	state["sessions"] = Object{}
	state["approvals"] = Object{}
	policyPath := filepath.Join(a.Config.StateRoot, "policy.json")
	changes := []restoreFile{
		{path: a.Config.Path, data: mustMarshal(manifest), mode: 0o640},
		{path: a.AccountsPath, data: mustMarshal(accounts), mode: 0o600},
		{path: a.StatePath, data: mustMarshal(state), mode: 0o600},
	}
	if sessionPolicy == nil {
		changes = append(changes, restoreFile{path: a.PolicyPath, remove: true})
	} else {
		changes = append(changes, restoreFile{path: a.PolicyPath, data: mustMarshal(sessionPolicy), mode: 0o644})
	}
	if legacyPolicy == nil {
		changes = append(changes, restoreFile{path: policyPath, remove: true})
	} else {
		changes = append(changes, restoreFile{path: policyPath, data: mustMarshal(legacyPolicy), mode: 0o640})
	}
	if err := applyRestoreFiles(changes); err != nil {
		return nil, err
	}
	result := Object{"imported": true, "restart_required": restartRequired, "restart_scheduled": false}
	if restartRequired {
		result["restart_scheduled"] = scheduleEnvironmentRestart() == nil
	}
	return result, nil
}

func mustMarshal(value any) []byte {
	data, err := MarshalPretty(value)
	if err != nil {
		return nil
	}
	return data
}

func scheduleEnvironmentRestart() error {
	if _, err := os.Stat("/run/systemd/system"); err != nil {
		return err
	}
	systemdRun := "/usr/bin/systemd-run"
	if _, err := os.Stat(systemdRun); err != nil {
		return err
	}
	unit := "ade-environment-import-restart-" + strings.ReplaceAll(randomUUID(), "-", "")
	command := exec.Command(systemdRun, "--no-block", "--unit="+unit, "--on-active=2s", "/usr/bin/systemctl", "restart", "agent-environment.service")
	if err := command.Run(); err != nil {
		return err
	}
	return nil
}

func (a *EnvironmentAuthority) importEnvironmentBackup(request Object, state Object) (Object, error) {
	users, err := a.loadAccounts()
	if err != nil {
		return nil, err
	}
	if len(users) != 0 {
		return nil, fmt.Errorf("WebDAV import is only available before the first account is created")
	}
	passphrase, ok := request["backup_passphrase"].(string)
	if !ok || len(passphrase) < environmentBackupPassMinSize || len(passphrase) > 1024 {
		return nil, fmt.Errorf("backup passphrase must be between 12 and 1024 bytes")
	}
	envelope, err := downloadWebDAVBackup(request)
	if err != nil {
		return nil, err
	}
	manifest, accounts, sessionPolicy, legacyPolicy, _, err := a.decodeAndValidateEnvironmentBackup(envelope, passphrase)
	if err != nil {
		return nil, err
	}
	return a.restoreEnvironmentBackup(manifest, accounts, sessionPolicy, legacyPolicy, state)
}
