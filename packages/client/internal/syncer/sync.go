package syncer

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/mr5/modelshelf/client/internal/api"
	"github.com/mr5/modelshelf/client/internal/catalog"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/domain"
)

type directoryMode struct {
	path string
	mode os.FileMode
}

type copyJob struct {
	source      string
	destination string
	mode        os.FileMode
}

func SelectArtifact(
	artifacts []domain.ArtifactSummary, desired domain.DesiredModel,
) *domain.ArtifactSummary {
	var selected *domain.ArtifactSummary
	for index := range artifacts {
		artifact := &artifacts[index]
		if artifact.Provider != desired.Provider || artifact.SourceID != desired.ID {
			continue
		}
		if desired.ResolvedRevision != "" {
			if artifact.ResolvedRevision != desired.ResolvedRevision {
				continue
			}
		} else if artifact.RequestedRevision != desired.RequestedRevision &&
			artifact.ResolvedRevision != desired.RequestedRevision {
			continue
		}
		if artifact.SelectionDigest != domain.SelectionDigest(desired.Files) {
			continue
		}
		if selected == nil || artifact.CreatedAt.After(selected.CreatedAt) {
			selected = artifact
		}
	}
	return selected
}

func One(
	ctx context.Context,
	configuration config.Config,
	client *api.Client,
	desired domain.DesiredModel,
) (domain.ArtifactSummary, error) {
	artifacts, err := client.Artifacts(ctx, "")
	if err != nil {
		return domain.ArtifactSummary{}, err
	}
	artifact := SelectArtifact(artifacts, desired)
	if artifact == nil {
		return domain.ArtifactSummary{}, fmt.Errorf(
			"artifact is not available: %s:%s", desired.Provider, desired.ID,
		)
	}
	return SyncArtifact(ctx, configuration, desired, *artifact)
}

func SyncArtifact(
	ctx context.Context,
	configuration config.Config,
	desired domain.DesiredModel,
	artifact domain.ArtifactSummary,
) (domain.ArtifactSummary, error) {
	desired.ResolvedRevision = artifact.ResolvedRevision
	desired.ArtifactID = artifact.ArtifactID
	desired.RelativePath = artifact.RelativePath
	destination, err := config.ArtifactPath(configuration, artifact.RelativePath)
	if err != nil {
		return domain.ArtifactSummary{}, err
	}
	// A satisfied desired state does not need a live NFS read. This keeps sync
	// idempotent during a transient exporter outage while the HTTP catalog is
	// still available.
	if current, readErr := catalog.ReadManifest(destination); readErr == nil {
		if current.ArtifactID != artifact.ArtifactID {
			return domain.ArtifactSummary{}, fmt.Errorf(
				"canonical path collision at %s: contains %s, expected %s",
				destination, current.ArtifactID, artifact.ArtifactID,
			)
		}
		failures, verifyErr := catalog.Verify(destination, catalog.VerifyOptions{})
		if verifyErr == nil && len(failures) == 0 {
			if err := EnsureReferences(configuration, desired, destination); err != nil {
				return domain.ArtifactSummary{}, err
			}
			return artifact, nil
		}
	}
	relative, err := filepath.Rel(config.ArtifactStoreRoot(configuration), destination)
	if err != nil {
		return domain.ArtifactSummary{}, err
	}
	source := filepath.Join(configuration.NFSLocalPath, relative)
	info, err := os.Stat(source)
	if err != nil || !info.IsDir() {
		if err == nil {
			err = errors.New("not a directory")
		}
		return domain.ArtifactSummary{}, fmt.Errorf("NFS artifact is not visible at %s: %w", source, err)
	}
	stagingParent := config.StagingRoot(configuration)
	if err := os.MkdirAll(stagingParent, 0o755); err != nil {
		return domain.ArtifactSummary{}, fmt.Errorf("create staging directory: %w", err)
	}
	staging, err := os.MkdirTemp(stagingParent, "sync-*")
	if err != nil {
		return domain.ArtifactSummary{}, fmt.Errorf("create staging tree: %w", err)
	}
	defer RemoveTree(staging)
	directories, err := copyTree(ctx, source, staging)
	if err != nil {
		return domain.ArtifactSummary{}, err
	}
	metadataDirectory := filepath.Join(staging, ".modelshelf")
	if err := os.Chmod(metadataDirectory, 0o755); err != nil {
		return domain.ArtifactSummary{}, fmt.Errorf("make metadata directory writable: %w", err)
	}
	syncState := map[string]any{
		"schemaVersion": 1,
		"artifactId":    artifact.ArtifactID,
		"serverUrl":     configuration.ServerURL,
		"syncedAt":      time.Now().UTC().Format(time.RFC3339Nano),
	}
	if err := writeAtomicJSON(filepath.Join(metadataDirectory, "sync.json"), syncState); err != nil {
		return domain.ArtifactSummary{}, err
	}
	failures, err := catalog.Verify(staging, catalog.VerifyOptions{})
	if err != nil {
		return domain.ArtifactSummary{}, err
	}
	if len(failures) != 0 {
		return domain.ArtifactSummary{}, fmt.Errorf(
			"quick verification failed: %s", strings.Join(failures, "; "),
		)
	}
	freezeDirectories(staging, directories)
	if err := AtomicPublish(staging, destination); err != nil {
		return domain.ArtifactSummary{}, err
	}
	if err := EnsureReferences(configuration, desired, destination); err != nil {
		return domain.ArtifactSummary{}, err
	}
	return artifact, nil
}

