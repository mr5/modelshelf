package cli

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	clientconfig "github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/domain"
	"github.com/mr5/modelshelf/client/internal/lockfile"
)

func TestStatusAndVerifyStableExitCodes(t *testing.T) {
	root := t.TempDir()
	configPath := filepath.Join(root, "config.yml")
	configuration := `serverUrl: http://127.0.0.1:1
nfsLocalPath: /mnt/modelshelf
localBasePath: ` + filepath.Join(root, "models") + `
models: []
`
	if err := os.WriteFile(configPath, []byte(configuration), 0o600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	command := NewWithIO("test", "commit", bytes.NewBuffer(nil), &output, &output)
	command.SetArgs([]string{"--config", configPath, "status", "huggingface", "owner/model"})
	err := command.Execute()
	if ExitCode(err) != ExitUnavailable {
		t.Fatalf("status exit=%d err=%v output=%s", ExitCode(err), err, output.String())
	}
	output.Reset()
	command = NewWithIO("test", "commit", bytes.NewBuffer(nil), &output, &output)
	command.SetArgs([]string{"verify", root, "--full"})
	err = command.Execute()
	if ExitCode(err) != ExitCorrupt {
		t.Fatalf("verify exit=%d err=%v output=%s", ExitCode(err), err, output.String())
	}
}

func TestRootContainsCompleteCommandSet(t *testing.T) {
	command := NewWithIO("test", "commit", bytes.NewBuffer(nil), &bytes.Buffer{}, &bytes.Buffer{})
	wanted := map[string]bool{
		"add": false, "remove": false, "search": false, "sync": false, "list": false,
		"status": false, "mount": false, "unmount": false, "verify": false, "tui": false,
		"hash-password": false,
		"upgrade":       false,
	}
	for _, child := range command.Commands() {
		if _, ok := wanted[child.Name()]; ok {
			wanted[child.Name()] = true
		}
	}
	for name, present := range wanted {
		if !present {
			t.Errorf("missing command %s", name)
		}
	}
}

func TestUpgradeCheckUsesConfiguredServerDistribution(t *testing.T) {
	archive := "modelshelf_" + runtime.GOOS + "_" + runtime.GOARCH + ".tar.gz"
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v1/info" {
			http.NotFound(writer, request)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(writer, `{
  "name":"ModelShelf","version":"0.2.0","nfs":null,
  "client":{"available":true,"version":"0.2.0","installUrl":"%s/install.sh",
  "downloadUrl":"%s/api/v1/client","platforms":[{"os":"%s","arch":"%s","filename":"%s"}]}
}`, server.URL, server.URL, runtime.GOOS, runtime.GOARCH, archive)
	}))
	defer server.Close()

	root := t.TempDir()
	configPath := filepath.Join(root, "config.yml")
	configuration := "serverUrl: " + server.URL + `
nfsLocalPath: /mnt/modelshelf
localBasePath: ` + filepath.Join(root, "models") + `
models: []
`
	if err := os.WriteFile(configPath, []byte(configuration), 0o600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	command := NewWithIO("0.1.0", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "upgrade", "--check"})
	if err := command.Execute(); err != nil {
		t.Fatalf("upgrade --check: %v output=%s", err, output.String())
	}
	if !strings.Contains(output.String(), "Upgrade available: 0.1.0 -> 0.2.0") {
		t.Fatalf("output = %s", output.String())
	}
}

func TestHashPasswordFromStdin(t *testing.T) {
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	command := NewWithIO(
		"test",
		"commit",
		strings.NewReader("correct horse battery staple\n"),
		&stdout,
		&stderr,
	)
	command.SetArgs([]string{"hash-password", "--stdin"})
	if err := command.Execute(); err != nil {
		t.Fatalf("hash-password: %v stderr=%s", err, stderr.String())
	}
	hash := strings.TrimSpace(stdout.String())
	if !strings.HasPrefix(hash, "$argon2id$v=19$m=65536,t=3,p=4$") {
		t.Fatalf("unexpected hash: %q", hash)
	}
	if strings.Contains(stdout.String(), "correct horse") || strings.Contains(stderr.String(), "correct horse") {
		t.Fatal("plaintext password was written to command output")
	}
}

