package syncer

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mr5/modelshelf/client/internal/api"
	"github.com/mr5/modelshelf/client/internal/catalog"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/domain"
)

func TestNativeSyncReconcilesAndAtomicallyReplaces(t *testing.T) {
	root := t.TempDir()
	nfs := filepath.Join(root, "nfs")
	local := filepath.Join(root, "local")
	first := createArtifact(t, nfs, "commit-one", "first", time.Now().Add(-time.Hour))
	second := createArtifact(t, nfs, "commit-two", "second", time.Now())
	var mutex sync.RWMutex
	available := []domain.ArtifactSummary{first}
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v1/artifacts" {
			http.NotFound(writer, request)
			return
		}
		mutex.RLock()
		defer mutex.RUnlock()
		writer.Header().Set("Content-Type", "application/json")
		if err := json.NewEncoder(writer).Encode(available); err != nil {
			t.Error(err)
		}
	}))
	defer server.Close()
	configuration := config.Config{
		ServerURL:     server.URL,
		NFSLocalPath:  nfs,
		LocalBasePath: local,
		Models: []domain.DesiredModel{{
			Alias: "production", Provider: domain.ProviderHuggingFace,
			ID: "owner/model", RequestedRevision: "main",
		}},
	}
	client := api.New(server.URL, "")
	desired := configuration.Models[0]
	result, err := One(context.Background(), configuration, client, desired)
	if err != nil {
		t.Fatal(err)
	}
	if result.ResolvedRevision != "commit-one" {
		t.Fatalf("first result = %#v", result)
	}
	firstDestination, _ := config.ArtifactPath(configuration, first.RelativePath)
	alias := filepath.Join(local, "aliases", "production")
	revisionReference := filepath.Join(filepath.Dir(firstDestination), "main")
	assertSyncedContent(t, firstDestination, "first", first.ArtifactID)
	assertSyncedContent(t, alias, "first", first.ArtifactID)
	assertSyncedContent(t, revisionReference, "first", first.ArtifactID)
	firstSyncState := readSyncState(t, firstDestination)
	// Once desired state is satisfied, a transient NFS outage must not make an
	// otherwise idempotent reconcile fail.
	if err := os.RemoveAll(filepath.Join(nfs, first.RelativePath)); err != nil {
		t.Fatal(err)
	}
	if _, err := One(context.Background(), configuration, client, desired); err != nil {
		t.Fatal(err)
	}
	if readSyncState(t, firstDestination)["syncedAt"] != firstSyncState["syncedAt"] {
		t.Fatal("idempotent sync rewrote an already-ready model")
	}
	mutex.Lock()
	available = []domain.ArtifactSummary{first, second}
	mutex.Unlock()
	result, err = One(context.Background(), configuration, client, desired)
	if err != nil {
		t.Fatal(err)
	}
	if result.ResolvedRevision != "commit-two" {
		t.Fatalf("second result = %#v", result)
	}
	secondDestination, _ := config.ArtifactPath(configuration, second.RelativePath)
	assertSyncedContent(t, secondDestination, "second", second.ArtifactID)
	assertSyncedContent(t, alias, "second", second.ArtifactID)
	assertSyncedContent(t, revisionReference, "second", second.ArtifactID)
	assertSyncedContent(t, firstDestination, "first", first.ArtifactID)
	if entries, err := os.ReadDir(filepath.Join(local, "models", ".staging")); err != nil || len(entries) != 0 {
		t.Fatalf("staging was not cleaned: entries=%v err=%v", entries, err)
	}
}