func EnsureReferences(configuration config.Config, desired domain.DesiredModel, artifactPath string) error {
	references, err := ManagedReferencePaths(configuration, desired, artifactPath)
	if err != nil {
		return err
	}
	for _, reference := range references {
		if err := ensureReference(reference, artifactPath); err != nil {
			return err
		}
	}
	return nil
}

func ReferenceFailures(configuration config.Config, desired domain.DesiredModel, artifactPath string) []string {
	references, err := ManagedReferencePaths(configuration, desired, artifactPath)
	if err != nil {
		return []string{err.Error()}
	}
	failures := []string{}
	for _, reference := range references {
		info, statErr := os.Lstat(reference)
		if statErr != nil {
			failures = append(failures, "missing reference: "+reference)
			continue
		}
		if info.Mode()&os.ModeSymlink == 0 {
			failures = append(failures, "reference is not a symbolic link: "+reference)
			continue
		}
		if !sameFile(reference, artifactPath) {
			failures = append(failures, "reference points to a different artifact: "+reference)
		}
	}
	return failures
}

func ManagedReferencePaths(
	configuration config.Config, desired domain.DesiredModel, artifactPath string,
) ([]string, error) {
	references, err := config.ReferencePaths(configuration, desired)
	if err != nil {
		return nil, err
	}
	revisionReference, exists, err := config.RevisionReferencePath(configuration, desired, artifactPath)
	if err != nil {
		return nil, err
	}
	if exists {
		references = append(references, revisionReference)
	}
	return references, nil
}

// RemoveReferences removes only references owned by this config declaration:
// its alias and optional path. Requested-revision references may be shared by
// multiple declarations and are reconciled separately against the lock file.
func RemoveReferences(configuration config.Config, desired domain.DesiredModel) error {
	references, err := config.ReferencePaths(configuration, desired)
	if err != nil {
		return err
	}
	for _, reference := range references {
		if err := RemoveReference(configuration, reference); err != nil {
			return err
		}
	}
	return nil
}

func RemoveReference(configuration config.Config, reference string) error {
	return removeManagedReference(configuration, reference)
}

func ensureReference(reference, artifactPath string) error {
	reference = filepath.Clean(reference)
	artifactPath = filepath.Clean(artifactPath)
	if reference == artifactPath {
		return errors.New("reference path cannot be the canonical artifact path")
	}
	if info, err := os.Lstat(reference); err == nil {
		if info.Mode()&os.ModeSymlink == 0 {
			return fmt.Errorf("refusing to replace non-symlink reference path %s", reference)
		}
		if sameFile(reference, artifactPath) {
			return nil
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	parent := filepath.Dir(reference)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create reference parent: %w", err)
	}
	temporaryDirectory, err := os.MkdirTemp(parent, ".modelshelf-link-*")
	if err != nil {
		return fmt.Errorf("create temporary reference: %w", err)
	}
	defer os.RemoveAll(temporaryDirectory)
	temporaryReference := filepath.Join(temporaryDirectory, "link")
	relativeTarget, err := filepath.Rel(parent, artifactPath)
	if err != nil {
		return fmt.Errorf("resolve relative reference target: %w", err)
	}
	if err := os.Symlink(relativeTarget, temporaryReference); err != nil {
		return fmt.Errorf("create reference: %w", err)
	}
	if err := os.Rename(temporaryReference, reference); err != nil {
		return fmt.Errorf("atomically publish reference %s: %w", reference, err)
	}
	return syncParent(reference)
}

func sameFile(left, right string) bool {
	leftInfo, leftErr := os.Stat(left)
	rightInfo, rightErr := os.Stat(right)
	return leftErr == nil && rightErr == nil && os.SameFile(leftInfo, rightInfo)
}

func removeManagedReference(configuration config.Config, reference string) error {
	info, err := os.Lstat(reference)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink == 0 {
		return fmt.Errorf("refusing to remove non-symlink reference path %s", reference)
	}
	target, err := os.Readlink(reference)
	if err != nil {
		return fmt.Errorf("read managed reference %s: %w", reference, err)
	}
	if !filepath.IsAbs(target) {
		target = filepath.Join(filepath.Dir(reference), target)
	}
	store := filepath.Clean(config.ArtifactStoreRoot(configuration))
	relative, err := filepath.Rel(store, filepath.Clean(target))
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return fmt.Errorf("refusing to remove unmanaged reference %s", reference)
	}
	if err := os.Remove(reference); err != nil {
		return err
	}
	return syncParent(reference)
}

