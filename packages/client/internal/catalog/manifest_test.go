package catalog

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mr5/modelshelf/client/internal/domain"
)

func TestVerifyQuickFullAndUnexpected(t *testing.T) {
	root := t.TempDir()
	modelPath := filepath.Join(root, "nested", "model.bin")
	if err := os.MkdirAll(filepath.Dir(modelPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(modelPath, []byte("weights"), 0o644); err != nil {
		t.Fatal(err)
	}
	digest, err := SHA256File(modelPath)
	if err != nil {
		t.Fatal(err)
	}
	entry := domain.FileEntry{Path: "nested/model.bin", Size: 7, SHA256: digest}
	manifest := validManifest([]domain.FileEntry{entry})
	writeManifest(t, root, manifest)

	assertNoFailures(t, root, VerifyOptions{})
	assertNoFailures(t, root, VerifyOptions{Full: true, Unexpected: true})

	if err := os.WriteFile(modelPath, []byte("WEIGHTS"), 0o644); err != nil {
		t.Fatal(err)
	}
	assertNoFailures(t, root, VerifyOptions{})
	failures, err := Verify(root, VerifyOptions{Full: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(failures) != 1 || failures[0] != "sha256: nested/model.bin" {
		t.Fatalf("unexpected full failures: %#v", failures)
	}
	if err := os.WriteFile(filepath.Join(root, "extra"), []byte("extra"), 0o644); err != nil {
		t.Fatal(err)
	}
	failures, err = Verify(root, VerifyOptions{Unexpected: true})
	if err != nil {
		t.Fatal(err)
	}
	if !contains(failures, "unexpected: extra") {
		t.Fatalf("unexpected file was not reported: %#v", failures)
	}
}

func TestValidateManifestRejectsUnsafeAndDuplicatePaths(t *testing.T) {
	digest := strings.Repeat("a", 64)
	for _, candidate := range []string{"../escape", "/absolute", "a\\b", "a//b", "./a"} {
		manifest := validManifest([]domain.FileEntry{{Path: candidate, Size: 1, SHA256: digest}})
		if err := ValidateManifest(manifest); err == nil {
			t.Fatalf("unsafe path %q was accepted", candidate)
		}
	}
	manifest := validManifest([]domain.FileEntry{
		{Path: "same", Size: 1, SHA256: digest},
		{Path: "same", Size: 1, SHA256: digest},
	})
	if err := ValidateManifest(manifest); err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("duplicate path result: %v", err)
	}
}

func TestValidateManifestRejectsFutureSchemaVersion(t *testing.T) {
	manifest := validManifest([]domain.FileEntry{})
	manifest.SchemaVersion = CurrentManifestSchemaVersion + 1
	if err := ValidateManifest(manifest); err == nil || !strings.Contains(err.Error(), "upgrade ModelShelf") {
		t.Fatalf("future manifest result: %v", err)
	}
}

func TestVerifyAcceptsSelectedArtifactIdentity(t *testing.T) {
	root := t.TempDir()
	modelPath := filepath.Join(root, "model.gguf")
	if err := os.WriteFile(modelPath, []byte("weights"), 0o444); err != nil {
		t.Fatal(err)
	}
	digest, _ := SHA256File(modelPath)
	manifest := validManifest([]domain.FileEntry{{Path: "model.gguf", Size: 7, SHA256: digest}})
	manifest.Source.SelectedPaths = []string{"model.gguf"}
	manifest.ArtifactID += ":files:" + domain.SelectionDigest(manifest.Source.SelectedPaths)
	writeManifest(t, root, manifest)
	assertNoFailures(t, root, VerifyOptions{Full: true})
}

func TestVerifyRejectsExpectedSymlink(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "target")
	if err := os.WriteFile(target, []byte("weights"), 0o644); err != nil {
		t.Fatal(err)
	}
	digest, _ := SHA256File(target)
	if err := os.Symlink(target, filepath.Join(root, "model.bin")); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	manifest := validManifest([]domain.FileEntry{{Path: "model.bin", Size: 7, SHA256: digest}})
	writeManifest(t, root, manifest)
	failures, err := Verify(root, VerifyOptions{Full: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(failures) != 1 || !strings.HasPrefix(failures[0], "type:") {
		t.Fatalf("symlink was not rejected: %#v", failures)
	}
}

func validManifest(files []domain.FileEntry) domain.ArtifactManifest {
	source := domain.SourceReference{
		Provider:          domain.ProviderHuggingFace,
		ID:                "owner/model",
		RequestedRevision: "main",
		ResolvedRevision:  "abc123",
	}
	var total int64
	for _, entry := range files {
		total += entry.Size
	}
	return domain.ArtifactManifest{
		SchemaVersion: 1,
		ArtifactID: source.Provider + ":" + encodeSegment(source.ID) + ":" +
			encodeSegment(source.ResolvedRevision),
		Name:          "model",
		Version:       "abc123",
		Source:        source,
		ContentSHA256: ContentDigest(files),
		CreatedAt:     time.Now().UTC(),
		TotalSize:     total,
		FileCount:     len(files),
		Files:         files,
	}
}

func writeManifest(t *testing.T, root string, manifest domain.ArtifactManifest) {
	t.Helper()
	directory := filepath.Join(root, ".modelshelf")
	if err := os.MkdirAll(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, "manifest.json"), data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func assertNoFailures(t *testing.T, root string, options VerifyOptions) {
	t.Helper()
	failures, err := Verify(root, options)
	if err != nil {
		t.Fatal(err)
	}
	if len(failures) != 0 {
		t.Fatalf("verification failures: %#v", failures)
	}
}

func contains(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}
