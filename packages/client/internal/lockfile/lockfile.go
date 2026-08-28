package lockfile

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"time"

	"github.com/mr5/modelshelf/client/internal/domain"
	"gopkg.in/yaml.v3"
)

type Model struct {
	Alias            string    `yaml:"alias,omitempty"`
	Provider         string    `yaml:"provider"`
	ID               string    `yaml:"id"`
	Revision         string    `yaml:"revision"`
	Path             string    `yaml:"path,omitempty"`
	ResolvedRevision string    `yaml:"resolvedRevision"`
	ArtifactID       string    `yaml:"artifactId"`
	RelativePath     string    `yaml:"relativePath"`
	LockedAt         time.Time `yaml:"lockedAt"`
}

type File struct {
	SchemaVersion  int     `yaml:"schemaVersion"`
	Models         []Model `yaml:"models"`
	needsMigration bool
}

const CurrentSchemaVersion = 1

type UnsupportedSchemaVersionError struct {
	Version int
}

func (unsupported *UnsupportedSchemaVersionError) Error() string {
	if unsupported.Version > CurrentSchemaVersion {
		return fmt.Sprintf(
			"lock schemaVersion %d is newer than supported version %d; upgrade ModelShelf",
			unsupported.Version, CurrentSchemaVersion,
		)
	}
	return fmt.Sprintf(
		"unsupported lock schemaVersion %d (supported: %d); upgrade ModelShelf",
		unsupported.Version, CurrentSchemaVersion,
	)
}

type InvalidError struct {
	Path string
	Err  error
}

func (invalid *InvalidError) Error() string {
	return fmt.Sprintf("invalid generated lock file %s: %v", invalid.Path, invalid.Err)
}

func (invalid *InvalidError) Unwrap() error { return invalid.Err }

func Empty() File { return File{SchemaVersion: CurrentSchemaVersion, Models: []Model{}} }

func Path(configPath string) string {
	extension := filepath.Ext(configPath)
	if extension == ".yml" || extension == ".yaml" {
		return strings.TrimSuffix(configPath, extension) + ".lock" + extension
	}
	return configPath + ".lock.yml"
}

func Load(path string) (File, bool, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return Empty(), false, nil
	}
	if err != nil {
		return File{}, false, fmt.Errorf("read lock file %s: %w", path, err)
	}
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	decoder.KnownFields(true)
	var result File
	if err := decoder.Decode(&result); err != nil {
		return File{}, false, &InvalidError{Path: path, Err: err}
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return File{}, false, &InvalidError{Path: path, Err: errors.New("trailing YAML document")}
	}
	if result.SchemaVersion == 0 {
		// Pre-release generated lock files had the v1 shape without a marker.
		result.SchemaVersion = CurrentSchemaVersion
		result.needsMigration = true
	}
	if err := Validate(result); err != nil {
		return File{}, false, &InvalidError{Path: path, Err: err}
	}
	return result, true, nil
}

func Validate(file File) error {
	if file.SchemaVersion == 0 {
		file.SchemaVersion = CurrentSchemaVersion
	}
	if file.SchemaVersion != CurrentSchemaVersion {
		return &UnsupportedSchemaVersionError{Version: file.SchemaVersion}
	}
	seen := map[string]struct{}{}
	seenAliases := map[string]struct{}{}
	for index, model := range file.Models {
		if !domain.ValidProvider(model.Provider) || model.ID == "" || model.Revision == "" ||
			model.ResolvedRevision == "" || model.ArtifactID == "" || model.RelativePath == "" ||
			model.LockedAt.IsZero() {
			return fmt.Errorf("models[%d] is incomplete", index)
		}
		key := DeclarationKey(model.Alias, model.Provider, model.ID, model.Revision)
		if _, ok := seen[key]; ok {
			return fmt.Errorf("duplicate declaration %s", key)
		}
		seen[key] = struct{}{}
		if model.Alias != "" {
			if _, ok := seenAliases[model.Alias]; ok {
				return fmt.Errorf("duplicate alias %q", model.Alias)
			}
			seenAliases[model.Alias] = struct{}{}
		}
	}
	return nil
}

func Save(file File, path string) error {
	file.SchemaVersion = CurrentSchemaVersion
	file.needsMigration = false
	Sort(&file)
	if err := Validate(file); err != nil {
		return err
	}
	data, err := yaml.Marshal(file)
	if err != nil {
		return fmt.Errorf("encode lock file: %w", err)
	}
	parent := filepath.Dir(path)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create lock directory: %w", err)
	}
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
		return fmt.Errorf("publish lock file: %w", err)
	}
	directory, err := os.Open(parent)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func SelectorKey(provider, id, revision string) string {
	return provider + "\x00" + id + "\x00" + revision
}

func DeclarationKey(alias, provider, id, revision string) string {
	if alias != "" {
		return "alias\x00" + alias
	}
	return "selector\x00" + SelectorKey(provider, id, revision)
}

func DesiredKey(desired domain.DesiredModel) string {
	return DeclarationKey(desired.Alias, desired.Provider, desired.ID, desired.RequestedRevision)
}

func Find(file File, desired domain.DesiredModel) *Model {
	key := DesiredKey(desired)
	for index := range file.Models {
		candidate := &file.Models[index]
		if DeclarationKey(candidate.Alias, candidate.Provider, candidate.ID, candidate.Revision) == key {
			return candidate
		}
	}
	return nil
}

func Equal(left, right File) bool {
	left.Models = append([]Model(nil), left.Models...)
	right.Models = append([]Model(nil), right.Models...)
	Sort(&left)
	Sort(&right)
	return reflect.DeepEqual(left, right)
}

func Sort(file *File) {
	sort.Slice(file.Models, func(left, right int) bool {
		return DeclarationKey(
			file.Models[left].Alias, file.Models[left].Provider, file.Models[left].ID, file.Models[left].Revision,
		) < DeclarationKey(
			file.Models[right].Alias, file.Models[right].Provider, file.Models[right].ID, file.Models[right].Revision,
		)
	})
}
