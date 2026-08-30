package catalog

import (
	"bufio"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/mr5/modelshelf/client/internal/domain"
)

const ManifestPath = ".modelshelf/manifest.json"
const CurrentManifestSchemaVersion = 2

type VerifyOptions struct {
	Full       bool
	Unexpected bool
}

func ReadManifest(root string) (domain.ArtifactManifest, error) {
	manifestPath := filepath.Join(root, filepath.FromSlash(ManifestPath))
	file, err := os.Open(manifestPath)
	if err != nil {
		return domain.ArtifactManifest{}, fmt.Errorf("read manifest: %w", err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return domain.ArtifactManifest{}, fmt.Errorf("inspect manifest: %w", err)
	}
	if info.Size() > 64*1024*1024 {
		return domain.ArtifactManifest{}, errors.New("manifest exceeds the 64 MiB safety limit")
	}
	decoder := json.NewDecoder(io.LimitReader(file, 64*1024*1024))
	decoder.DisallowUnknownFields()
	var manifest domain.ArtifactManifest
	if err := decoder.Decode(&manifest); err != nil {
		return domain.ArtifactManifest{}, fmt.Errorf("invalid manifest JSON: %w", err)
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return domain.ArtifactManifest{}, errors.New("invalid manifest JSON: trailing data")
	}
	if err := ValidateManifest(manifest); err != nil {
		return domain.ArtifactManifest{}, err
	}
	return manifest, nil
}

func ValidateManifest(manifest domain.ArtifactManifest) error {
	if manifest.SchemaVersion < 1 || manifest.SchemaVersion > CurrentManifestSchemaVersion {
		if manifest.SchemaVersion > CurrentManifestSchemaVersion {
			return fmt.Errorf(
				"manifest schemaVersion %d is newer than supported version %d; upgrade ModelShelf",
				manifest.SchemaVersion, CurrentManifestSchemaVersion,
			)
		}
		return fmt.Errorf(
			"unsupported manifest schemaVersion %d (supported: %d)",
			manifest.SchemaVersion, CurrentManifestSchemaVersion,
		)
	}
	if manifest.ArtifactID == "" || manifest.Name == "" || manifest.Version == "" {
		return errors.New("manifest artifactId, name, and version are required")
	}
	if !domain.ValidProvider(manifest.Source.Provider) {
		return fmt.Errorf("manifest has invalid provider %q", manifest.Source.Provider)
	}
	if manifest.Source.ID == "" || manifest.Source.RequestedRevision == "" ||
		manifest.Source.ResolvedRevision == "" {
		return errors.New("manifest source id and revisions are required")
	}
	if !validSHA256(manifest.ContentSHA256) {
		return errors.New("manifest contentSha256 is invalid")
	}
	if manifest.TotalSize < 0 || manifest.FileCount < 0 {
		return errors.New("manifest size and file count cannot be negative")
	}
	if manifest.CreatedAt.IsZero() || manifest.Files == nil {
		return errors.New("manifest createdAt and files are required")
	}
	seen := make(map[string]struct{}, len(manifest.Files))
	for _, entry := range manifest.Files {
		if err := validateManifestPath(entry.Path); err != nil {
			return err
		}
		if entry.Size < 0 || !validSHA256(entry.SHA256) {
			return fmt.Errorf("manifest file %q has invalid size or SHA-256", entry.Path)
		}
		if _, ok := seen[entry.Path]; ok {
			return fmt.Errorf("manifest contains duplicate path %q", entry.Path)
		}
		seen[entry.Path] = struct{}{}
	}
	if manifest.Source.SelectedPaths != nil {
		selected := domain.CanonicalFiles(manifest.Source.SelectedPaths)
		if len(selected) == 0 {
			return errors.New("manifest source selectedPaths cannot be empty")
		}
		if len(selected) != len(seen) {
			return errors.New("manifest selectedPaths does not match files")
		}
		for _, selectedPath := range selected {
			if _, ok := seen[selectedPath]; !ok {
				return errors.New("manifest selectedPaths does not match files")
			}
		}
	}
	return nil
}

func Verify(root string, options VerifyOptions) ([]string, error) {
	manifest, err := ReadManifest(root)
	if err != nil {
		return nil, err
	}
	failures := []string{}
	expected := make(map[string]struct{}, len(manifest.Files))
	for _, entry := range manifest.Files {
		expected[entry.Path] = struct{}{}
		candidate := filepath.Join(root, filepath.FromSlash(entry.Path))
		info, err := os.Lstat(candidate)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				failures = append(failures, "missing: "+entry.Path)
				continue
			}
			return nil, fmt.Errorf("inspect %s: %w", entry.Path, err)
		}
		if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
			failures = append(failures, "type: "+entry.Path+" is not a regular file")
			continue
		}
		if info.Size() != entry.Size {
			failures = append(failures, fmt.Sprintf(
				"size: %s expected=%d actual=%d", entry.Path, entry.Size, info.Size(),
			))
			continue
		}
		if options.Full {
			digest, err := SHA256File(candidate)
			if err != nil {
				return nil, err
			}
			if digest != entry.SHA256 {
				failures = append(failures, "sha256: "+entry.Path)
			}
		}
	}
	if manifest.FileCount != len(manifest.Files) {
		failures = append(failures, "manifest fileCount does not match files length")
	}
	var total int64
	for _, entry := range manifest.Files {
		if entry.Size > math.MaxInt64-total {
			failures = append(failures, "manifest totalSize overflows int64")
			total = -1
			break
		}
		total += entry.Size
	}
	if manifest.TotalSize != total {
		failures = append(failures, "manifest totalSize does not match file sizes")
	}
	if ContentDigest(manifest.Files) != manifest.ContentSHA256 {
		failures = append(failures, "manifest contentSha256 does not match file entries")
	}
	expectedID := manifest.Source.Provider + ":" + encodeSegment(manifest.Source.ID) + ":" +
		encodeSegment(manifest.Source.ResolvedRevision)
	if selectionDigest := domain.SelectionDigest(manifest.Source.SelectedPaths); selectionDigest != "" {
		expectedID += ":files:" + selectionDigest
	}
	if manifest.ArtifactID != expectedID {
		failures = append(failures, "manifest artifactId does not match source identity")
	}
	if options.Unexpected {
		actual, inventoryFailures, err := InventoryPaths(root)
		if err != nil {
			return nil, err
		}
		failures = append(failures, inventoryFailures...)
		for _, actualPath := range actual {
			if _, ok := expected[actualPath]; !ok {
				failures = append(failures, "unexpected: "+actualPath)
			}
		}
	}
	return failures, nil
}

