package cli

import (
	"bufio"
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"text/tabwriter"
	"time"

	charmterm "github.com/charmbracelet/x/term"
	"github.com/mr5/modelshelf/client/internal/api"
	"github.com/mr5/modelshelf/client/internal/catalog"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/domain"
	"github.com/mr5/modelshelf/client/internal/lockfile"
	"github.com/mr5/modelshelf/client/internal/mount"
	"github.com/mr5/modelshelf/client/internal/passwordhash"
	"github.com/mr5/modelshelf/client/internal/syncer"
	"github.com/mr5/modelshelf/client/internal/tui"
	clientupgrade "github.com/mr5/modelshelf/client/internal/upgrade"
	"github.com/spf13/cobra"
)

const (
	ExitOK          = 0
	ExitNotReady    = 2
	ExitCorrupt     = 3
	ExitUnavailable = 4
)

type ExitError struct {
	Code int
	Err  error
}

func (exit *ExitError) Error() string { return exit.Err.Error() }
func (exit *ExitError) Unwrap() error { return exit.Err }

type Application struct {
	ConfigPath string
	Version    string
	Commit     string
	Stdout     io.Writer
	Stderr     io.Writer
	Stdin      io.Reader
}

func New(version, commit string) *cobra.Command {
	return NewWithIO(version, commit, os.Stdin, os.Stdout, os.Stderr)
}

func NewWithIO(version, commit string, stdin io.Reader, stdout, stderr io.Writer) *cobra.Command {
	application := &Application{
		Version: version,
		Commit:  commit,
		Stdout:  stdout,
		Stderr:  stderr,
		Stdin:   stdin,
	}
	root := &cobra.Command{
		Use:           "modelshelf",
		Short:         "Declarative ModelShelf client",
		SilenceErrors: true,
		SilenceUsage:  true,
		Version:       versionString(version, commit),
	}
	root.SetOut(application.Stdout)
	root.SetErr(application.Stderr)
	root.SetIn(application.Stdin)
	root.PersistentFlags().StringVar(
		&application.ConfigPath, "config", "", "config file (default: $MODELSHELF_CONFIG or ~/.config/modelshelf/config.yml)",
	)
	root.AddCommand(
		application.addCommand(),
		application.removeCommand(),
		application.searchCommand(),
		application.listCommand(),
		application.syncCommand(),
		application.statusCommand(),
		application.verifyCommand(),
		application.mountCommand(),
		application.unmountCommand(),
		application.upgradeCommand(),
		application.hashPasswordCommand(),
		application.tuiCommand(),
	)
	return root
}

func (application *Application) upgradeCommand() *cobra.Command {
	var check bool
	var force bool
	var github bool
	var targetVersion string
	command := &cobra.Command{
		Use:   "upgrade",
		Short: "Upgrade this client from its ModelShelf server or GitHub Releases",
		Args:  cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			archive, err := clientupgrade.CurrentPlatformArchive()
			if err != nil {
				return err
			}
			release := clientupgrade.Release{Archive: archive}
			if github || targetVersion != "" {
				repository := os.Getenv("MODELSHELF_GITHUB_REPOSITORY")
				if repository == "" {
					repository = "mr5/modelshelf"
				}
				if targetVersion == "" {
					apiBase := os.Getenv("MODELSHELF_GITHUB_API_URL")
					if apiBase == "" {
						apiBase = "https://api.github.com"
					}
					targetVersion, err = clientupgrade.LatestGitHubVersion(
						command.Context(), http.DefaultClient, apiBase, repository, os.Getenv("GITHUB_TOKEN"),
					)
					if err != nil {
						return err
					}
				}
				release.DownloadBase, err = clientupgrade.GitHubDownloadBase(repository, targetVersion)
				if err != nil {
					return err
				}
				release.Version = targetVersion
				release.Source = "GitHub release " + repository
			} else {
				configuration, _, loadErr := config.Load(application.ConfigPath)
				if loadErr != nil {
					return loadErr
				}
				info, infoErr := api.New(configuration.ServerURL, configuration.WriteToken).Info(command.Context())
				if infoErr != nil {
					return infoErr
				}
				if info.Client == nil || !info.Client.Available {
					return errors.New("the configured ModelShelf server does not provide a complete client distribution")
				}
				if info.Client.Version == "" || info.Client.DownloadURL == "" {
					return errors.New("the configured ModelShelf server does not report an upgrade version and download URL")
				}
				platformAvailable := false
				for _, platform := range info.Client.Platforms {
					if platform.OS == runtime.GOOS && platform.Arch == runtime.GOARCH && platform.Filename == archive {
						platformAvailable = true
						break
					}
				}
				if !platformAvailable {
					return fmt.Errorf("the configured server has no client package for %s/%s", runtime.GOOS, runtime.GOARCH)
				}
				release.Version = info.Client.Version
				release.DownloadBase = info.Client.DownloadURL
				release.Source = "ModelShelf server " + configuration.ServerURL
			}

			currentVersion := application.Version
			if currentVersion == "" {
				currentVersion = "dev"
			}
			comparison, compareErr := clientupgrade.CompareVersions(currentVersion, release.Version)
			if compareErr == nil && comparison == 0 && (!force || check) {
				fmt.Fprintf(application.Stdout, "ModelShelf CLI %s is already up to date (%s).\n", currentVersion, release.Source)
				return nil
			}
			if check {
				if compareErr != nil {
					fmt.Fprintf(application.Stdout, "Current version %s cannot be compared; target is %s from %s.\n", currentVersion, release.Version, release.Source)
				} else if comparison > 0 {
					fmt.Fprintf(application.Stdout, "Current version %s is newer than %s from %s.\n", currentVersion, release.Version, release.Source)
				} else {
					fmt.Fprintf(application.Stdout, "Upgrade available: %s -> %s (%s).\n", currentVersion, release.Version, release.Source)
				}
				return nil
			}
			if compareErr != nil && !force {
				return fmt.Errorf("%w; use --force to replace a development or non-semantic build", compareErr)
			}
			if compareErr == nil && comparison > 0 && !force {
				return fmt.Errorf("target %s is older than current version %s; use --force to downgrade", release.Version, currentVersion)
			}
			fmt.Fprintf(application.Stderr, "Downloading ModelShelf CLI %s for %s/%s from %s…\n", release.Version, runtime.GOOS, runtime.GOARCH, release.Source)
			if err := clientupgrade.Install(command.Context(), release, clientupgrade.Options{}); err != nil {
				return err
			}
			fmt.Fprintf(application.Stdout, "Upgraded ModelShelf CLI %s -> %s.\n", currentVersion, release.Version)
			return nil
		},
	}
	command.Flags().BoolVar(&check, "check", false, "check the available version without replacing the executable")
	command.Flags().BoolVar(&force, "force", false, "reinstall the same version or explicitly allow a downgrade")
	command.Flags().BoolVar(&github, "github", false, "use the latest GitHub release instead of the configured server")
	command.Flags().StringVar(&targetVersion, "version", "", "install a specific GitHub release version (implies --github)")
	return command
}

