#!/bin/bash
# Download one pinned upstream release; never install or execute the worker.
set -euo pipefail
umask 077

readonly VERSION='v1.8.6'
readonly ASSET='clash-speedtest_Darwin_arm64.tar.gz'
readonly SHA256='e41fa9b2bf7409206ed9e58e4dbc4b0b7b09d3f2d8a6946bfdad2b85a14d2fb2'
readonly URL="https://github.com/faceair/clash-speedtest/releases/download/${VERSION}/${ASSET}"
readonly PROJECT_DIR="$(cd -P "$(dirname "$0")/.." && pwd -P)"
readonly BUILD_DIR="$PROJECT_DIR/build"
readonly DEPS_DIR="$BUILD_DIR/deps"
readonly ARCHIVE="$DEPS_DIR/$ASSET"
readonly WORKER="$DEPS_DIR/clash-speedtest"
download_tmp=''
worker_tmp=''

die() { printf 'fetch-worker: %s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'HELP'
Usage: Scripts/fetch-worker.sh

Fetch the official faceair/clash-speedtest v1.8.6 Darwin arm64 release into
build/deps. The downloaded archive is verified against a pinned SHA-256 on
every invocation; its single executable is streamed out, not unpacked over
any existing files. Existing mismatched paths are never replaced. If a cache
is damaged, inspect it and remove it manually before rerunning this script.
HELP
}
if [[ $# -gt 0 ]]; then
    if [[ $# -eq 1 && ( "$1" == '--help' || "$1" == '-h' ) ]]; then usage; exit 0; fi
    usage >&2; exit 2
fi

# Only create directories directly below the physically resolved project root.
# Refuse symlinks or directories owned by another user rather than write there.
ensure_dir() {
    local dir="$1"
    if [[ -e "$dir" || -L "$dir" ]]; then
        [[ -d "$dir" && ! -L "$dir" && -O "$dir" ]] || die "unsafe directory: $dir"
    else
        mkdir "$dir" || die "cannot create directory: $dir"
        [[ -d "$dir" && ! -L "$dir" && -O "$dir" ]] || die "unsafe new directory: $dir"
    fi
}

# Only remove exact files created by mktemp within the checked deps directory.
cleanup() {
    local path
    for path in "$download_tmp" "$worker_tmp"; do
        case "$path" in
            "$DEPS_DIR"/.download.????????|"$DEPS_DIR"/.worker.????????)
                if [[ -f "$path" && ! -L "$path" && -O "$path" && -d "$BUILD_DIR" && ! -L "$BUILD_DIR" && \
                      -d "$DEPS_DIR" && ! -L "$DEPS_DIR" && -O "$DEPS_DIR" && \
                      "$(cd -P "$DEPS_DIR" && pwd -P)" == "$DEPS_DIR" ]]; then
                    rm -f -- "$path"
                fi
                ;;
        esac
    done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
ensure_dir "$BUILD_DIR"
ensure_dir "$DEPS_DIR"

verify_archive() {
    [[ -f "$1" && ! -L "$1" ]] || die "archive is not a regular, non-symlink file: $1"
    local actual
    actual="$(shasum -a 256 < "$1" | awk '{print $1}')"
    [[ "$actual" == "$SHA256" ]] || die "SHA-256 mismatch for $1; expected $SHA256, got $actual. Inspect and remove the cache manually."
}

if [[ -e "$ARCHIVE" || -L "$ARCHIVE" ]]; then
    verify_archive "$ARCHIVE"
else
    download_tmp="$(mktemp "$DEPS_DIR/.download.XXXXXXXX")"
    printf 'Downloading official %s %s...\n' "$VERSION" "$ASSET" >&2
    # -q must be first: ignore user curlrc; follow only HTTPS redirects.
    curl -q --fail --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 20 --max-time 300 --retry 2 --max-filesize 15000000 \
        --output "$download_tmp" "$URL"
    verify_archive "$download_tmp"
    # link(1) calls link(2) directly: unlike ln it cannot link inside a
    # destination directory (or follow a directory symlink) created in a race.
    /bin/link "$download_tmp" "$ARCHIVE" || die "archive destination already exists or cannot be created: $ARCHIVE"
fi

# Recheck the retained archive every time before streaming out the exact member.
verify_archive "$ARCHIVE"
worker_tmp="$(mktemp "$DEPS_DIR/.worker.XXXXXXXX")"
tar -xOzf "$ARCHIVE" clash-speedtest > "$worker_tmp" || die 'expected worker member missing from verified archive'
[[ -s "$worker_tmp" ]] || die 'verified archive yielded an empty worker'
chmod 755 "$worker_tmp"
if [[ -e "$WORKER" || -L "$WORKER" ]]; then
    [[ -f "$WORKER" && ! -L "$WORKER" && -x "$WORKER" ]] || die "unsafe or non-executable worker path: $WORKER"
    cmp -s "$worker_tmp" "$WORKER" || die "cached worker differs from verified archive: $WORKER; inspect and remove it manually"
else
    /bin/link "$worker_tmp" "$WORKER" || die "worker destination already exists or cannot be created: $WORKER"
fi
printf 'Verified official archive SHA-256: %s\nWorker: %s\n' "$SHA256" "$WORKER"
