package upgrade

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInstallVerifiesAndAtomicallyReplacesExecutable(t *testing.T) {
	archiveName := "modelshelf_linux_amd64.tar.gz"
	binary := []byte("#!/bin/sh\necho 'modelshelf version v1.2.0'\n")
	archive := releaseArchive(t, binary)
	digest := sha256.Sum256(archive)
	server := releaseServer(t, archiveName, archive, fmt.Sprintf("%x  %s\n", digest, archiveName))
	defer server.Close()

	target := filepath.Join(t.TempDir(), "modelshelf")
	if err := os.WriteFile(target, []byte("old binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	err := Install(context.Background(), Release{
		Version: "v1.2.0", DownloadBase: server.URL, Archive: archiveName,
	}, Options{HTTPClient: server.Client(), ExecutablePath: target})
	if err != nil {
		t.Fatal(err)
	}
	installed, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(installed, binary) {
		t.Fatalf("installed binary = %q", installed)
	}
}

func TestInstallRejectsChecksumMismatchWithoutChangingExecutable(t *testing.T) {
	archiveName := "modelshelf_linux_amd64.tar.gz"
	archive := releaseArchive(t, []byte("#!/bin/sh\necho 'modelshelf version v1.2.0'\n"))
	server := releaseServer(t, archiveName, archive, strings.Repeat("0", 64)+"  "+archiveName+"\n")
	defer server.Close()

	target := filepath.Join(t.TempDir(), "modelshelf")
	if err := os.WriteFile(target, []byte("old binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	err := Install(context.Background(), Release{
		Version: "v1.2.0", DownloadBase: server.URL, Archive: archiveName,
	}, Options{HTTPClient: server.Client(), ExecutablePath: target})
	if err == nil || !strings.Contains(err.Error(), "checksum verification failed") {
		t.Fatalf("error = %v", err)
	}
	installed, readErr := os.ReadFile(target)
	if readErr != nil || string(installed) != "old binary" {
		t.Fatalf("installed = %q err=%v", installed, readErr)
	}
}

func TestCompareVersions(t *testing.T) {
	tests := []struct {
		left, right string
		want        int
	}{
		{"v1.2.0", "1.2.0", 0},
		{"1.2.1", "1.2.0", 1},
		{"1.2.0-beta.2", "1.2.0-beta.11", -1},
		{"1.2.0-rc.1", "1.2.0", -1},
	}
	for _, test := range tests {
		got, err := CompareVersions(test.left, test.right)
		if err != nil || got != test.want {
			t.Errorf("CompareVersions(%q, %q) = %d, %v; want %d", test.left, test.right, got, err, test.want)
		}
	}
	if _, err := CompareVersions("dev", "1.0.0"); err == nil {
		t.Fatal("development version was accepted as semantic")
	}
	for _, invalid := range []string{"1.0.0-", "1.0.0-01", "1.0.0+", "1.0.0-rc/1"} {
		if _, err := CompareVersions(invalid, "1.0.0"); err == nil {
			t.Errorf("invalid version %q was accepted", invalid)
		}
	}
}

func TestLatestGitHubVersion(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/repos/mr5/modelshelf/releases/latest" {
			http.NotFound(writer, request)
			return
		}
		if request.Header.Get("Authorization") != "Bearer token" {
			t.Errorf("authorization = %q", request.Header.Get("Authorization"))
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"tag_name":"v1.2.0"}`))
	}))
	defer server.Close()
	version, err := LatestGitHubVersion(
		context.Background(), server.Client(), server.URL, "mr5/modelshelf", "token",
	)
	if err != nil || version != "v1.2.0" {
		t.Fatalf("version = %q err=%v", version, err)
	}
}

func releaseArchive(t *testing.T, binary []byte) []byte {
	t.Helper()
	var result bytes.Buffer
	compressed := gzip.NewWriter(&result)
	archive := tar.NewWriter(compressed)
	if err := archive.WriteHeader(&tar.Header{
		Name: "modelshelf", Mode: 0o755, Size: int64(len(binary)), Typeflag: tar.TypeReg,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := archive.Write(binary); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := compressed.Close(); err != nil {
		t.Fatal(err)
	}
	return result.Bytes()
}

func releaseServer(t *testing.T, archiveName string, archive []byte, checksums string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/checksums.txt":
			_, _ = writer.Write([]byte(checksums))
		case "/" + archiveName:
			_, _ = writer.Write(archive)
		default:
			http.NotFound(writer, request)
		}
	}))
}
