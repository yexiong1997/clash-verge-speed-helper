#!/bin/bash
# Build the multi-subscription helper only; never install, launch or test it.
set -euo pipefail
umask 077
export PATH='/usr/bin:/bin:/usr/sbin:/sbin'
export COPYFILE_DISABLE=1

readonly PROJECT_DIR="$(cd -P "$(dirname "$0")/.." && pwd -P)"
readonly BUILD_DIR="$PROJECT_DIR/build"
readonly UPSTREAM_COMMIT='d3b9b151893fa7415fc4197242aadedc25e40391'
readonly UPSTREAM_SHA256='125bc299e6384a068fb5bcf445b2a917a1ec06d2c358a940a77962c345560592'
readonly BUNDLE_EXECUTABLE='ClashMultiSpeedHelper'
readonly BUNDLE_ID='local.clashverge.multispeedhelper'
app_path=''
app_id=''
work_id=''
swift_tmp=''
swift_tmp_id=''
app_created=0

usage() {
    cat <<'HELP'
Usage:
  Scripts/build-multi.sh
  Scripts/build-multi.sh --output /existing/parent/NewName.app
  Scripts/build-multi.sh --help

Builds a NEW arm64 macOS 12+ AppKit bundle without installing or running it.
Default: build/Clash 多订阅测速助手.app (build/ is ignored by git). An explicit
output's parent must already exist, be user-owned and not group/world writable.
All output path components must be non-symlinks; '..' is not accepted. The .app
must not already exist, including as a dangling symlink. Nothing is overwritten.

A fresh, isolated copy of Scripts/build-verified-worker.sh builds the STRICT
worker from pinned source and patch inside the new app's temporary workspace.
Compiler scratch uses a separate private, ASCII-only directory in /private/tmp.
This can download Go, upstream source and modules over the network and needs
substantial disk space. Existing repository worker binaries are never trusted,
replaced or reused. The upstream builder otherwise refuses to replace a
non-identical existing output. After a failure, the incomplete app is retained
and its path reported; inspect and remove it yourself before retrying.

No Developer ID signature or notarization is provided. Source and license
materials are included, but redistribution may have further legal obligations.
Do not run this builder while other agents are editing its Swift/worker inputs.
HELP
}
die() { printf 'build-multi: %s\n' "$*" >&2; exit 1; }

