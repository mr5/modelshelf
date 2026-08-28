package tui

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/mr5/modelshelf/client/internal/api"
	"github.com/mr5/modelshelf/client/internal/catalog"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/syncer"
)

type row struct {
	location string
	provider string
	model    string
	revision string
	files    string
	size     string
	state    string
}

type dataMessage struct {
	rows        []row
	serverCount int
	localCount  int
	remoteError error
}

type model struct {
	configuration config.Config
	client        *api.Client
	search        textinput.Model
	rows          []row
	serverCount   int
	localCount    int
	remoteError   error
	loading       bool
	offset        int
	height        int
	width         int
}

var (
	titleStyle   = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("63"))
	headerStyle  = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("245"))
	serverStyle  = lipgloss.NewStyle().Foreground(lipgloss.Color("81"))
	readyStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("42"))
	problemStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("203"))
	mutedStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("241"))
)

func Run(configuration config.Config, client *api.Client) error {
	input := textinput.New()
	input.Placeholder = "Filter server and local models…"
	input.Prompt = "/ "
	input.Focus()
	local := localRows(configuration)
	application := model{
		configuration: configuration,
		client:        client,
		search:        input,
		rows:          local,
		localCount:    len(configuration.Models),
		loading:       true,
		height:        24,
		width:         120,
	}
	_, err := tea.NewProgram(application, tea.WithAltScreen()).Run()
	return err
}

func (application model) Init() tea.Cmd {
	return application.loadData()
}

func (application model) Update(message tea.Msg) (tea.Model, tea.Cmd) {
	switch message := message.(type) {
	case tea.KeyMsg:
		switch message.String() {
		case "ctrl+c", "esc":
			return application, tea.Quit
		case "ctrl+r":
			application.loading = true
			return application, application.loadData()
		case "up":
			if application.offset > 0 {
				application.offset--
			}
			return application, nil
		case "down":
			if application.offset < max(0, len(application.filteredRows())-application.visibleRows()) {
				application.offset++
			}
			return application, nil
		}
	case tea.WindowSizeMsg:
		application.width = message.Width
		application.height = message.Height
		application.search.Width = max(20, min(80, message.Width-6))
	case dataMessage:
		application.rows = message.rows
		application.serverCount = message.serverCount
		application.localCount = message.localCount
		application.remoteError = message.remoteError
		application.loading = false
		application.offset = 0
	}
	var command tea.Cmd
	application.search, command = application.search.Update(message)
	if application.offset > max(0, len(application.filteredRows())-application.visibleRows()) {
		application.offset = max(0, len(application.filteredRows())-application.visibleRows())
	}
	return application, command
}

func (application model) View() string {
	var output strings.Builder
	output.WriteString(titleStyle.Render("ModelShelf"))
	output.WriteString("  ")
	output.WriteString(mutedStyle.Render("local + server catalog"))
	output.WriteString("\n\n")
	output.WriteString(application.search.View())
	output.WriteString("\n\n")
	output.WriteString(headerStyle.Render(formatRow(row{
		location: "LOCATION",
		provider: "PROVIDER",
		model:    "MODEL",
		revision: "REVISION",
		files:    "FILES",
		size:     "SIZE",
		state:    "STATE",
	})))
	output.WriteByte('\n')
	rows := application.filteredRows()
	visible := application.visibleRows()
	end := min(len(rows), application.offset+visible)
	for _, item := range rows[application.offset:end] {
		line := formatRow(item)
		if item.location == "server" {
			line = serverStyle.Render(line)
		} else if item.state == "ready" {
			line = readyStyle.Render(line)
		} else if item.state == "corrupt" {
			line = problemStyle.Render(line)
		}
		output.WriteString(line)
		output.WriteByte('\n')
	}
	if len(rows) == 0 && !application.loading {
		output.WriteString(mutedStyle.Render("No matching models."))
		output.WriteByte('\n')
	}
	output.WriteByte('\n')
	if application.loading {
		output.WriteString("Refreshing…")
	} else {
		output.WriteString(fmt.Sprintf(
			"%d server artifacts · %d desired local models", application.serverCount, application.localCount,
		))
	}
	if application.remoteError != nil {
		output.WriteString(problemStyle.Render(" · server unavailable: " + application.remoteError.Error()))
	}
	output.WriteByte('\n')
	output.WriteString(mutedStyle.Render("↑/↓ scroll · Ctrl+R refresh · Esc quit"))
	return output.String()
}

