package syncer

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/mr5/modelshelf/client/internal/catalog"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/distribution"
	"github.com/mr5/modelshelf/client/internal/domain"
)

// Executed only by scripts/test_client_distribution.sh in isolated containers.
func requireNFSIntegration(t *testing.T) {
	t.Helper()
	if os.Getenv("MODELSHELF_NFS_INTEGRATION") != "1" {
		t.Skip("run scripts/test_client_distribution.sh for real NFS tests")
	}
}

func TestNFSIntegrationSeed(t *testing.T) {
	requireNFSIntegration(t)
	central := config.Config{ServerURL: "http://metadata.test", LocalBasePath: "/data/central", NFSLocalPath: "/mnt/central", Distribution: &config.Distribution{Enabled: true, Allow: []string{os.Getenv("TEST_CIDR")}}}
	peer := central
	peerPort, err := strconv.Atoi(os.Getenv("PEER_PORT"))
	if err != nil {
		t.Fatal(err)
	}
	peer.Distribution = &config.Distribution{Enabled: true, Allow: central.Distribution.Allow, Port: &peerPort}
	peer.LocalBasePath = "/data/peer"
	peer.NFSLocalPath = config.PublishedRoot(central)
	artifacts := map[string]domain.ArtifactSummary{}
	for _, revision := range []string{"ready", "missing", "file-missing", "corrupt", "manifest-corrupt", "outage-strict", "outage-fallback"} {
		a := createArtifact(t, config.PublishedRoot(central), revision, "verified-model-"+revision, time.Now())
		artifacts[revision] = a
		if revision == "missing" {
			continue
		}
		if _, err := syncConfigured(context.Background(), peer, domain.DesiredModel{}, a, os.Stdout, func(config.Config) error { return nil }); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"file-missing", "corrupt", "manifest-corrupt"} {
		target := filepath.Join(config.PublishedRoot(peer), artifacts[name].RelativePath, "model.bin")
		if name == "file-missing" {
			if err := os.Remove(target); err != nil {
				t.Fatal(err)
			}
			continue
		}
		content := []byte(strings.Repeat("x", len("verified-model-"+name)))
		if name == "manifest-corrupt" {
			target = filepath.Join(config.PublishedRoot(peer), artifacts[name].RelativePath, catalog.ManifestPath)
			content = []byte("{}")
		}
		if err := os.Chmod(target, 0644); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, content, 0444); err != nil {
			t.Fatal(err)
		}
	}
	data, _ := json.Marshal(artifacts)
	if err := os.WriteFile("/data/artifacts.json", data, 0644); err != nil {
		t.Fatal(err)
	}
	for name, c := range map[string]config.Config{"central": central, "peer": peer} {
		conf, _, err := distribution.Plan(c)
		if err != nil {
			t.Fatal(err)
		}
		// Container kernels may deny PR_SET_IO_FLUSHER; same compatibility setting as the repository exporter.
		conf = strings.Replace(conf, "Protocols = 4;", "Allow_Set_Io_Flusher_Fail = true;\n Protocols = 4;", 1)
		if err := os.WriteFile("/data/"+name+".conf", []byte(conf), 0644); err != nil {
			t.Fatal(err)
		}
	}
}

