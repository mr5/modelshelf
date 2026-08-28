package main

import (
	"context"
	"fmt"
	"os"

	"github.com/modelshelf/modelshelf/client/internal/cli"
)

var (
	version = "dev"
	commit  = "unknown"
)

func main() {
	err := cli.Execute(context.Background(), version, commit)
	if err == nil {
		return
	}
	fmt.Fprintln(os.Stderr, err)
	os.Exit(cli.ExitCode(err))
}
