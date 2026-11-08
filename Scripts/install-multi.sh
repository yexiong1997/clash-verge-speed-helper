#!/bin/bash
# Build, verify, install, and prune the multi-subscription helper.
set -euo pipefail
umask 077
export PATH='/usr/bin:/bin:/usr/sbin:/sbin'
export COPYFILE_DISABLE=1

readonly PROJECT_DIR="$(cd -P "$(dirname "$0")/.." && pwd -P)"
readonly BUILD_SCRIPT="$PROJECT_DIR/Scripts/build-multi.sh"
readonly INSTALL_ROOT="${CLASH_MULTI_INSTALL_ROOT:-$HOME/Applications}"
readonly APP_NAME='Clash 多订阅测速助手.app'
readonly APP_PATH="$INSTALL_ROOT/$APP_NAME"
stage=''

usage() {
    cat <<'HELP'
Usage:
  Scripts/install-multi.sh

Builds a fresh verified app in /private/tmp, verifies it, replaces the current
user's Clash 多订阅测速助手 app, and removes stale helper variants/backups from
the same Applications directory. It never touches the official Clash Verge.app
or unrelated applications. Set CLASH_MULTI_INSTALL_ROOT only for an explicit,
user-owned test/install root.
HELP
}

die() { printf 'install-multi: %s\n' "$*" >&2; exit 1; }

if [[ $# -eq 1 && ( "$1" == '--help' || "$1" == '-h' ) ]]; then
    usage
    exit 0
fi
[[ $# -eq 0 ]] || { usage >&2; exit 2; }
[[ "$(uname -s)" == 'Darwin' && "$(uname -m)" == 'arm64' ]] || die 'Apple Silicon macOS is required'
[[ -f "$BUILD_SCRIPT" && ! -L "$BUILD_SCRIPT" && -x "$BUILD_SCRIPT" ]] || die 'build-multi.sh is missing or not executable'

if [[ ! -e "$INSTALL_ROOT" && ! -L "$INSTALL_ROOT" ]]; then
    /bin/mkdir -p "$INSTALL_ROOT"
fi
[[ -d "$INSTALL_ROOT" && ! -L "$INSTALL_ROOT" && -O "$INSTALL_ROOT" ]] || die 'install root must be an owned, non-symlink directory'
[[ "$(cd -P "$INSTALL_ROOT" && pwd -P)" == "$INSTALL_ROOT" ]] || die 'install root must resolve physically'

stage="$(/usr/bin/mktemp -d /private/tmp/clash-multi-install.XXXXXXXX)"
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$stage" && -d "$stage" && ! -L "$stage" && -O "$stage" ]]; then
        /bin/rm -rf -- "$stage"
    fi
    exit "$status"
}
trap cleanup EXIT

readonly STAGED_APP="$stage/$APP_NAME"
/bin/bash "$BUILD_SCRIPT" --output "$STAGED_APP"
/usr/bin/codesign --verify --deep --strict "$STAGED_APP"

# Do not remove an installed app while its executable is still running. The
# helper is intentionally the only process path this script is allowed to stop.
/usr/bin/python3 -I - "$INSTALL_ROOT" "$APP_NAME" <<'PY'
import os, signal, subprocess, sys, time
root, app_name = sys.argv[1:]
target = os.path.realpath(os.path.join(root, app_name))
prefix = target + '/Contents/MacOS/ClashMultiSpeedHelper'
lines = subprocess.run(['/bin/ps', '-axo', 'pid=,command='], check=True,
                       stdout=subprocess.PIPE, text=True).stdout.splitlines()
pids = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    fields = line.split(None, 1)
    if len(fields) == 2 and fields[1].startswith(prefix):
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid != os.getpid() and pid != os.getppid():
            pids.append(pid)
for pid in sorted(set(pids)):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
deadline = time.time() + 10
while time.time() < deadline:
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            alive.append(pid)
        else:
            alive.append(pid)
    if not alive:
        break
    time.sleep(0.2)
if alive:
    raise SystemExit('installed helper is still running; close it before installing')
PY

# Only immediate children with the helper's exact name family are removable.
# This includes the active app, runtime fallback copies, and dated backups,
# while leaving Clash Verge and every unrelated .app untouched.
/usr/bin/python3 -I - "$INSTALL_ROOT" "$APP_NAME" <<'PY'
import os, shutil, stat, sys
root, app_name = sys.argv[1:]
root = os.path.realpath(root)
base = app_name[:-4]  # remove .app
allowed = {
    app_name,
}
def is_helper_variant(name):
    return (name in allowed or
            (name.startswith(base + '.') and name.endswith('.app')) or
            (name.startswith(app_name + '.backup-')))
for entry in os.scandir(root):
    if not is_helper_variant(entry.name):
        continue
    path = entry.path
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
        os.unlink(path)
    elif stat.S_ISDIR(info.st_mode):
        if info.st_uid != os.geteuid():
            raise SystemExit('refusing to remove a helper variant owned by another user: ' + path)
        shutil.rmtree(path)
    else:
        raise SystemExit('refusing to remove an unexpected helper entry: ' + path)
PY

/usr/bin/ditto "$STAGED_APP" "$APP_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_PATH"
printf 'Installed and verified: %s\n' "$APP_PATH"
printf 'Removed stale helper variants from: %s\n' "$INSTALL_ROOT"
