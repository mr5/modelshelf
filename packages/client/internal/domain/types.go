package domain

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strings"
	"time"
)

const (
	ProviderHuggingFace   = "huggingface"
	ProviderModelScopeCN  = "modelscope-cn"
	ProviderModelScopeAI  = "modelscope-ai"
	ProviderGitHubRelease = "github-release"
	ProviderKaggle        = "kaggle"
	ProviderHTTP          = "http"
	ProviderFilesystem    = "filesystem"
)

var providers = map[string]struct{}{
	ProviderHuggingFace:   {},
	ProviderModelScopeCN:  {},
	ProviderModelScopeAI:  {},
	ProviderGitHubRelease: {},
	ProviderKaggle:        {},
	ProviderHTTP:          {},
	ProviderFilesystem:    {},
}

func ValidProvider(provider string) bool {
	_, ok := providers[provider]
	return ok
}

type DesiredModel struct {
	Alias             string   `yaml:"alias,omitempty" json:"alias,omitempty"`
	Provider          string   `yaml:"provider" json:"provider"`
	ID                string   `yaml:"id" json:"id"`
	RequestedRevision string   `yaml:"revision,omitempty" json:"requestedRevision"`
	Artifact          string   `yaml:"artifact,omitempty" json:"artifact,omitempty"`
	ResolvedRevision  string   `yaml:"-" json:"-"`
	ArtifactID        string   `yaml:"-" json:"-"`
	RelativePath      string   `yaml:"-" json:"-"`
	Path              string   `yaml:"path,omitempty" json:"path,omitempty"`
	Files             []string `yaml:"files,omitempty" json:"files,omitempty"`
}

type ArtifactSummary struct {
	ArtifactID        string    `json:"artifactId"`
	Alias             string    `json:"alias,omitempty"`
	Name              string    `json:"name"`
	Version           string    `json:"version"`
	Provider          string    `json:"provider"`
	SourceID          string    `json:"sourceId"`
	RequestedRevision string    `json:"requestedRevision"`
	ResolvedRevision  string    `json:"resolvedRevision"`
	TotalSize         int64     `json:"totalSize"`
	FileCount         int       `json:"fileCount"`
	CreatedAt         time.Time `json:"createdAt"`
	RelativePath      string    `json:"relativePath"`
	SelectionDigest   string    `json:"selectionDigest,omitempty"`
	SelectedPaths     []string  `json:"selectedPaths,omitempty"`
}

type DownloadTask struct {
	ID                string    `json:"id"`
	Provider          string    `json:"provider"`
	SourceID          string    `json:"sourceId"`
	RequestedRevision string    `json:"requestedRevision"`
	ResolvedRevision  string    `json:"resolvedRevision,omitempty"`
	Status            string    `json:"status"`
	Progress          int       `json:"progress"`
	BytesDownloaded   int64     `json:"bytesDownloaded"`
	TotalBytes        *int64    `json:"totalBytes,omitempty"`
	CreatedAt         time.Time `json:"createdAt"`
	UpdatedAt         time.Time `json:"updatedAt"`
	Error             string    `json:"error,omitempty"`
	ArtifactID        string    `json:"artifactId,omitempty"`
	SelectedPaths     []string  `json:"selectedPaths,omitempty"`
}

type NFSInfo struct {
	Host       string `json:"host"`
	Port       int    `json:"port"`
	ExportPath string `json:"exportPath"`
	Version    string `json:"version"`
}

type ClientPlatform struct {
	OS       string `json:"os"`
	Arch     string `json:"arch"`
	Filename string `json:"filename"`
}

type ClientDistribution struct {
	Available   bool             `json:"available"`
	Version     string           `json:"version,omitempty"`
	InstallURL  string           `json:"installUrl"`
	DownloadURL string           `json:"downloadUrl"`
	Platforms   []ClientPlatform `json:"platforms"`
}

type ServerInfo struct {
	Name    string              `json:"name"`
	Version string              `json:"version"`
	NFS     *NFSInfo            `json:"nfs"`
	Client  *ClientDistribution `json:"client,omitempty"`
}

type FileEntry struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

type SourceReference struct {
	Provider          string   `json:"provider"`
	ID                string   `json:"id"`
	RequestedRevision string   `json:"requestedRevision"`
	ResolvedRevision  string   `json:"resolvedRevision"`
	URL               string   `json:"url,omitempty"`
	SelectedPaths     []string `json:"selectedPaths,omitempty"`
}

type ArtifactManifest struct {
	SchemaVersion int             `json:"schemaVersion"`
	ArtifactID    string          `json:"artifactId"`
	Name          string          `json:"name"`
	Version       string          `json:"version"`
	Format        string          `json:"format,omitempty"`
	Source        SourceReference `json:"source"`
	ContentSHA256 string          `json:"contentSha256"`
	CreatedAt     time.Time       `json:"createdAt"`
	TotalSize     int64           `json:"totalSize"`
	FileCount     int             `json:"fileCount"`
	Files         []FileEntry     `json:"files"`
}

func CanonicalFiles(files []string) []string {
	if files == nil {
		return nil
	}
	seen := make(map[string]struct{}, len(files))
	for _, file := range files {
		seen[file] = struct{}{}
	}
	result := make([]string, 0, len(seen))
	for file := range seen {
		result = append(result, file)
	}
	sort.Strings(result)
	return result
}

func SelectionDigest(files []string) string {
	if files == nil {
		return ""
	}
	digest := sha256.New()
	for _, file := range CanonicalFiles(files) {
		_, _ = digest.Write([]byte(file))
		_, _ = digest.Write([]byte{0})
	}
	return hex.EncodeToString(digest.Sum(nil))
}

func FilesKey(files []string) string {
	return strings.Join(CanonicalFiles(files), "\x00")
}
