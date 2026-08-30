package lockfile

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/mr5/modelshelf/client/internal/domain"
)

func TestPathFollowsConfig(t *testing.T) {
	if got := Path("/etc/models/app.yml"); got != "/etc/models/app.lock.yml" {
		t.Fatalf("path = %q", got)
	}
	if got := Path("/etc/models/app"); got != "/etc/models/app.lock.yml" {
		t.Fatalf("extensionless path = %q", got)
	}
}

func TestRoundTripAndFind(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.lock.yml")
	wanted := File{Models: []Model{{
		Alias: "mini", Provider: "huggingface", ID: "owner/model", Revision: "main",
		Files:            []string{"model.gguf"},
		ResolvedRevision: "abc", ArtifactID: "artifact",
		RelativePath: "huggingface/owner/model/abc", LockedAt: time.Now().UTC(),
	}}}
	if err := Save(wanted, path); err != nil {
		t.Fatal(err)
	}
	loaded, exists, err := Load(path)
	if err != nil || !exists {
		t.Fatalf("load exists=%v err=%v", exists, err)
	}
	entry := Find(loaded, domain.DesiredModel{
		Alias: "mini", Provider: "huggingface", ID: "owner/model", RequestedRevision: "main",
		Files: []string{"model.gguf"},
	})
	if entry == nil || entry.ResolvedRevision != "abc" || len(entry.Files) != 1 {
		t.Fatalf("entry = %#v", entry)
	}
}

func TestFindUsesConfiguredArtifactReference(t *testing.T) {
	wanted := File{Models: []Model{{
		Alias: "runtime", Provider: "huggingface", ID: "owner/model", Revision: "main",
		Artifact: "quantized-model", Files: []string{"model.gguf"},
		ResolvedRevision: "abc", ArtifactID: "immutable-id",
		RelativePath: "huggingface/owner/model/abc/files", LockedAt: time.Now().UTC(),
	}}}
	entry := Find(wanted, domain.DesiredModel{
		Alias: "runtime", Provider: "huggingface", ID: "owner/model",
		RequestedRevision: "main", Artifact: "quantized-model",
	})
	if entry == nil || entry.ArtifactID != "immutable-id" {
		t.Fatalf("entry = %#v", entry)
	}
}

func TestMissingVersionIsMigratedAndFutureVersionIsRejected(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.lock.yml")
	if err := os.WriteFile(path, []byte("models: []\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	loaded, _, err := Load(path)
	if err != nil || loaded.SchemaVersion != CurrentSchemaVersion {
		t.Fatalf("legacy lock = %#v err=%v", loaded, err)
	}
	if err := os.WriteFile(path, []byte("schemaVersion: 3\nmodels: []\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := Load(path); err == nil {
		t.Fatal("future lock schema was accepted")
	}
}

func TestAllowsMultipleAliasesForOneSelector(t *testing.T) {
	now := time.Now().UTC()
	file := File{Models: []Model{
		{
			Alias: "primary", Provider: "huggingface", ID: "owner/model", Revision: "main",
			ResolvedRevision: "abc", ArtifactID: "artifact",
			RelativePath: "huggingface/owner/model/abc", LockedAt: now,
		},
		{
			Alias: "secondary", Provider: "huggingface", ID: "owner/model", Revision: "main",
			ResolvedRevision: "abc", ArtifactID: "artifact",
			RelativePath: "huggingface/owner/model/abc", LockedAt: now,
		},
	}}
	if err := Validate(file); err != nil {
		t.Fatalf("duplicate selector aliases rejected: %v", err)
	}
	entry := Find(file, domain.DesiredModel{
		Alias: "secondary", Provider: "huggingface", ID: "owner/model", RequestedRevision: "main",
	})
	if entry == nil || entry.Alias != "secondary" {
		t.Fatalf("entry = %#v", entry)
	}
}
