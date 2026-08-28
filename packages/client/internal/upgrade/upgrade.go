package upgrade

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

const (
	maxArchiveSize  = 512 * 1024 * 1024
	maxBinarySize   = 256 * 1024 * 1024
	maxMetadataSize = 4 * 1024 * 1024
)

type Release struct {
	Version      string
	DownloadBase string
	Archive      string
	Source       string
}

type Options struct {
	HTTPClient     *http.Client
	ExecutablePath string
}

func PlatformArchive(goos, goarch string) (string, error) {
	if goos != "linux" && goos != "darwin" {
		return "", fmt.Errorf("unsupported operating system %q", goos)
	}
	if goarch != "amd64" && goarch != "arm64" {
		return "", fmt.Errorf("unsupported architecture %q", goarch)
	}
	return fmt.Sprintf("modelshelf_%s_%s.tar.gz", goos, goarch), nil
}

func CurrentPlatformArchive() (string, error) {
	return PlatformArchive(runtime.GOOS, runtime.GOARCH)
}

func GitHubDownloadBase(repository, version string) (string, error) {
	parts := strings.Split(repository, "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" ||
		!safeReleaseName(parts[0]) || !safeReleaseName(parts[1]) ||
		parts[0] == "." || parts[0] == ".." || parts[1] == "." || parts[1] == ".." {
		return "", errors.New("GitHub repository must be owner/repository")
	}
	if !safeReleaseName(version) {
		return "", errors.New("GitHub release version contains invalid characters")
	}
	return "https://github.com/" + repository + "/releases/download/" + version, nil
}

func LatestGitHubVersion(
	ctx context.Context,
	client *http.Client,
	apiBase, repository, token string,
) (string, error) {
	if _, err := GitHubDownloadBase(repository, "probe"); err != nil {
		return "", err
	}
	if client == nil {
		client = http.DefaultClient
	}
	requestURL := strings.TrimRight(apiBase, "/") + "/repos/" + repository + "/releases/latest"
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, requestURL, nil)
	if err != nil {
		return "", fmt.Errorf("create GitHub release request: %w", err)
	}
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("User-Agent", "modelshelf-cli")
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := client.Do(request)
	if err != nil {
		return "", fmt.Errorf("query latest GitHub release: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		detail, _ := io.ReadAll(io.LimitReader(response.Body, maxMetadataSize))
		return "", fmt.Errorf(
			"GitHub returned HTTP %d: %s",
			response.StatusCode,
			strings.TrimSpace(string(detail)),
		)
	}
	var payload struct {
		TagName string `json:"tag_name"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, maxMetadataSize)).Decode(&payload); err != nil {
		return "", fmt.Errorf("decode latest GitHub release: %w", err)
	}
	if !safeReleaseName(payload.TagName) {
		return "", errors.New("GitHub returned an invalid release tag")
	}
	return payload.TagName, nil
}

func Install(ctx context.Context, release Release, options Options) error {
	if _, err := CompareVersions(release.Version, release.Version); err != nil {
		return fmt.Errorf("invalid target version: %w", err)
	}
	if release.Archive == "" {
		return errors.New("release archive filename is required")
	}
	downloadBase := strings.TrimRight(release.DownloadBase, "/")
	base, err := url.Parse(downloadBase)
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") || base.Host == "" {
		return errors.New("release download base must be an absolute HTTP(S) URL")
	}
	client := options.HTTPClient
	if client == nil {
		client = http.DefaultClient
	}

	checksums, err := downloadBytes(ctx, client, downloadBase+"/checksums.txt", maxMetadataSize)
	if err != nil {
		return fmt.Errorf("download release checksums: %w", err)
	}
	expected, err := expectedChecksum(checksums, release.Archive)
	if err != nil {
		return err
	}

	archive, err := os.CreateTemp("", "modelshelf-upgrade-*.tar.gz")
	if err != nil {
		return fmt.Errorf("create temporary archive: %w", err)
	}
	archivePath := archive.Name()
	defer os.Remove(archivePath)
	defer archive.Close()

	response, err := get(ctx, client, downloadBase+"/"+url.PathEscape(release.Archive))
	if err != nil {
		return fmt.Errorf("download release archive: %w", err)
	}
	hash := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(archive, hash), io.LimitReader(response.Body, maxArchiveSize+1))
	closeErr := response.Body.Close()
	if copyErr != nil {
		return fmt.Errorf("download release archive: %w", copyErr)
	}
	if closeErr != nil {
		return fmt.Errorf("download release archive: %w", closeErr)
	}
	if written > maxArchiveSize {
		return errors.New("release archive exceeds the 512 MiB safety limit")
	}
	actual := hash.Sum(nil)
	if subtle.ConstantTimeCompare(actual, expected) != 1 {
		return errors.New("release archive SHA-256 checksum verification failed")
	}
	if err := archive.Sync(); err != nil {
		return fmt.Errorf("sync release archive: %w", err)
	}
	if _, err := archive.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("rewind release archive: %w", err)
	}

	executablePath := options.ExecutablePath
	if executablePath == "" {
		executablePath, err = os.Executable()
		if err != nil {
			return fmt.Errorf("locate current executable: %w", err)
		}
	}
	if resolved, resolveErr := filepath.EvalSymlinks(executablePath); resolveErr == nil {
		executablePath = resolved
	}
	executablePath, err = filepath.Abs(executablePath)
	if err != nil {
		return fmt.Errorf("resolve current executable: %w", err)
	}
	currentInfo, err := os.Stat(executablePath)
	if err != nil {
		return fmt.Errorf("inspect current executable: %w", err)
	}
	if !currentInfo.Mode().IsRegular() {
		return errors.New("current executable is not a regular file")
	}

	temporary, err := os.CreateTemp(filepath.Dir(executablePath), ".modelshelf-upgrade-*")
	if err != nil {
		if errors.Is(err, os.ErrPermission) {
			return fmt.Errorf("cannot update %s: permission denied; rerun with sufficient privileges", executablePath)
		}
		return fmt.Errorf("create replacement beside current executable: %w", err)
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)

	if err := extractBinary(archive, temporary); err != nil {
		temporary.Close()
		return err
	}
	mode := currentInfo.Mode().Perm()
	if mode == 0 {
		mode = 0o755
	}
	if err := temporary.Chmod(mode); err != nil {
		temporary.Close()
		return fmt.Errorf("set replacement permissions: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return fmt.Errorf("sync replacement executable: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close replacement executable: %w", err)
	}

	output, err := exec.CommandContext(ctx, temporaryPath, "--version").CombinedOutput()
	if err != nil {
		return fmt.Errorf("validate replacement executable: %w: %s", err, strings.TrimSpace(string(output)))
	}
	if !outputContainsVersion(string(output), release.Version) {
		return fmt.Errorf(
			"replacement reports an unexpected version: %s",
			strings.TrimSpace(string(output)),
		)
	}
	if err := os.Rename(temporaryPath, executablePath); err != nil {
		return fmt.Errorf("atomically replace %s: %w", executablePath, err)
	}
	if directory, openErr := os.Open(filepath.Dir(executablePath)); openErr == nil {
		_ = directory.Sync()
		_ = directory.Close()
	}
	return nil
}

func CompareVersions(left, right string) (int, error) {
	l, err := parseVersion(left)
	if err != nil {
		return 0, fmt.Errorf("cannot compare version %q", left)
	}
	r, err := parseVersion(right)
	if err != nil {
		return 0, fmt.Errorf("cannot compare version %q", right)
	}
	for index := range l.numbers {
		if l.numbers[index] < r.numbers[index] {
			return -1, nil
		}
		if l.numbers[index] > r.numbers[index] {
			return 1, nil
		}
	}
	if l.prerelease == "" && r.prerelease != "" {
		return 1, nil
	}
	if l.prerelease != "" && r.prerelease == "" {
		return -1, nil
	}
	return comparePrerelease(l.prerelease, r.prerelease), nil
}

type semanticVersion struct {
	numbers    [3]uint64
	prerelease string
}

func parseVersion(value string) (semanticVersion, error) {
	trimmed := strings.TrimPrefix(strings.TrimPrefix(strings.TrimSpace(value), "v"), "V")
	if buildIndex := strings.IndexByte(trimmed, '+'); buildIndex >= 0 {
		build := trimmed[buildIndex+1:]
		if build == "" || strings.ContainsRune(build, '+') || !validVersionIdentifiers(build, true) {
			return semanticVersion{}, errors.New("invalid semantic version")
		}
		trimmed = trimmed[:buildIndex]
	}
	prerelease := ""
	hadPrerelease := false
	if prereleaseIndex := strings.IndexByte(trimmed, '-'); prereleaseIndex >= 0 {
		hadPrerelease = true
		prerelease = trimmed[prereleaseIndex+1:]
		trimmed = trimmed[:prereleaseIndex]
	}
	parts := strings.Split(trimmed, ".")
	if len(parts) != 3 || (hadPrerelease && prerelease == "") {
		return semanticVersion{}, errors.New("invalid semantic version")
	}
	result := semanticVersion{prerelease: prerelease}
	for index, part := range parts {
		if part == "" || (len(part) > 1 && part[0] == '0') {
			return semanticVersion{}, errors.New("invalid semantic version")
		}
		parsed, err := strconv.ParseUint(part, 10, 64)
		if err != nil {
			return semanticVersion{}, errors.New("invalid semantic version")
		}
		result.numbers[index] = parsed
	}
	if prerelease != "" && !validVersionIdentifiers(prerelease, false) {
		return semanticVersion{}, errors.New("invalid semantic version")
	}
	return result, nil
}

func validVersionIdentifiers(value string, allowNumericLeadingZero bool) bool {
	for _, identifier := range strings.Split(value, ".") {
		if identifier == "" {
			return false
		}
		numeric := true
		for _, character := range identifier {
			if character < '0' || character > '9' {
				numeric = false
			}
			if (character >= 'a' && character <= 'z') ||
				(character >= 'A' && character <= 'Z') ||
				(character >= '0' && character <= '9') || character == '-' {
				continue
			}
			return false
		}
		if numeric && !allowNumericLeadingZero && len(identifier) > 1 && identifier[0] == '0' {
			return false
		}
	}
	return true
}

func comparePrerelease(left, right string) int {
	leftParts := strings.Split(left, ".")
	rightParts := strings.Split(right, ".")
	for index := 0; index < len(leftParts) && index < len(rightParts); index++ {
		if leftParts[index] == rightParts[index] {
			continue
		}
		leftNumber, leftErr := strconv.ParseUint(leftParts[index], 10, 64)
		rightNumber, rightErr := strconv.ParseUint(rightParts[index], 10, 64)
		if leftErr == nil && rightErr == nil {
			if leftNumber < rightNumber {
				return -1
			}
			return 1
		}
		if leftErr == nil {
			return -1
		}
		if rightErr == nil {
			return 1
		}
		if leftParts[index] < rightParts[index] {
			return -1
		}
		return 1
	}
	if len(leftParts) < len(rightParts) {
		return -1
	}
	if len(leftParts) > len(rightParts) {
		return 1
	}
	return 0
}

func safeReleaseName(value string) bool {
	if value == "" {
		return false
	}
	for _, character := range value {
		if (character >= 'a' && character <= 'z') ||
			(character >= 'A' && character <= 'Z') ||
			(character >= '0' && character <= '9') ||
			strings.ContainsRune("._-", character) {
			continue
		}
		return false
	}
	return true
}

func downloadBytes(ctx context.Context, client *http.Client, location string, limit int64) ([]byte, error) {
	response, err := get(ctx, client, location)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, errors.New("response exceeds safety limit")
	}
	return data, nil
}

func get(ctx context.Context, client *http.Client, location string) (*http.Response, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, location, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("User-Agent", "modelshelf-cli")
	response, err := client.Do(request)
	if err != nil {
		return nil, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		detail, _ := io.ReadAll(io.LimitReader(response.Body, maxMetadataSize))
		response.Body.Close()
		return nil, fmt.Errorf("HTTP %d: %s", response.StatusCode, strings.TrimSpace(string(detail)))
	}
	return response, nil
}

func expectedChecksum(data []byte, archive string) ([]byte, error) {
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 || strings.TrimPrefix(fields[1], "*") != archive {
			continue
		}
		decoded, err := hex.DecodeString(fields[0])
		if err != nil || len(decoded) != sha256.Size || fields[0] != strings.ToLower(fields[0]) {
			break
		}
		return decoded, nil
	}
	return nil, fmt.Errorf("missing or invalid SHA-256 checksum for %s", archive)
}

func extractBinary(archive io.ReadSeeker, destination *os.File) error {
	if _, err := archive.Seek(0, io.SeekStart); err != nil {
		return err
	}
	compressed, err := gzip.NewReader(archive)
	if err != nil {
		return fmt.Errorf("open release archive: %w", err)
	}
	defer compressed.Close()
	reader := tar.NewReader(compressed)
	found := false
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return fmt.Errorf("read release archive: %w", err)
		}
		if strings.TrimPrefix(path.Clean(header.Name), "./") != "modelshelf" {
			continue
		}
		if found || (header.Typeflag != tar.TypeReg && header.Typeflag != tar.TypeRegA) {
			return errors.New("release archive contains an invalid modelshelf executable")
		}
		if header.Size < 1 || header.Size > maxBinarySize {
			return errors.New("release executable has an invalid size")
		}
		written, err := io.Copy(destination, io.LimitReader(reader, maxBinarySize+1))
		if err != nil {
			return fmt.Errorf("extract release executable: %w", err)
		}
		if written != header.Size {
			return errors.New("release executable size does not match the archive header")
		}
		found = true
	}
	if !found {
		return errors.New("release archive does not contain the modelshelf executable")
	}
	return nil
}

func outputContainsVersion(output, version string) bool {
	for _, field := range strings.Fields(output) {
		candidate := strings.Trim(field, "(),[]")
		if comparison, err := CompareVersions(candidate, version); err == nil && comparison == 0 {
			return true
		}
	}
	return false
}
