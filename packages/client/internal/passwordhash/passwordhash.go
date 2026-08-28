package passwordhash

import (
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"

	"golang.org/x/crypto/argon2"
)

const (
	timeCost    uint32 = 3
	memoryCost  uint32 = 64 * 1024
	parallelism uint8  = 4
	saltLength         = 16
	keyLength   uint32 = 32
)

// Generate creates an Argon2id PHC string compatible with the ModelShelf server.
func Generate(password []byte) (string, error) {
	if len(password) == 0 {
		return "", errors.New("password must not be empty")
	}
	salt := make([]byte, saltLength)
	if _, err := rand.Read(salt); err != nil {
		return "", fmt.Errorf("generate password salt: %w", err)
	}
	digest := argon2.IDKey(password, salt, timeCost, memoryCost, parallelism, keyLength)
	encoding := base64.RawStdEncoding
	return fmt.Sprintf(
		"$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2.Version,
		memoryCost,
		timeCost,
		parallelism,
		encoding.EncodeToString(salt),
		encoding.EncodeToString(digest),
	), nil
}
