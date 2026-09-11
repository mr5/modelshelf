package mount

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"unicode"

	"github.com/mr5/modelshelf/client/internal/api"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/domain"
	"github.com/mr5/modelshelf/client/internal/nfsio"
)

func Mount(ctx context.Context, configuration config.Config, client *api.Client) error {
	info, err := Resolve(ctx, configuration, client)
	if err != nil {
		return err
	}
	return mountInfo(ctx, configuration, client, info)
}

func Resolve(ctx context.Context, c config.Config, client *api.Client) (domain.ServerInfo, error) {
	if c.Upstream != nil {
		return domain.ServerInfo{NFS: &domain.NFSInfo{Host: c.Upstream.Host, Port: c.Upstream.NFSPort(), ExportPath: "/modelshelf", Version: "4.1"}}, nil
	}
	return client.Info(ctx)
}

func mountInfo(ctx context.Context, configuration config.Config, client *api.Client, info domain.ServerInfo) error {
	if info.NFS == nil || info.NFS.Host == "" || info.NFS.ExportPath == "" {
		return errors.New("server does not advertise an NFS export")
	}
	if info.NFS.Port < 1 || info.NFS.Port > 65535 {
		return errors.New("server advertised an invalid NFS port")
	}
	version, err := validatedNFSVersion(info.NFS.Version)
	if err != nil {
		return err
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
		if err := checkExisting(ctx, target, source, info.NFS.Port, false); err != nil {
			return err
		}
		if err := installSystemdMount(ctx, target, source, info.NFS.Port, version); err != nil {
			return err
		}
		if configuration.Upstream != nil && configuration.Upstream.Fallback {
			return Mount(ctx, config.FallbackConfig(configuration), client)
		}
		return nil
	case "darwin":
		if err := checkExisting(ctx, target, source, info.NFS.Port, false); err != nil {
			return err
		}
		if err := run(
			ctx,
			"sudo",
			"mount_nfs",
			"-o",
			fmt.Sprintf("ro,soft,vers=4,timeo=50,retrans=2,port=%d", info.NFS.Port),
			source,
			target,
		); err != nil {
			return err
		}
		if configuration.Upstream != nil && configuration.Upstream.Fallback {
			return Mount(ctx, config.FallbackConfig(configuration), client)
		}
		return nil
	default:
		return invalidMount("NFS mount is supported on Linux and macOS, not %s", runtime.GOOS)
	}
}

func Unmount(ctx context.Context, configuration config.Config) error {
	if configuration.Upstream != nil && configuration.Upstream.Fallback {
		if err := Unmount(ctx, config.FallbackConfig(configuration)); err != nil {
			return err
		}
	}
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

func installSystemdMount(ctx context.Context, target, source string, port int, version string) error {
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
Options=ro,softerr,timeo=50,retrans=2,vers=%s,port=%d,lookupcache=positive,_netdev,nofail
TimeoutSec=60

[Install]
WantedBy=multi-user.target
`, source, target, version, port)
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

func validatedNFSVersion(version string) (string, error) {
	if version == "" {
		// Older API implementations may omit this additive field. Use the
		// compatibility baseline instead of negotiating the highest minor version.
		return "4.1", nil
	}
	if version != "4.1" && version != "4.2" {
		return "", errors.New("server advertised an unsupported NFS version")
	}
	return version, nil
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
	if !filepath.IsAbs(target) || strings.ContainsAny(target, "\r\n%\t ") {
		return errors.New("nfsLocalPath must be an absolute path without whitespace or percent signs")
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

type InvalidMountError struct{ Message string }

func (e *InvalidMountError) Error() string { return e.Message }
func invalidMount(format string, args ...any) error {
	return &InvalidMountError{Message: fmt.Sprintf(format, args...)}
}

// CheckSource refuses an unmounted directory or a mount from a different server.
func CheckSource(ctx context.Context, c config.Config, client *api.Client) error {
	info, err := Resolve(ctx, c, client)
	if err != nil {
		return err
	}
	if info.NFS == nil {
		return errors.New("server does not advertise NFS")
	}
	source, err := validatedNFSSource(info.NFS.Host, info.NFS.ExportPath)
	if err != nil {
		return err
	}
	if err := checkExisting(ctx, c.NFSLocalPath, source, info.NFS.Port, false); err != nil {
		return err
	}
	// Trigger a configured automount before inspecting its identity.
	if _, err := nfsio.Stat(filepath.Join(c.NFSLocalPath, ".")); err != nil {
		return err
	}
	return checkExisting(ctx, c.NFSLocalPath, source, info.NFS.Port, true)
}

func checkExisting(ctx context.Context, target, source string, expectedPort int, required bool) error {
	if runtime.GOOS == "darwin" {
		output, err := exec.CommandContext(ctx, "mount").Output()
		if err != nil {
			return err
		}
		for _, line := range strings.Split(string(output), "\n") {
			if strings.Contains(line, " on "+target+" (") {
				if !strings.HasPrefix(line, source+" on ") || !strings.Contains(line, "nfs") {
					return invalidMount("mount source mismatch at %s: expected %s", target, source)
				}
				return nil
			}
		}
	} else {
		output, err := exec.CommandContext(ctx, "findmnt", "--json", "--mountpoint", target, "--output", "SOURCE,FSTYPE,OPTIONS").Output()
		if err == nil {
			var result struct {
				Filesystems []struct {
					Source  string
					Fstype  string
					Options string
				}
			}
			if err := json.Unmarshal(output, &result); err != nil {
				return err
			}
			for _, fs := range result.Filesystems {
				if fs.Fstype == "autofs" {
					continue
				}
				if fs.Source != source || (fs.Fstype != "nfs4" && fs.Fstype != "nfs") {
					return invalidMount("mount source mismatch at %s: got %s, expected %s", target, fs.Source, source)
				}
				if err := checkPort(fs.Options, expectedPort); err != nil {
					return err
				}
				if !strings.Contains(","+fs.Options+",", ",ro,") {
					return invalidMount("NFS mount %s must be read-only", target)
				}
				if !strings.Contains(","+fs.Options+",", ",softerr,") && !strings.Contains(","+fs.Options+",", ",soft,") {
					return invalidMount("NFS mount %s must use finite retries; unmount and run modelshelf mount", target)
				}
				return nil
			}
		} else if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 1 {
			return invalidMount("inspect mount: %v", err)
		}
	}
	if required {
		return fmt.Errorf("NFS source %s is not mounted at %s; run modelshelf mount", source, target)
	}
	return nil
}

func checkPort(options string, expected int) error {
	actual := 2049
	for _, option := range strings.Split(options, ",") {
		if value, ok := strings.CutPrefix(option, "port="); ok {
			port, err := strconv.Atoi(value)
			if err != nil || port < 0 || port > 65535 {
				return invalidMount("invalid mounted NFS port: %s", value)
			}
			if port != 0 {
				actual = port
			}
		}
	}
	if actual != expected {
		return invalidMount("mounted NFS port mismatch: got %d, expected %d; unmount and run modelshelf mount", actual, expected)
	}
	return nil
}
