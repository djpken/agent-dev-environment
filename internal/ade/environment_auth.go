package ade

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"

	"golang.org/x/crypto/scrypt"
)

const sessionHours = 12
const passwordScryptN = 1 << 15
const passwordScryptR = 8
const passwordScryptP = 1
const passwordHashBytes = 32

var usernamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._@+-]{0,127}$`)

type EnvironmentAuthority struct {
	Config                                              *EnvironmentConfig
	Root, StatePath, LockPath, PolicyPath, AccountsPath string
	Owner                                               uint32
}

func NewEnvironmentAuthority(config *EnvironmentConfig) (*EnvironmentAuthority, error) {
	info, err := os.Stat(config.Path)
	if err != nil {
		return nil, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return nil, fmt.Errorf("authorization storage ownership cannot be verified")
	}
	owner := stat.Uid
	parentInfo, err := os.Stat(filepath.Dir(config.Path))
	if err != nil {
		return nil, err
	}
	parentStat, ok := parentInfo.Sys().(*syscall.Stat_t)
	if !ok || parentStat.Uid != owner || parentInfo.Mode().Perm()&0o022 != 0 {
		return nil, fmt.Errorf("authorization parent must be owned by the config owner and not writable by others")
	}
	root := filepath.Join(filepath.Dir(config.Path), strings.TrimSuffix(filepath.Base(config.Path), filepath.Ext(config.Path))+".auth")
	if err := os.MkdirAll(root, 0o755); err != nil {
		return nil, err
	}
	if err := os.Chmod(root, 0o755); err != nil {
		return nil, err
	}
	authority := &EnvironmentAuthority{Config: config, Root: root, StatePath: filepath.Join(root, "sessions.json"), LockPath: filepath.Join(root, ".lock"), PolicyPath: filepath.Join(root, "policy.json"), AccountsPath: filepath.Join(root, "accounts.json"), Owner: owner}
	if err := authority.checkPath(root, true); err != nil {
		return nil, err
	}
	return authority, nil
}

func (a *EnvironmentAuthority) checkPath(path string, directory bool) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("authorization storage ownership or permissions are invalid")
	}
	if directory && !info.IsDir() || !directory && !info.Mode().IsRegular() || stat.Uid != a.Owner || info.Mode().Perm()&0o022 != 0 {
		return fmt.Errorf("authorization storage ownership or permissions are invalid")
	}
	return nil
}

func (a *EnvironmentAuthority) load() (Object, error) {
	if _, err := os.Lstat(a.StatePath); err == nil {
		if err := a.checkPath(a.StatePath, false); err != nil {
			return nil, err
		}
		info, _ := os.Stat(a.StatePath)
		if info.Mode().Perm()&0o077 != 0 {
			return nil, fmt.Errorf("session storage must be private to its owner")
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	state, err := readJSONOr(a.StatePath, Object{"vm_id": a.Config.VMID, "challenges": Object{}, "sessions": Object{}, "approvals": Object{}})
	if err != nil {
		return nil, err
	}
	if state["vm_id"] != a.Config.VMID {
		return nil, fmt.Errorf("session storage VM identity does not match")
	}
	now := time.Now().UTC()
	for _, group := range []string{"challenges", "sessions", "approvals"} {
		values := object(state[group])
		if values == nil {
			return nil, fmt.Errorf("invalid authorization state")
		}
		for key, raw := range values {
			item := object(raw)
			expiry, _ := item["expires_at"].(string)
			timeValue, err := parseTimestamp(expiry)
			if err != nil || !timeValue.After(now) {
				delete(values, key)
			}
		}
	}
	return state, nil
}

func authMap(state Object, key string) Object {
	value := object(state[key])
	if value == nil {
		value = Object{}
		state[key] = value
	}
	return value
}
func (a *EnvironmentAuthority) write(state Object) error { return WriteJSON(a.StatePath, state, 0o600) }
func tokenID(token any) (string, error) {
	value, ok := token.(string)
	if !ok || len(value) < 32 || len(value) > 128 {
		return "", fmt.Errorf("session expired")
	}
	hash := sha256.Sum256([]byte(value))
	return hex.EncodeToString(hash[:]), nil
}

func normalizeUsername(value any) (string, error) {
	username, ok := value.(string)
	if !ok {
		return "", fmt.Errorf("invalid credentials")
	}
	username = strings.ToLower(strings.TrimSpace(username))
	if !usernamePattern.MatchString(username) {
		return "", fmt.Errorf("invalid credentials")
	}
	return username, nil
}

func passwordHash(password string, salt []byte) ([]byte, error) {
	return scrypt.Key([]byte(password), salt, passwordScryptN, passwordScryptR, passwordScryptP, passwordHashBytes)
}

func (a *EnvironmentAuthority) loadAccounts() (Object, error) {
	if _, err := os.Lstat(a.AccountsPath); os.IsNotExist(err) {
		return Object{}, nil
	} else if err != nil {
		return nil, err
	}
	if err := a.checkPath(a.AccountsPath, false); err != nil {
		return nil, err
	}
	info, err := os.Stat(a.AccountsPath)
	if err != nil {
		return nil, err
	}
	if info.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("account storage must be private to its owner")
	}
	stored, err := ReadJSON(a.AccountsPath)
	if err != nil {
		return nil, err
	}
	return validateAccountStore(stored, a.Config.VMID)
}

func validateAccountStore(stored Object, vmID string) (Object, error) {
	if stored["vm_id"] != vmID {
		return nil, fmt.Errorf("account storage VM identity does not match")
	}
	users := object(stored["users"])
	if users == nil {
		return nil, fmt.Errorf("account storage is invalid")
	}
	for username, raw := range users {
		account := object(raw)
		if account == nil || account["username"] != username || !usernamePattern.MatchString(username) || roleRank(fmt.Sprint(account["role"])) < 0 {
			return nil, fmt.Errorf("account storage is invalid")
		}
		if _, ok := account["credential_id"].(string); !ok {
			return nil, fmt.Errorf("account storage is invalid")
		}
		salt, saltOK := account["salt"].(string)
		hash, hashOK := account["password_hash"].(string)
		decodedSalt, saltErr := base64.RawStdEncoding.DecodeString(salt)
		decodedHash, hashErr := base64.RawStdEncoding.DecodeString(hash)
		if !saltOK || !hashOK || saltErr != nil || hashErr != nil || len(decodedSalt) != 16 || len(decodedHash) != passwordHashBytes || fmt.Sprint(account["hash_version"]) != "1" {
			return nil, fmt.Errorf("account storage is invalid")
		}
	}
	return users, nil
}

func newPasswordAccount(username, password, role string) (Object, error) {
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return nil, err
	}
	hash, err := passwordHash(password, salt)
	if err != nil {
		return nil, err
	}
	return Object{
		"username": username, "role": role,
		"credential_id": randomUUID(), "hash_version": 1,
		"salt":          base64.RawStdEncoding.EncodeToString(salt),
		"password_hash": base64.RawStdEncoding.EncodeToString(hash),
	}, nil
}

// SetAccount is a local, root-only bootstrap and password rotation operation.
// It is intentionally absent from Dispatch so the HTTP helper cannot provision accounts.
func (a *EnvironmentAuthority) SetAccount(username, password, role string) error {
	if os.Geteuid() != 0 || a.Owner != 0 {
		return fmt.Errorf("account setup requires root and a root-owned config")
	}
	normalized, err := normalizeUsername(username)
	if err != nil {
		return fmt.Errorf("username must be 1-128 characters using letters, numbers, dot, underscore, at, plus, or hyphen")
	}
	if roleRank(role) < 0 {
		return fmt.Errorf("role must be viewer, operator, or admin")
	}
	if len(password) < 12 || len(password) > 1024 {
		return fmt.Errorf("password must be between 12 and 1024 bytes")
	}
	return Lock(a.LockPath, false, func() error {
		state, err := a.load()
		if err != nil {
			return err
		}
		users, err := a.loadAccounts()
		if err != nil {
			return err
		}
		account, err := newPasswordAccount(normalized, password, role)
		if err != nil {
			return err
		}
		users[normalized] = account
		if err := WriteJSON(a.AccountsPath, Object{"vm_id": a.Config.VMID, "users": users}, 0o600); err != nil {
			return err
		}
		removedSessions := map[string]bool{}
		for id, raw := range authMap(state, "sessions") {
			if account := object(raw); account != nil && account["username"] == normalized {
				delete(authMap(state, "sessions"), id)
				removedSessions[id] = true
			}
		}
		for id, raw := range authMap(state, "approvals") {
			if approval := object(raw); approval != nil && removedSessions[fmt.Sprint(approval["session_id"])] {
				delete(authMap(state, "approvals"), id)
			}
		}
		return a.write(state)
	})
}

func (a *EnvironmentAuthority) register(state, request Object) (Object, error) {
	username, err := normalizeUsername(request["username"])
	password, passwordOK := request["password"].(string)
	if err != nil || !passwordOK || len(password) < 12 || len(password) > 1024 {
		return nil, fmt.Errorf("username or password is invalid")
	}
	users, err := a.loadAccounts()
	if err != nil {
		return nil, err
	}
	if len(users) != 0 {
		return nil, fmt.Errorf("registration is already complete")
	}
	account, err := newPasswordAccount(username, password, "admin")
	if err != nil {
		return nil, err
	}
	users[username] = account
	if err := WriteJSON(a.AccountsPath, Object{"vm_id": a.Config.VMID, "users": users}, 0o600); err != nil {
		return nil, err
	}
	tokenBytes := make([]byte, 32)
	if _, err := rand.Read(tokenBytes); err != nil {
		return nil, err
	}
	token := base64.RawURLEncoding.EncodeToString(tokenBytes)
	issued := Object{
		"username": username, "role": "admin", "account_version": account["credential_id"],
		"expires_at": time.Now().UTC().Add(sessionHours * time.Hour).Format(time.RFC3339),
	}
	authMap(state, "sessions")[sha256Hex(token)] = issued
	return Object{
		"session": token, "username": username, "role": "admin", "expires_at": issued["expires_at"],
	}, nil
}

func walletRole(config *EnvironmentConfig, address string) string {
	address = strings.ToLower(address)
	for _, wallet := range config.Wallets {
		if wallet["address"] == address {
			role, _ := wallet["role"].(string)
			return role
		}
	}
	return ""
}
func roleRank(role string) int {
	switch role {
	case "viewer":
		return 0
	case "operator":
		return 1
	case "admin":
		return 2
	}
	return -1
}
func (a *EnvironmentAuthority) session(state Object, id, minimum string) (Object, error) {
	stored := object(authMap(state, "sessions")[id])
	if stored == nil {
		return nil, fmt.Errorf("session expired")
	}
	expiry, _ := stored["expires_at"].(string)
	expires, err := parseTimestamp(expiry)
	if err != nil || !expires.After(time.Now().UTC()) {
		return nil, fmt.Errorf("session expired")
	}
	username, _ := stored["username"].(string)
	users, err := a.loadAccounts()
	if err != nil {
		return nil, err
	}
	current := object(users[username])
	if current == nil || stored["account_version"] != current["credential_id"] {
		return nil, fmt.Errorf("session is no longer valid")
	}
	role, _ := current["role"].(string)
	if roleRank(role) < roleRank(minimum) {
		return nil, fmt.Errorf("account role %s required", minimum)
	}
	return Object{"username": username, "role": role, "expires_at": expiry}, nil
}

func (a *EnvironmentAuthority) login(state, request Object) (Object, error) {
	username, err := normalizeUsername(request["username"])
	password, passwordOK := request["password"].(string)
	if err != nil || !passwordOK || len(password) < 1 || len(password) > 1024 {
		return nil, fmt.Errorf("invalid credentials")
	}
	users, err := a.loadAccounts()
	if err != nil {
		return nil, err
	}
	account := object(users[username])
	var salt []byte
	if account != nil {
		encodedSalt, _ := account["salt"].(string)
		salt, err = base64.RawStdEncoding.DecodeString(encodedSalt)
		if err != nil {
			return nil, fmt.Errorf("account storage is invalid")
		}
	} else {
		salt = []byte("ade-unknown-account-v1")
	}
	actual, err := passwordHash(password, salt)
	if err != nil {
		return nil, err
	}
	if account == nil {
		return nil, fmt.Errorf("invalid credentials")
	}
	encodedHash, _ := account["password_hash"].(string)
	expected, err := base64.RawStdEncoding.DecodeString(encodedHash)
	if err != nil || subtle.ConstantTimeCompare(actual, expected) != 1 {
		return nil, fmt.Errorf("invalid credentials")
	}
	role, _ := account["role"].(string)
	tokenBytes := make([]byte, 32)
	if _, err := rand.Read(tokenBytes); err != nil {
		return nil, err
	}
	token := base64.RawURLEncoding.EncodeToString(tokenBytes)
	session := Object{"username": username, "role": role, "account_version": account["credential_id"], "expires_at": time.Now().UTC().Add(sessionHours * time.Hour).Format(time.RFC3339)}
	authMap(state, "sessions")[sha256Hex(token)] = session
	result := Object{"session": token}
	result["username"] = username
	result["role"] = role
	result["expires_at"] = session["expires_at"]
	return result, nil
}

func (a *EnvironmentAuthority) selection(request Object, policy bool) (string, []string, Object, error) {
	action := "update"
	if !policy {
		if value, ok := request["action"].(string); ok && value != "" {
			action = value
		}
	}
	if action != "update" && action != "restart" {
		return "", nil, nil, fmt.Errorf("unsupported control action")
	}
	field := "component_ids"
	if policy {
		field = "components"
	}
	ids, err := validateStringArray(request[field], field, 1)
	if err != nil {
		return "", nil, nil, fmt.Errorf("components must be a non-empty list of unique identifiers")
	}
	seen := map[string]bool{}
	for _, id := range ids {
		if seen[id] {
			return "", nil, nil, fmt.Errorf("components must be a non-empty list of unique identifiers")
		}
		seen[id] = true
	}
	ordered, err := ComponentOrder(a.Config, ids)
	if err != nil {
		return "", nil, nil, err
	}
	for _, component := range ordered {
		if !component.Enabled || (action == "update" && component.UpdateCommand == nil) || (action == "restart" && component.RestartCommand == nil) {
			return "", nil, nil, fmt.Errorf("component does not support %s: %s", action, component.ID)
		}
	}
	targets, err := ValidateTargetVersions(a.Config, ids, request["target_versions"])
	if err != nil {
		return "", nil, nil, err
	}
	if action == "restart" && len(targets) > 0 {
		return "", nil, nil, fmt.Errorf("restart does not accept target versions")
	}
	return action, ids, targets, nil
}

func runBinding(run Object) Object {
	result := Object{}
	for _, key := range []string{"id", "trigger", "action", "component_ids", "target_versions", "requested_by"} {
		result[key] = run[key]
	}
	return result
}

func (a *EnvironmentAuthority) Dispatch(request Object) (Object, error) {
	if request == nil {
		return nil, fmt.Errorf("authorization request must be an object")
	}
	var result Object
	err := Lock(a.LockPath, false, func() error {
		state, err := a.load()
		if err != nil {
			return err
		}
		operation, ok := request["operation"].(string)
		if !ok {
			return fmt.Errorf("authorization operation must be text")
		}
		if operation == "setup_status" {
			users, err := a.loadAccounts()
			if err != nil {
				return err
			}
			result = Object{"registered": len(users) != 0}
			return nil
		}
		if operation == "register" {
			result, err = a.register(state, request)
			if err != nil {
				return err
			}
		} else if operation == "webdav_import" {
			result, err = a.importEnvironmentBackup(request, state)
			if err != nil {
				return err
			}
		} else if operation == "login" {
			result, err = a.login(state, request)
			if err != nil {
				return err
			}
		} else {
			sessionID, err := tokenID(request["token"])
			if err != nil {
				return err
			}
			if operation == "logout" {
				delete(authMap(state, "sessions"), sessionID)
				result = Object{"signed_out": true}
			} else {
				minimum := "viewer"
				if operation == "authorize_run" {
					minimum = "operator"
				}
				if operation == "save_policy" || operation == "webdav_backup" {
					minimum = "admin"
				}
				session, err := a.session(state, sessionID, minimum)
				if err != nil {
					return err
				}
				switch operation {
				case "session":
					result = session
				case "policy_status":
					policy, err := a.ReadPolicy()
					if err != nil {
						return err
					}
					result = PolicyPublic(policy, a.Config)
				case "authorize_run":
					action, ids, targets, err := a.selection(request, false)
					if err != nil {
						return err
					}
					store, err := NewRunStore(a.Config.StateRoot)
					if err != nil {
						return err
					}
					run, err := store.Create("manual", ids, fmt.Sprint(session["username"]), action, targets, Object{"kind": "session"})
					if err != nil {
						return err
					}
					expiry, _ := session["expires_at"].(string)
					authMap(state, "approvals")[fmt.Sprint(run["id"])] = Object{"binding": runBinding(run), "session_id": sessionID, "expires_at": expiry}
					result = run
				case "save_policy":
					enabled := true
					if raw, ok := request["enabled"]; ok {
						var isBool bool
						enabled, isBool = raw.(bool)
						if !isBool {
							return fmt.Errorf("policy enabled must be a boolean")
						}
					}
					if request["expires_at"] != nil {
						return fmt.Errorf("scheduled policies do not have an expiry")
					}
					policy := Object{"components": []string{}, "target_versions": Object{}, "release_channel": "stable", "allow_restart": true}
					if enabled {
						_, ids, targets, err := a.selection(request, true)
						if err != nil {
							return err
						}
						if request["release_channel"] != nil && request["release_channel"] != "stable" {
							return fmt.Errorf("only stable release channel is allowed")
						}
						if request["allow_restart"] != nil && request["allow_restart"] != true {
							return fmt.Errorf("component updates require restart permission")
						}
						policy["components"] = ids
						policy["target_versions"] = targets
					}
					policy["policy_id"] = randomUUID()
					policy["vm_id"] = a.Config.VMID
					policy["enabled"] = enabled
					policy["signer"] = session["username"]
					policy["authorization"] = "session"
					policy["updated_at"] = isoNow()
					if err := WriteJSON(a.PolicyPath, policy, 0o644); err != nil {
						return err
					}
					result = PolicyPublic(policy, a.Config)
				case "webdav_backup":
					passphrase, ok := request["backup_passphrase"].(string)
					if !ok || len(passphrase) < environmentBackupPassMinSize || len(passphrase) > 1024 {
						return fmt.Errorf("backup passphrase must be between 12 and 1024 bytes")
					}
					backup, err := a.createEnvironmentBackup(passphrase)
					if err != nil {
						return err
					}
					if err := uploadWebDAVBackup(request, backup); err != nil {
						return err
					}
					result = Object{"backed_up": true, "size_bytes": len(backup)}
				default:
					return fmt.Errorf("unsupported authorization operation")
				}
			}
		}
		return a.write(state)
	})
	return result, err
}

func (a *EnvironmentAuthority) ReadPolicy() (Object, error) {
	if _, err := os.Lstat(a.PolicyPath); err == nil {
		if err := a.checkPath(a.PolicyPath, false); err != nil {
			return nil, err
		}
		return ReadJSON(a.PolicyPath)
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	store, err := NewRunStore(a.Config.StateRoot)
	if err != nil {
		return nil, err
	}
	policy, err := store.ReadPolicy()
	if err != nil {
		return nil, err
	}
	if policy != nil && policy["authorization"] != "session" {
		return policy, nil
	}
	return nil, nil
}

func (a *EnvironmentAuthority) ConsumeRun(run Object) (bool, string) {
	approved := false
	reason := "manual run approval is missing, expired, or already used"
	err := Lock(a.LockPath, false, func() error {
		state, err := a.load()
		if err != nil {
			return err
		}
		id, _ := run["id"].(string)
		approvals := authMap(state, "approvals")
		approval := object(approvals[id])
		if approval == nil {
			return nil
		}
		if !reflect.DeepEqual(approval["binding"], runBinding(run)) || run["status"] != "queued" {
			reason = "manual run does not match its session approval"
			return nil
		}
		sessionID, _ := approval["session_id"].(string)
		if _, err := a.session(state, sessionID, "operator"); err != nil {
			reason = err.Error()
			return nil
		}
		if _, _, _, err := a.selection(run, false); err != nil {
			reason = err.Error()
			return nil
		}
		delete(approvals, id)
		if err := a.write(state); err != nil {
			return err
		}
		approved = true
		reason = "ok"
		return nil
	})
	if err != nil {
		return false, err.Error()
	}
	return approved, reason
}

func RegistrationMessage(config *EnvironmentConfig, address, nonce, issued, expires string) string {
	payload := Object{"action": "register", "address": address, "expires_at": expires, "issued_at": issued, "nonce": nonce, "role": "admin", "vm_binding": sha256Hex(config.VMID)}
	data, _ := CanonicalJSON(payload, false, false)
	return "ADE-ENVIRONMENT-REGISTRATION-V1\n" + string(data)
}
func VerifyRegistrationRequest(config *EnvironmentConfig, address, message, signature string) (bool, string) {
	if len(config.Wallets) > 0 {
		return false, "registration is already complete"
	}
	normalized, err := NormalizeWalletAddress(address)
	if err != nil {
		return false, err.Error()
	}
	prefix := "ADE-ENVIRONMENT-REGISTRATION-V1\n"
	if !strings.HasPrefix(message, prefix) {
		return false, "registration message has an invalid prefix"
	}
	payload, err := DecodeJSON([]byte(strings.TrimPrefix(message, prefix)))
	if err != nil {
		return false, "registration message is not valid JSON"
	}
	if len(payload) != 7 || payload["action"] != "register" || payload["role"] != "admin" {
		return false, "registration role or action is invalid"
	}
	if payload["address"] != normalized || payload["vm_binding"] != sha256Hex(config.VMID) {
		return false, "registration identity does not match this VM"
	}
	nonce, _ := payload["nonce"].(string)
	if !registrationNonce.MatchString(nonce) {
		return false, "registration nonce is invalid"
	}
	issued, err := parseTimestamp(fmt.Sprint(payload["issued_at"]))
	if err != nil {
		return false, err.Error()
	}
	expires, err := parseTimestamp(fmt.Sprint(payload["expires_at"]))
	if err != nil {
		return false, err.Error()
	}
	now := time.Now().UTC()
	if issued.After(now.Add(time.Minute)) {
		return false, "registration message is issued in the future"
	}
	if !expires.After(now) || !expires.After(issued) {
		return false, "registration message has expired"
	}
	if expires.Sub(issued) > 15*time.Minute {
		return false, "registration message lifetime is too long"
	}
	if !VerifyWalletSignature(normalized, []byte(message), signature) {
		return false, "registration signature is invalid"
	}
	return true, "ok"
}

func EnrollSignedWallet(config *EnvironmentConfig, address, message, signature string) (Object, error) {
	if os.Geteuid() != 0 {
		return nil, fmt.Errorf("signed wallet enrollment must run as root")
	}
	address, err := NormalizeWalletAddress(address)
	if err != nil {
		return nil, err
	}
	var result Object
	lock := filepath.Join(config.StateRoot, ".registration.lock")
	err = Lock(lock, false, func() error {
		current, err := LoadEnvironmentConfig(config.Path)
		if err != nil {
			return err
		}
		valid, reason := VerifyRegistrationRequest(current, address, message, signature)
		if !valid {
			return fmt.Errorf("%s", reason)
		}
		raw, err := ReadJSON(current.Path)
		if err != nil {
			return err
		}
		walletConfig := object(raw["wallets"])
		if walletConfig == nil {
			walletConfig = Object{}
			raw["wallets"] = walletConfig
		}
		wallets := listValue(walletConfig["authorized"])
		if walletConfig["authorized"] != nil && wallets == nil {
			return fmt.Errorf("wallets.authorized must be a list")
		}
		if len(wallets) > 0 {
			return fmt.Errorf("registration is already complete")
		}
		info, err := os.Stat(current.Path)
		if err != nil {
			return err
		}
		walletConfig["authorized"] = []any{Object{"address": address, "role": "admin"}}
		mode := info.Mode().Perm()
		if mode == 0 {
			mode = 0o640
		}
		return WriteJSON(current.Path, raw, mode)
	})
	if err != nil {
		return nil, err
	}
	result = Object{"registered": true, "address": address, "role": "admin", "config": config.Path}
	return result, nil
}

func PolicyMessage(policy Object) string {
	components := []string{}
	for _, value := range listValue(policy["components"]) {
		if text, ok := value.(string); ok {
			components = append(components, text)
		}
	}
	sort.Strings(components)
	targets := object(policy["target_versions"])
	if targets == nil {
		targets = Object{}
	}
	payload := Object{"allow_restart": true, "components": components, "expires_at": policy["expires_at"], "policy_id": policy["policy_id"], "release_channel": "stable", "schedule": "daily@04:00+08", "target_versions": targets, "vm_id": policy["vm_id"]}
	if raw, ok := policy["allow_restart"].(bool); ok {
		payload["allow_restart"] = raw
	}
	if raw, ok := policy["release_channel"].(string); ok {
		payload["release_channel"] = raw
	}
	encoded, _ := CanonicalJSON(payload, false, false)
	return "ADE-ENVIRONMENT-POLICY-V1\n" + string(encoded)
}

func VerifyPolicy(config *EnvironmentConfig, policy Object) (bool, string) {
	if policy == nil {
		return false, "daily updates are not enabled"
	}
	if policy["authorization"] == "session" {
		authority, err := NewEnvironmentAuthority(config)
		if err != nil {
			return false, err.Error()
		}
		canonical, err := authority.ReadPolicy()
		if err != nil {
			return false, err.Error()
		}
		if !reflect.DeepEqual(policy, canonical) {
			return false, "policy does not match the approved schedule"
		}
		if !boolValue(policy["enabled"]) {
			return false, "daily updates are disabled"
		}
	}
	if policy["vm_id"] != config.VMID {
		return false, "policy VM identity does not match"
	}
	channel := "stable"
	if raw, ok := policy["release_channel"].(string); ok {
		channel = raw
	}
	if channel != "stable" {
		return false, "only stable release channel is allowed"
	}
	ids, ok := policy["components"].([]any)
	if !ok {
		return false, "policy components are invalid"
	}
	selected := []string{}
	for _, raw := range ids {
		text, ok := raw.(string)
		if !ok {
			return false, "policy components are invalid"
		}
		selected = append(selected, text)
	}
	known := map[string]bool{}
	for _, component := range config.Components {
		if component.Enabled && component.UpdateCommand != nil {
			known[component.ID] = true
		}
	}
	if len(selected) == 0 {
		return false, "policy components are outside the VM allowlist"
	}
	for _, id := range selected {
		if !known[id] {
			return false, "policy components are outside the VM allowlist"
		}
	}
	target, err := ValidateTargetVersions(config, selected, policy["target_versions"])
	if err != nil {
		return false, err.Error()
	}
	if !reflect.DeepEqual(target, object(policy["target_versions"])) {
		return false, "policy target versions are invalid"
	}
	if policy["authorization"] == "session" {
		return true, "daily updates enabled until disabled"
	}
	signer, _ := policy["signer"].(string)
	if role := walletRole(config, signer); role != "operator" && role != "admin" {
		return false, "policy signer is not authorized"
	}
	signature, _ := policy["signature"].(string)
	if !VerifyWalletSignature(signer, []byte(PolicyMessage(policy)), signature) {
		return false, "policy signature is invalid"
	}
	expires, err := parseTimestamp(fmt.Sprint(policy["expires_at"]))
	if err != nil || !expires.After(time.Now().UTC()) {
		return false, "policy expiry is invalid"
	}
	return true, "ok"
}

func PolicyPublic(policy Object, config *EnvironmentConfig) Object {
	if policy == nil {
		return Object{"enrolled": false, "enabled": false, "valid": false, "reason": "daily updates are not enabled"}
	}
	valid, reason := VerifyPolicy(config, policy)
	components := listValue(policy["components"])
	targets := object(policy["target_versions"])
	if targets == nil {
		targets = Object{}
	}
	channel := policy["release_channel"]
	if channel == nil {
		channel = "stable"
	}
	return Object{"enrolled": true, "enabled": policy["enabled"] == nil || boolValue(policy["enabled"]), "valid": valid, "reason": reason, "policy_id": policy["policy_id"], "vm_id": policy["vm_id"], "components": components, "target_versions": targets, "release_channel": channel, "expires_at": policy["expires_at"], "signer": policy["signer"]}
}