func nfsIntegrationSync(t *testing.T, revision string, fallback bool, publish bool) {
	t.Helper()
	data, err := os.ReadFile("/data/artifacts.json")
	if err != nil {
		t.Fatal(err)
	}
	var artifacts map[string]domain.ArtifactSummary
	if err := json.Unmarshal(data, &artifacts); err != nil {
		t.Fatal(err)
	}
	artifact := artifacts[revision]
	var infoRequests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/artifacts":
			_ = json.NewEncoder(w).Encode([]domain.ArtifactSummary{artifact})
		case "/api/v1/info":
			infoRequests.Add(1)
			_ = json.NewEncoder(w).Encode(domain.ServerInfo{NFS: &domain.NFSInfo{Host: os.Getenv("CENTRAL_IP"), Port: 2049, ExportPath: "/modelshelf", Version: "4.1"}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	c := config.Config{SchemaVersion: 3, ServerURL: server.URL, NFSLocalPath: "/mnt/peer", LocalBasePath: filepath.Join(t.TempDir(), "local"), Upstream: &config.Upstream{Host: os.Getenv("PEER_IP"), Fallback: fallback}}
	peerPort, err := strconv.Atoi(os.Getenv("PEER_PORT"))
	if err != nil {
		t.Fatal(err)
	}
	c.Upstream.Port = &peerPort
	desired := domain.DesiredModel{Alias: "test", Provider: artifact.Provider, ID: artifact.SourceID, RequestedRevision: "main", Artifact: artifact.ArtifactID}
	c.Models = []domain.DesiredModel{desired}
	if publish {
		c.Upstream = nil
		c.NFSLocalPath = "/mnt/peer-fallback"
		c.Distribution = &config.Distribution{Enabled: true, Allow: []string{"192.168.100.0/24"}}
	}
	var log bytes.Buffer
	started := time.Now()
	path := filepath.Join(t.TempDir(), "config.yml")
	if err := config.Save(c, path); err != nil {
		t.Fatal(err)
	}
	command := exec.Command("sh", "-c", `umask 077; exec /modelshelf --config "$1" sync`, "modelshelf-integration", path)
	command.Stdout = &log
	command.Stderr = &log
	err = command.Run()

	expectSuccess := revision == "ready" || fallback
	if expectSuccess && err != nil {
		t.Fatalf("sync: %v\n%s", err, log.String())
	}
	if !expectSuccess && err == nil {
		t.Fatalf("strict sync unexpectedly succeeded: %s", log.String())
	}
	if !fallback && infoRequests.Load() != 0 {
		t.Fatal("strict peer sync contacted central NFS metadata")
	}
	if fallback && revision != "ready" && (!strings.Contains(log.String(), "explicitly enabled fallback") || infoRequests.Load() == 0) {
		t.Fatalf("fallback was not visible: %s", log.String())
	}
	destination, _ := config.ArtifactPath(c, artifact.RelativePath)
	if expectSuccess {
		failures, e := catalog.Verify(destination, catalog.VerifyOptions{Full: true})
		if e != nil || len(failures) > 0 {
			t.Fatalf("verification: %v %v", failures, e)
		}
		state := readSyncState(t, destination)
		mountPath := "/mnt/peer"
		if revision != "ready" || publish {
			mountPath += "-fallback"
		}
		if state["sourcePath"] != filepath.Join(mountPath, artifact.RelativePath) {
			t.Fatalf("wrong actual source: %v", state)
		}
	} else if _, e := os.Stat(destination); !os.IsNotExist(e) {
		t.Fatal("failed sync published data")
	}
	if publish {
		target := filepath.Join(config.PublishedRoot(c), artifact.RelativePath)
		failures, e := catalog.Verify(target, catalog.VerifyOptions{Full: true, Unexpected: true})
		if e != nil || len(failures) > 0 {
			t.Fatalf("publication after NFS sync: %v %v", failures, e)
		}
		if e := filepath.WalkDir(config.PublishedRoot(c), func(path string, entry os.DirEntry, e error) error {
			if e != nil {
				return e
			}
			info, e := entry.Info()
			if e != nil {
				return e
			}
			if entry.IsDir() && info.Mode().Perm()&0005 != 0005 {
				return fmt.Errorf("directory is not traversable: %s", path)
			}
			if !entry.IsDir() && info.Mode().Perm()&0004 != 0004 {
				return fmt.Errorf("file is not readable: %s", path)
			}
			return nil
		}); e != nil {
			t.Fatal(e)
		}
	}
	t.Logf("revision=%s fallback=%t elapsed=%s\n%s", revision, fallback, time.Since(started), log.String())
}

func TestNFSIntegrationRead(t *testing.T) {
	requireNFSIntegration(t)
	for _, path := range []string{".staging", ".distribution", "aliases"} {
		if _, err := os.Stat(filepath.Join("/mnt/peer", path)); !os.IsNotExist(err) {
			t.Fatalf("private path visible: %s (%v)", path, err)
		}
	}
	// Mount with rw explicitly: the server must still reject writes.
	if err := os.MkdirAll("/mnt/write-probe", 0755); err != nil {
		t.Fatal(err)
	}
	output, err := exec.Command("mount", "-t", "nfs4", "-o", "rw,nosharecache,vers=4.1,softerr,timeo=10,retrans=1,port="+os.Getenv("PEER_PORT"), os.Getenv("PEER_IP")+":/modelshelf", "/mnt/write-probe").CombinedOutput()
	if err != nil {
		t.Fatalf("write probe mount: %v %s", err, output)
	}
	defer exec.Command("umount", "/mnt/write-probe").Run()
	if err := os.WriteFile("/mnt/write-probe/should-not-exist", []byte("x"), 0644); !errors.Is(err, syscall.EROFS) {
		t.Fatalf("expected server read-only rejection, got %v", err)
	}
	for _, revision := range []string{"ready", "missing", "file-missing", "corrupt", "manifest-corrupt"} {
		for _, fallback := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/fallback=%t", revision, fallback), func(t *testing.T) { nfsIntegrationSync(t, revision, fallback, false) })
		}
	}
}

func TestNFSIntegrationPublish(t *testing.T) {
	requireNFSIntegration(t)
	nfsIntegrationSync(t, "ready", false, true)
}

func TestNFSIntegrationOutage(t *testing.T) {
	requireNFSIntegration(t)
	nfsIntegrationSync(t, "outage-strict", false, false)
	nfsIntegrationSync(t, "outage-fallback", true, false)
}