func (application *Application) hashPasswordCommand() *cobra.Command {
	var readStdin bool
	command := &cobra.Command{
		Use:   "hash-password",
		Short: "Generate an Argon2id hash for the ModelShelf web password",
		Args:  cobra.NoArgs,
		RunE: func(_ *cobra.Command, _ []string) error {
			password, err := application.readPassword(readStdin)
			if err != nil {
				return err
			}
			defer clear(password)
			hash, err := passwordhash.Generate(password)
			if err != nil {
				return err
			}
			fmt.Fprintln(application.Stdout, hash)
			return nil
		},
	}
	command.Flags().BoolVar(
		&readStdin,
		"stdin",
		false,
		"read one password line from stdin without confirmation (for automation)",
	)
	return command
}

type terminalInput interface {
	Fd() uintptr
}

func (application *Application) readPassword(readStdin bool) ([]byte, error) {
	if readStdin {
		password, err := bufio.NewReader(application.Stdin).ReadString('\n')
		if err != nil && !errors.Is(err, io.EOF) {
			return nil, fmt.Errorf("read password from stdin: %w", err)
		}
		return []byte(strings.TrimSuffix(strings.TrimSuffix(password, "\n"), "\r")), nil
	}
	terminal, ok := application.Stdin.(terminalInput)
	if !ok || !charmterm.IsTerminal(terminal.Fd()) {
		return nil, errors.New("stdin is not a terminal; use --stdin to read one password line")
	}
	first, err := application.readHiddenPassword(terminal.Fd(), "Password: ")
	if err != nil {
		return nil, err
	}
	defer clear(first)
	second, err := application.readHiddenPassword(terminal.Fd(), "Confirm password: ")
	if err != nil {
		return nil, err
	}
	defer clear(second)
	if len(first) == 0 {
		return nil, errors.New("password must not be empty")
	}
	if len(first) != len(second) || subtle.ConstantTimeCompare(first, second) != 1 {
		return nil, errors.New("passwords do not match")
	}
	return bytes.Clone(first), nil
}

func (application *Application) readHiddenPassword(fd uintptr, prompt string) ([]byte, error) {
	fmt.Fprint(application.Stderr, prompt)
	password, err := charmterm.ReadPassword(fd)
	fmt.Fprintln(application.Stderr)
	if err != nil {
		return nil, fmt.Errorf("read password: %w", err)
	}
	return password, nil
}

func ExitCode(err error) int {
	if err == nil {
		return ExitOK
	}
	var exit *ExitError
	if errors.As(err, &exit) {
		return exit.Code
	}
	return 1
}

func (application *Application) addCommand() *cobra.Command {
	var revision string
	var modelPath string
	var alias string
	var artifactReference string
	var files []string
	command := &cobra.Command{
		Use:   "add <provider> <model-id>",
		Short: "Add a desired model, then sync it or create a server task",
		Args:  cobra.ExactArgs(2),
		RunE: func(command *cobra.Command, arguments []string) error {
			provider, id := arguments[0], arguments[1]
			if err := validateProvider(provider); err != nil {
				return err
			}
			if artifactReference != "" && command.Flags().Changed("file") {
				return errors.New("--artifact and --file cannot be used together")
			}
			if provider == domain.ProviderFilesystem && !command.Flags().Changed("revision") {
				revision = "content"
			}
			configuration, path, err := application.loadConfig()
			if err != nil {
				return err
			}
			var desired *domain.DesiredModel
			if command.Flags().Changed("alias") && alias != "" {
				desired = findDesiredByAlias(&configuration, alias)
				if desired != nil && (desired.Provider != provider || desired.ID != id) {
					return fmt.Errorf("alias %q already identifies %s", alias,
						config.ModelKey(desired.Provider, desired.ID))
				}
			} else {
				desired, err = findUniqueDesired(&configuration, provider, id)
				if err != nil {
					return err
				}
			}
			if desired == nil {
				configuration.Models = append(configuration.Models, domain.DesiredModel{
					Alias: alias, Provider: provider, ID: id,
					RequestedRevision: revision, Artifact: artifactReference, Path: modelPath, Files: files,
				})
				desired = &configuration.Models[len(configuration.Models)-1]
			} else {
				desired.RequestedRevision = revision
				desired.ResolvedRevision = ""
				desired.Artifact = artifactReference
				desired.Files = domain.CanonicalFiles(files)
				if modelPath != "" {
					desired.Path = modelPath
				}
				if command.Flags().Changed("alias") {
					desired.Alias = alias
				}
			}
			if err := config.Save(configuration, path); err != nil {
				return err
			}
			selectedAlias := desired.Alias
			return application.reconcileAndSync(command.Context(), configuration, path,
				func(candidate domain.DesiredModel) bool {
					if selectedAlias != "" {
						return candidate.Alias == selectedAlias
					}
					return candidate.Provider == provider && candidate.ID == id &&
						candidate.RequestedRevision == revision &&
						candidate.Artifact == artifactReference &&
						domain.FilesKey(candidate.Files) == domain.FilesKey(files)
				}, false, false)
		},
	}
	command.Flags().StringVarP(&revision, "revision", "r", "main", "requested branch, tag, or revision")
	command.Flags().StringVar(&modelPath, "path", "", "additional symbolic-link path for this model")
	command.Flags().StringVar(&alias, "alias", "", "optional unique local alias")
	command.Flags().StringVar(&artifactReference, "artifact", "", "published artifact alias or ID to use")
	command.Flags().StringArrayVar(&files, "file", nil, "file in one complete GGUF variant (repeatable)")
	return command
}

