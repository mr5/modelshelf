package lockfile

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/modelshelf/modelshelf/client/internal/domain"
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
	})
	if entry == nil || entry.ResolvedRevision != "abc" {
		t.Fatalf("entry = %#v", entry)
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
