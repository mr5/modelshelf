package config

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/modelshelf/modelshelf/client/internal/domain"
)

func TestLoadAndSaveYAML(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	path := filepath.Join(home, "config.yml")
	input := `serverUrl: http://modelshelf.test:8080
nfsLocalPath: ~/nfs
localBasePath: ~/models
writeToken: secret
models:
  - alias: mini-lm
    provider: huggingface
    id: owner/model
    revision: main
  - provider: modelscope-cn
    id: owner/second
    path: custom/second
`
	if err := os.WriteFile(path, []byte(input), 0o644); err != nil {
		t.Fatal(err)
	}
	configuration, resolvedPath, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if resolvedPath != path || configuration.NFSLocalPath != filepath.Join(home, "nfs") ||
		configuration.LocalBasePath != filepath.Join(home, "models") {
		t.Fatalf("paths not expanded: %#v", configuration)
	}
	if configuration.Models[1].RequestedRevision != "main" {
		t.Fatalf("missing requested revision default: %#v", configuration.Models[1])
	}
	if err := Save(configuration, path); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("config mode = %o", info.Mode().Perm())
	}
	reloaded, _, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(reloaded.Models) != 2 || reloaded.Models[0].RequestedRevision != "main" {
		t.Fatalf("round trip mismatch: %#v", reloaded)
	}
	if reloaded.Models[0].Alias != "mini-lm" {
		t.Fatalf("alias did not round trip: %#v", reloaded.Models[0])
	}
	if reloaded.SchemaVersion != CurrentSchemaVersion {
		t.Fatalf("schema version = %d", reloaded.SchemaVersion)
	}
}

