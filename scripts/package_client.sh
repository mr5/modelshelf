#!/bin/sh
set -eu

modelshelf_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
modelshelf_client="$modelshelf_root/packages/client"
modelshelf_output=${OUTPUT_DIR:-$modelshelf_client/dist}
modelshelf_version=${CLIENT_VERSION:-dev}
modelshelf_commit=${CLIENT_COMMIT:-unknown}
modelshelf_temporary=$(mktemp -d "${TMPDIR:-/tmp}/modelshelf-package.XXXXXX")
trap 'rm -rf "$modelshelf_temporary"' EXIT HUP INT TERM

mkdir -p "$modelshelf_output"
for modelshelf_target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
  modelshelf_os=${modelshelf_target%/*}
  modelshelf_arch=${modelshelf_target#*/}
  modelshelf_stage="$modelshelf_temporary/${modelshelf_os}_${modelshelf_arch}"
  modelshelf_archive="modelshelf_${modelshelf_os}_${modelshelf_arch}.tar.gz"
  mkdir -p "$modelshelf_stage"
  (
    cd "$modelshelf_client"
    CGO_ENABLED=0 GOOS="$modelshelf_os" GOARCH="$modelshelf_arch" go build \
      -trimpath \
      -ldflags "-s -w -X main.version=$modelshelf_version -X main.commit=$modelshelf_commit" \
      -o "$modelshelf_stage/modelshelf" \
      ./cmd/modelshelf
  )
  cp "$modelshelf_client/README.md" "$modelshelf_stage/README.md"
  tar -C "$modelshelf_stage" -czf "$modelshelf_output/$modelshelf_archive" modelshelf README.md
done

: > "$modelshelf_output/checksums.txt"
for modelshelf_archive in "$modelshelf_output"/modelshelf_*.tar.gz; do
  if command -v sha256sum >/dev/null 2>&1; then
    modelshelf_digest=$(sha256sum "$modelshelf_archive" | awk '{print $1}')
  else
    modelshelf_digest=$(shasum -a 256 "$modelshelf_archive" | awk '{print $1}')
  fi
  printf '%s  %s\n' "$modelshelf_digest" "$(basename "$modelshelf_archive")" \
    >> "$modelshelf_output/checksums.txt"
done
cp "$modelshelf_client/install.sh" "$modelshelf_output/install.sh"
printf '%s\n' "$modelshelf_version" > "$modelshelf_output/version.txt"

echo "Packaged ModelShelf CLI $modelshelf_version in $modelshelf_output"