func TestHashPasswordRequiresExplicitStdinModeForPipes(t *testing.T) {
	var output bytes.Buffer
	command := NewWithIO("test", "commit", strings.NewReader("secret\n"), &output, &output)
	command.SetArgs([]string{"hash-password"})
	err := command.Execute()
	if err == nil || !strings.Contains(err.Error(), "use --stdin") {
		t.Fatalf("error = %v output=%s", err, output.String())
	}
}

func TestAddCreatesServerTaskAndPersistsDesiredState(t *testing.T) {
	created := false
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer write-token" {
			http.Error(writer, `{"detail":"unauthorized"}`, http.StatusUnauthorized)
			return
		}
		switch {
		case request.Method == http.MethodGet && request.URL.Path == "/api/v1/artifacts":
			writer.Header().Set("Content-Type", "application/json")
			_, _ = writer.Write([]byte("[]"))
		case request.Method == http.MethodPost && request.URL.Path == "/api/v1/tasks":
			var payload map[string]string
			if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
				t.Error(err)
			}
			if payload["id"] != "owner/model" || payload["revision"] != "release" {
				t.Errorf("payload = %#v", payload)
			}
			created = true
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusAccepted)
			_, _ = writer.Write([]byte(`{
  "id":"task-id","provider":"huggingface","sourceId":"owner/model",
  "requestedRevision":"release","status":"queued","progress":0,"bytesDownloaded":0,
  "createdAt":"2026-01-01T00:00:00Z","updatedAt":"2026-01-01T00:00:00Z"
}`))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	root := t.TempDir()
	configPath := filepath.Join(root, "config.yml")
	configuration := "serverUrl: " + server.URL + `
nfsLocalPath: /mnt/modelshelf
localBasePath: ` + filepath.Join(root, "models") + `
writeToken: write-token
models: []
`
	if err := os.WriteFile(configPath, []byte(configuration), 0o600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	command := NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{
		"--config", configPath, "add", "huggingface", "owner/model", "--revision", "release",
		"--alias", "chat-model",
	})
	if err := command.Execute(); ExitCode(err) != ExitNotReady {
		t.Fatalf("add exit=%d err=%v output=%s", ExitCode(err), err, output.String())
	}
	if !created || !strings.Contains(output.String(), "Download task created task-id") {
		t.Fatalf("task created=%v output=%s", created, output.String())
	}
	loaded, _, err := clientconfig.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(loaded.Models) != 1 || loaded.Models[0].RequestedRevision != "release" ||
		loaded.Models[0].Alias != "chat-model" {
		t.Fatalf("saved models = %#v", loaded.Models)
	}
}

func TestAliasCanReferenceConfiguredModel(t *testing.T) {
	root := t.TempDir()
	configPath := filepath.Join(root, "config.yml")
	configuration := `serverUrl: http://127.0.0.1:1
nfsLocalPath: /mnt/modelshelf
localBasePath: ` + filepath.Join(root, "models") + `
models:
  - alias: mini-lm
    provider: huggingface
    id: owner/model
    revision: main
`
	if err := os.WriteFile(configPath, []byte(configuration), 0o600); err != nil {
		t.Fatal(err)
	}

	var output bytes.Buffer
	command := NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "status", "mini-lm"})
	err := command.Execute()
	if ExitCode(err) != ExitNotReady || !strings.Contains(err.Error(), "model is not locked") {
		t.Fatalf("alias status exit=%d err=%v output=%s", ExitCode(err), err, output.String())
	}

	output.Reset()
	command = NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "list"})
	if err := command.Execute(); err != nil || !strings.Contains(output.String(), "mini-lm") {
		t.Fatalf("alias list err=%v output=%s", err, output.String())
	}

	output.Reset()
	command = NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "remove", "mini-lm"})
	if err := command.Execute(); err != nil {
		t.Fatalf("alias remove: %v output=%s", err, output.String())
	}
	loaded, _, err := clientconfig.Load(configPath)
	if err != nil || len(loaded.Models) != 0 {
		t.Fatalf("models after alias remove = %#v err=%v", loaded.Models, err)
	}
}