func (application *Application) removeCommand() *cobra.Command {
	var yes bool
	command := &cobra.Command{
		Use:   "remove <alias> | <provider> <model-id>",
		Short: "Remove a desired model and optionally its local files",
		Args:  cobra.RangeArgs(1, 2),
		RunE: func(_ *cobra.Command, arguments []string) error {
			configuration, path, err := application.loadConfig()
			if err != nil {
				return err
			}
			index, err := desiredIndexFromArguments(configuration, arguments)
			if err != nil {
				return err
			}
			if index < 0 {
				return &ExitError{Code: ExitNotReady, Err: errors.New("model is not configured")}
			}
			desired := configuration.Models[index]
			lockPath := lockfile.Path(path)
			locked, lockExists, err := application.loadLock(lockPath, true)
			if err != nil {
				return err
			}
			var removedLock *lockfile.Model
			if entry := lockfile.Find(locked, desired); entry != nil {
				copy := *entry
				removedLock = &copy
			}
			configuration.Models = append(configuration.Models[:index], configuration.Models[index+1:]...)
			if err := config.Save(configuration, path); err != nil {
				return err
			}
			if lockExists {
				key := lockfile.DesiredKey(desired)
				filtered := locked.Models[:0]
				for _, entry := range locked.Models {
					if lockfile.DeclarationKey(
						entry.Alias, entry.Provider, entry.ID, entry.Revision, entry.Files,
					) != key {
						filtered = append(filtered, entry)
					}
				}
				locked.Models = filtered
				if err := lockfile.Save(locked, lockPath); err != nil {
					return err
				}
			}
			if err := syncer.RemoveReferences(configuration, desired); err != nil {
				return err
			}
			if removedLock == nil {
				fmt.Fprintln(application.Stdout, "Removed configuration and references; no locked local artifact was found")
				return nil
			}
			target, err := config.ArtifactPath(configuration, removedLock.RelativePath)
			if err != nil {
				return err
			}
			desired.ResolvedRevision = removedLock.ResolvedRevision
			desired.ArtifactID = removedLock.ArtifactID
			desired.RelativePath = removedLock.RelativePath
			if err := removeUnreferencedRevisionReference(configuration, desired, target, locked); err != nil {
				return err
			}
			shared := false
			for _, entry := range locked.Models {
				if entry.ArtifactID == removedLock.ArtifactID {
					shared = true
					break
				}
			}
			if shared {
				fmt.Fprintln(application.Stdout, "Removed configuration and references; shared artifact files were retained")
				return nil
			}
			deleteFiles := yes
			if !deleteFiles {
				if _, err := os.Stat(target); err == nil {
					deleteFiles, err = application.confirm("Delete unreferenced artifact files at " + target + "? [y/N] ")
					if err != nil {
						return err
					}
				}
			}
			if deleteFiles {
				if err := syncer.RemoveTree(target); err != nil {
					return err
				}
				fmt.Fprintf(application.Stdout, "Removed configuration, references, and artifact files: %s\n", target)
			} else {
				fmt.Fprintln(application.Stdout, "Removed configuration and references; artifact files were retained")
			}
			return nil
		},
	}
	command.Flags().BoolVarP(&yes, "yes", "y", false, "delete local files without prompting")
	return command
}

func (application *Application) searchCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "search <query>",
		Short: "Search server-side model names and ids",
		Args:  cobra.ExactArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			configuration, _, err := application.loadConfig()
			if err != nil {
				return err
			}
			items, err := api.New(configuration.ServerURL, configuration.WriteToken).
				Artifacts(command.Context(), arguments[0])
			if err != nil {
				return err
			}
			writer := tabwriter.NewWriter(application.Stdout, 0, 4, 2, ' ', 0)
			fmt.Fprintln(writer, "PROVIDER\tMODEL\tVERSION\tRESOLVED REVISION\tSIZE")
			for _, item := range items {
				fmt.Fprintf(writer, "%s\t%s\t%s\t%s\t%s\n",
					item.Provider, item.SourceID, item.Version, item.ResolvedRevision, humanSize(item.TotalSize),
				)
			}
			return writer.Flush()
		},
	}
}

func (application *Application) listCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List desired local models and their observed state",
		Args:  cobra.NoArgs,
		RunE: func(_ *cobra.Command, _ []string) error {
			configuration, path, err := application.loadConfig()
			if err != nil {
				return err
			}
			locked, _, err := application.loadLock(lockfile.Path(path), true)
			if err != nil {
				return err
			}
			writer := tabwriter.NewWriter(application.Stdout, 0, 4, 2, ' ', 0)
			fmt.Fprintln(writer, "ALIAS\tPROVIDER\tMODEL\tDESIRED\tSTATE\tREVISION\tSIZE\tSYNCED")
			for _, desired := range configuration.Models {
				if entry := lockfile.Find(locked, desired); entry != nil {
					desired.ResolvedRevision = entry.ResolvedRevision
					desired.ArtifactID = entry.ArtifactID
					desired.RelativePath = entry.RelativePath
				}
				state, revision, size, synced := localState(configuration, desired)
				wanted := desired.ResolvedRevision
				if wanted == "" {
					wanted = desired.RequestedRevision
				}
				displaySize := "—"
				if size >= 0 {
					displaySize = humanSize(size)
				}
				alias := desired.Alias
				if alias == "" {
					alias = "—"
				}
				fmt.Fprintf(writer, "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
					alias, desired.Provider, desired.ID, wanted, state, revision, displaySize, synced,
				)
			}
			return writer.Flush()
		},
	}
}

