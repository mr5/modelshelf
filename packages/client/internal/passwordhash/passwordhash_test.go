package passwordhash

import (
	"encoding/base64"
	"strconv"
	"strings"
	"testing"

	"golang.org/x/crypto/argon2"
)

func TestGenerateProducesVerifiableArgon2idPHCString(t *testing.T) {
	password := []byte("correct horse battery staple")
	hash, err := Generate(password)
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(hash, "$")
	if len(parts) != 6 || parts[1] != "argon2id" || parts[2] != "v=19" {
		t.Fatalf("invalid PHC string: %q", hash)
	}
	parameters := map[string]int{}
	for _, item := range strings.Split(parts[3], ",") {
		keyValue := strings.SplitN(item, "=", 2)
		value, parseErr := strconv.Atoi(keyValue[1])
		if parseErr != nil {
			t.Fatal(parseErr)
		}
		parameters[keyValue[0]] = value
	}
	salt, err := base64.RawStdEncoding.DecodeString(parts[4])
	if err != nil {
		t.Fatal(err)
	}
	digest, err := base64.RawStdEncoding.DecodeString(parts[5])
	if err != nil {
		t.Fatal(err)
	}
	actual := argon2.IDKey(
		password,
		salt,
		uint32(parameters["t"]),
		uint32(parameters["m"]),
		uint8(parameters["p"]),
		uint32(len(digest)),
	)
	if string(actual) != string(digest) {
		t.Fatal("generated password hash cannot be verified")
	}
}

func TestGenerateRejectsEmptyPasswordAndUsesRandomSalt(t *testing.T) {
	if _, err := Generate(nil); err == nil {
		t.Fatal("empty password was accepted")
	}
	first, err := Generate([]byte("same password"))
	if err != nil {
		t.Fatal(err)
	}
	second, err := Generate([]byte("same password"))
	if err != nil {
		t.Fatal(err)
	}
	if first == second {
		t.Fatal("password hashes reused a salt")
	}
}