func TestLoadRejectsFutureConfigAndLocalLayoutSchemas(t *testing.T) {
	root := t.TempDir()
	configPath := filepath.Join(root, "config.yml")
	input := `schemaVersion: 2
serverUrl: http://modelshelf.test:8080
nfsLocalPath: /mnt/modelshelf
localBasePath: ` + filepath.Join(root, "local") + `
`
	if err := os.WriteFile(configPath, []byte(input), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := Load(configPath); err == nil {
		t.Fatal("future config schema was accepted")
	}

	input = `schemaVersion: 1
serverUrl: http://modelshelf.test:8080
nfsLocalPath: /mnt/modelshelf
localBasePath: ` + filepath.Join(root, "local") + `
`
	if err := os.WriteFile(configPath, []byte(input), 0o600); err != nil {
		t.Fatal(err)
	}
	configuration, _, err := Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := EnsureLocalLayout(configuration); err != nil {
		t.Fatal(err)
	}
	layoutPath := filepath.Join(configuration.LocalBasePath, ".modelshelf", "layout.json")
	if err := os.WriteFile(layoutPath, []byte(`{"schemaVersion":2,"kind":"modelshelf-client-layout"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := Load(configPath); err == nil {
		t.Fatal("future local layout schema was accepted")
	}
}

func TestValidateRejectsInvalidProvider(t *testing.T) {
	configuration, err := Defaults()
	if err != nil {
		t.Fatal(err)
	}
	configuration.Models = append(configuration.Models, model("invalid", "one"))
	if err := configuration.Validate(); err == nil {
		t.Fatal("invalid provider was accepted")
	}
}

func TestValidateRejectsDuplicateModel(t *testing.T) {
	configuration, err := Defaults()
	if err != nil {
		t.Fatal(err)
	}
	configuration.Models = append(configuration.Models,
		model(domain.ProviderHuggingFace, "owner/model"),
		model(domain.ProviderHuggingFace, "owner/model"),
	)
	if err := configuration.Validate(); err == nil {
		t.Fatal("duplicate model was accepted")
	}
}

func TestValidateAllowsDuplicateSelectorWithUniqueAliases(t *testing.T) {
	configuration, err := Defaults()
	if err != nil {
		t.Fatal(err)
	}
	first := model(domain.ProviderHuggingFace, "owner/model")
	first.Alias = "chat-a"
	second := model(domain.ProviderHuggingFace, "owner/model")
	second.Alias = "chat-b"
	configuration.Models = append(configuration.Models, first, second)
	if err := configuration.Validate(); err != nil {
		t.Fatalf("duplicate selector with unique aliases rejected: %v", err)
	}
}

func TestValidateRejectsDuplicateAlias(t *testing.T) {
	configuration, err := Defaults()
	if err != nil {
		t.Fatal(err)
	}
	first := model(domain.ProviderHuggingFace, "owner/model")
	first.Alias = "chat"
	second := model(domain.ProviderModelScopeCN, "owner/second")
	second.Alias = "chat"
	configuration.Models = append(configuration.Models, first, second)
	if err := configuration.Validate(); err == nil {
		t.Fatal("duplicate alias was accepted")
	}
}

func TestValidateAllowsMultipleRevisionsWithAliases(t *testing.T) {
	configuration, err := Defaults()
	if err != nil {
		t.Fatal(err)
	}
	first := model(domain.ProviderHuggingFace, "owner/model")
	first.Alias = "prod"
	second := model(domain.ProviderHuggingFace, "owner/model")
	second.Alias = "canary"
	second.RequestedRevision = "release"
	configuration.Models = append(configuration.Models, first, second)
	if err := configuration.Validate(); err != nil {
		t.Fatalf("multiple aliased revisions rejected: %v", err)
	}
}

func TestValidateRequiresAliasesForMultipleRevisions(t *testing.T) {
	configuration, err := Defaults()
	if err != nil {
		t.Fatal(err)
	}
	first := model(domain.ProviderHuggingFace, "owner/model")
	second := model(domain.ProviderHuggingFace, "owner/model")
	second.RequestedRevision = "release"
	configuration.Models = append(configuration.Models, first, second)
	if err := configuration.Validate(); err == nil {
		t.Fatal("multiple revisions without aliases were accepted")
	}
}

func TestLoadRejectsInternalRevisionFields(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.yml")
	input := `serverUrl: http://modelshelf.test:8080
nfsLocalPath: /mnt/modelshelf
localBasePath: /models
models:
  - provider: huggingface
    id: owner/model
    requestedRevision: main
`
	if err := os.WriteFile(configPath, []byte(input), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := Load(configPath); err == nil {
		t.Fatal("requestedRevision config field was accepted")
	}
}

func TestLoadRejectsUnknownFields(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.yml")
	input := `serverUrl: http://modelshelf.test:8080
nfsLocalPath: /mnt/modelshelf
localBasePath: /models
serverURLTypo: http://wrong.invalid
`
	if err := os.WriteFile(configPath, []byte(input), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := Load(configPath); err == nil {
		t.Fatal("unknown config field was accepted")
	}
}

func TestReferenceAndArtifactPaths(t *testing.T) {
	configuration := Config{LocalBasePath: "/var/lib/models"}
	for _, configuredPath := range []string{
		"../escape", ".", "/", "/var/lib/models", ".staging/model", "models/manual",
		"aliases/manual",
	} {
		_, err := ReferencePaths(configuration, domain.DesiredModel{Path: configuredPath})
		if err == nil {
			t.Errorf("dangerous model path %q was accepted", configuredPath)
		}
	}
	paths, err := ReferencePaths(configuration, domain.DesiredModel{Alias: "qwen-prod", Path: "runtime/qwen"})
	wanted := []string{"/var/lib/models/aliases/qwen-prod", "/var/lib/models/runtime/qwen"}
	if err != nil || len(paths) != 2 || paths[0] != wanted[0] || paths[1] != wanted[1] {
		t.Fatalf("reference paths = %#v, %v", paths, err)
	}
	path, err := ArtifactPath(configuration, "huggingface/owner/model/abc123")
	if err != nil || path != "/var/lib/models/models/huggingface/owner/model/abc123" {
		t.Fatalf("artifact path = %q, %v", path, err)
	}
	if StagingRoot(configuration) != "/var/lib/models/models/.staging" {
		t.Fatalf("staging root = %q", StagingRoot(configuration))
	}
	if _, err := ArtifactPath(configuration, ".staging/sync-1"); err == nil {
		t.Fatal("reserved staging relativePath was accepted")
	}
	desired := domain.DesiredModel{
		RequestedRevision: "feature/one", ResolvedRevision: "abc123",
	}
	reference, ok, err := RevisionReferencePath(configuration, desired, path)
	if err != nil || !ok || reference != "/var/lib/models/models/huggingface/owner/model/feature%2Fone" {
		t.Fatalf("revision reference = %q, %v, %v", reference, ok, err)
	}
	desired.RequestedRevision = desired.ResolvedRevision
	if reference, ok, err := RevisionReferencePath(configuration, desired, path); err != nil || ok || reference != "" {
		t.Fatalf("immutable revision reference = %q, %v, %v", reference, ok, err)
	}
}

func TestValidateRejectsFilesystemRootAsLocalBase(t *testing.T) {
	configuration, err := Defaults()
	if err != nil {
		t.Fatal(err)
	}
	configuration.LocalBasePath = string(filepath.Separator)
	if err := configuration.Validate(); err == nil {
		t.Fatal("filesystem root was accepted as localBasePath")
	}
}

func model(provider, id string) domain.DesiredModel {
	return domain.DesiredModel{Provider: provider, ID: id, RequestedRevision: "main"}
}