func (application *Application) syncCommand() *cobra.Command {
	var provider string
	var modelID string
	var update bool
	var frozen bool
	command := &cobra.Command{
		Use:   "sync [alias]",
		Short: "Idempotently reconcile configured models from the NFS mount",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(command *cobra.Command, arguments []string) error {
			alias := ""
			if len(arguments) == 1 {
				alias = arguments[0]
				if provider != "" || modelID != "" {
					return errors.New("alias cannot be combined with --provider or --model-id")
				}
			}
			if provider != "" {
				if err := validateProvider(provider); err != nil {
					return err
				}
			}
			if update && frozen {
				return errors.New("--update and --frozen-lockfile cannot be combined")
			}
			configuration, path, err := application.loadConfig()
			if err != nil {
				return err
			}
			matched := alias == ""
			for _, desired := range configuration.Models {
				if alias != "" && desired.Alias != alias {
					continue
				}
				if provider != "" && desired.Provider != provider {
					continue
				}
				if modelID != "" && desired.ID != modelID {
					continue
				}
				matched = true
			}
			if alias != "" && !matched {
				return &ExitError{
					Code: ExitUnavailable, Err: fmt.Errorf("model alias %q is not configured", alias),
				}
			}
			return application.reconcileAndSync(command.Context(), configuration, path,
				func(desired domain.DesiredModel) bool {
					if alias != "" {
						return desired.Alias == alias
					}
					return (provider == "" || desired.Provider == provider) &&
						(modelID == "" || desired.ID == modelID)
				}, update, frozen)
		},
	}
	command.Flags().StringVar(&provider, "provider", "", "sync only this provider")
	command.Flags().StringVar(&modelID, "model-id", "", "sync only this model id")
	command.Flags().BoolVar(&update, "update", false, "refresh branch or tag resolutions in the lock file")
	command.Flags().BoolVar(&frozen, "frozen-lockfile", false, "fail instead of changing the lock file")
	return command
}

func (application *Application) statusCommand() *cobra.Command {
	var all bool
	command := &cobra.Command{
		Use:   "status <alias> | <provider> <model-id> | --all",
		Short: "Check desired state (exit 0 ready, 2 not ready, 3 corrupt, 4 unavailable)",
		Args: func(command *cobra.Command, arguments []string) error {
			if all {
				if len(arguments) != 0 {
					return errors.New("--all cannot be combined with a model argument")
				}
				return nil
			}
			return cobra.RangeArgs(1, 2)(command, arguments)
		},
		RunE: func(command *cobra.Command, arguments []string) error {
			configuration, path, err := application.loadConfig()
			if err != nil {
				return err
			}
			locked, _, err := application.loadLock(lockfile.Path(path), true)
			if err != nil {
				return err
			}
			if all {
				failures := 0
				worstExitCode := ExitOK
				for _, desired := range configuration.Models {
					artifactID, statusErr := application.desiredStatus(
						command.Context(), configuration, locked, desired,
					)
					label := desired.Alias
					if label == "" {
						label = desired.Provider + "/" + desired.ID
					}
					if statusErr == nil {
						fmt.Fprintf(application.Stdout, "%s: ready: %s\n", label, artifactID)
						continue
					}
					code := ExitCode(statusErr)
					if code == 1 {
						return statusErr
					}
					failures++
					if code > worstExitCode {
						worstExitCode = code
					}
					fmt.Fprintf(application.Stdout, "%s: %s\n", label, statusErr)
				}
				if failures != 0 {
					return &ExitError{
						Code: worstExitCode,
						Err: fmt.Errorf(
							"%d of %d configured models are not ready",
							failures,
							len(configuration.Models),
						),
					}
				}
				fmt.Fprintf(application.Stdout, "ready: %d configured models\n", len(configuration.Models))
				return nil
			}
			desired, err := desiredFromArguments(&configuration, arguments)
			if err != nil {
				return err
			}
			if desired == nil {
				return &ExitError{Code: ExitUnavailable, Err: errors.New("unavailable: model is not configured")}
			}
			artifactID, err := application.desiredStatus(
				command.Context(), configuration, locked, *desired,
			)
			if err != nil {
				return err
			}
			fmt.Fprintf(application.Stdout, "ready: %s\n", artifactID)
			return nil
		},
	}
	command.Flags().BoolVar(&all, "all", false, "check every configured model")
	return command
}

