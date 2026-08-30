package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"github.com/mr5/modelshelf/client/internal/domain"
	"gopkg.in/yaml.v3"
)

type Config struct {
	SchemaVersion int                   `yaml:"schemaVersion"`
	ServerURL     string                `yaml:"serverUrl"`
	NFSLocalPath  string                `yaml:"nfsLocalPath"`
	LocalBasePath string                `yaml:"localBasePath"`
	WriteToken    string                `yaml:"writeToken,omitempty"`
	Models        []domain.DesiredModel `yaml:"models,omitempty"`
}

const CurrentSchemaVersion = 2

const CurrentLocalLayoutSchemaVersion = 1

var ggufSplitPattern = regexp.MustCompile(`(?i)^(.*)-(\d{5})-of-(\d{5})\.gguf$`)

type localLayout struct {
	SchemaVersion int    `json:"schemaVersion"`
	Kind          string `json:"kind"`
}

func Defaults() (Config, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return Config{}, fmt.Errorf("resolve home directory: %w", err)
	}
	return Config{
		SchemaVersion: CurrentSchemaVersion,
		ServerURL:     "http://localhost:8080",
		NFSLocalPath:  "/mnt/modelshelf",
		LocalBasePath: filepath.Join(home, ".local", "share", "modelshelf"),
		Models:        []domain.DesiredModel{},
	}, nil
}

func DefaultPath() (string, error) {
	if configured := os.Getenv("MODELSHELF_CONFIG"); configured != "" {
		return ExpandPath(configured)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolve home directory: %w", err)
	}
	return filepath.Join(home, ".config", "modelshelf", "config.yml"), nil
}