func (application model) loadData() tea.Cmd {
	return func() tea.Msg {
		rows := []row{}
		artifacts, remoteError := application.client.Artifacts(context.Background(), "")
		for _, artifact := range artifacts {
			rows = append(rows, row{
				location: "server",
				provider: artifact.Provider,
				model:    artifact.SourceID,
				revision: artifact.ResolvedRevision,
				files:    fmt.Sprint(artifact.FileCount),
				size:     humanSize(artifact.TotalSize),
				state:    "available",
			})
		}
		rows = append(rows, localRows(application.configuration)...)
		sort.SliceStable(rows, func(left, right int) bool {
			if rows[left].location != rows[right].location {
				return rows[left].location < rows[right].location
			}
			return rows[left].model < rows[right].model
		})
		return dataMessage{
			rows:        rows,
			serverCount: len(artifacts),
			localCount:  len(application.configuration.Models),
			remoteError: remoteError,
		}
	}
}

func localRows(configuration config.Config) []row {
	rows := make([]row, 0, len(configuration.Models))
	for _, desired := range configuration.Models {
		item := row{
			location: "local",
			provider: desired.Provider,
			model:    desired.ID,
			revision: desired.ResolvedRevision,
			files:    "—",
			size:     "—",
			state:    "missing",
		}
		if item.revision == "" {
			item.revision = desired.RequestedRevision
		}
		target, err := config.ArtifactPath(configuration, desired.RelativePath)
		if err == nil {
			if manifest, readErr := catalog.ReadManifest(target); readErr == nil {
				item.files = fmt.Sprint(manifest.FileCount)
				item.size = humanSize(manifest.TotalSize)
				item.revision = manifest.Source.ResolvedRevision
				failures, verifyErr := catalog.Verify(target, catalog.VerifyOptions{})
				if verifyErr == nil && len(failures) == 0 {
					item.state = "ready"
					identityMismatch := manifest.Source.Provider != desired.Provider ||
						manifest.Source.ID != desired.ID
					if desired.ResolvedRevision != "" {
						identityMismatch = identityMismatch ||
							manifest.Source.ResolvedRevision != desired.ResolvedRevision
					} else {
						identityMismatch = identityMismatch ||
							(manifest.Source.RequestedRevision != desired.RequestedRevision &&
								manifest.Source.ResolvedRevision != desired.RequestedRevision)
					}
					if identityMismatch {
						item.state = "stale"
					} else if len(syncer.ReferenceFailures(configuration, desired, target)) != 0 {
						item.state = "stale"
					}
				} else {
					item.state = "corrupt"
				}
			}
		}
		rows = append(rows, item)
	}
	return rows
}

func (application model) filteredRows() []row {
	query := strings.ToLower(strings.TrimSpace(application.search.Value()))
	if query == "" {
		return application.rows
	}
	result := []row{}
	for _, item := range application.rows {
		haystack := strings.ToLower(strings.Join([]string{
			item.location, item.provider, item.model, item.revision, item.state,
		}, " "))
		if strings.Contains(haystack, query) {
			result = append(result, item)
		}
	}
	return result
}

func (application model) visibleRows() int {
	return max(3, application.height-10)
}

func formatRow(item row) string {
	return fmt.Sprintf(
		"%-9s  %-14s  %-38s  %-14s  %7s  %10s  %-10s",
		truncate(item.location, 9),
		truncate(item.provider, 14),
		truncate(item.model, 38),
		truncate(item.revision, 14),
		truncate(item.files, 7),
		truncate(item.size, 10),
		truncate(item.state, 10),
	)
}

func truncate(value string, width int) string {
	runes := []rune(value)
	if len(runes) <= width {
		return value
	}
	if width <= 1 {
		return string(runes[:width])
	}
	return string(runes[:width-1]) + "…"
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