func (application *Application) desiredStatus(
	ctx context.Context,
	configuration config.Config,
	locked lockfile.File,
	desired domain.DesiredModel,
) (string, error) {
	entry := lockfile.Find(locked, desired)
	if entry == nil {
		return "", &ExitError{Code: ExitNotReady, Err: errors.New("not ready: model is not locked; run modelshelf sync")}
	}
	desired.ResolvedRevision = entry.ResolvedRevision
	desired.ArtifactID = entry.ArtifactID
	desired.RelativePath = entry.RelativePath
	provider, id := desired.Provider, desired.ID
	target, err := config.ArtifactPath(configuration, entry.RelativePath)
	if err != nil {
		return "", err
	}
	if info, statErr := os.Stat(target); statErr != nil || !info.IsDir() {
		message := "not ready: local directory is missing"
		if configuration.WriteToken != "" {
			tasks, taskErr := api.New(configuration.ServerURL, configuration.WriteToken).
				Tasks(ctx)
			if taskErr == nil {
				matching := []domain.DownloadTask{}
				for _, task := range tasks {
					if task.Provider == provider && task.SourceID == id {
						matching = append(matching, task)
					}
				}
				sort.Slice(matching, func(left, right int) bool {
					return matching[left].CreatedAt.After(matching[right].CreatedAt)
				})
				if len(matching) != 0 {
					latest := matching[0]
					message += fmt.Sprintf("; server task %s %d%%", latest.Status, latest.Progress)
					if latest.Error != "" {
						message += " (" + latest.Error + ")"
					}
				}
			}
		}
		return "", &ExitError{Code: ExitNotReady, Err: errors.New(message)}
	}
	failures, err := catalog.Verify(target, catalog.VerifyOptions{})
	if err != nil {
		return "", &ExitError{Code: ExitCorrupt, Err: fmt.Errorf("corrupt: %w", err)}
	}
	if len(failures) != 0 {
		return "", &ExitError{Code: ExitCorrupt, Err: errors.New("corrupt: " + strings.Join(failures, "; "))}
	}
	manifest, err := catalog.ReadManifest(target)
	if err != nil {
		return "", &ExitError{Code: ExitCorrupt, Err: err}
	}
	if !manifestMatchesDesired(manifest, desired) {
		return "", &ExitError{
			Code: ExitNotReady,
			Err:  errors.New("not ready: local artifact identity differs from desired state"),
		}
	}
	if failures := syncer.ReferenceFailures(configuration, desired, target); len(failures) != 0 {
		return "", &ExitError{Code: ExitNotReady, Err: errors.New("not ready: " + strings.Join(failures, "; "))}
	}
	return manifest.ArtifactID, nil
}

func (application *Application) verifyCommand() *cobra.Command {
	var full bool
	var unexpected bool
	command := &cobra.Command{
		Use:   "verify <model-path-or-alias>",
		Short: "Verify manifest, expected paths and sizes; --full also checks SHA-256",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, arguments []string) error {
			target := ""
			var configuredDesired *domain.DesiredModel
			configuration, configPath, configErr := application.loadConfig()
			if configErr == nil {
				if desired := findDesiredByAlias(&configuration, arguments[0]); desired != nil {
					configuredDesired = desired
					locked, _, lockErr := application.loadLock(lockfile.Path(configPath), true)
					if lockErr != nil {
						return lockErr
					}
					if entry := lockfile.Find(locked, *desired); entry != nil {
						var err error
						target, err = config.ArtifactPath(configuration, entry.RelativePath)
						if err != nil {
							return err
						}
						desired.ResolvedRevision = entry.ResolvedRevision
						desired.ArtifactID = entry.ArtifactID
						desired.RelativePath = entry.RelativePath
					}
				}
			}
			if configuredDesired != nil && target == "" {
				return &ExitError{Code: ExitNotReady, Err: errors.New("model is not locked; run modelshelf sync")}
			}
			if target == "" {
				var err error
				target, err = config.ExpandPath(arguments[0])
				if err != nil {
					return err
				}
			}
			failures, err := catalog.Verify(target, catalog.VerifyOptions{
				Full: full, Unexpected: unexpected,
			})
			if err != nil {
				return &ExitError{Code: ExitCorrupt, Err: fmt.Errorf("invalid manifest: %w", err)}
			}
			if len(failures) != 0 {
				for _, failure := range failures {
					fmt.Fprintln(application.Stderr, "FAIL", failure)
				}
				return &ExitError{Code: ExitCorrupt, Err: errors.New("verification failed")}
			}
			if configuredDesired != nil {
				if failures := syncer.ReferenceFailures(configuration, *configuredDesired, target); len(failures) != 0 {
					for _, failure := range failures {
						fmt.Fprintln(application.Stderr, "FAIL", failure)
					}
					return &ExitError{Code: ExitNotReady, Err: errors.New("reference verification failed")}
				}
			}
			fmt.Fprintln(application.Stdout, "verified")
			return nil
		},
	}
	command.Flags().BoolVar(&full, "full", false, "check every file SHA-256")
	command.Flags().BoolVar(&unexpected, "unexpected", false, "report files absent from the manifest")
	return command
}

func (application *Application) mountCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "mount",
		Short: "Mount the server's read-only NFSv4 export",
		Args:  cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			configuration, _, err := application.loadConfig()
			if err != nil {
				return err
			}
			return mount.Mount(
				command.Context(), configuration, api.New(configuration.ServerURL, configuration.WriteToken),
			)
		},
	}
}

func (application *Application) unmountCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "unmount",
		Short: "Unmount the configured NFS export",
		Args:  cobra.NoArgs,
		RunE: func(command *cobra.Command, _ []string) error {
			configuration, _, err := application.loadConfig()
			if err != nil {
				return err
			}
			return mount.Unmount(command.Context(), configuration)
		},
	}
}

func (application *Application) tuiCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "tui",
		Short: "Browse and filter local and server models interactively",
		Args:  cobra.NoArgs,
		RunE: func(_ *cobra.Command, _ []string) error {
			configuration, path, err := application.loadConfig()
			if err != nil {
				return err
			}
			locked, _, err := application.loadLock(lockfile.Path(path), true)
			if err != nil {
				return err
			}
			for index := range configuration.Models {
				if entry := lockfile.Find(locked, configuration.Models[index]); entry != nil {
					configuration.Models[index].ResolvedRevision = entry.ResolvedRevision
					configuration.Models[index].ArtifactID = entry.ArtifactID
					configuration.Models[index].RelativePath = entry.RelativePath
				}
			}
			return tui.Run(configuration, api.New(configuration.ServerURL, configuration.WriteToken))
		},
	}
}