func InventoryPaths(root string) ([]string, []string, error) {
	actual := []string{}
	failures := []string{}
	err := filepath.WalkDir(root, func(candidate string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(root, candidate)
		if err != nil {
			return err
		}
		if relative == "." {
			return nil
		}
		if entry.IsDir() && filepath.Base(candidate) == ".modelshelf" {
			return filepath.SkipDir
		}
		if entry.Type()&os.ModeSymlink != 0 {
			failures = append(failures, "type: "+filepath.ToSlash(relative)+" is a symbolic link")
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if !entry.IsDir() {
			info, err := entry.Info()
			if err != nil {
				return err
			}
			if !info.Mode().IsRegular() {
				failures = append(failures, "type: "+filepath.ToSlash(relative)+" is not regular")
				return nil
			}
			actual = append(actual, filepath.ToSlash(relative))
		}
		return nil
	})
	sort.Strings(actual)
	sort.Strings(failures)
	return actual, failures, err
}

func SHA256File(candidate string) (string, error) {
	file, err := os.Open(candidate)
	if err != nil {
		return "", fmt.Errorf("open %s: %w", candidate, err)
	}
	defer file.Close()
	digest := sha256.New()
	buffered := bufio.NewReaderSize(file, 4*1024*1024)
	if _, err := io.Copy(digest, buffered); err != nil {
		return "", fmt.Errorf("hash %s: %w", candidate, err)
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func ContentDigest(files []domain.FileEntry) string {
	ordered := append([]domain.FileEntry(nil), files...)
	sort.Slice(ordered, func(left, right int) bool { return ordered[left].Path < ordered[right].Path })
	digest := sha256.New()
	for _, entry := range ordered {
		io.WriteString(digest, entry.Path)
		digest.Write([]byte{0})
		io.WriteString(digest, strconv.FormatInt(entry.Size, 10))
		digest.Write([]byte{0})
		io.WriteString(digest, entry.SHA256)
		digest.Write([]byte{'\n'})
	}
	return hex.EncodeToString(digest.Sum(nil))
}

func validateManifestPath(value string) error {
	if value == "" || value == "." || strings.Contains(value, "\\") || path.IsAbs(value) ||
		path.Clean(value) != value {
		return fmt.Errorf("manifest path must be a safe POSIX relative path: %q", value)
	}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." || part == ".." {
			return fmt.Errorf("manifest path must be a safe POSIX relative path: %q", value)
		}
	}
	return nil
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == sha256.Size && value == strings.ToLower(value)
}

func encodeSegment(value string) string {
	return base64.RawURLEncoding.EncodeToString([]byte(value))
}
