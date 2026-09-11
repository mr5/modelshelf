package syncer

import (
	"context"
	"errors"
	"fmt"
	"io"
	"path/filepath"

	"github.com/mr5/modelshelf/client/internal/api"
	"github.com/mr5/modelshelf/client/internal/catalog"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/domain"
	"github.com/mr5/modelshelf/client/internal/mount"
)

// SourceError distinguishes remote failures from local writes and cancellation.
type SourceError struct{ Err error }

func (e *SourceError) Error() string { return e.Err.Error() }
func (e *SourceError) Unwrap() error { return e.Err }
func sourceError(err error) error    { return &SourceError{Err: err} }

type sourceReader struct {
	io.Reader
	ctx context.Context
}

func (r sourceReader) Read(p []byte) (int, error) {
	if r.ctx != nil && r.ctx.Err() != nil {
		return 0, r.ctx.Err()
	}
	n, err := r.Reader.Read(p)
	if err != nil && err != io.EOF {
		err = sourceError(err)
	}
	return n, err
}

func SyncConfigured(ctx context.Context, c config.Config, client *api.Client, desired domain.DesiredModel, artifact domain.ArtifactSummary, output io.Writer) (domain.ArtifactSummary, error) {
	return syncConfigured(ctx, c, desired, artifact, output, func(c config.Config) error { return mount.CheckSource(ctx, c, client) })
}

func syncConfigured(ctx context.Context, c config.Config, desired domain.DesiredModel, artifact domain.ArtifactSummary, output io.Writer, check func(config.Config) error) (domain.ArtifactSummary, error) {
	if err := ctx.Err(); err != nil {
		return domain.ArtifactSummary{}, err
	}
	destination, err := config.ArtifactPath(c, artifact.RelativePath)
	if err != nil {
		return domain.ArtifactSummary{}, err
	}
	localReady := false
	if m, e := catalog.ReadManifest(destination); e == nil {
		if m.ArtifactID != artifact.ArtifactID {
			return domain.ArtifactSummary{}, fmt.Errorf("canonical path collision at %s: contains %s, expected %s", destination, m.ArtifactID, artifact.ArtifactID)
		}
		failures, e := catalog.Verify(destination, catalog.VerifyOptions{Full: true})
		localReady = e == nil && len(failures) == 0
	}
	if localReady {
		desired.ResolvedRevision = artifact.ResolvedRevision
		desired.ArtifactID = artifact.ArtifactID
		desired.RelativePath = artifact.RelativePath
		if err := EnsureReferences(c, desired, destination); err != nil {
			return domain.ArtifactSummary{}, err
		}
		if c.Distribution != nil && c.Distribution.Enabled {
			if err := PublishDistribution(ctx, c, artifact); err != nil {
				return domain.ArtifactSummary{}, err
			}
		}
		return artifact, nil
	}
	attempt := func(candidate config.Config) (domain.ArtifactSummary, error) {
		if !localReady && candidate.Upstream != nil {
			if err := check(candidate); err != nil {
				var invalid *mount.InvalidMountError
				if errors.As(err, &invalid) {
					return domain.ArtifactSummary{}, err
				}
				return domain.ArtifactSummary{}, sourceError(err)
			}
		}
		return SyncArtifact(ctx, candidate, desired, artifact)
	}
	result, err := attempt(c)
	var remote *SourceError
	if err != nil && c.Upstream != nil {
		if ctx.Err() != nil {
			return domain.ArtifactSummary{}, ctx.Err()
		}
		if !c.Upstream.Fallback {
			return domain.ArtifactSummary{}, fmt.Errorf("upstream %s failed (server fallback disabled): %w", c.Upstream.Host, err)
		}
		if errors.As(err, &remote) {
			fmt.Fprintf(output, "Upstream %s failed: %v; explicitly enabled fallback to server %s for artifact %s\n", c.Upstream.Host, err, c.ServerURL, artifact.ArtifactID)
			fallback := config.FallbackConfig(c)
			if e := check(fallback); e != nil {
				return domain.ArtifactSummary{}, fmt.Errorf("server fallback unavailable: %w", e)
			}
			result, err = SyncArtifact(ctx, fallback, desired, artifact)
			if err == nil {
				fmt.Fprintf(output, "Synced %s from server fallback %s\n", artifact.ArtifactID, c.ServerURL)
			}
		}
	}
	if err != nil {
		return domain.ArtifactSummary{}, err
	}
	if c.Distribution != nil && c.Distribution.Enabled {
		if err := PublishDistribution(ctx, c, artifact); err != nil {
			return domain.ArtifactSummary{}, fmt.Errorf("model is local but distribution publication failed: %w", err)
		}
		fmt.Fprintf(output, "Published %s at %s\n", artifact.ArtifactID, filepath.Join(config.PublishedRoot(c), artifact.RelativePath))
	}
	return result, nil
}