func (application *Application) loadConfig() (config.Config, string, error) {
	return config.Load(application.ConfigPath)
}

func (application *Application) loadLock(path string, tolerateInvalid bool) (lockfile.File, bool, error) {
	locked, exists, err := lockfile.Load(path)
	if err == nil {
		return locked, exists, nil
	}
	var invalid *lockfile.InvalidError
	if !tolerateInvalid || !errors.As(err, &invalid) {
		return lockfile.File{}, false, err
	}
	var unsupported *lockfile.UnsupportedSchemaVersionError
	if errors.As(err, &unsupported) && unsupported.Version > lockfile.CurrentSchemaVersion {
		return lockfile.File{}, false, err
	}
	fmt.Fprintf(application.Stderr,
		"Generated lock file %s is incompatible; the next non-frozen sync will rebuild it.\n", path)
	return lockfile.Empty(), false, nil
}

func (application *Application) confirm(prompt string) (bool, error) {
	fmt.Fprint(application.Stdout, prompt)
	line, err := bufio.NewReader(application.Stdin).ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return false, err
	}
	answer := strings.ToLower(strings.TrimSpace(line))
	return answer == "y" || answer == "yes", nil
}

func (application *Application) reconcileAndSync(
	ctx context.Context,
	configuration config.Config,
	configPath string,
	selected func(domain.DesiredModel) bool,
	update bool,
	frozen bool,
) error {
	if err := config.EnsureLocalLayout(configuration); err != nil {
		return err
	}
	client := api.New(configuration.ServerURL, configuration.WriteToken)
	artifacts, err := client.Artifacts(ctx, "")
	if err != nil {
		return err
	}
	lockPath := lockfile.Path(configPath)
	current, exists, err := application.loadLock(lockPath, !frozen)
	if err != nil {
		return err
	}
	if frozen && !exists {
		return &ExitError{Code: ExitUnavailable, Err: errors.New("frozen lock file is missing: " + lockPath)}
	}
	if frozen && !lockMatchesConfig(current, configuration) {
		return &ExitError{Code: ExitUnavailable, Err: errors.New(
			"config and lock file differ; run sync without --frozen-lockfile",
		)}
	}
	now := time.Now().UTC()
	candidate := lockfile.Empty()
	lockedArtifacts := map[string]domain.ArtifactSummary{}
	for _, desired := range configuration.Models {
		entry := lockfile.Find(current, desired)
		if entry != nil && !lockEntryMatchesDesired(*entry, desired) {
			entry = nil
		}
		var artifact *domain.ArtifactSummary
		if entry != nil && !update {
			artifact = findArtifactByID(artifacts, entry.ArtifactID)
			if artifact == nil || artifact.ResolvedRevision != entry.ResolvedRevision {
				return &ExitError{Code: ExitUnavailable, Err: fmt.Errorf(
					"locked artifact is unavailable for %s @ %s", displayModel(desired), entry.ResolvedRevision,
				)}
			}
		} else {
			artifact = syncer.SelectArtifact(artifacts, desired)
			if artifact == nil {
				return application.handleUnavailable(ctx, client, configuration, desired)
			}
		}
		lockedAt := now
		if entry != nil && entry.ArtifactID == artifact.ArtifactID {
			lockedAt = entry.LockedAt
		}
		locked := lockfile.Model{
			Alias: desired.Alias, Provider: desired.Provider, ID: desired.ID,
			Revision: desired.RequestedRevision, Artifact: desired.Artifact,
			Path: desired.Path, Files: selectedFiles(desired, *artifact),
			ResolvedRevision: artifact.ResolvedRevision, ArtifactID: artifact.ArtifactID,
			RelativePath: artifact.RelativePath, LockedAt: lockedAt,
		}
		candidate.Models = append(candidate.Models, locked)
		lockedArtifacts[lockfile.DesiredKey(desired)] = *artifact
	}
	if frozen && !lockfile.Equal(current, candidate) {
		return &ExitError{Code: ExitUnavailable, Err: errors.New(
			"config and lock file differ; run sync without --frozen-lockfile",
		)}
	}
	if !frozen && !lockfile.Equal(current, candidate) {
		if err := lockfile.Save(candidate, lockPath); err != nil {
			return err
		}
		fmt.Fprintf(application.Stdout, "Updated lock file %s\n", lockPath)
	}
	failures := 0
	for _, desired := range configuration.Models {
		if !selected(desired) {
			continue
		}
		key := lockfile.DesiredKey(desired)
		artifact := lockedArtifacts[key]
		desired.ResolvedRevision = artifact.ResolvedRevision
		desired.ArtifactID = artifact.ArtifactID
		desired.RelativePath = artifact.RelativePath
		desired.Files = selectedFiles(desired, artifact)
		result, syncErr := syncer.SyncArtifact(ctx, configuration, desired, artifact)
		if syncErr != nil {
			failures++
			fmt.Fprintf(application.Stderr, "failed %s: %v\n", displayModel(desired), syncErr)
			continue
		}
		fmt.Fprintf(application.Stdout, "ready %s @ %s\n", displayModel(desired), result.ResolvedRevision)
	}
	if failures != 0 {
		return &ExitError{Code: ExitNotReady, Err: fmt.Errorf("%d model sync(s) failed", failures)}
	}
	if err := reconcileLocalReferences(configuration, current, candidate); err != nil {
		return err
	}
	return nil
}