func TestSyncReusesFilesFromExistingLocalRevision(t *testing.T) {
	root := t.TempDir()
	nfs := filepath.Join(root, "nfs")
	local := filepath.Join(root, "local")
	first := createArtifactFiles(t, nfs, "commit-one", map[string]string{
		"shared.bin": "unchanged", "version.txt": "one",
	}, time.Now().Add(-time.Hour))
	second := createArtifactFiles(t, nfs, "commit-two", map[string]string{
		"shared.bin": "unchanged", "version.txt": "two",
	}, time.Now())
	configuration := config.Config{NFSLocalPath: nfs, LocalBasePath: local}
	desired := domain.DesiredModel{
		Provider: domain.ProviderHuggingFace, ID: "owner/model", RequestedRevision: "main",
	}
	if _, err := SyncArtifact(context.Background(), configuration, desired, first); err != nil {
		t.Fatal(err)
	}
	firstRoot, _ := config.ArtifactPath(configuration, first.RelativePath)
	firstShared, err := os.Stat(filepath.Join(firstRoot, "shared.bin"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := SyncArtifact(context.Background(), configuration, desired, second); err != nil {
		t.Fatal(err)
	}
	secondRoot, _ := config.ArtifactPath(configuration, second.RelativePath)
	secondShared, err := os.Stat(filepath.Join(secondRoot, "shared.bin"))
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(firstShared, secondShared) {
		t.Fatal("unchanged file was not hardlinked from the existing local revision")
	}
}

func TestSyncBatchReusesRevisionPublishedEarlierInSameRun(t *testing.T) {
	root := t.TempDir()
	nfs := filepath.Join(root, "nfs")
	configuration := config.Config{NFSLocalPath: nfs, LocalBasePath: filepath.Join(root, "local")}
	artifacts := []domain.ArtifactSummary{
		createArtifactFiles(t, nfs, "commit-one", map[string]string{"shared.bin": "same"}, time.Now()),
		createArtifactFiles(t, nfs, "commit-two", map[string]string{"shared.bin": "same"}, time.Now()),
	}
	for _, artifact := range artifacts {
		desired := domain.DesiredModel{
			Provider: domain.ProviderHuggingFace, ID: "owner/model",
			RequestedRevision: artifact.ResolvedRevision,
		}
		if _, err := SyncArtifact(context.Background(), configuration, desired, artifact); err != nil {
			t.Fatal(err)
		}
	}
	firstRoot, _ := config.ArtifactPath(configuration, artifacts[0].RelativePath)
	secondRoot, _ := config.ArtifactPath(configuration, artifacts[1].RelativePath)
	first, _ := os.Stat(filepath.Join(firstRoot, "shared.bin"))
	second, _ := os.Stat(filepath.Join(secondRoot, "shared.bin"))
	if first == nil || second == nil || !os.SameFile(first, second) {
		t.Fatal("second revision in one sync run did not reuse the first revision")
	}
	if err := RemoveTree(firstRoot); err != nil {
		t.Fatal(err)
	}
	remaining, err := os.Stat(filepath.Join(secondRoot, "shared.bin"))
	if err != nil {
		t.Fatal("deleting one revision removed the shared file from another revision")
	}
	if remaining.Mode().Perm()&0o222 != 0 {
		t.Fatalf("deleting one revision made the shared inode writable: %o", remaining.Mode().Perm())
	}
}

func TestSyncRetainsCompletedStagingFilesAfterFailure(t *testing.T) {
	root := t.TempDir()
	nfs := filepath.Join(root, "nfs")
	configuration := config.Config{NFSLocalPath: nfs, LocalBasePath: filepath.Join(root, "local")}
	artifact := createArtifactFiles(t, nfs, "commit", map[string]string{
		"first.bin": "first", "second.bin": "second",
	}, time.Now())
	source := filepath.Join(nfs, filepath.FromSlash(artifact.RelativePath))
	manifest, err := catalog.ReadManifest(source)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte(artifact.ArtifactID))
	stage := filepath.Join(config.StagingRoot(configuration), fmt.Sprintf("sync-%x", digest[:]))
	state := stagingState{SchemaVersion: 1, ArtifactID: artifact.ArtifactID, ContentSHA256: manifest.ContentSHA256}
	if err := prepareStaging(stage, state); err != nil {
		t.Fatal(err)
	}
	stagedFirst := filepath.Join(stage, "first.bin")
	data, _ := os.ReadFile(filepath.Join(source, "first.bin"))
	if err := os.WriteFile(stagedFirst, data, 0o444); err != nil {
		t.Fatal(err)
	}
	secondSource := filepath.Join(source, "second.bin")
	secondData, _ := os.ReadFile(secondSource)
	if err := os.Remove(secondSource); err != nil {
		t.Fatal(err)
	}
	desired := domain.DesiredModel{Provider: domain.ProviderHuggingFace, ID: "owner/model", RequestedRevision: "main"}
	if _, err := SyncArtifact(context.Background(), configuration, desired, artifact); err == nil {
		t.Fatal("sync unexpectedly succeeded with a missing NFS file")
	}
	before, err := os.Stat(stagedFirst)
	if err != nil {
		t.Fatal("completed staged file was removed after failure")
	}
	if err := os.WriteFile(secondSource, secondData, 0o444); err != nil {
		t.Fatal(err)
	}
	if _, err := SyncArtifact(context.Background(), configuration, desired, artifact); err != nil {
		t.Fatal(err)
	}
	destination, _ := config.ArtifactPath(configuration, artifact.RelativePath)
	after, err := os.Stat(filepath.Join(destination, "first.bin"))
	if err != nil || !os.SameFile(before, after) {
		t.Fatal("completed staged file was recopied instead of resumed")
	}
}

func TestSelectArtifactRequiresRequestedRevisionOrPin(t *testing.T) {
	now := time.Now()
	artifacts := []domain.ArtifactSummary{
		{Provider: "huggingface", SourceID: "owner/model", RequestedRevision: "tag", ResolvedRevision: "one", CreatedAt: now},
		{Provider: "huggingface", SourceID: "owner/model", RequestedRevision: "main", ResolvedRevision: "two", CreatedAt: now.Add(time.Second)},
	}
	desired := domain.DesiredModel{Provider: "huggingface", ID: "owner/model", RequestedRevision: "missing"}
	if SelectArtifact(artifacts, desired) != nil {
		t.Fatal("artifact from a different requested revision was selected")
	}
	desired.ResolvedRevision = "one"
	selected := SelectArtifact(artifacts, desired)
	if selected == nil || selected.ResolvedRevision != "one" {
		t.Fatalf("pinned artifact = %#v", selected)
	}
}

func TestSelectArtifactDistinguishesFullAndSelectedVariants(t *testing.T) {
	now := time.Now()
	artifacts := []domain.ArtifactSummary{
		{
			ArtifactID: "full", Provider: "huggingface", SourceID: "owner/model",
			RequestedRevision: "main", ResolvedRevision: "commit", CreatedAt: now,
		},
		{
			ArtifactID: "partial", Provider: "huggingface", SourceID: "owner/model",
			RequestedRevision: "main", ResolvedRevision: "commit", CreatedAt: now.Add(time.Second),
			SelectionDigest: domain.SelectionDigest([]string{"model.gguf"}),
			SelectedPaths:   []string{"model.gguf"},
		},
	}
	full := SelectArtifact(artifacts, domain.DesiredModel{
		Provider: "huggingface", ID: "owner/model", RequestedRevision: "main",
	})
	if full == nil || full.ArtifactID != "full" {
		t.Fatalf("full selection = %#v", full)
	}
	partial := SelectArtifact(artifacts, domain.DesiredModel{
		Provider: "huggingface", ID: "owner/model", RequestedRevision: "main",
		Files: []string{"model.gguf"},
	})
	if partial == nil || partial.ArtifactID != "partial" {
		t.Fatalf("partial selection = %#v", partial)
	}
}

func TestDuplicateAliasesAndCustomPathShareCanonicalArtifact(t *testing.T) {
	root := t.TempDir()
	nfs := filepath.Join(root, "nfs")
	local := filepath.Join(root, "local")
	artifact := createArtifact(t, nfs, "commit-one", "shared", time.Now())
	configuration := config.Config{NFSLocalPath: nfs, LocalBasePath: local}
	primary := domain.DesiredModel{
		Alias: "primary", Provider: domain.ProviderHuggingFace, ID: "owner/model",
		RequestedRevision: "main", Path: "runtime/model",
	}
	secondary := domain.DesiredModel{
		Alias: "secondary", Provider: domain.ProviderHuggingFace, ID: "owner/model",
		RequestedRevision: "main",
	}
	if _, err := SyncArtifact(context.Background(), configuration, primary, artifact); err != nil {
		t.Fatal(err)
	}
	if _, err := SyncArtifact(context.Background(), configuration, secondary, artifact); err != nil {
		t.Fatal(err)
	}
	canonical, _ := config.ArtifactPath(configuration, artifact.RelativePath)
	for _, reference := range []string{
		filepath.Join(local, "aliases", "primary"),
		filepath.Join(local, "aliases", "secondary"),
		filepath.Join(local, "runtime", "model"),
		filepath.Join(filepath.Dir(canonical), "main"),
	} {
		info, err := os.Lstat(reference)
		if err != nil || info.Mode()&os.ModeSymlink == 0 {
			t.Fatalf("reference %s is not a symlink: info=%v err=%v", reference, info, err)
		}
		referenceInfo, referenceErr := os.Stat(reference)
		canonicalInfo, canonicalErr := os.Stat(canonical)
		if referenceErr != nil || canonicalErr != nil || !os.SameFile(referenceInfo, canonicalInfo) {
			t.Fatalf("reference %s does not resolve to %s: reference=%v canonical=%v", reference, canonical, referenceErr, canonicalErr)
		}
	}
	modelFiles := 0
	if err := filepath.WalkDir(local, func(path string, entry os.DirEntry, err error) error {
		if err == nil && !entry.IsDir() && entry.Type()&os.ModeSymlink == 0 && entry.Name() == "model.bin" {
			modelFiles++
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if modelFiles != 1 {
		t.Fatalf("physical model file copies = %d; want 1", modelFiles)
	}
	if err := RemoveReferences(configuration, primary); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(filepath.Join(local, "aliases", "primary")); !os.IsNotExist(err) {
		t.Fatalf("primary alias still exists: %v", err)
	}
	if _, err := os.Stat(canonical); err != nil {
		t.Fatalf("canonical artifact was removed: %v", err)
	}
	if _, err := os.Stat(filepath.Join(local, "aliases", "secondary")); err != nil {
		t.Fatalf("secondary alias was affected: %v", err)
	}
}

func TestSyncRefusesRequestedRevisionDirectoryCollision(t *testing.T) {
	root := t.TempDir()
	nfs := filepath.Join(root, "nfs")
	local := filepath.Join(root, "local")
	artifact := createArtifact(t, nfs, "commit-one", "content", time.Now())
	configuration := config.Config{NFSLocalPath: nfs, LocalBasePath: local}
	canonical, err := config.ArtifactPath(configuration, artifact.RelativePath)
	if err != nil {
		t.Fatal(err)
	}
	collision := filepath.Join(filepath.Dir(canonical), "main")
	if err := os.MkdirAll(collision, 0o755); err != nil {
		t.Fatal(err)
	}
	desired := domain.DesiredModel{
		Provider: domain.ProviderHuggingFace, ID: "owner/model", RequestedRevision: "main",
	}
	if _, err := SyncArtifact(context.Background(), configuration, desired, artifact); err == nil ||
		!strings.Contains(err.Error(), "refusing to replace non-symlink reference path") {
		t.Fatalf("collision error = %v", err)
	}
	if info, err := os.Stat(collision); err != nil || !info.IsDir() {
		t.Fatalf("collision directory was changed: info=%v err=%v", info, err)
	}
}

func createArtifact(
	t *testing.T, nfs, revision, content string, created time.Time,
) domain.ArtifactSummary {
	t.Helper()
	relative := filepath.Join("huggingface", "owner", "model", revision)
	root := filepath.Join(nfs, relative)
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	modelPath := filepath.Join(root, "model.bin")
	if err := os.WriteFile(modelPath, []byte(content), 0o444); err != nil {
		t.Fatal(err)
	}
	digest, err := catalog.SHA256File(modelPath)
	if err != nil {
		t.Fatal(err)
	}
	files := []domain.FileEntry{{Path: "model.bin", Size: int64(len(content)), SHA256: digest}}
	source := domain.SourceReference{
		Provider: "huggingface", ID: "owner/model", RequestedRevision: "main", ResolvedRevision: revision,
	}
	artifactID := source.Provider + ":" + base64.RawURLEncoding.EncodeToString([]byte(source.ID)) + ":" +
		base64.RawURLEncoding.EncodeToString([]byte(revision))
	manifest := domain.ArtifactManifest{
		SchemaVersion: 1, ArtifactID: artifactID, Name: "model", Version: revision,
		Source: source, ContentSHA256: catalog.ContentDigest(files), CreatedAt: created,
		TotalSize: int64(len(content)), FileCount: 1, Files: files,
	}
	metadata := filepath.Join(root, ".modelshelf")
	if err := os.Mkdir(metadata, 0o755); err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(metadata, "manifest.json"), data, 0o444); err != nil {
		t.Fatal(err)
	}
	return domain.ArtifactSummary{
		ArtifactID: artifactID, Name: "model", Version: revision, Provider: source.Provider,
		SourceID: source.ID, RequestedRevision: "main", ResolvedRevision: revision,
		TotalSize: int64(len(content)), FileCount: 1, CreatedAt: created,
		RelativePath: filepath.ToSlash(relative),
	}
}

func createArtifactFiles(
	t *testing.T, nfs, revision string, contents map[string]string, created time.Time,
) domain.ArtifactSummary {
	t.Helper()
	relative := filepath.Join("huggingface", "owner", "model", revision)
	root := filepath.Join(nfs, relative)
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	files := make([]domain.FileEntry, 0, len(contents))
	var total int64
	for path, content := range contents {
		candidate := filepath.Join(root, filepath.FromSlash(path))
		if err := os.MkdirAll(filepath.Dir(candidate), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(candidate, []byte(content), 0o444); err != nil {
			t.Fatal(err)
		}
		digest, err := catalog.SHA256File(candidate)
		if err != nil {
			t.Fatal(err)
		}
		size := int64(len(content))
		total += size
		files = append(files, domain.FileEntry{Path: path, Size: size, SHA256: digest})
	}
	sort.Slice(files, func(left, right int) bool { return files[left].Path < files[right].Path })
	source := domain.SourceReference{
		Provider: "huggingface", ID: "owner/model", RequestedRevision: "main", ResolvedRevision: revision,
	}
	artifactID := source.Provider + ":" + base64.RawURLEncoding.EncodeToString([]byte(source.ID)) + ":" +
		base64.RawURLEncoding.EncodeToString([]byte(revision))
	manifest := domain.ArtifactManifest{
		SchemaVersion: 1, ArtifactID: artifactID, Name: "model", Version: revision,
		Source: source, ContentSHA256: catalog.ContentDigest(files), CreatedAt: created,
		TotalSize: total, FileCount: len(files), Files: files,
	}
	metadata := filepath.Join(root, ".modelshelf")
	if err := os.Mkdir(metadata, 0o755); err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(metadata, "manifest.json"), data, 0o444); err != nil {
		t.Fatal(err)
	}
	return domain.ArtifactSummary{
		ArtifactID: artifactID, Name: "model", Version: revision, Provider: source.Provider,
		SourceID: source.ID, RequestedRevision: "main", ResolvedRevision: revision,
		TotalSize: total, FileCount: len(files), CreatedAt: created,
		RelativePath: filepath.ToSlash(relative),
	}
}

func assertSyncedContent(t *testing.T, destination, content, artifactID string) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(destination, "model.bin"))
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != content {
		t.Fatalf("content = %q", data)
	}
	failures, err := catalog.Verify(destination, catalog.VerifyOptions{Full: true, Unexpected: true})
	if err != nil || len(failures) != 0 {
		t.Fatalf("verify failures=%v err=%v", failures, err)
	}
	if readSyncState(t, destination)["artifactId"] != artifactID {
		t.Fatal("sync state artifact id mismatch")
	}
}

func readSyncState(t *testing.T, destination string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(destination, ".modelshelf", "sync.json"))
	if err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatal(err)
	}
	return result
}
