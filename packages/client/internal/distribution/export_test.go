package distribution

import (
	"github.com/mr5/modelshelf/client/internal/config"
	"strings"
	"testing"
)

func TestExportPlanIsReadOnlyAndIsolated(t *testing.T) {
	c := config.Config{ServerURL: "http://metadata.test", NFSLocalPath: "/mnt/modelshelf", LocalBasePath: "/var/lib/modelshelf", Distribution: &config.Distribution{Enabled: true, Allow: []string{"192.168.100.2/32"}}}
	conf, unit, err := Plan(c)
	if err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{"Path = \"/var/lib/modelshelf/.distribution/published\"", "Pseudo = /modelshelf", "Access_Type = NONE", "Access_Type = RO", "192.168.100.2/32", "Root_Squash"} {
		if !strings.Contains(conf, text) {
			t.Errorf("missing %s", text)
		}
	}
	if strings.Contains(conf, "Access_Type = RW") || !strings.Contains(unit, "/etc/ganesha/modelshelf.conf") {
		t.Fatal("unsafe plan")
	}
	c.LocalBasePath = "/tmp/inject\"; Path=/;"
	if _, _, err := Plan(c); err == nil {
		t.Fatal("accepted injected export path")
	}
}

func TestExportPort(t *testing.T) {
	c := config.Config{ServerURL: "http://metadata.test", NFSLocalPath: "/mnt/modelshelf", LocalBasePath: "/var/lib/modelshelf", Distribution: &config.Distribution{Enabled: true, Allow: []string{"192.168.100.2/32"}}}
	conf, _, err := Plan(c)
	if err != nil || !strings.Contains(conf, "NFS_Port = 2049;") {
		t.Fatalf("default port: %v %s", err, conf)
	}
	port := 12049
	c.Distribution.Port = &port
	conf, _, err = Plan(c)
	if err != nil || !strings.Contains(conf, "NFS_Port = 12049;") {
		t.Fatalf("custom port: %v %s", err, conf)
	}
	for _, value := range []int{0, -1, 65536} {
		c.Distribution.Port = &value
		if _, _, err := Plan(c); err == nil {
			t.Fatalf("accepted port %d", value)
		}
	}
}
