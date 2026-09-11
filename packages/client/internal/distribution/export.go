// Package distribution manages a dedicated Linux NFS-Ganesha service. It never
// rewrites the host's existing kernel NFS exports or Ganesha configuration.
package distribution

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/mr5/modelshelf/client/internal/config"
)

const service = "modelshelf-export.service"
const confPath = "/etc/ganesha/modelshelf.conf"

func Plan(c config.Config) (string, string, error) {
	if c.Distribution == nil || !c.Distribution.Enabled {
		return "", "", fmt.Errorf("set distribution.enabled: true and distribution.allow first")
	}
	if err := c.Validate(); err != nil {
		return "", "", err
	}
	root := config.PublishedRoot(c)
	if strings.ContainsAny(root, "\"\\\n\r%") {
		return "", "", fmt.Errorf("unsupported character in distribution path")
	}
	clients := strings.Join(c.Distribution.Allow, ", ")
	for _, cidr := range c.Distribution.Allow {
		if cidr == "0.0.0.0/0" || cidr == "::/0" {
			clients = "*"
		}
	}
	conf := fmt.Sprintf(`NFS_Core_Param {
 Protocols = 4;
 Enable_UDP = false;
 Clustered = false;
 NFS_Port = %d;
 Enable_NLM = false;
 Enable_RQUOTA = false;
}
EXPORT {
 Export_Id = 1;
 Path = "%s";
 Pseudo = /modelshelf;
 Access_Type = NONE;
 Protocols = 4;
 Transports = TCP;
 SecType = sys;
 Squash = Root_Squash;
 Attr_Expiration_Time = 0;
 Trust_Readdir_Negative_Cache = false;
 CLIENT {
  Clients = %s;
  Access_Type = RO;
  Protocols = 4;
  Transports = TCP;
  SecType = sys;
 }
 FSAL { Name = VFS; }
}
`, c.Distribution.NFSPort(), root, clients)
	unit := `[Unit]
Description=ModelShelf read-only client distribution
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
RuntimeDirectory=modelshelf-export
ExecStart=/usr/bin/ganesha.nfsd -F -f /etc/ganesha/modelshelf.conf -p /run/modelshelf-export/ganesha.pid -L STDERR
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
`
	return conf, unit, nil
}

func Enable(ctx context.Context, c config.Config) error {
	if runtime.GOOS != "linux" {
		return fmt.Errorf("export enable requires Linux and systemd; a macOS client may consume a Linux export")
	}
	conf, unit, err := Plan(c)
	if err != nil {
		return err
	}
	if _, err := os.Stat("/usr/bin/ganesha.nfsd"); err != nil {
		return fmt.Errorf("install nfs-ganesha and nfs-ganesha-vfs before enabling distribution: %w", err)
	}
	port := c.Distribution.NFSPort()
	listeners, err := exec.CommandContext(ctx, "ss", "-H", "-ltn", "sport = :"+fmt.Sprint(port)).Output()
	if err != nil {
		return fmt.Errorf("inspect NFS listening port (requires iproute2/ss): %w", err)
	}
	if len(strings.TrimSpace(string(listeners))) > 0 {
		// Updating our existing export is allowed. Do not stop or overwrite another service.
		existing, readErr := os.ReadFile(confPath)
		active := exec.CommandContext(ctx, "systemctl", "is-active", "--quiet", service).Run() == nil
		if readErr != nil || !active || !strings.Contains(string(existing), fmt.Sprintf("NFS_Port = %d;", port)) {
			return fmt.Errorf("TCP %d is already in use; choose another distribution.port or resolve the conflict", port)
		}
	}
	if err := config.EnsureDistributionLayout(c); err != nil {
		return err
	}
	// Keep the local user's ownership: subsequent sync commands publish here.
	dir, err := os.MkdirTemp("", "modelshelf-export-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	for _, file := range []struct{ name, body, target string }{{"modelshelf.conf", conf, confPath}, {service, unit, "/etc/systemd/system/" + service}} {
		source := filepath.Join(dir, file.name)
		if err := os.WriteFile(source, []byte(file.body), 0600); err != nil {
			return err
		}
		if err := run(ctx, "sudo", "install", "-D", "-m", "0644", source, file.target); err != nil {
			return err
		}
	}
	if err := run(ctx, "sudo", "systemctl", "daemon-reload"); err != nil {
		return err
	}
	if err := run(ctx, "sudo", "systemctl", "enable", service); err != nil {
		return err
	}
	return run(ctx, "sudo", "systemctl", "restart", service)
}
func Disable(ctx context.Context) error {
	if runtime.GOOS != "linux" {
		return fmt.Errorf("export service management requires Linux")
	}
	return run(ctx, "sudo", "systemctl", "disable", "--now", service)
}
func Status(ctx context.Context) error { return run(ctx, "systemctl", "status", "--no-pager", service) }
func run(ctx context.Context, name string, args ...string) error {
	command := exec.CommandContext(ctx, name, args...)
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	if err := command.Run(); err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}