func copyTree(ctx context.Context, source, destination string) ([]directoryMode, error) {
	directories := []directoryMode{}
	jobs := []copyJob{}
	err := filepath.WalkDir(source, func(candidate string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(source, candidate)
		if err != nil {
			return err
		}
		if relative == "." {
			return nil
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("symbolic links are not allowed in artifacts: %s", relative)
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		target := filepath.Join(destination, relative)
		if entry.IsDir() {
			if err := os.Mkdir(target, 0o755); err != nil && !errors.Is(err, os.ErrExist) {
				return err
			}
			directories = append(directories, directoryMode{path: target, mode: info.Mode().Perm()})
			return nil
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("unsupported artifact file type: %s", relative)
		}
		jobs = append(jobs, copyJob{source: candidate, destination: target, mode: info.Mode().Perm()})
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("scan NFS artifact: %w", err)
	}
	workerCount := min(max(runtime.NumCPU(), 1), 8)
	jobChannel := make(chan copyJob)
	workerContext, cancel := context.WithCancel(ctx)
	defer cancel()
	var waitGroup sync.WaitGroup
	var firstError error
	var errorOnce sync.Once
	for range workerCount {
		waitGroup.Add(1)
		go func() {
			defer waitGroup.Done()
			buffer := make([]byte, 4*1024*1024)
			for job := range jobChannel {
				if workerContext.Err() != nil {
					continue
				}
				if err := copyFile(job, buffer); err != nil {
					errorOnce.Do(func() {
						firstError = err
						cancel()
					})
				}
			}
		}()
	}
sendLoop:
	for _, job := range jobs {
		select {
		case <-workerContext.Done():
			break sendLoop
		case jobChannel <- job:
		}
	}
	close(jobChannel)
	waitGroup.Wait()
	if firstError != nil {
		return nil, firstError
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return directories, nil
}

func copyFile(job copyJob, buffer []byte) error {
	source, err := os.Open(job.source)
	if err != nil {
		return fmt.Errorf("open source %s: %w", job.source, err)
	}
	defer source.Close()
	destination, err := os.OpenFile(job.destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("create destination %s: %w", job.destination, err)
	}
	succeeded := false
	defer func() {
		destination.Close()
		if !succeeded {
			os.Remove(job.destination)
		}
	}()
	if _, err := io.CopyBuffer(destination, source, buffer); err != nil {
		return fmt.Errorf("copy %s: %w", job.source, err)
	}
	if err := destination.Sync(); err != nil {
		return fmt.Errorf("sync %s: %w", job.destination, err)
	}
	if err := destination.Close(); err != nil {
		return err
	}
	if err := os.Chmod(job.destination, job.mode); err != nil {
		return err
	}
	succeeded = true
	return nil
}

func freezeDirectories(root string, directories []directoryMode) {
	sort.Slice(directories, func(left, right int) bool {
		return strings.Count(directories[left].path, string(filepath.Separator)) >
			strings.Count(directories[right].path, string(filepath.Separator))
	})
	for _, directory := range directories {
		if filepath.Clean(directory.path) == filepath.Join(root, ".modelshelf") {
			continue
		}
		_ = os.Chmod(directory.path, directory.mode)
	}
	_ = os.Chmod(root, 0o755)
}

func writeAtomicJSON(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	parent := filepath.Dir(path)
	temporary, err := os.CreateTemp(parent, "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o600); err != nil {
		temporary.Close()
		return err
	}
	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}
	directory, err := os.Open(parent)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func RemoveTree(root string) error {
	if _, err := os.Lstat(root); errors.Is(err, os.ErrNotExist) {
		return nil
	}
	directories := []string{}
	err := filepath.WalkDir(root, func(candidate string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			if errors.Is(walkErr, os.ErrPermission) {
				_ = os.Chmod(filepath.Dir(candidate), 0o700)
				return nil
			}
			return walkErr
		}
		if entry.IsDir() {
			_ = os.Chmod(candidate, 0o700)
			directories = append(directories, candidate)
		} else if entry.Type()&os.ModeSymlink == 0 {
			_ = os.Chmod(candidate, 0o600)
		}
		return nil
	})
	if err != nil {
		return err
	}
	sort.Slice(directories, func(left, right int) bool { return len(directories[left]) > len(directories[right]) })
	return os.RemoveAll(root)
}

func AtomicPublish(staging, destination string) error {
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		return err
	}
	_, err := os.Lstat(destination)
	if errors.Is(err, os.ErrNotExist) {
		if err := os.Rename(staging, destination); err != nil {
			return fmt.Errorf("publish local model: %w", err)
		}
		return syncParent(destination)
	}
	if err != nil {
		return err
	}
	exchanged, err := atomicExchange(staging, destination)
	if err != nil {
		return fmt.Errorf("atomic directory exchange: %w", err)
	}
	if !exchanged {
		return errors.New(
			"this platform/filesystem cannot atomically exchange an existing model directory; " +
				"choose a revision-specific destination or remove the old copy first",
		)
	}
	if err := syncParent(destination); err != nil {
		return err
	}
	return RemoveTree(staging)
}

func syncParent(candidate string) error {
	directory, err := os.Open(filepath.Dir(candidate))
	if err != nil {
		return fmt.Errorf("open publish directory: %w", err)
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return fmt.Errorf("sync publish directory: %w", err)
	}
	return nil
}