requested_output=''
if [[ $# -eq 1 && ( "$1" == '--help' || "$1" == '-h' ) ]]; then
    usage
    exit 0
elif [[ $# -eq 0 ]]; then
    requested_output="$BUILD_DIR/Clash 多订阅测速助手.app"
elif [[ $# -eq 2 && "$1" == '--output' && -n "$2" ]]; then
    requested_output="$2"
else
    usage >&2
    exit 2
fi

# Limit both the tools used here and those inherited by the worker builder.
[[ "$(uname -s)" == 'Darwin' && "$(uname -m)" == 'arm64' ]] || die 'Apple Silicon macOS (Darwin arm64) is required'
for tool in /bin/bash /bin/cat /bin/cp /bin/mkdir /bin/chmod /bin/link \
            /usr/bin/python3 /usr/bin/env /usr/bin/printenv /usr/bin/stat \
            /usr/bin/awk /usr/bin/grep /usr/bin/xcrun /usr/bin/sw_vers \
            /usr/bin/curl /usr/bin/tar /usr/bin/patch /usr/bin/mktemp \
            /usr/bin/cmp /usr/bin/shasum /usr/bin/file /usr/bin/lipo \
            /usr/bin/codesign /usr/bin/xattr /usr/bin/plutil /usr/libexec/PlistBuddy; do
    [[ -x "$tool" ]] || die "missing required tool: $tool"
done
macos_version="$(/usr/bin/sw_vers -productVersion)"
macos_major="${macos_version%%.*}"
[[ "$macos_major" =~ ^[0-9]+$ && "$macos_major" -ge 12 ]] || die 'macOS 12 or later is required'

# A clean checkout need not have build/. Only create the one ignored parent,
# never replace it, and reject a symlink or a directory owned by somebody else.
if [[ $# -eq 0 ]]; then
    if [[ ! -e "$BUILD_DIR" && ! -L "$BUILD_DIR" ]]; then
        /bin/mkdir "$BUILD_DIR" || die 'cannot create build directory'
    fi
    [[ -d "$BUILD_DIR" && ! -L "$BUILD_DIR" && -O "$BUILD_DIR" ]] || die 'unsafe build directory'
    [[ "$(cd -P "$BUILD_DIR" && pwd -P)" == "$BUILD_DIR" ]] || die 'build directory is not physically contained in the project'
fi

# Check every component, not only the final name. Disallow '..' instead of
# normalizing it across a symlink, require a private owned parent, and make
# the resulting path absolute. mkdir below is the atomic no-overwrite check.
app_path="$(/usr/bin/python3 -I - "$requested_output" <<'PY'
import os, stat, sys
path = sys.argv[1]
def fail(message):
    print('build-multi: ' + message, file=sys.stderr)
    raise SystemExit(1)
if not path or '\n' in path or '\r' in path:
    fail('output path is empty or contains a newline')
if any(p == '..' for p in path.split('/')):
    fail("output path must not contain '..'")
if not os.path.isabs(path):
    path = os.path.join(os.getcwd(), path)
path = os.path.normpath(path)
if not os.path.basename(path).endswith('.app') or os.path.basename(path) == '.app':
    fail('output name must end in .app')
parts = [p for p in path.split('/') if p]
current = '/'
for part in parts[:-1]:
    current = os.path.join(current, part)
    try:
        st = os.lstat(current)
    except OSError:
        fail('output parent does not exist or cannot be inspected: ' + current)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        fail('output parent contains a symlink or non-directory: ' + current)
try:
    parent = os.lstat(os.path.dirname(path))
except OSError:
    fail('output parent cannot be inspected')
if parent.st_uid != os.geteuid() or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
    fail('output parent must be user-owned and not group/world writable')
if os.path.lexists(path):
    fail('output already exists (including symlinks); refusing to overwrite: ' + path)
print(path)
PY
)" || exit 1

# Read/copy only these explicit inputs. In particular, never compile main.swift
# or copy the legacy worker. Do not run until the owners have finished editing.
readonly SOURCES_DIR="$PROJECT_DIR/Sources"
readonly WORKER_DIR="$PROJECT_DIR/Worker"
readonly WORKER_BUILDER="$PROJECT_DIR/Scripts/build-verified-worker.sh"
[[ -d "$SOURCES_DIR" && ! -L "$SOURCES_DIR" && -d "$WORKER_DIR" && ! -L "$WORKER_DIR" ]] || die 'source or worker directory is missing or a symlink'
swift_sources=(MultiMain.swift ProfileRegistry.swift MultiBatchRunner.swift VerifiedMeasurement.swift CompositeScore.swift TestSchedule.swift SwitchController.swift AssistedSwitch.swift ClashRuntimeInjector.swift)
for name in "${swift_sources[@]}"; do
    [[ -f "$SOURCES_DIR/$name" && ! -L "$SOURCES_DIR/$name" && -r "$SOURCES_DIR/$name" ]] || die "required Swift source missing or unsafe: $name"
done
for input in "$WORKER_BUILDER" "$WORKER_DIR/verified-worker.patch" "$WORKER_DIR/UPSTREAM-GPL-3.0.txt" "$WORKER_DIR/NOTICE.md" "$WORKER_DIR/CONTRACT.md"; do
    [[ -f "$input" && ! -L "$input" && -r "$input" ]] || die "required worker input missing or unsafe: $input"
done

# A failed build is never auto-deleted. Disable any output executables only if
# the destination is still the exact private directory this invocation created.
app_is_ours() {
    [[ -n "$app_path" && -n "$app_id" && -d "$app_path" && ! -L "$app_path" && -O "$app_path" ]] || return 1
    [[ "$(cd -P "$app_path" && pwd -P)" == "$app_path" ]] || return 1
    [[ "$(/usr/bin/stat -f '%d:%i' "$app_path")" == "$app_id" ]]
}
# Remove only the private compiler scratch directory that this invocation
# created. Directory descriptors and identity checks prevent following links or
# crossing mount points; failures leave the scratch tree for manual inspection.
cleanup_swift_tmp() {
    [[ -n "$swift_tmp" && -n "$swift_tmp_id" ]] || return 0
    /usr/bin/python3 -I - "$swift_tmp" "$swift_tmp_id" <<'PY'
import os, stat, sys
path, expected = sys.argv[1:]
name = os.path.basename(path)
if os.path.dirname(path) != '/private/tmp' or not name.startswith('clashmulti-swift.') or os.path.realpath(path) != path:
    raise RuntimeError('unsafe compiler scratch path')
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
parent = os.open('/private/tmp', flags)
def identity(st):
    return '%s:%s' % (st.st_dev, st.st_ino)
try:
    root = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if identity(root) != expected or root.st_uid != os.geteuid():
        raise RuntimeError('compiler scratch directory identity changed')
    def remove_dir(parent_fd, entry, expected_id):
        fd = os.open(entry, flags, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if identity(info) != expected_id or info.st_dev != root.st_dev or info.st_uid != os.geteuid():
                raise RuntimeError('compiler scratch changed, crosses a mount, or has a different owner')
            os.fchmod(fd, info.st_mode | stat.S_IRWXU)
            for child in os.listdir(fd):
                child_info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(child_info.st_mode):
                    remove_dir(fd, child, identity(child_info))
                else:
                    os.unlink(child, dir_fd=fd)
        finally:
            os.close(fd)
        if identity(os.stat(entry, dir_fd=parent_fd, follow_symlinks=False)) != expected_id:
            raise RuntimeError('compiler scratch changed before removal')
        os.rmdir(entry, dir_fd=parent_fd)
    remove_dir(parent, name, expected)
finally:
    os.close(parent)
PY
}
cleanup_on_exit() {
    local status=$? target
    trap - EXIT
    if ! cleanup_swift_tmp; then
        printf 'build-multi: compiler scratch retained for manual inspection: %s\n' "$swift_tmp" >&2
        [[ $status -ne 0 ]] || status=1
    fi
    if [[ $status -ne 0 && $app_created -eq 1 ]]; then
        if app_is_ours && [[ -d "$app_path/Contents" && ! -L "$app_path/Contents" && -d "$app_path/Contents/MacOS" && ! -L "$app_path/Contents/MacOS" ]]; then
            for target in "$app_path/Contents/MacOS/$BUNDLE_EXECUTABLE" "$app_path/Contents/MacOS/clash-speedtest"; do
                if [[ -f "$target" && ! -L "$target" && -O "$target" ]]; then
                    /bin/chmod 644 "$target" || :
                fi
            done
        fi
        printf 'build-multi: incomplete output retained; inspect and remove manually before retrying: %s\n' "$app_path" >&2
    fi
    exit "$status"
}
trap cleanup_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

/bin/mkdir "$app_path" || die "cannot reserve output (it may already exist): $app_path"
app_created=1
[[ -d "$app_path" && ! -L "$app_path" && -O "$app_path" ]] || die 'new output is not a private directory'
app_id="$(/usr/bin/stat -f '%d:%i' "$app_path")"
app_is_ours || die 'new output changed during creation'
readonly WORK="$app_path/.build-multi-work"
/bin/mkdir "$WORK" "$app_path/Contents"
/bin/mkdir "$WORK/home" "$WORK/tmp" "$WORK/cache" "$WORK/Sources" "$WORK/worker-project" \
    "$app_path/Contents/MacOS" "$app_path/Contents/Resources"
/bin/mkdir "$WORK/worker-project/Scripts" "$WORK/worker-project/Worker" \
    "$app_path/Contents/Resources/WorkerSource"
work_id="$(/usr/bin/stat -f '%d:%i' "$WORK")"
readonly RESOURCES="$app_path/Contents/Resources/WorkerSource"

# Swift's driver on some macOS/Xcode releases traps if TMPDIR contains non-ASCII
# bytes. The app name may be Chinese, so use separately verified private scratch
# under the physical /private/tmp path. No generated executable is launched.
swift_tmp="$(/usr/bin/mktemp -d /private/tmp/clashmulti-swift.XXXXXXXX)" || die 'cannot create compiler scratch directory'
[[ "$swift_tmp" == /private/tmp/clashmulti-swift.* && -d "$swift_tmp" && ! -L "$swift_tmp" && -O "$swift_tmp" ]] || die 'compiler scratch directory is unsafe'
[[ "$(cd -P "$swift_tmp" && pwd -P)" == "$swift_tmp" ]] || die 'compiler scratch directory is not physical'
swift_tmp_id="$(/usr/bin/stat -f '%d:%i' "$swift_tmp")" || die 'cannot identify compiler scratch directory'
swiftc_path="$(/usr/bin/env -i PATH="$PATH" HOME="$WORK/home" TMPDIR="$swift_tmp/" /usr/bin/xcrun --sdk macosx --find swiftc)" || die 'cannot locate the macOS Swift compiler'
sdk_path="$(/usr/bin/env -i PATH="$PATH" HOME="$WORK/home" TMPDIR="$swift_tmp/" /usr/bin/xcrun --sdk macosx --show-sdk-path)" || die 'cannot locate the macOS SDK'
[[ -x "$swiftc_path" && -d "$sdk_path" ]] || die 'the macOS Swift compiler or SDK is unavailable'

# Snapshot all build inputs first. The worker builder resolves its own root, so
# this isolated copy has the same build/verified-worker/clash-speedtest output
# layout without touching existing repository caches or worker binaries.
for name in "${swift_sources[@]}"; do /bin/cp -X "$SOURCES_DIR/$name" "$WORK/Sources/$name"; done
/bin/cp -X "$WORKER_BUILDER" "$WORK/worker-project/Scripts/build-verified-worker.sh"
/bin/cp -X "$WORKER_DIR/verified-worker.patch" "$WORK/worker-project/Worker/verified-worker.patch"
/bin/cp -X "$WORKER_DIR/UPSTREAM-GPL-3.0.txt" "$RESOURCES/UPSTREAM-GPL-3.0.txt"
/bin/cp -X "$WORKER_DIR/NOTICE.md" "$RESOURCES/NOTICE.md"
/bin/cp -X "$WORKER_DIR/CONTRACT.md" "$RESOURCES/CONTRACT.md"
/bin/cp -X "$WORK/worker-project/Worker/verified-worker.patch" "$RESOURCES/verified-worker.patch"
/bin/cp -X "$WORK/worker-project/Scripts/build-verified-worker.sh" "$RESOURCES/build-verified-worker.sh"

printf '%s\n' 'Compiling the multi-subscription AppKit helper without launching it...'
swift_inputs=()
for name in "${swift_sources[@]}"; do swift_inputs+=("$WORK/Sources/$name"); done
/usr/bin/env -i PATH="$PATH" HOME="$WORK/home" TMPDIR="$swift_tmp/" \
    CLANG_MODULE_CACHE_PATH="$WORK/cache" SWIFT_MODULECACHE_PATH="$WORK/cache" \
    "$swiftc_path" -swift-version 5 -O -parse-as-library \
    -target arm64-apple-macos12.0 -sdk "$sdk_path" -framework AppKit \
    -module-cache-path "$WORK/cache" "${swift_inputs[@]}" -o "$WORK/$BUNDLE_EXECUTABLE" \
    || die 'Swift compilation failed'
[[ -f "$WORK/$BUNDLE_EXECUTABLE" && ! -L "$WORK/$BUNDLE_EXECUTABLE" ]] || die 'Swift executable was not created'

# The online worker keeps the upstream archive in its bootstrap cache; its
# offline path instead copies and verifies that archive inside its unique private
# run. Never use the caller's external cache as the packaged source directly.
resolve_worker_source_archive() {
    local build_root="$1"
    if [[ ${VERIFIED_WORKER_OFFLINE_CACHE+x} ]]; then
        local -a isolated_runs=("$build_root"/.verified-worker.*)
        [[ ${#isolated_runs[@]} -eq 1 && -d "${isolated_runs[0]}" && ! -L "${isolated_runs[0]}" && -O "${isolated_runs[0]}" ]] || return 1
        printf '%s\n' "${isolated_runs[0]}/upstream.tar.gz"
    else
        printf '%s\n' "$build_root/verified-worker-bootstrap/upstream.tar.gz"
    fi
}

if [[ ${VERIFIED_WORKER_OFFLINE_CACHE+x} ]]; then
    printf '%s\n' 'Building the strict worker afresh from the explicitly selected offline cache...'
else
    printf '%s\n' 'Building the strict worker afresh from its pinned source; public artifact downloads may be needed...'
fi
worker_env=("PATH=$PATH" "HOME=$WORK/home" "TMPDIR=$swift_tmp/")
for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy VERIFIED_WORKER_GOPROXY VERIFIED_WORKER_OFFLINE_CACHE; do
    if value="$(/usr/bin/printenv "$name" 2>/dev/null)"; then worker_env+=("$name=$value"); fi
done
/usr/bin/env -i "${worker_env[@]}" /bin/bash "$WORK/worker-project/Scripts/build-verified-worker.sh" || die 'verified worker source build failed; no pre-existing binary will be used'
worker="$WORK/worker-project/build/verified-worker/clash-speedtest"
source_archive="$(resolve_worker_source_archive "$WORK/worker-project/build")" || die 'verified worker source location is ambiguous or unsafe'
[[ -f "$worker" && ! -L "$worker" && -O "$worker" && -x "$worker" && -s "$worker" ]] || die 'successful worker build did not produce a safe executable'
[[ -f "$source_archive" && ! -L "$source_archive" && -O "$source_archive" && -s "$source_archive" ]] || die 'verified upstream source archive is missing or unsafe'
source_sha="$(/usr/bin/shasum -a 256 < "$source_archive" | /usr/bin/awk '{print $1}')"
[[ "$source_sha" == "$UPSTREAM_SHA256" ]] || die 'verified upstream source archive does not match the expected commit archive'

# Do not ship a mixture of files from concurrently changing inputs.
for name in "${swift_sources[@]}"; do /usr/bin/cmp -s "$SOURCES_DIR/$name" "$WORK/Sources/$name" || die "Swift source changed during build: $name"; done
/usr/bin/cmp -s "$WORKER_BUILDER" "$RESOURCES/build-verified-worker.sh" || die 'worker builder changed during build'
for name in verified-worker.patch UPSTREAM-GPL-3.0.txt NOTICE.md CONTRACT.md; do
    /usr/bin/cmp -s "$WORKER_DIR/$name" "$RESOURCES/$name" || die "worker input changed during build: $name"
done
cleanup_swift_tmp || die 'safe compiler scratch cleanup failed; incomplete app retained'
swift_tmp=''
swift_tmp_id=''

# Verify the upstream license text against the actual pinned source archive.
/usr/bin/tar -xOzf "$source_archive" "clash-speedtest-$UPSTREAM_COMMIT/LICENSE" > "$WORK/upstream-license.txt" || die 'cannot extract the upstream license from the source archive'
/usr/bin/cmp -s "$WORK/upstream-license.txt" "$RESOURCES/UPSTREAM-GPL-3.0.txt" || die 'local GPL text does not match the verified upstream license'
/bin/cp -X "$source_archive" "$RESOURCES/upstream.tar.gz"

check_arm64_executable() {
    local binary="$1" kind arches
    [[ -f "$binary" && ! -L "$binary" && -s "$binary" ]] || die "unsafe or empty executable: $binary"
    kind="$(/usr/bin/file -b "$binary")" || die 'cannot inspect Mach-O type'
    [[ "$kind" == 'Mach-O 64-bit executable arm64'* ]] || die "not a thin arm64 Mach-O executable: $binary"
    arches="$(/usr/bin/lipo -archs "$binary")" || die 'cannot inspect executable architectures'
    [[ "$arches" == 'arm64' ]] || die "unexpected executable architectures: $arches"
    /usr/bin/lipo -verify_arch arm64 "$binary" || die 'executable does not contain arm64 code'
}
check_arm64_executable "$WORK/$BUNDLE_EXECUTABLE"
check_arm64_executable "$worker"
worker_sha="$(/usr/bin/shasum -a 256 < "$worker" | /usr/bin/awk '{print $1}')"
patch_sha="$(/usr/bin/shasum -a 256 < "$RESOURCES/verified-worker.patch" | /usr/bin/awk '{print $1}')"
builder_sha="$(/usr/bin/shasum -a 256 < "$RESOURCES/build-verified-worker.sh" | /usr/bin/awk '{print $1}')"
# Create the destination files with no permissions at all, so neither can be
# executed even briefly as it is copied into the still-incomplete app bundle.
(umask 777; /bin/cp -X "$WORK/$BUNDLE_EXECUTABLE" "$app_path/Contents/MacOS/$BUNDLE_EXECUTABLE")
(umask 777; /bin/cp -X "$worker" "$app_path/Contents/MacOS/clash-speedtest")
/bin/chmod 644 "$app_path/Contents/MacOS/$BUNDLE_EXECUTABLE" "$app_path/Contents/MacOS/clash-speedtest"
/usr/bin/cmp -s "$WORK/$BUNDLE_EXECUTABLE" "$app_path/Contents/MacOS/$BUNDLE_EXECUTABLE" || die 'Swift executable changed while being copied'
/usr/bin/cmp -s "$worker" "$app_path/Contents/MacOS/clash-speedtest" || die 'worker changed while being copied'

cat > "$app_path/Contents/Resources/BUILD-PROVENANCE.txt" <<NOTICE
Worker: faceair/clash-speedtest v1.8.6, modified STRICT JSON-v1 worker
Upstream: https://github.com/faceair/clash-speedtest
Source commit: $UPSTREAM_COMMIT
Source archive: https://codeload.github.com/faceair/clash-speedtest/tar.gz/$UPSTREAM_COMMIT
Source archive SHA-256: $source_sha
Local source archive: WorkerSource/upstream.tar.gz
Local source patch: WorkerSource/verified-worker.patch
Source patch SHA-256: $patch_sha
Local source builder: WorkerSource/build-verified-worker.sh
Source builder SHA-256: $builder_sha
Worker SHA-256 before local ad-hoc signing: $worker_sha
Upstream license: WorkerSource/UPSTREAM-GPL-3.0.txt (GNU GPL version 3)
See WorkerSource/NOTICE.md and CONTRACT.md. Go module source dependencies are
identified by upstream go.mod/go.sum; the included builder fetches them or
verifies them from an explicitly selected offline cache.
Local references and a license notice do not by themselves establish legal
compliance; review all corresponding-source and third-party obligations before
redistribution. No Developer ID signature or notarization is provided.
NOTICE

cat > "$app_path/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
  <key>CFBundleDisplayName</key><string>Clash 多订阅测速助手</string>
  <key>CFBundleExecutable</key><string>ClashMultiSpeedHelper</string>
  <key>CFBundleIdentifier</key><string>local.clashverge.multispeedhelper</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>Clash 多订阅测速助手</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST
/usr/bin/plutil -lint "$app_path/Contents/Info.plist" >/dev/null || die 'Info.plist is invalid'
for pair in "CFBundleExecutable:$BUNDLE_EXECUTABLE" "CFBundleIdentifier:$BUNDLE_ID" 'CFBundlePackageType:APPL' 'LSMinimumSystemVersion:12.0'; do
    key="${pair%%:*}"; expected="${pair#*:}"
    actual="$(/usr/libexec/PlistBuddy -c "Print :$key" "$app_path/Contents/Info.plist")" || die "missing Info.plist key: $key"
    [[ "$actual" == "$expected" ]] || die "incorrect Info.plist key: $key"
done

# Remove only this invocation's temporary tree, never the destination or any
# external cache. Walk through directory descriptors, do not follow symlinks,
# reject mount points, and check app/work inode identities before removal.
app_is_ours || die 'output changed before temporary workspace cleanup'
/usr/bin/python3 -I - "$app_path" "$app_id" "$work_id" <<'PY' || die 'safe workspace cleanup failed; incomplete app retained'
import os, stat, sys
app, expected_app, expected_work = sys.argv[1:]
def identity(st):
    return '%s:%s' % (st.st_dev, st.st_ino)
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
appfd = os.open(app, flags)
try:
    ast = os.fstat(appfd)
    if identity(ast) != expected_app or ast.st_uid != os.geteuid() or os.path.realpath(app) != app:
        raise RuntimeError('output directory identity changed')
    def remove_dir(parent, name, expected):
        fd = os.open(name, flags, dir_fd=parent)
        try:
            st = os.fstat(fd)
            if identity(st) != expected or st.st_dev != ast.st_dev or st.st_uid != os.geteuid():
                raise RuntimeError('workspace directory changed, crosses a mount, or has a different owner')
            os.fchmod(fd, st.st_mode | stat.S_IRWXU)
            for child in os.listdir(fd):
                cst = os.stat(child, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(cst.st_mode):
                    remove_dir(fd, child, identity(cst))
                else:
                    os.unlink(child, dir_fd=fd)
        finally:
            os.close(fd)
        if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected:
            raise RuntimeError('workspace directory changed before removal')
        os.rmdir(name, dir_fd=parent)
    remove_dir(appfd, '.build-multi-work', expected_work)
finally:
    os.close(appfd)
PY

app_is_ours || die 'output changed before signing'
/bin/chmod 755 "$app_path" "$app_path/Contents" "$app_path/Contents/MacOS" "$app_path/Contents/Resources" "$RESOURCES"
/bin/chmod 644 "$app_path/Contents/Info.plist" "$app_path/Contents/Resources/BUILD-PROVENANCE.txt" "$RESOURCES"/*
# Sign the nested worker first. Both executables are still non-executable until
# signing completes. Only remove FinderInfo from the exact newly-created root.
umask 022
/usr/bin/codesign --force --sign - --timestamp=none "$app_path/Contents/MacOS/clash-speedtest" || die 'worker signing failed'
attributes="$(/usr/bin/xattr "$app_path")" || die 'cannot inspect output extended attributes'
if printf '%s\n' "$attributes" | /usr/bin/grep -Fxq 'com.apple.FinderInfo'; then
    app_is_ours || die 'output changed before FinderInfo removal'
    /usr/bin/xattr -d com.apple.FinderInfo "$app_path" || die 'cannot remove FinderInfo from new output root'
fi
/usr/bin/codesign --force --sign - --timestamp=none "$app_path" || die 'app signing failed'
/bin/chmod 755 "$app_path/Contents/MacOS/$BUNDLE_EXECUTABLE" "$app_path/Contents/MacOS/clash-speedtest"
/usr/bin/codesign --verify --strict "$app_path/Contents/MacOS/clash-speedtest" || die 'worker signature verification failed'
/usr/bin/codesign --verify --deep --strict "$app_path" || die 'app signature verification failed'
for target in "$app_path/Contents/MacOS/clash-speedtest" "$app_path"; do
    signature="$(/usr/bin/codesign -dvv "$target" 2>&1)" || die 'cannot inspect signature'
    [[ "$signature" == *'Signature=adhoc'* ]] || die 'unexpected signing identity; expected ad-hoc signature'
done
check_arm64_executable "$app_path/Contents/MacOS/$BUNDLE_EXECUTABLE"
check_arm64_executable "$app_path/Contents/MacOS/clash-speedtest"
printf 'Built and verified ad-hoc signature: %s\n' "$app_path"
printf 'Worker SHA-256 before ad-hoc signing: %s\n' "$worker_sha"
printf '%s\n' 'WARNING: this app is NOT Developer ID signed or notarized; macOS Gatekeeper may block it.' >&2
printf '%s\n' 'Nothing was installed, launched, or tested against a profile, node, or controller.'