func reconcileLocalReferences(
	configuration config.Config, previous, desiredLock lockfile.File,
) error {
	desiredReferences := map[string]struct{}{}
	for _, desired := range configuration.Models {
		entry := lockfile.Find(desiredLock, desired)
		if entry == nil {
			continue
		}
		desired.ResolvedRevision = entry.ResolvedRevision
		desired.ArtifactID = entry.ArtifactID
		desired.RelativePath = entry.RelativePath
		artifactPath, err := config.ArtifactPath(configuration, entry.RelativePath)
		if err != nil {
			return err
		}
		references, err := syncer.ManagedReferencePaths(configuration, desired, artifactPath)
		if err != nil {
			return err
		}
		for _, reference := range references {
			desiredReferences[filepath.Clean(reference)] = struct{}{}
		}
		manifest, readErr := catalog.ReadManifest(artifactPath)
		if readErr == nil && manifest.ArtifactID == entry.ArtifactID {
			if failures, verifyErr := catalog.Verify(artifactPath, catalog.VerifyOptions{}); verifyErr == nil && len(failures) == 0 {
				if err := syncer.EnsureReferences(configuration, desired, artifactPath); err != nil {
					return err
				}
			}
		}
	}
	for _, old := range previous.Models {
		oldDesired := domain.DesiredModel{
			Alias: old.Alias, Provider: old.Provider, ID: old.ID,
			RequestedRevision: old.Revision, ResolvedRevision: old.ResolvedRevision,
			ArtifactID: old.ArtifactID, RelativePath: old.RelativePath, Path: old.Path,
			Files: old.Files,
		}
		artifactPath, err := config.ArtifactPath(configuration, old.RelativePath)
		if err != nil {
			return err
		}
		references, err := syncer.ManagedReferencePaths(configuration, oldDesired, artifactPath)
		if err != nil {
			return err
		}
		for _, reference := range references {
			if _, retained := desiredReferences[filepath.Clean(reference)]; retained {
				continue
			}
			if err := syncer.RemoveReference(configuration, reference); err != nil {
				return err
			}
		}
	}
	return nil
}

func removeUnreferencedRevisionReference(
	configuration config.Config,
	removed domain.DesiredModel,
	removedArtifactPath string,
	remaining lockfile.File,
) error {
	reference, exists, err := config.RevisionReferencePath(configuration, removed, removedArtifactPath)
	if err != nil || !exists {
		return err
	}
	for _, entry := range remaining.Models {
		artifactPath, pathErr := config.ArtifactPath(configuration, entry.RelativePath)
		if pathErr != nil {
			return pathErr
		}
		desired := domain.DesiredModel{
			Provider: entry.Provider, ID: entry.ID,
			RequestedRevision: entry.Revision, ResolvedRevision: entry.ResolvedRevision,
			ArtifactID: entry.ArtifactID, RelativePath: entry.RelativePath, Files: entry.Files,
		}
		candidate, candidateExists, candidateErr := config.RevisionReferencePath(
			configuration, desired, artifactPath,
		)
		if candidateErr != nil {
			return candidateErr
		}
		if candidateExists && filepath.Clean(candidate) == filepath.Clean(reference) {
			return nil
		}
	}
	return syncer.RemoveReference(configuration, reference)
}

func (application *Application) handleUnavailable(
	ctx context.Context, client *api.Client, configuration config.Config, desired domain.DesiredModel,
) error {
	if desired.Artifact != "" {
		return &ExitError{Code: ExitUnavailable, Err: fmt.Errorf(
			"published artifact %q is unavailable", desired.Artifact,
		)}
	}
	if desired.Provider == domain.ProviderFilesystem {
		return &ExitError{Code: ExitUnavailable, Err: errors.New(
			"filesystem artifact is unavailable; import it with modelshelf-server import",
		)}
	}
	if configuration.WriteToken == "" {
		return &ExitError{Code: ExitUnavailable, Err: fmt.Errorf(
			"artifact is unavailable for %s revision %q and no writeToken is configured",
			displayModel(desired), desired.RequestedRevision,
		)}
	}
	if tasks, err := client.Tasks(ctx); err == nil {
		for _, task := range tasks {
			if task.Provider == desired.Provider && task.SourceID == desired.ID &&
				task.RequestedRevision == desired.RequestedRevision &&
				domain.FilesKey(task.SelectedPaths) == domain.FilesKey(desired.Files) &&
				task.Status != "failed" && task.Status != "cancelled" &&
				task.Status != "completed" {
				fmt.Fprintf(application.Stdout, "Download task %s is %s for %s\n",
					task.ID, task.Status, displayModel(desired))
				return &ExitError{Code: ExitNotReady, Err: errors.New("artifact download is not ready")}
			}
		}
	}
	task, err := client.CreateTask(
		ctx, desired.Provider, desired.ID, desired.RequestedRevision, desired.Files,
	)
	if err != nil {
		return &ExitError{Code: ExitUnavailable, Err: fmt.Errorf(
			"revision %q could not be requested: %w", desired.RequestedRevision, err,
		)}
	}
	fmt.Fprintf(application.Stdout, "Download task created %s for %s\n", task.ID, displayModel(desired))
	return &ExitError{Code: ExitNotReady, Err: errors.New("artifact download is not ready")}
}

func findArtifactByID(
	artifacts []domain.ArtifactSummary, artifactID string,
) *domain.ArtifactSummary {
	for index := range artifacts {
		if artifacts[index].ArtifactID == artifactID {
			return &artifacts[index]
		}
	}
	return nil
}

func displayModel(desired domain.DesiredModel) string {
	if desired.Alias != "" {
		return desired.Alias
	}
	return config.ModelKey(desired.Provider, desired.ID)
}

func lockMatchesConfig(locked lockfile.File, configuration config.Config) bool {
	if len(locked.Models) != len(configuration.Models) {
		return false
	}
	for _, desired := range configuration.Models {
		entry := lockfile.Find(locked, desired)
		if entry == nil || !lockEntryMatchesDesired(*entry, desired) {
			return false
		}
	}
	return true
}

