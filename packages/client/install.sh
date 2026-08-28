#!/bin/sh
set -eu

# The ModelShelf server replaces this marker with its own package endpoint.
# MODELSHELF_SERVER_DOWNLOAD_BASE

fail() {
  echo "modelshelf installer: $*" >&2
  exit 1
}

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v tar >/dev/null 2>&1 || fail "tar is required"
command -v install >/dev/null 2>&1 || fail "install is required"

case "$(uname -s)" in
  Linux) modelshelf_os=linux ;;
  Darwin) modelshelf_os=darwin ;;
  *) fail "unsupported operating system: $(uname -s)" ;;
esac

case "$(uname -m)" in
  x86_64|amd64) modelshelf_arch=amd64 ;;
  arm64|aarch64) modelshelf_arch=arm64 ;;
  *) fail "unsupported architecture: $(uname -m)" ;;
esac

modelshelf_repo=${MODELSHELF_GITHUB_REPOSITORY:-mr5/modelshelf}
if [ -n "${MODELSHELF_VERSION:-}" ]; then
  case "$MODELSHELF_VERSION" in
    *[!A-Za-z0-9._-]*|'') fail "MODELSHELF_VERSION contains invalid characters" ;;
  esac
  modelshelf_github_base="https://github.com/$modelshelf_repo/releases/download/$MODELSHELF_VERSION"
else
  modelshelf_github_base="https://github.com/$modelshelf_repo/releases/latest/download"
fi
modelshelf_download_base=${MODELSHELF_CLIENT_DOWNLOAD_BASE:-${MODELSHELF_SERVER_DOWNLOAD_BASE:-$modelshelf_github_base}}
modelshelf_install_dir=${MODELSHELF_INSTALL_DIR:-/usr/local/bin}
modelshelf_archive="modelshelf_${modelshelf_os}_${modelshelf_arch}.tar.gz"
modelshelf_temporary=$(mktemp -d "${TMPDIR:-/tmp}/modelshelf-install.XXXXXX")
trap 'rm -rf "$modelshelf_temporary"' EXIT HUP INT TERM

echo "Downloading ModelShelf CLI for ${modelshelf_os}/${modelshelf_arch}…" >&2
curl -fsSL --retry 3 "$modelshelf_download_base/$modelshelf_archive" \
  -o "$modelshelf_temporary/$modelshelf_archive"
curl -fsSL --retry 3 "$modelshelf_download_base/checksums.txt" \
  -o "$modelshelf_temporary/checksums.txt"

modelshelf_expected=$(awk -v archive="$modelshelf_archive" '
  $2 == archive || $2 == "*" archive { print $1; exit }
' "$modelshelf_temporary/checksums.txt")
case "$modelshelf_expected" in
  *[!0-9a-fA-F]*|'') fail "missing or invalid checksum for $modelshelf_archive" ;;
esac
[ "${#modelshelf_expected}" -eq 64 ] || fail "invalid checksum length for $modelshelf_archive"

if command -v sha256sum >/dev/null 2>&1; then
  modelshelf_actual=$(sha256sum "$modelshelf_temporary/$modelshelf_archive" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  modelshelf_actual=$(shasum -a 256 "$modelshelf_temporary/$modelshelf_archive" | awk '{print $1}')
else
  fail "sha256sum or shasum is required"
fi
[ "$modelshelf_actual" = "$modelshelf_expected" ] || fail "SHA-256 checksum verification failed"

tar -xzf "$modelshelf_temporary/$modelshelf_archive" -C "$modelshelf_temporary"
[ -f "$modelshelf_temporary/modelshelf" ] || fail "archive does not contain the modelshelf binary"

if [ ! -d "$modelshelf_install_dir" ]; then
  if mkdir -p "$modelshelf_install_dir" 2>/dev/null; then
    :
  elif command -v sudo >/dev/null 2>&1; then
    sudo mkdir -p "$modelshelf_install_dir"
  else
    fail "cannot create $modelshelf_install_dir; set MODELSHELF_INSTALL_DIR to a writable directory"
  fi
fi

if [ -w "$modelshelf_install_dir" ]; then
  install -m 0755 "$modelshelf_temporary/modelshelf" "$modelshelf_install_dir/modelshelf"
elif command -v sudo >/dev/null 2>&1; then
  sudo install -m 0755 "$modelshelf_temporary/modelshelf" "$modelshelf_install_dir/modelshelf"
else
  fail "cannot write to $modelshelf_install_dir; set MODELSHELF_INSTALL_DIR to a writable directory"
fi

echo "Installed $modelshelf_install_dir/modelshelf" >&2
"$modelshelf_install_dir/modelshelf" --version
