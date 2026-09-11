package cli

import (
	"fmt"
	"github.com/mr5/modelshelf/client/internal/config"
	"github.com/mr5/modelshelf/client/internal/distribution"
	"github.com/spf13/cobra"
)

func (a *Application) exportCommand() *cobra.Command {
	root := &cobra.Command{Use: "export", Short: "Manage read-only model distribution (Linux NFS-Ganesha)"}
	for _, action := range []string{"plan", "enable", "disable", "status"} {
		root.AddCommand(&cobra.Command{Use: action, Args: cobra.NoArgs, RunE: func(cmd *cobra.Command, _ []string) error {
			c, _, err := config.Load(a.ConfigPath)
			if err != nil {
				return err
			}
			switch action {
			case "plan":
				conf, unit, err := distribution.Plan(c)
				if err != nil {
					return err
				}
				fmt.Fprintf(a.Stdout, "# /etc/ganesha/modelshelf.conf\n%s\n# /etc/systemd/system/modelshelf-export.service\n%s", conf, unit)
				return nil
			case "enable":
				return distribution.Enable(cmd.Context(), c)
			case "disable":
				return distribution.Disable(cmd.Context())
			default:
				fmt.Fprintf(a.Stdout, "Published root: %s\nNFS port: %d\n", config.PublishedRoot(c), c.Distribution.NFSPort())
				if c.Distribution != nil {
					fmt.Fprintf(a.Stdout, "Publication enabled: %t\nAllowed clients: %v\n", c.Distribution.Enabled, c.Distribution.Allow)
				}
				return distribution.Status(cmd.Context())
			}
		}})
	}
	return root
}
