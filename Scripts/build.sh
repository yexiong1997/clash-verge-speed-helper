#!/bin/bash
# Build locally under build/ only. Never install or launch the resulting app.
set -euo pipefail
umask 077

readonly PROJECT_DIR="$(cd -P "$(dirname "$0")/.." && pwd -P)"
readonly BUILD_DIR="$PROJECT_DIR/build"
readonly SOURCES_DIR="$PROJECT_DIR/Sources"
local_worker=''
allow_unverified=0
module_cache=''
app_created=0
app_path=''

usage() {
    cat <<'HELP'
Usage:
  Scripts/build.sh
  Scripts/build.sh --local-worker /absolute/or/relative/path --allow-unverified-local-worker

The default build fetches and re-verifies the pinned official v1.8.6 archive.
It creates build/Clash 节点测速助手.app and refuses to replace an existing app.
The pinned worker is GPL-3.0; its license is included in the default app.
Before redistribution, review corresponding-source and third-party obligations.
For offline development ONLY, both local-worker arguments are required. This
checks Mach-O type/architecture and prints SHA-256, but does NOT establish
origin or correctness. The output has LOCAL-UNVERIFIED in its name and a
warning inside the app; do not distribute it. Ad-hoc signatures are not Apple
Developer ID signatures or notarization. No installed app/config is modified.
If a build fails after creating an app, inspect and remove that output yourself.
HELP
}
die() { printf 'build: %s\n' "$*" >&2; exit 1; }

