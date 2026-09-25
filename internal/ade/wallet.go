package ade

import (
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/decred/dcrd/dcrec/secp256k1/v4/ecdsa"
	"golang.org/x/crypto/sha3"
)

var walletAddressPattern = regexp.MustCompile(`^0x[0-9a-fA-F]{40}$`)
var walletSignaturePattern = regexp.MustCompile(`^0x[0-9a-fA-F]{130}$`)

func NormalizeWalletAddress(value any) (string, error) {
	address, ok := value.(string)
	if !ok || !walletAddressPattern.MatchString(address) {
		return "", fmt.Errorf("wallet address must be a 0x-prefixed Ethereum address")
	}
	return strings.ToLower(address), nil
}

func VerifyWalletSignature(address string, message []byte, signature string) bool {
	address, err := NormalizeWalletAddress(address)
	if err != nil || !walletSignaturePattern.MatchString(signature) {
		return false
	}
	raw, err := hex.DecodeString(strings.TrimPrefix(signature, "0x"))
	if err != nil {
		return false
	}
	recovery := raw[64]
	if recovery >= 27 {
		recovery -= 27
	}
	if recovery > 1 {
		return false
	}
	personal := []byte(fmt.Sprintf("\x19Ethereum Signed Message:\n%d", len(message)))
	personal = append(personal, message...)
	hasher := sha3.NewLegacyKeccak256()
	_, _ = hasher.Write(personal)
	digest := hasher.Sum(nil)
	compact := make([]byte, 65)
	compact[0] = 27 + 4 + recovery
	copy(compact[1:], raw[:64])
	publicKey, _, err := ecdsa.RecoverCompact(compact, digest)
	if err != nil {
		return false
	}
	serialized := publicKey.SerializeUncompressed()
	hasher.Reset()
	_, _ = hasher.Write(serialized[1:])
	hash := hasher.Sum(nil)
	recovered := "0x" + hex.EncodeToString(hash[len(hash)-20:])
	return subtle.ConstantTimeCompare([]byte(recovered), []byte(address)) == 1
}

func parseTimestamp(value string) (time.Time, error) {
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil {
		return time.Time{}, fmt.Errorf("timestamp must be ISO-8601")
	}
	return parsed.UTC(), nil
}