func TestSyncWritesLockBeforeLocalCopy(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet || request.URL.Path != "/api/v1/artifacts" {
			http.NotFound(writer, request)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`[{
  "artifactId":"huggingface:b3duZXIvbW9kZWw:Y29tbWl0LW9uZQ",
  "name":"model","version":"main","provider":"huggingface","sourceId":"owner/model",
  "requestedRevision":"main","resolvedRevision":"commit-one","totalSize":7,"fileCount":1,
  "createdAt":"2026-01-01T00:00:00Z","relativePath":"artifacts/model"
}]`))
	}))
	defer server.Close()

	root := t.TempDir()
	configPath := filepath.Join(root, "config.yml")
	configuration := "serverUrl: " + server.URL + `
nfsLocalPath: ` + filepath.Join(root, "nfs") + `
localBasePath: ` + filepath.Join(root, "models") + `
models:
  - alias: prod
    provider: huggingface
    id: owner/model
    revision: main
`
	if err := os.WriteFile(configPath, []byte(configuration), 0o600); err != nil {
		t.Fatal(err)
	}
	futureLock := `schemaVersion: 3
models: []
`
	if err := os.WriteFile(lockfile.Path(configPath), []byte(futureLock), 0o600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	frozenCommand := NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	frozenCommand.SetArgs([]string{"--config", configPath, "sync", "--frozen-lockfile"})
	if err := frozenCommand.Execute(); err == nil || !strings.Contains(err.Error(), "newer than supported") {
		t.Fatalf("frozen future lock err=%v output=%s", err, output.String())
	}
	output.Reset()
	guardedCommand := NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	guardedCommand.SetArgs([]string{"--config", configPath, "sync", "prod"})
	if err := guardedCommand.Execute(); err == nil || !strings.Contains(err.Error(), "newer than supported") {
		t.Fatalf("future lock was silently rebuilt err=%v output=%s", err, output.String())
	}
	if err := os.Remove(lockfile.Path(configPath)); err != nil {
		t.Fatal(err)
	}
	output.Reset()
	command := NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "sync", "prod"})
	err := command.Execute()
	if ExitCode(err) != ExitNotReady {
		t.Fatalf("sync exit=%d err=%v output=%s", ExitCode(err), err, output.String())
	}
	locked, exists, lockErr := lockfile.Load(lockfile.Path(configPath))
	if lockErr != nil || !exists || len(locked.Models) != 1 ||
		locked.Models[0].ResolvedRevision != "commit-one" {
		t.Fatalf("lock exists=%v value=%#v err=%v", exists, locked, lockErr)
	}
	rebuiltData, err := os.ReadFile(lockfile.Path(configPath))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(rebuiltData), "schemaVersion: 2") {
		t.Fatalf("rebuilt lock has no current schemaVersion:\n%s", rebuiltData)
	}

	configuration = "serverUrl: " + server.URL + `
nfsLocalPath: ` + filepath.Join(root, "nfs") + `
localBasePath: ` + filepath.Join(root, "models") + `
models: []
`
	if err := os.WriteFile(configPath, []byte(configuration), 0o600); err != nil {
		t.Fatal(err)
	}
	output.Reset()
	command = NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "sync"})
	if err := command.Execute(); err != nil {
		t.Fatalf("sync deletion: %v output=%s", err, output.String())
	}
	locked, _, lockErr = lockfile.Load(lockfile.Path(configPath))
	if lockErr != nil || len(locked.Models) != 0 {
		t.Fatalf("lock after deletion = %#v err=%v", locked, lockErr)
	}
}