func Load(path string) (Config, string, error) {
	defaults, err := Defaults()
	if err != nil {
		return Config{}, "", err
	}
	if path == "" {
		path, err = DefaultPath()
	} else {
		path, err = ExpandPath(path)
	}
	if err != nil {
		return Config{}, "", err
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return defaults, path, nil
	}
	if err != nil {
		return Config{}, "", fmt.Errorf("read config %s: %w", path, err)
	}
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	decoder.KnownFields(true)
	if err := decoder.Decode(&defaults); err != nil {
		return Config{}, "", fmt.Errorf("parse config %s: %w", path, err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		if err == nil {
			err = errors.New("multiple YAML documents are not allowed")
		}
		return Config{}, "", fmt.Errorf("parse config %s: %w", path, err)
	}
	if defaults.NFSLocalPath, err = ExpandPath(defaults.NFSLocalPath); err != nil {
		return Config{}, "", err
	}
	if defaults.LocalBasePath, err = ExpandPath(defaults.LocalBasePath); err != nil {
		return Config{}, "", err
	}
	if err := defaults.Validate(); err != nil {
		return Config{}, "", fmt.Errorf("invalid config %s: %w", path, err)
	}
	if err := CheckLocalLayout(defaults); err != nil {
		return Config{}, "", err
	}
	return defaults, path, nil
}

func (config *Config) Validate() error {
	if config.SchemaVersion == 0 {
		// Pre-release configuration files did not include an explicit version.
		config.SchemaVersion = 1
	}
	if config.SchemaVersion == 1 {
		config.SchemaVersion = CurrentSchemaVersion
	}
	if config.SchemaVersion != CurrentSchemaVersion {
		return fmt.Errorf(
			"unsupported config schemaVersion %d (supported: %d); upgrade ModelShelf",
			config.SchemaVersion, CurrentSchemaVersion,
		)
	}
	if config.ServerURL == "" {
		return errors.New("serverUrl is required")
	}
	parsedURL, err := url.Parse(config.ServerURL)
	if err != nil || (parsedURL.Scheme != "http" && parsedURL.Scheme != "https") || parsedURL.Host == "" {
		return errors.New("serverUrl must be an absolute HTTP(S) URL")
	}
	if config.NFSLocalPath == "" || config.LocalBasePath == "" {
		return errors.New("nfsLocalPath and localBasePath are required")
	}
	if !filepath.IsAbs(config.NFSLocalPath) || !filepath.IsAbs(config.LocalBasePath) {
		return errors.New("nfsLocalPath and localBasePath must be absolute")
	}
	if filepath.Clean(config.NFSLocalPath) == string(filepath.Separator) {
		return errors.New("nfsLocalPath cannot be the filesystem root")
	}
	if filepath.Clean(config.LocalBasePath) == string(filepath.Separator) {
		return errors.New("localBasePath cannot be the filesystem root")
	}
	seenSources := map[string]bool{}
	seenAliases := map[string]struct{}{}
	seenReferences := map[string]string{}
	for index, model := range config.Models {
		if model.Artifact != "" {
			if strings.TrimSpace(model.Artifact) != model.Artifact ||
				strings.ContainsAny(model.Artifact, "\r\n\t") {
				return fmt.Errorf("models[%d].artifact is invalid", index)
			}
			if model.Files != nil {
				return fmt.Errorf("models[%d] cannot define both artifact and files", index)
			}
		}
		if model.Files != nil {
			if len(model.Files) == 0 {
				return fmt.Errorf("models[%d].files cannot be empty", index)
			}
			if model.Provider != domain.ProviderHuggingFace &&
				model.Provider != domain.ProviderModelScopeCN &&
				model.Provider != domain.ProviderModelScopeAI {
				return fmt.Errorf("models[%d].files is not supported for provider %q", index, model.Provider)
			}
			for _, file := range model.Files {
				if err := validateSelectedFile(file); err != nil {
					return fmt.Errorf("models[%d].files: %w", index, err)
				}
			}
			config.Models[index].Files = domain.CanonicalFiles(model.Files)
			model.Files = config.Models[index].Files
			if err := validateGGUFVariant(model.Files); err != nil {
				return fmt.Errorf("models[%d].files: %w", index, err)
			}
		}
		if model.Alias != "" {
			if strings.TrimSpace(model.Alias) != model.Alias {
				return fmt.Errorf("models[%d].alias cannot start or end with whitespace", index)
			}
			if strings.ContainsAny(model.Alias, "\\/\r\n\t") {
				return fmt.Errorf("models[%d].alias cannot contain slashes or whitespace controls", index)
			}
			if model.Alias == "." || model.Alias == ".." ||
				model.Alias == ".modelshelf" || model.Alias == ".staging" {
				return fmt.Errorf("models[%d].alias uses a reserved name", index)
			}
			if _, ok := seenAliases[model.Alias]; ok {
				return fmt.Errorf("duplicate model alias %q", model.Alias)
			}
			seenAliases[model.Alias] = struct{}{}
		}
		if !domain.ValidProvider(model.Provider) {
			return fmt.Errorf("models[%d] has invalid provider %q", index, model.Provider)
		}
		if strings.TrimSpace(model.ID) == "" {
			return fmt.Errorf("models[%d].id is required", index)
		}
		if model.RequestedRevision == "" {
			config.Models[index].RequestedRevision = "main"
			model.RequestedRevision = "main"
		}
		sourceKey := ModelKey(model.Provider, model.ID)
		if previousHadAlias, ok := seenSources[sourceKey]; ok {
			if model.Alias == "" || !previousHadAlias {
				return fmt.Errorf("models sharing %s must each define a unique alias", sourceKey)
			}
		} else {
			seenSources[sourceKey] = model.Alias != ""
		}
		selector := sourceKey + "@" + model.RequestedRevision + "#" + domain.SelectionDigest(model.Files)
		if model.Artifact != "" {
			selector = "artifact:" + model.Artifact
		}
		references, referenceErr := ReferencePaths(*config, model)
		if referenceErr != nil {
			return fmt.Errorf("models[%d]: %w", index, referenceErr)
		}
		for _, reference := range references {
			cleaned := filepath.Clean(reference)
			if previous, ok := seenReferences[cleaned]; ok {
				return fmt.Errorf("models[%d] reference path %q is already used by %s", index, cleaned, previous)
			}
			seenReferences[cleaned] = selector
		}
	}
	return nil
}

func validateSelectedFile(file string) error {
	if file == "" || file == "." || strings.HasPrefix(file, "/") || strings.Contains(file, "\\") {
		return fmt.Errorf("%q is not a safe POSIX relative path", file)
	}
	for _, segment := range strings.Split(file, "/") {
		if segment == "" || segment == "." || segment == ".." {
			return fmt.Errorf("%q is not a safe POSIX relative path", file)
		}
	}
	return nil
}

func validateGGUFVariant(files []string) error {
	if len(files) == 0 {
		return errors.New("must identify one complete GGUF variant")
	}
	for _, file := range files {
		name := strings.ToLower(path.Base(file))
		if !strings.HasSuffix(name, ".gguf") {
			return errors.New("only recognized GGUF variants may select files")
		}
		for _, marker := range []string{"adapter", "draft", "lora", "mmproj", "mtp", "projector"} {
			if strings.Contains(name, marker) {
				return fmt.Errorf("%q has an unsupported GGUF dependency role", file)
			}
		}
	}

	first := ggufSplitPattern.FindStringSubmatch(files[0])
	if first == nil {
		if len(files) != 1 {
			return errors.New("multiple unsharded GGUF files are not one variant")
		}
		return nil
	}
	total, _ := strconv.Atoi(first[3])
	if total < 1 || len(files) != total {
		return fmt.Errorf("split GGUF variant requires %d shards", total)
	}
	seen := make(map[int]bool, total)
	for _, file := range files {
		match := ggufSplitPattern.FindStringSubmatch(file)
		if match == nil || !strings.EqualFold(match[1], first[1]) || match[3] != first[3] {
			return errors.New("files do not belong to one split GGUF variant")
		}
		index, _ := strconv.Atoi(match[2])
		if index < 1 || index > total || seen[index] {
			return errors.New("split GGUF shard numbering is invalid")
		}
		seen[index] = true
	}
	return nil
}

func Save(config Config, path string) error {
	if err := config.Validate(); err != nil {
		return err
	}
	parent := filepath.Dir(path)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create config directory: %w", err)
	}
	data, err := yaml.Marshal(config)
	if err != nil {
		return fmt.Errorf("encode config: %w", err)
	}
	temporary, err := os.CreateTemp(parent, "."+filepath.Base(path)+".*")
	if err != nil {
		return fmt.Errorf("create temporary config: %w", err)
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
		return fmt.Errorf("publish config: %w", err)
	}
	return syncDirectory(parent)
}

func syncDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func ExpandPath(path string) (string, error) {
	if path == "~" || strings.HasPrefix(path, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		if path == "~" {
			return home, nil
		}
		return filepath.Join(home, path[2:]), nil
	}
	return path, nil
}

func ReferencePaths(config Config, model domain.DesiredModel) ([]string, error) {
	result := []string{}
	if model.Alias != "" {
		result = append(result, filepath.Join(config.LocalBasePath, "aliases", model.Alias))
	}
	if model.Path != "" {
		configuredPath, err := ExpandPath(model.Path)
		if err != nil {
			return nil, err
		}
		var candidate string
		if filepath.IsAbs(configuredPath) {
			candidate = filepath.Clean(configuredPath)
		} else {
			cleaned := filepath.Clean(configuredPath)
			if cleaned == "." || cleaned == ".." ||
				strings.HasPrefix(cleaned, ".."+string(filepath.Separator)) {
				return nil, errors.New("relative model path cannot escape localBasePath")
			}
			candidate = filepath.Join(config.LocalBasePath, cleaned)
		}
		if candidate == string(filepath.Separator) ||
			candidate == filepath.Clean(config.LocalBasePath) {
			return nil, errors.New("model path cannot be the filesystem root or localBasePath")
		}
		if pathWithin(candidate, ArtifactStoreRoot(config)) {
			return nil, errors.New("model path cannot point inside the canonical models directory")
		}
		if pathWithin(candidate, filepath.Join(config.LocalBasePath, ".staging")) {
			return nil, errors.New("model path cannot use the reserved .staging directory")
		}
		if pathWithin(candidate, filepath.Join(config.LocalBasePath, ".modelshelf")) {
			return nil, errors.New("model path cannot use the reserved .modelshelf directory")
		}
		if pathWithin(candidate, filepath.Join(config.LocalBasePath, "aliases")) {
			return nil, errors.New("model path cannot point inside the reserved aliases directory")
		}
		result = append(result, candidate)
	}
	deduplicated := make([]string, 0, len(result))
	seen := map[string]struct{}{}
	for _, candidate := range result {
		cleaned := filepath.Clean(candidate)
		if _, ok := seen[cleaned]; ok {
			continue
		}
		seen[cleaned] = struct{}{}
		deduplicated = append(deduplicated, cleaned)
	}
	return deduplicated, nil
}

func ArtifactStoreRoot(config Config) string {
	return filepath.Join(config.LocalBasePath, "models")
}

func StagingRoot(config Config) string {
	return filepath.Join(ArtifactStoreRoot(config), ".staging")
}