func lockEntryMatchesDesired(entry lockfile.Model, desired domain.DesiredModel) bool {
	matches := entry.Alias == desired.Alias && entry.Provider == desired.Provider &&
		entry.ID == desired.ID && entry.Revision == desired.RequestedRevision &&
		entry.Path == desired.Path
	if desired.Artifact != "" {
		return matches && entry.Artifact == desired.Artifact
	}
	return matches && domain.FilesKey(entry.Files) == domain.FilesKey(desired.Files)
}

func selectedFiles(desired domain.DesiredModel, artifact domain.ArtifactSummary) []string {
	if desired.Artifact != "" {
		return artifact.SelectedPaths
	}
	return desired.Files
}

func findDesired(configuration *config.Config, provider, id string) *domain.DesiredModel {
	for index := range configuration.Models {
		if configuration.Models[index].Provider == provider && configuration.Models[index].ID == id {
			return &configuration.Models[index]
		}
	}
	return nil
}

func findUniqueDesired(
	configuration *config.Config, provider, id string,
) (*domain.DesiredModel, error) {
	var result *domain.DesiredModel
	for index := range configuration.Models {
		candidate := &configuration.Models[index]
		if candidate.Provider != provider || candidate.ID != id {
			continue
		}
		if result != nil {
			return nil, fmt.Errorf("%s has multiple revisions; use an alias", config.ModelKey(provider, id))
		}
		result = candidate
	}
	return result, nil
}

func findDesiredByAlias(configuration *config.Config, alias string) *domain.DesiredModel {
	for index := range configuration.Models {
		if configuration.Models[index].Alias == alias {
			return &configuration.Models[index]
		}
	}
	return nil
}

func desiredFromArguments(
	configuration *config.Config, arguments []string,
) (*domain.DesiredModel, error) {
	if len(arguments) == 1 {
		return findDesiredByAlias(configuration, arguments[0]), nil
	}
	if err := validateProvider(arguments[0]); err != nil {
		return nil, err
	}
	return findUniqueDesired(configuration, arguments[0], arguments[1])
}

func desiredIndexFromArguments(configuration config.Config, arguments []string) (int, error) {
	if len(arguments) == 1 {
		for index := range configuration.Models {
			if configuration.Models[index].Alias == arguments[0] {
				return index, nil
			}
		}
		return -1, nil
	}
	if err := validateProvider(arguments[0]); err != nil {
		return -1, err
	}
	matched := -1
	for index := range configuration.Models {
		candidate := configuration.Models[index]
		if candidate.Provider == arguments[0] && candidate.ID == arguments[1] {
			if matched >= 0 {
				return -1, fmt.Errorf("%s has multiple revisions; use an alias",
					config.ModelKey(arguments[0], arguments[1]))
			}
			matched = index
		}
	}
	return matched, nil
}

func desiredIndex(configuration config.Config, provider, id string) int {
	for index := range configuration.Models {
		if configuration.Models[index].Provider == provider && configuration.Models[index].ID == id {
			return index
		}
	}
	return -1
}

func validateProvider(provider string) error {
	if !domain.ValidProvider(provider) {
		return fmt.Errorf("invalid provider %q", provider)
	}
	return nil
}

func localState(
	configuration config.Config, desired domain.DesiredModel,
) (state, revision string, size int64, synced string) {
	target, err := config.ArtifactPath(configuration, desired.RelativePath)
	if err != nil {
		return "missing", "—", -1, "—"
	}
	manifest, err := catalog.ReadManifest(target)
	if err != nil {
		if _, statErr := os.Stat(target); errors.Is(statErr, os.ErrNotExist) {
			return "missing", "—", -1, "—"
		}
		return "corrupt", "—", -1, "—"
	}
	failures, verifyErr := catalog.Verify(target, catalog.VerifyOptions{})
	state = "ready"
	if verifyErr != nil || len(failures) != 0 {
		state = "corrupt"
	} else if !manifestMatchesDesired(manifest, desired) {
		state = "stale"
	} else if len(syncer.ReferenceFailures(configuration, desired, target)) != 0 {
		state = "stale"
	}
	synced = "—"
	data, readErr := os.ReadFile(target + string(os.PathSeparator) + ".modelshelf" +
		string(os.PathSeparator) + "sync.json")
	if readErr == nil {
		var syncState map[string]any
		if json.Unmarshal(data, &syncState) == nil {
			version, hasVersion := syncState["schemaVersion"].(float64)
			if !hasVersion || version == 1 {
				if value, ok := syncState["syncedAt"].(string); ok {
					synced = value
				}
			}
		}
	}
	return state, manifest.Source.ResolvedRevision, manifest.TotalSize, synced
}

func manifestMatchesDesired(
	manifest domain.ArtifactManifest, desired domain.DesiredModel,
) bool {
	if desired.Artifact != "" && desired.ArtifactID != "" {
		return manifest.ArtifactID == desired.ArtifactID
	}
	if manifest.Source.Provider != desired.Provider || manifest.Source.ID != desired.ID {
		return false
	}
	if domain.FilesKey(manifest.Source.SelectedPaths) != domain.FilesKey(desired.Files) {
		return false
	}
	if desired.ResolvedRevision != "" {
		return manifest.Source.ResolvedRevision == desired.ResolvedRevision
	}
	return manifest.Source.RequestedRevision == desired.RequestedRevision ||
		manifest.Source.ResolvedRevision == desired.RequestedRevision
}

func humanSize(value int64) string {
	size := float64(value)
	for _, unit := range []string{"B", "KiB", "MiB", "GiB", "TiB"} {
		if size < 1024 || unit == "TiB" {
			return fmt.Sprintf("%.1f %s", size, unit)
		}
		size /= 1024
	}
	return fmt.Sprint(value)
}

func versionString(version, commit string) string {
	if version == "" {
		version = "dev"
	}
	if commit != "" && commit != "unknown" {
		return version + " (" + commit + ")"
	}
	return version
}

func Execute(ctx context.Context, version, commit string) error {
	command := New(version, commit)
	command.SetContext(ctx)
	return command.Execute()
}
