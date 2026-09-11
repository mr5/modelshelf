package syncer

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/mr5/modelshelf/client/internal/catalog"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/domain"
)

// PublishDistribution constructs an isolated immutable view using hard links.
// Temporary directories are outside the exported tree; old revisions are retained.
func PublishDistribution(ctx context.Context, c config.Config, a domain.ArtifactSummary) error {
	source, err := config.ArtifactPath(c, a.RelativePath)
	if err != nil {
		return err
	}
	manifest, err := catalog.ReadManifest(source)
	if err != nil {
		return err
	}
	if manifest.ArtifactID != a.ArtifactID {
		return fmt.Errorf("distribution artifact identity mismatch")
	}
	failures, err := catalog.Verify(source, catalog.VerifyOptions{Full: true})
	if err != nil {
		return err
	}
	if len(failures) > 0 {
		return fmt.Errorf("distribution verification failed: %s", strings.Join(failures, "; "))
	}
	destination := filepath.Join(config.PublishedRoot(c), a.RelativePath)
	if current, e := catalog.ReadManifest(destination); e == nil && current.ArtifactID == manifest.ArtifactID && current.ContentSHA256 == manifest.ContentSHA256 {
		failures, e := catalog.Verify(destination, catalog.VerifyOptions{Full: true, Unexpected: true})
		if e == nil && len(failures) == 0 {
			return nil
		}
	}
	root := config.DistributionRoot(c)
	if err := config.EnsureDistributionLayout(c); err != nil {
		return err
	}
	stage, err := os.MkdirTemp(filepath.Join(root, "staging"), "publish-")
	if err != nil {
		return err
	}
	defer RemoveTree(stage)
	for _, entry := range manifest.Files {
		if err := ctx.Err(); err != nil {
			return err
		}
		for _, part := range strings.Split(entry.Path, "/") {
			if part == ".modelshelf" {
				return fmt.Errorf("reserved manifest path: %s", entry.Path)
			}
		}
		fileInfo, err := os.Stat(filepath.Join(source, filepath.FromSlash(entry.Path)))
		if err != nil {
			return err
		}
		if fileInfo.Mode().Perm()&0004 == 0 {
			return fmt.Errorf("model file must be readable by NFS clients before publication: %s", entry.Path)
		}
		target := filepath.Join(stage, filepath.FromSlash(entry.Path))
		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			return err
		}
		if err := os.Link(filepath.Join(source, filepath.FromSlash(entry.Path)), target); err != nil {
			return fmt.Errorf("publish hard link (distribution and models must share a filesystem): %w", err)
		}
	}
	if err := os.MkdirAll(filepath.Join(stage, ".modelshelf"), 0755); err != nil {
		return err
	}
	if err := writeAtomicJSON(filepath.Join(stage, catalog.ManifestPath), manifest); err != nil {
		return err
	}
	if err := os.Chmod(filepath.Join(stage, catalog.ManifestPath), 0444); err != nil {
		return err
	}
	if err := filepath.WalkDir(stage, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return os.Chmod(path, 0755)
		}
		return nil
	}); err != nil {
		return err
	}
	destination = filepath.Join(config.PublishedRoot(c), a.RelativePath)
	if err := os.MkdirAll(filepath.Dir(destination), 0755); err != nil {
		return err
	}
	for parent := filepath.Dir(destination); ; parent = filepath.Dir(parent) {
		if err := os.Chmod(parent, 0755); err != nil {
			return err
		}
		if parent == config.PublishedRoot(c) {
			break
		}
	}
	return AtomicPublish(stage, destination)
}
