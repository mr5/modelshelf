package syncer

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mr5/modelshelf/client/internal/catalog"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/domain"
	"github.com/mr5/modelshelf/client/internal/mount"
)

func TestPeerFailuresAndExplicitFallback(t *testing.T) {
	for _, failure := range []string{"artifact-missing", "file-missing", "corrupt", "manifest-corrupt", "unavailable", "permission"} {
		for _, fallback := range []bool{false, true} {
			t.Run(failure+"/"+map[bool]string{false: "strict", true: "fallback"}[fallback], func(t *testing.T) {
				root := t.TempDir()
				c := config.Config{ServerURL: "http://metadata.test", NFSLocalPath: filepath.Join(root, "peer"), LocalBasePath: filepath.Join(root, "local"), Upstream: &config.Upstream{Host: "peer", Fallback: fallback}}
				a := createArtifact(t, config.FallbackConfig(c).NFSLocalPath, "commit-one", "correct", time.Now())
				if failure != "artifact-missing" {
					createArtifact(t, c.NFSLocalPath, "commit-one", "correct", time.Now())
				}
				path := filepath.Join(c.NFSLocalPath, a.RelativePath, "model.bin")
				switch failure {
				case "file-missing":
					if err := os.Remove(path); err != nil {
						t.Fatal(err)
					}
				case "corrupt":
					_ = os.Chmod(path, 0644)
					if err := os.WriteFile(path, []byte("corrupt"), 0444); err != nil {
						t.Fatal(err)
					}
				case "manifest-corrupt":
					path = filepath.Join(c.NFSLocalPath, a.RelativePath, catalog.ManifestPath)
					_ = os.Chmod(path, 0644)
					if err := os.WriteFile(path, []byte("{}"), 0444); err != nil {
						t.Fatal(err)
					}
				}
				checks := []string{}
				check := func(candidate config.Config) error {
					checks = append(checks, candidate.NFSLocalPath)
					if candidate.Upstream != nil {
						if failure == "unavailable" {
							return errors.New("connection timed out")
						}
						if failure == "permission" {
							return os.ErrPermission
						}
					}
					return nil
				}
				var log bytes.Buffer
				_, err := syncConfigured(context.Background(), c, domain.DesiredModel{}, a, &log, check)
				if !fallback {
					if err == nil || !strings.Contains(err.Error(), "fallback disabled") {
						t.Fatalf("expected strict error, got %v", err)
					}
					if len(checks) != 1 || log.Len() != 0 {
						t.Fatalf("strict sync tried fallback: %v %s", checks, log.String())
					}
					if _, err := os.Stat(filepath.Join(config.ArtifactStoreRoot(c), a.RelativePath)); !os.IsNotExist(err) {
						t.Fatal("failed artifact published")
					}
					return
				}
				if err != nil {
					t.Fatal(err)
				}
				if len(checks) != 2 || !strings.Contains(log.String(), "explicitly enabled fallback") {
					t.Fatalf("fallback not recorded: %v %s", checks, log.String())
				}
				destination, _ := config.ArtifactPath(c, a.RelativePath)
				failures, err := catalog.Verify(destination, catalog.VerifyOptions{Full: true})
				if err != nil || len(failures) > 0 {
					t.Fatalf("bad fallback: %v %v", failures, err)
				}
				if readSyncState(t, destination)["sourcePath"] != filepath.Join(config.FallbackConfig(c).NFSLocalPath, a.RelativePath) {
					t.Fatal("source was not recorded")
				}
			})
		}
	}
}

func TestLocalWriteFailureAndCancellationNeverFallback(t *testing.T) {
	root := t.TempDir()
	c := config.Config{NFSLocalPath: filepath.Join(root, "peer"), LocalBasePath: filepath.Join(root, "local"), Upstream: &config.Upstream{Host: "peer", Fallback: true}}
	a := createArtifact(t, c.NFSLocalPath, "commit-one", "good", time.Now())
	if err := os.WriteFile(c.LocalBasePath, []byte("not a directory"), 0600); err != nil {
		t.Fatal(err)
	}
	calls := 0
	check := func(config.Config) error { calls++; return nil }
	var log bytes.Buffer
	if _, err := syncConfigured(context.Background(), c, domain.DesiredModel{}, a, &log, check); err == nil {
		t.Fatal("expected local write error")
	}
	if calls != 1 || log.Len() != 0 {
		t.Fatal("local write failure fell back")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := syncConfigured(ctx, c, domain.DesiredModel{}, a, &log, check); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation: %v", err)
	}
	if calls != 1 {
		t.Fatal("cancelled sync accessed upstream")
	}
}