func TestSyncLocksDuplicateAliasesToOneArtifact(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet || request.URL.Path != "/api/v1/artifacts" {
			http.NotFound(writer, request)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`[{
  "artifactId":"huggingface:b3duZXIvbW9kZWw:Y29tbWl0LW9uZQ",
  "name":"model","version":"main","provider":"huggingface","sourceId":"owner/model",
  "requestedRevision":"main","resolvedRevision":"commit-one","totalSize":7,"fileCount":1,
  "createdAt":"2026-01-01T00:00:00Z","relativePath":"huggingface/owner/model/commit-one"
}]`))
	}))
	defer server.Close()

	root := t.TempDir()
	configPath := filepath.Join(root, "config.yml")
	configuration := "serverUrl: " + server.URL + `
nfsLocalPath: ` + filepath.Join(root, "nfs") + `
localBasePath: ` + filepath.Join(root, "local") + `
models:
  - alias: primary
    provider: huggingface
    id: owner/model
    revision: main
  - alias: secondary
    provider: huggingface
    id: owner/model
    revision: main
`
	if err := os.WriteFile(configPath, []byte(configuration), 0o600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	command := NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "sync"})
	if err := command.Execute(); ExitCode(err) != ExitNotReady {
		t.Fatalf("sync exit=%d err=%v output=%s", ExitCode(err), err, output.String())
	}
	locked, exists, err := lockfile.Load(lockfile.Path(configPath))
	if err != nil || !exists || len(locked.Models) != 2 {
		t.Fatalf("lock exists=%v models=%#v err=%v", exists, locked.Models, err)
	}
	if locked.Models[0].ArtifactID != locked.Models[1].ArtifactID ||
		locked.Models[0].Alias == locked.Models[1].Alias {
		t.Fatalf("duplicate alias locks = %#v", locked.Models)
	}
}

func TestRemoveDuplicateAliasRetainsSharedCanonicalArtifact(t *testing.T) {
	root := t.TempDir()
	configPath := filepath.Join(root, "config.yml")
	localBase := filepath.Join(root, "local")
	configuration := `serverUrl: http://127.0.0.1:1
nfsLocalPath: /mnt/modelshelf
localBasePath: ` + localBase + `
models:
  - alias: primary
    provider: huggingface
    id: owner/model
    revision: main
  - alias: secondary
    provider: huggingface
    id: owner/model
    revision: main
`
	if err := os.WriteFile(configPath, []byte(configuration), 0o600); err != nil {
		t.Fatal(err)
	}
	relativePath := "huggingface/owner/model/commit-one"
	canonical := filepath.Join(localBase, "models", filepath.FromSlash(relativePath))
	if err := os.MkdirAll(canonical, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(canonical, "model.bin"), []byte("shared"), 0o444); err != nil {
		t.Fatal(err)
	}
	aliases := filepath.Join(localBase, "aliases")
	if err := os.MkdirAll(aliases, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, alias := range []string{"primary", "secondary"} {
		if err := os.Symlink(canonical, filepath.Join(aliases, alias)); err != nil {
			t.Fatal(err)
		}
	}
	revisionReference := filepath.Join(filepath.Dir(canonical), "main")
	if err := os.Symlink(filepath.Base(canonical), revisionReference); err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	locked := lockfile.File{Models: []lockfile.Model{
		{
			Alias: "primary", Provider: "huggingface", ID: "owner/model", Revision: "main",
			ResolvedRevision: "commit-one", ArtifactID: "artifact", RelativePath: relativePath,
			LockedAt: now,
		},
		{
			Alias: "secondary", Provider: "huggingface", ID: "owner/model", Revision: "main",
			ResolvedRevision: "commit-one", ArtifactID: "artifact", RelativePath: relativePath,
			LockedAt: now,
		},
	}}
	if err := lockfile.Save(locked, lockfile.Path(configPath)); err != nil {
		t.Fatal(err)
	}

	var output bytes.Buffer
	command := NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "remove", "primary", "-y"})
	if err := command.Execute(); err != nil {
		t.Fatalf("remove primary: %v output=%s", err, output.String())
	}
	if _, err := os.Stat(canonical); err != nil {
		t.Fatalf("shared canonical artifact was removed: %v", err)
	}
	if _, err := os.Lstat(filepath.Join(aliases, "primary")); !os.IsNotExist(err) {
		t.Fatalf("primary alias remains: %v", err)
	}
	if _, err := os.Stat(filepath.Join(aliases, "secondary")); err != nil {
		t.Fatalf("secondary alias was affected: %v", err)
	}
	if _, err := os.Stat(revisionReference); err != nil {
		t.Fatalf("shared revision reference was affected: %v", err)
	}

	output.Reset()
	command = NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "remove", "secondary", "-y"})
	if err := command.Execute(); err != nil {
		t.Fatalf("remove secondary: %v output=%s", err, output.String())
	}
	if _, err := os.Stat(canonical); !os.IsNotExist(err) {
		t.Fatalf("unreferenced canonical artifact remains: %v", err)
	}
	if _, err := os.Lstat(revisionReference); !os.IsNotExist(err) {
		t.Fatalf("unreferenced revision reference remains: %v", err)
	}
}