func ArtifactPath(config Config, relativePath string) (string, error) {
	if relativePath == "" || strings.Contains(relativePath, "\\") || filepath.IsAbs(relativePath) {
		return "", errors.New("artifact relativePath must be a non-empty POSIX relative path")
	}
	cleaned := filepath.Clean(filepath.FromSlash(relativePath))
	if cleaned == "." || cleaned == ".." || strings.HasPrefix(cleaned, ".."+string(filepath.Separator)) {
		return "", errors.New("artifact relativePath escapes the local artifact store")
	}
	if filepath.ToSlash(cleaned) != relativePath {
		return "", errors.New("artifact relativePath is not canonical")
	}
	if cleaned == ".staging" || strings.HasPrefix(cleaned, ".staging"+string(filepath.Separator)) {
		return "", errors.New("artifact relativePath uses the reserved staging directory")
	}
	return filepath.Join(ArtifactStoreRoot(config), cleaned), nil
}

func RevisionReferencePath(
	config Config, model domain.DesiredModel, artifactPath string,
) (string, bool, error) {
	if model.RequestedRevision == "" || model.ResolvedRevision == "" ||
		model.RequestedRevision == model.ResolvedRevision {
		return "", false, nil
	}
	store := filepath.Clean(ArtifactStoreRoot(config))
	artifactPath = filepath.Clean(artifactPath)
	if !pathWithin(artifactPath, store) || filepath.Dir(artifactPath) == store {
		return "", false, errors.New("canonical artifact path is outside a model revision directory")
	}
	segment, err := escapeReferenceSegment(model.RequestedRevision)
	if err != nil {
		return "", false, err
	}
	if digest := domain.SelectionDigest(model.Files); digest != "" {
		segment += "~files-" + digest
	}
	reference := filepath.Join(filepath.Dir(artifactPath), segment)
	if reference == artifactPath {
		return "", false, nil
	}
	return reference, true, nil
}

func escapeReferenceSegment(value string) (string, error) {
	if value == "" {
		return "", errors.New("revision reference cannot be empty")
	}
	var escaped strings.Builder
	for _, character := range value {
		unsafe := character == '/' || character == '\\' || character == '%' ||
			unicode.IsSpace(character) || unicode.Is(unicode.C, character)
		if !unsafe {
			escaped.WriteRune(character)
			continue
		}
		for _, valueByte := range []byte(string(character)) {
			fmt.Fprintf(&escaped, "%%%02X", valueByte)
		}
	}
	result := escaped.String()
	if result == "." || result == ".." {
		result = strings.ReplaceAll(result, ".", "%2E")
	}
	return result, nil
}

func pathWithin(candidate, root string) bool {
	relative, err := filepath.Rel(filepath.Clean(root), filepath.Clean(candidate))
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

func ModelKey(provider, id string) string {
	return provider + ":" + id
}

func localLayoutPath(config Config) string {
	return filepath.Join(config.LocalBasePath, ".modelshelf", "layout.json")
}

func CheckLocalLayout(config Config) error {
	path := localLayoutPath(config)
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read local layout marker %s: %w", path, err)
	}
	defer file.Close()
	decoder := json.NewDecoder(io.LimitReader(file, 1024*1024))
	decoder.DisallowUnknownFields()
	var layout localLayout
	if err := decoder.Decode(&layout); err != nil {
		return fmt.Errorf("invalid local layout marker %s: %w", path, err)
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return fmt.Errorf("invalid local layout marker %s: trailing data", path)
	}
	if layout.SchemaVersion != CurrentLocalLayoutSchemaVersion ||
		layout.Kind != "modelshelf-client-layout" {
		return fmt.Errorf(
			"unsupported local layout schemaVersion %d (supported: %d); upgrade ModelShelf",
			layout.SchemaVersion, CurrentLocalLayoutSchemaVersion,
		)
	}
	return nil
}

func EnsureLocalLayout(config Config) error {
	if err := CheckLocalLayout(config); err != nil {
		return err
	}
	path := localLayoutPath(config)
	if _, err := os.Stat(path); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	parent := filepath.Dir(path)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create local layout directory: %w", err)
	}
	data, err := json.MarshalIndent(localLayout{
		SchemaVersion: CurrentLocalLayoutSchemaVersion,
		Kind:          "modelshelf-client-layout",
	}, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	temporary, err := os.CreateTemp(parent, ".layout.json.*")
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
		return fmt.Errorf("publish local layout marker: %w", err)
	}
	return syncDirectory(parent)
}