func TestDistributionPublishesOnlyVerifiedFilesAndRetainsOldRevisions(t *testing.T) {
	root := t.TempDir()
	c := config.Config{NFSLocalPath: filepath.Join(root, "central"), LocalBasePath: filepath.Join(root, "local"), Distribution: &config.Distribution{Enabled: true, Allow: []string{"192.168.100.0/24"}}}
	a := createArtifact(t, c.NFSLocalPath, "one", "model-one", time.Now())
	var log bytes.Buffer
	if _, err := syncConfigured(context.Background(), c, domain.DesiredModel{}, a, &log, func(config.Config) error { return nil }); err != nil {
		t.Fatal(err)
	}
	source, _ := config.ArtifactPath(c, a.RelativePath)
	target := filepath.Join(config.PublishedRoot(c), a.RelativePath)
	original, _ := os.Stat(filepath.Join(source, "model.bin"))
	published, _ := os.Stat(filepath.Join(target, "model.bin"))
	if !os.SameFile(original, published) {
		t.Fatal("publication duplicated model bytes")
	}
	metadata, err := os.ReadDir(filepath.Join(target, ".modelshelf"))
	if err != nil || len(metadata) != 1 || metadata[0].Name() != "manifest.json" {
		t.Fatalf("export leaks metadata: %v %v", metadata, err)
	}
	info, _ := os.Stat(filepath.Join(target, catalog.ManifestPath))
	if info.Mode().Perm() != 0444 {
		t.Fatalf("manifest not remotely readable: %v", info.Mode())
	}
	before, _ := os.Stat(target)
	// A satisfied local state must not require a live upstream; publication is idempotent.
	if err := RemoveTree(c.NFSLocalPath); err != nil {
		t.Fatal(err)
	}
	if _, err := syncConfigured(context.Background(), c, domain.DesiredModel{}, a, &log, func(config.Config) error { t.Fatal("accessed upstream"); return nil }); err != nil {
		t.Fatal(err)
	}
	after, _ := os.Stat(target)
	if !os.SameFile(before, after) {
		t.Fatal("idempotent publication replaced directory")
	}
	b := createArtifact(t, c.NFSLocalPath, "two", "model-two", time.Now())
	if _, err := syncConfigured(context.Background(), c, domain.DesiredModel{}, b, &log, func(config.Config) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if err := RemoveTree(source); err != nil {
		t.Fatal(err)
	}
	if failures, err := catalog.Verify(target, catalog.VerifyOptions{Full: true}); err != nil || len(failures) > 0 {
		t.Fatalf("old export lost after local deletion: %v %v", failures, err)
	}
}

func TestInvalidMountNeverFallsBackAndLocalReadyDoesNotCheckPeer(t *testing.T) {
	root := t.TempDir()
	c := config.Config{NFSLocalPath: filepath.Join(root, "peer"), LocalBasePath: filepath.Join(root, "local"), Upstream: &config.Upstream{Host: "peer", Fallback: true}}
	a := createArtifact(t, c.NFSLocalPath, "one", "correct", time.Now())
	calls := 0
	check := func(config.Config) error { calls++; return &mount.InvalidMountError{Message: "wrong mounted source"} }
	var log bytes.Buffer
	if _, err := syncConfigured(context.Background(), c, domain.DesiredModel{}, a, &log, check); err == nil {
		t.Fatal("accepted wrong mount")
	}
	if calls != 1 || log.Len() != 0 {
		t.Fatal("invalid mount triggered fallback")
	}
	if _, err := SyncArtifact(context.Background(), c, domain.DesiredModel{}, a); err != nil {
		t.Fatal(err)
	}
	if err := RemoveTree(c.NFSLocalPath); err != nil {
		t.Fatal(err)
	}
	if _, err := syncConfigured(context.Background(), c, domain.DesiredModel{}, a, &log, check); err != nil {
		t.Fatalf("ready local model required peer: %v", err)
	}
	if calls != 1 {
		t.Fatal("ready model inspected upstream")
	}
	collision := a
	collision.ArtifactID = "different-artifact"
	if _, err := syncConfigured(context.Background(), c, domain.DesiredModel{}, collision, &log, check); err == nil || !strings.Contains(err.Error(), "canonical path collision") {
		t.Fatalf("local collision: %v", err)
	}
	if calls != 1 {
		t.Fatal("local collision accessed upstream")
	}

}

func TestCorruptedReusableFileIsCopiedAgain(t *testing.T) {
	root := t.TempDir()
	c := config.Config{NFSLocalPath: filepath.Join(root, "peer"), LocalBasePath: filepath.Join(root, "local")}
	a := createArtifact(t, c.NFSLocalPath, "one", "correct", time.Now())
	if _, err := SyncArtifact(context.Background(), c, domain.DesiredModel{}, a); err != nil {
		t.Fatal(err)
	}
	old, _ := config.ArtifactPath(c, a.RelativePath)
	file := filepath.Join(old, "model.bin")
	if err := os.Chmod(file, 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(file, []byte("corrupt"), 0444); err != nil {
		t.Fatal(err)
	}
	b := createArtifact(t, c.NFSLocalPath, "two", "correct", time.Now())
	if _, err := SyncArtifact(context.Background(), c, domain.DesiredModel{}, b); err != nil {
		t.Fatal(err)
	}
	next, _ := config.ArtifactPath(c, b.RelativePath)
	failures, err := catalog.Verify(next, catalog.VerifyOptions{Full: true})
	if err != nil || len(failures) > 0 {
		t.Fatalf("reused corrupted data: %v %v", failures, err)
	}
}