func TestAddFilesystemRequiresServerSideImport(t *testing.T) {
	posted := false
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch {
		case request.Method == http.MethodGet && request.URL.Path == "/api/v1/artifacts":
			writer.Header().Set("Content-Type", "application/json")
			_, _ = writer.Write([]byte("[]"))
		case request.Method == http.MethodPost && request.URL.Path == "/api/v1/tasks":
			posted = true
			http.Error(writer, "unexpected task", http.StatusInternalServerError)
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	root := t.TempDir()
	configPath := filepath.Join(root, "config.yml")
	configuration := "serverUrl: " + server.URL + `
nfsLocalPath: /mnt/modelshelf
localBasePath: ` + filepath.Join(root, "models") + `
writeToken: write-token
models: []
`
	if err := os.WriteFile(configPath, []byte(configuration), 0o600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	command := NewWithIO("test", "commit", strings.NewReader(""), &output, &output)
	command.SetArgs([]string{"--config", configPath, "add", "filesystem", "offline/model"})
	err := command.Execute()
	if err == nil || !strings.Contains(err.Error(), "modelshelf-server import") {
		t.Fatalf("error = %v output=%s", err, output.String())
	}
	if posted {
		t.Fatal("filesystem add created a download task")
	}
	loaded, _, loadErr := clientconfig.Load(configPath)
	if loadErr != nil {
		t.Fatal(loadErr)
	}
	if len(loaded.Models) != 1 || loaded.Models[0].RequestedRevision != "content" {
		t.Fatalf("saved models = %#v", loaded.Models)
	}
}

func TestManifestMatchesDesiredIdentity(t *testing.T) {
	manifest := domain.ArtifactManifest{Source: domain.SourceReference{
		Provider: "huggingface", ID: "owner/model",
		RequestedRevision: "main", ResolvedRevision: "commit-one",
	}}
	desired := domain.DesiredModel{
		Provider: "huggingface", ID: "owner/model", RequestedRevision: "main",
	}
	if !manifestMatchesDesired(manifest, desired) {
		t.Fatal("matching requested revision was rejected")
	}
	desired.RequestedRevision = "release"
	if manifestMatchesDesired(manifest, desired) {
		t.Fatal("different requested revision was accepted")
	}
	desired.RequestedRevision = "main"
	desired.ResolvedRevision = "commit-two"
	if manifestMatchesDesired(manifest, desired) {
		t.Fatal("different immutable revision was accepted")
	}
}

func TestLockEntryMatchDetectsAliasedSelectorEdit(t *testing.T) {
	entry := lockfile.Model{
		Alias: "prod", Provider: "huggingface", ID: "owner/model", Revision: "main",
		Path: "runtime/model",
	}
	desired := domain.DesiredModel{
		Alias: "prod", Provider: "huggingface", ID: "owner/model", RequestedRevision: "main",
		Path: "runtime/model",
	}
	if !lockEntryMatchesDesired(entry, desired) {
		t.Fatal("unchanged aliased selector did not match")
	}
	desired.RequestedRevision = "release"
	if lockEntryMatchesDesired(entry, desired) {
		t.Fatal("aliased revision edit reused the old lock entry")
	}
}
