package mount

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"unicode"

	"github.com/modelshelf/modelshelf/client/internal/api"
	"github.com/modelshelf/modelshelf/client/internal/config"
)

func Mount(ctx context.Context, configuration config.Config, client *api.Client) error {
	info, err := client.Info(ctx)
	if err != nil {
		return err
	}
	if info.NFS == nil || info.NFS.Host == "" || info.NFS.ExportPath == "" {
		return errors.New("server does not advertise an NFS export")
	}
	if info.NFS.Port < 1 || info.NFS.Port > 65535 {
		return errors.New("server advertised an invalid NFS port")
	}
	source, err := validatedNFSSource(info.NFS.Host, info.NFS.ExportPath)
	if err != nil {
		return err
	}
	target, err := filepath.Abs(configuration.NFSLocalPath)
	if err != nil {
		return err
	}
	if err := validateMountTarget(target); err != nil {
		return err
	}
	if err := run(ctx, "sudo", "mkdir", "-p", target); err != nil {
		return err
	}
	switch runtime.GOOS {
	case "linux":
		return installSystemdMount(ctx, target, source, info.NFS.Port)
	case "darwin":
		return run(
			ctx,
			"sudo",
			"mount_nfs",
			"-o",
			fmt.Sprintf("ro,vers=4,port=%d", info.NFS.Port),
			source,
			target,
		)
	default:
		return fmt.Errorf("NFS mount is supported on Linux and macOS, not %s", runtime.GOOS)
	}
}

func Unmount(ctx context.Context, configuration config.Config) error {
	target, err := filepath.Abs(configuration.NFSLocalPath)
	if err != nil {
		return err
	}
	if runtime.GOOS != "linux" {
		if runtime.GOOS != "darwin" {
			return fmt.Errorf("NFS unmount is supported on Linux and macOS, not %s", runtime.GOOS)
		}
		return run(ctx, "sudo", "umount", target)
	}
	unit, err := systemdUnit(ctx, target)
	if err != nil {
		return err
	}
	_ = run(ctx, "sudo", "systemctl", "disable", "--now", unit+".automount", unit+".mount")
	if err := run(
		ctx,
		"sudo",
		"rm",
		"-f",
		filepath.Join("/etc/systemd/system", unit+".automount"),
		filepath.Join("/etc/systemd/system", unit+".mount"),
	); err != nil {
		return err
	}
	return run(ctx, "sudo", "systemctl", "daemon-reload")
}

func installSystemdMount(ctx context.Context, target, source string, port int) error {
	if err := validateMountTarget(target); err != nil {
		return err
	}
	unit, err := systemdUnit(ctx, target)
	if err != nil {
		return err
	}
	mountContent := fmt.Sprintf(`[Unit]
Description=ModelShelf read-only NFS mount
After=network-online.target
Wants=network-online.target

[Mount]
What=%s
Where=%s
Type=nfs4
Options=ro,vers=4.2,port=%d,lookupcache=positive,_netdev,nofail
TimeoutSec=60

[Install]
WantedBy=multi-user.target
`, source, target, port)
	automountContent := fmt.Sprintf(`[Unit]
Description=ModelShelf NFS automount

[Automount]
Where=%s
TimeoutIdleSec=600

[Install]
WantedBy=multi-user.target
`, target)
	temporary, err := os.MkdirTemp("", "modelshelf-systemd-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temporary)
	mountFile := filepath.Join(temporary, unit+".mount")
	automountFile := filepath.Join(temporary, unit+".automount")
	if err := os.WriteFile(mountFile, []byte(mountContent), 0o644); err != nil {
		return err
	}
	if err := os.WriteFile(automountFile, []byte(automountContent), 0o644); err != nil {
		return err
	}
	if err := run(ctx, "sudo", "install", "-m", "0644", mountFile, "/etc/systemd/system/"); err != nil {
		return err
	}
	if err := run(
		ctx, "sudo", "install", "-m", "0644", automountFile, "/etc/systemd/system/",
	); err != nil {
		return err
	}
	if err := run(ctx, "sudo", "systemctl", "daemon-reload"); err != nil {
		return err
	}
	return run(ctx, "sudo", "systemctl", "enable", "--now", unit+".automount")
}

func validatedNFSSource(host, exportPath string) (string, error) {
	if host == "" || strings.TrimSpace(host) != host {
		return "", errors.New("server advertised an invalid NFS host")
	}
	normalizedHost := host
	if strings.HasPrefix(normalizedHost, "[") && strings.HasSuffix(normalizedHost, "]") {
		normalizedHost = normalizedHost[1 : len(normalizedHost)-1]
	}
	address := net.ParseIP(normalizedHost)
	if address == nil {
		for _, character := range normalizedHost {
			if !((character >= 'a' && character <= 'z') ||
				(character >= 'A' && character <= 'Z') ||
				(character >= '0' && character <= '9') ||
				strings.ContainsRune("._-", character)) {
				return "", errors.New("server advertised an invalid NFS host")
			}
		}
		if normalizedHost == "" {
			return "", errors.New("server advertised an invalid NFS host")
		}
	} else if strings.Contains(normalizedHost, ":") {
		normalizedHost = "[" + normalizedHost + "]"
	}
	if !strings.HasPrefix(exportPath, "/") || path.Clean(exportPath) != exportPath ||
		strings.Contains(exportPath, "\\") || strings.IndexFunc(exportPath, unicode.IsSpace) >= 0 {
		return "", errors.New("server advertised an invalid NFS export path")
	}
	return normalizedHost + ":" + exportPath, nil
}

func validateMountTarget(target string) error {
	if !filepath.IsAbs(target) || strings.ContainsAny(target, "\r\n") {
		return errors.New("nfsLocalPath must be an absolute path without line breaks")
	}
	return nil
}

func systemdUnit(ctx context.Context, target string) (string, error) {
	command := exec.CommandContext(ctx, "systemd-escape", "--path", target)
	output, err := command.Output()
	if err != nil {
		return "", fmt.Errorf("systemd-escape: %w", err)
	}
	return strings.TrimSpace(string(output)), nil
}

func run(ctx context.Context, name string, arguments ...string) error {
	command := exec.CommandContext(ctx, name, arguments...)
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	if err := command.Run(); err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}