if [[ $# -eq 1 && ( "$1" == '--help' || "$1" == '-h' ) ]]; then usage; exit 0; fi
if [[ $# -gt 0 ]]; then
    [[ $# -eq 3 && "$1" == '--local-worker' && "$3" == '--allow-unverified-local-worker' ]] || { usage >&2; exit 2; }
    local_worker="$2"
    [[ -n "$local_worker" ]] || die 'local worker path is empty'
    allow_unverified=1
fi
[[ "$(uname -s)" == 'Darwin' && "$(uname -m)" == 'arm64' ]] || die 'Apple Silicon macOS (Darwin arm64) is required'
[[ -f "$SOURCES_DIR/main.swift" && -f "$SOURCES_DIR/SwitchController.swift" && -f "$SOURCES_DIR/ClashRuntimeInjector.swift" ]] || die 'required Swift sources are missing'

ensure_dir() {
    local dir="$1"
    if [[ -e "$dir" || -L "$dir" ]]; then
        [[ -d "$dir" && ! -L "$dir" && -O "$dir" ]] || die "unsafe directory: $dir"
    else
        mkdir "$dir" || die "cannot create directory: $dir"
        [[ -d "$dir" && ! -L "$dir" && -O "$dir" ]] || die "unsafe new directory: $dir"
    fi
}

# The only recursive cleanup is of this invocation's mktemp compiler cache.
# Validate the exact parent, generated name, ownership and non-symlink type.
cleanup() {
    local status=$?
    case "$module_cache" in
        "$BUILD_DIR"/.swift-module-cache.????????)
            if [[ -d "$module_cache" && ! -L "$module_cache" && -O "$module_cache" && \
                  -d "$BUILD_DIR" && ! -L "$BUILD_DIR" && "$(cd -P "$BUILD_DIR" && pwd -P)" == "$BUILD_DIR" ]]; then
                rm -rf -- "$module_cache"
            fi
            ;;
    esac
    if [[ $status -ne 0 && $app_created -eq 1 ]]; then
        printf 'build: incomplete output retained for inspection: %s\n' "$app_path" >&2
        printf 'build: after checking it, remove it manually before retrying.\n' >&2
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ensure_dir "$BUILD_DIR"
if [[ $allow_unverified -eq 1 ]]; then
    [[ -f "$local_worker" && ! -L "$local_worker" && -r "$local_worker" ]] || die 'local worker must be a readable regular, non-symlink file'
    # Convert relative paths so a leading dash cannot be interpreted as an option.
    case "$local_worker" in /*) ;; *) local_worker="$PWD/$local_worker" ;; esac
    worker="$local_worker"
    app_path="$BUILD_DIR/Clash 节点测速助手-LOCAL-UNVERIFIED.app"
    display_name='Clash 节点测速助手（未验证，仅供本地开发）'
    bundle_id='local.clashverge.speedhelper.development'
    printf '%s\n' 'WARNING: an explicitly supplied local worker is NOT provenance-verified; output is for local development only.' >&2
else
    app_path="$BUILD_DIR/Clash 节点测速助手.app"
    display_name='Clash 节点测速助手'
    bundle_id='local.clashverge.speedhelper'
    # Avoid network or cache changes when the target already exists.
    [[ ! -e "$app_path" && ! -L "$app_path" ]] || die "output already exists; refusing to overwrite: $app_path"
    /bin/bash "$PROJECT_DIR/Scripts/fetch-worker.sh"
    worker="$BUILD_DIR/deps/clash-speedtest"
fi
[[ ! -e "$app_path" && ! -L "$app_path" ]] || die "output already exists; refusing to overwrite: $app_path"

# Validate the input without executing it. For local files, these are structural
# checks only, not a trust or provenance verification.
[[ -f "$worker" && ! -L "$worker" && -r "$worker" ]] || die "worker is not a readable regular, non-symlink file: $worker"
LC_ALL=C /usr/bin/file -b "$worker" | /usr/bin/grep -Eq '^Mach-O 64-bit executable arm64' || die 'worker must be a thin arm64 Mach-O executable'
/usr/bin/lipo -verify_arch arm64 "$worker" || die 'worker does not contain arm64 code'
worker_sha="$(shasum -a 256 < "$worker" | awk '{print $1}')"
printf 'Worker SHA-256 before local signing: %s\n' "$worker_sha"

module_cache="$(mktemp -d "$BUILD_DIR/.swift-module-cache.XXXXXXXX")"
# mkdir reserves the exact output name atomically, and never replaces any file.
mkdir "$app_path" || die "cannot reserve output (possibly already exists): $app_path"
app_created=1
mkdir "$app_path/Contents" "$app_path/Contents/MacOS" "$app_path/Contents/Resources"

cat > "$app_path/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
  <key>CFBundleDisplayName</key><string>$display_name</string>
  <key>CFBundleExecutable</key><string>ClashSpeedHelper</string>
  <key>CFBundleIdentifier</key><string>$bundle_id</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>$display_name</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST
cp "$worker" "$app_path/Contents/Resources/clash-speedtest"
# Detect a changed input during copying; signing deliberately changes this copy.
copy_sha="$(shasum -a 256 < "$app_path/Contents/Resources/clash-speedtest" | awk '{print $1}')"
[[ "$worker_sha" == "$copy_sha" ]] || die 'worker changed while it was being copied'
chmod 755 "$app_path/Contents/Resources/clash-speedtest"
if [[ $allow_unverified -eq 1 ]]; then
    cat > "$app_path/Contents/Resources/LOCAL-DEVELOPMENT-ONLY.txt" <<NOTICE
LOCAL DEVELOPMENT ONLY — DO NOT DISTRIBUTE
This bundle contains an explicitly supplied, provenance-unverified local worker.
The SHA-256 of the worker before the required ad-hoc signing was: $worker_sha
Mach-O/arm64 checks do not prove the worker's identity, origin or correctness.
NOTICE
else
    # Include the license from the same pinned official archive as the worker.
    # This notice alone does not discharge all distribution obligations.
    archive="$BUILD_DIR/deps/clash-speedtest_Darwin_arm64.tar.gz"
    expected_archive_sha='e41fa9b2bf7409206ed9e58e4dbc4b0b7b09d3f2d8a6946bfdad2b85a14d2fb2'
    [[ -f "$archive" && ! -L "$archive" ]] || die 'verified worker archive is missing or unsafe'
    archive_sha="$(shasum -a 256 < "$archive" | awk '{print $1}')"
    [[ "$archive_sha" == "$expected_archive_sha" ]] || die 'worker archive changed after verification'
    tar -xOzf "$archive" LICENSE > "$app_path/Contents/Resources/LICENSE-clash-speedtest.txt" || die 'upstream license missing from verified archive'
    [[ -s "$app_path/Contents/Resources/LICENSE-clash-speedtest.txt" ]] || die 'upstream license is empty'
    cat > "$app_path/Contents/Resources/THIRD-PARTY-clash-speedtest.txt" <<'NOTICE'
Component: faceair/clash-speedtest v1.8.6
Upstream: https://github.com/faceair/clash-speedtest
Source: https://github.com/faceair/clash-speedtest/tree/v1.8.6
Original Darwin arm64 archive SHA-256:
e41fa9b2bf7409206ed9e58e4dbc4b0b7b09d3f2d8a6946bfdad2b85a14d2fb2
License: GNU General Public License, version 3; see LICENSE-clash-speedtest.txt.
The bundled executable is ad-hoc re-signed locally. Before redistribution,
review all applicable GPL corresponding-source and third-party obligations.
NOTICE
fi

swiftc -swift-version 5 -O -target arm64-apple-macos12.0 \
    -module-cache-path "$module_cache" \
    "$SOURCES_DIR/main.swift" "$SOURCES_DIR/SwitchController.swift" "$SOURCES_DIR/ClashRuntimeInjector.swift" \
    -o "$app_path/Contents/MacOS/ClashSpeedHelper"
# App files need normal distribution permissions even though build/ is private.
chmod 755 "$app_path" "$app_path/Contents" "$app_path/Contents/MacOS" \
    "$app_path/Contents/Resources" "$app_path/Contents/MacOS/ClashSpeedHelper"
chmod 644 "$app_path/Contents/Info.plist"
if [[ $allow_unverified -eq 1 ]]; then
    chmod 644 "$app_path/Contents/Resources/LOCAL-DEVELOPMENT-ONLY.txt"
else
    chmod 644 "$app_path/Contents/Resources/LICENSE-clash-speedtest.txt" \
        "$app_path/Contents/Resources/THIRD-PARTY-clash-speedtest.txt"
fi
# Make the signature resources readable in a copied app, too.
umask 022
# Sign nested executable first, then seal and verify the complete app bundle.
codesign --force --sign - --timestamp=none "$app_path/Contents/Resources/clash-speedtest"
# Some Finder/File Provider setups attach FinderInfo to the newly-created bundle
# before signing; codesign rejects it. Remove only that attribute on this output,
# never quarantine or other metadata. A later concurrent re-attachment can still
# make signing fail, which is reported rather than silently bypassed.
if /usr/bin/xattr -p com.apple.FinderInfo "$app_path" >/dev/null 2>&1; then
    /usr/bin/xattr -d com.apple.FinderInfo "$app_path" || die 'failed to remove FinderInfo from newly-created output app'
fi
codesign --force --sign - --timestamp=none "$app_path"
codesign --verify --deep --strict "$app_path"
printf 'Built and ad-hoc verified: %s\n' "$app_path"
printf '%s\n' 'No Developer ID signature/notarization is provided; nothing was installed or run.'
