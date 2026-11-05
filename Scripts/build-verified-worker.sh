#!/bin/bash
# Build the GPLv3-derived verified worker; never install Go or use app data.
set -euo pipefail
umask 077

readonly ROOT="$(cd -P "$(dirname "$0")/.." && pwd -P)"
readonly BUILD="$ROOT/build"
readonly CACHE="$BUILD/verified-worker-bootstrap"
readonly OUTPUT_DIR="$BUILD/verified-worker"
readonly OUTPUT="$OUTPUT_DIR/clash-speedtest"
readonly COMMIT='d3b9b151893fa7415fc4197242aadedc25e40391'
readonly SOURCE_SHA256='125bc299e6384a068fb5bcf445b2a917a1ec06d2c358a940a77962c345560592'
readonly GO_VERSION='go1.25.0'
readonly GO_METADATA='https://go.dev/dl/?mode=json&include=all'
readonly SOURCE_URL="https://codeload.github.com/faceair/clash-speedtest/tar.gz/$COMMIT"
readonly MODULE_PROXY="${VERIFIED_WORKER_GOPROXY:-https://goproxy.cn}"
# Explicit opt-in: even an empty, but set, variable selects offline mode and
# fails validation. Offline mode trusts the pre-pinned Go SHA-256 below; it does
# not retrieve or re-check go.dev release metadata. No old compiler cache or
# worker binary is copied or executed.
OFFLINE=0
if [[ ${VERIFIED_WORKER_OFFLINE_CACHE+x} ]]; then OFFLINE=1; fi

die() { printf 'verified-worker build: %s\n' "$1" >&2; exit 1; }
if [[ $# -ne 0 ]]; then
    printf 'Usage: Scripts/build-verified-worker.sh\n' >&2
    exit 2
fi
if [[ "$OFFLINE" == 0 ]]; then
    case "$MODULE_PROXY" in
        https://goproxy.cn|https://proxy.golang.org) ;;
        *) die 'VERIFIED_WORKER_GOPROXY must be https://goproxy.cn or https://proxy.golang.org' ;;
    esac
    command -v curl >/dev/null 2>&1 || die 'missing required tool: curl'
fi
for cmd in python3 tar patch mktemp cmp; do
    command -v "$cmd" >/dev/null 2>&1 || die "missing required tool: $cmd"
done
case "$(uname -s):$(uname -m)" in
    Darwin:arm64) GO_OS=darwin; GO_ARCH=arm64; GO_SHA256=544932844156d8172f7a28f77f2ac9c15a23046698b6243f633b0a0b00c0749c ;;
    Darwin:x86_64) GO_OS=darwin; GO_ARCH=amd64; GO_SHA256=5bd60e823037062c2307c71e8111809865116714d6f6b410597cf5075dfd80ef ;;
    Linux:x86_64) GO_OS=linux; GO_ARCH=amd64; GO_SHA256=2852af0cb20a13139b3448992e69b868e50ed0f8a1e5940ee1de9e19a123b613 ;;
    Linux:aarch64|Linux:arm64) GO_OS=linux; GO_ARCH=arm64; GO_SHA256=05de75d6994a2783699815ee553bd5a9327d8b79991de36e38b66862782f54ae ;;
    *) die 'unsupported host operating system or architecture' ;;
esac
readonly GO_OS GO_ARCH GO_SHA256
readonly GO_FILE="$GO_VERSION.$GO_OS-$GO_ARCH.tar.gz"
SOURCE_FILE="$CACHE/upstream.tar.gz"
GO_ARCHIVE="$CACHE/$GO_FILE"
PATCH="$ROOT/Worker/verified-worker.patch"

ensure_dir() {
    local dir="$1"
    if [[ -e "$dir" || -L "$dir" ]]; then
        [[ -d "$dir" && ! -L "$dir" && -O "$dir" ]] || die 'unsafe existing build directory'
    else
        mkdir "$dir" || die 'cannot create build directory'
        [[ -d "$dir" && ! -L "$dir" && -O "$dir" ]] || die 'unsafe new build directory'
    fi
    [[ "$(cd -P "$dir" && pwd -P)" == "$dir" ]] || die 'build directory is not physically contained'
}
[[ -f "$PATCH" && ! -L "$PATCH" ]] || die 'source patch missing or unsafe'

# Keep each extraction and compiler home private, inside build. We intentionally
# leave the work directory for inspection; the build never removes existing data.
if [[ "$OFFLINE" == 1 ]]; then
    printf '%s\n' 'Offline mode: using pinned Go SHA-256, not fetching release metadata.' >&2
    # Source reads are descriptor-relative and never follow links. Ancestors of
    # the read-only cache may be shared, but the cache tree itself must be owned
    # by this user, have no ACLs granting unexpected access, and not be writable
    # by others. Write-path ancestors must also be safe (a root-owned sticky temp
    # directory is OK). This deliberately fails closed rather than writing via a
    # possibly replaceable project path. Only Go proxy cache data is copied;
    # extracted modules, ziphashes, toolchain, GOCACHE and worker are not trusted.
    RUN="$(python3 -I - "$ROOT" "$VERIFIED_WORKER_OFFLINE_CACHE" "$GO_FILE" "$GO_SHA256" "$SOURCE_SHA256" <<'PY'
import ctypes, errno, hashlib, os, secrets, stat, sys

project, cache, go_file, go_sum, source_sum = sys.argv[1:]
uid = os.geteuid()
flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
DIR = flags | os.O_DIRECTORY

if sys.platform == 'darwin':
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_fd.argtypes = [ctypes.c_int]
    libc.acl_get_fd.restype = ctypes.c_void_p
    libc.acl_free.argtypes = [ctypes.c_void_p]

def no_acl(fd):
    if sys.platform == 'darwin':
        ctypes.set_errno(0)
        acl = libc.acl_get_fd(fd)
        if acl:
            libc.acl_free(acl)
            raise ValueError('ACLs on private paths are not supported')
        if ctypes.get_errno() not in (errno.ENOENT, errno.ENOTSUP):
            raise OSError(ctypes.get_errno(), 'cannot inspect ACL')
    elif sys.platform.startswith('linux'):
        for name in ('system.posix_acl_access', 'system.posix_acl_default'):
            try:
                os.getxattr(fd, name)
            except OSError as e:
                if e.errno not in (errno.ENODATA, errno.ENOTSUP):
                    raise
            else:
                raise ValueError('ACLs on private paths are not supported')
    else:
        raise ValueError('cannot inspect ACLs on this platform')

def checked(fd, *, directory, owner=uid, private=True):
    s = os.fstat(fd)
    if (not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode))
            or (owner is not None and s.st_uid != owner)):
        raise ValueError('unsafe ownership or file type')
    if private:
        if s.st_mode & 0o022:
            raise ValueError('path is writable by other users')
        no_acl(fd)
    if not directory and s.st_nlink != 1:
        raise ValueError('hard-linked input is not accepted')
    return s

def open_path(path, *, write_path):
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError('cache and project paths must be absolute')
    parts = path.rstrip('/').split('/')[1:]
    if not parts or any(p in ('', '.', '..') for p in parts):
        raise ValueError('unsafe path component')
    fd = os.open('/', DIR)
    try:
        for index, part in enumerate(parts):
            nextfd = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = nextfd
            s = checked(fd, directory=True, owner=None, private=False)
            if s.st_uid not in (0, uid):
                raise ValueError('unsafe path ancestor ownership')
            final = index == len(parts) - 1
            if final:
                checked(fd, directory=True)
            elif write_path:
                # Sticky, root-owned public temp directories prevent other
                # users from replacing entries owned by this user or root.
                if s.st_uid == 0 and s.st_mode & stat.S_ISVTX:
                    no_acl(fd)
                else:
                    checked(fd, directory=True, owner=s.st_uid)
        return fd
    except BaseException:
        os.close(fd)
        raise

def child_dir(parent, name, *, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, DIR, dir_fd=parent)
    try:
        checked(fd, directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise

def mount_id(fd):
    if sys.platform.startswith('linux'):
        # st_dev alone cannot detect bind mounts of the same filesystem.
        with open('/proc/self/fdinfo/%d' % fd, encoding='ascii') as f:
            for line in f:
                if line.startswith('mnt_id:'):
                    return int(line.split()[1])
        raise ValueError('cannot inspect Linux mount identity')
    return None

def on_mount(fd, device, mount):
    if os.fstat(fd).st_dev != device or mount_id(fd) != mount:
        raise ValueError('unexpected mount inside cache')

def input_file(parent, name, device, mount):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('cache contains a symlink or special file')
    fd = os.open(name, flags, dir_fd=parent)
    try:
        after = checked(fd, directory=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError('cache entry changed while opening')
        on_mount(fd, device, mount)
        return fd, after
    except BaseException:
        os.close(fd)
        raise

def unchanged(fd, before):
    after = checked(fd, directory=False)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before, k) != getattr(after, k) for k in fields):
        raise ValueError('cache file changed while copying')

def copy_file(srcfd, before, destdir, name, digest=None):
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                  0o600, dir_fd=destdir)
    h = hashlib.sha256()
    try:
        while True:
            data = os.read(srcfd, 1024 * 1024)
            if not data:
                break
            h.update(data)
            view = memoryview(data)
            while view:
                count = os.write(out, view)
                if count <= 0:
                    raise OSError('short cache copy')
                view = view[count:]
        unchanged(srcfd, before)
        if digest is not None and h.hexdigest() != digest:
            raise ValueError('artifact SHA-256 verification failed')
    finally:
        os.close(out)

def copy_tree(src, dst, device, mount, parts=()):
    for name in sorted(os.listdir(src)):
        s = os.stat(name, dir_fd=src, follow_symlinks=False)
        subparts = parts + (name,)
        if stat.S_ISDIR(s.st_mode):
            child = child_dir(src, name)
            target = None
            try:
                opened = os.fstat(child)
                if (s.st_dev, s.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError('cache directory changed while opening')
                on_mount(child, device, mount)
                # Only these download files are needed for Go to reconstruct a
                # fresh module cache and re-hash zips against upstream go.sum.
                if dst is not None and (subparts in (('cache',), ('cache', 'download'))
                                        or subparts[:2] == ('cache', 'download')):
                    target = child_dir(dst, name, create=True)
                copy_tree(child, target, device, mount, subparts)
            finally:
                if target is not None:
                    os.close(target)
                os.close(child)
        else:
            source, before = input_file(src, name, device, mount)
            try:
                if (dst is not None and subparts[:2] == ('cache', 'download')
                        and name.endswith(('.mod', '.info', '.zip'))):
                    copy_file(source, before, dst, name)
            finally:
                os.close(source)

try:
    projectfd = open_path(project, write_path=True)
    cachefd = open_path(cache, write_path=False)
    device, mount = os.fstat(cachefd).st_dev, mount_id(cachefd)
    buildfd = child_dir(projectfd, 'build', create=True)
    outputfd = child_dir(buildfd, 'verified-worker', create=True)
    os.close(outputfd)
    runname = '.verified-worker.' + secrets.token_hex(12)
    os.mkdir(runname, 0o700, dir_fd=buildfd)
    runfd = child_dir(buildfd, runname)
    for name in ('home', 'tmp', 'source', 'toolchain', 'gopath', 'gocache', 'modcache'):
        os.mkdir(name, 0o700, dir_fd=runfd)
    workerfd = child_dir(projectfd, 'Worker')
    try:
        projectdev, projectmount = os.fstat(projectfd).st_dev, mount_id(projectfd)
        on_mount(workerfd, projectdev, projectmount)
        patchfd, before = input_file(workerfd, 'verified-worker.patch', projectdev, projectmount)
        try:
            copy_file(patchfd, before, runfd, 'verified-worker.patch')
        finally:
            os.close(patchfd)
    finally:
        os.close(workerfd)
    for name, digest in (('upstream.tar.gz', source_sum), (go_file, go_sum)):
        source, before = input_file(cachefd, name, device, mount)
        try:
            copy_file(source, before, runfd, name, digest)
        finally:
            os.close(source)
    source_mod = child_dir(cachefd, 'modcache')
    download_parent = child_dir(source_mod, 'cache')
    try:
        on_mount(download_parent, device, mount)
        download = child_dir(download_parent, 'download')
        try:
            on_mount(download, device, mount)
        finally:
            os.close(download)
    finally:
        os.close(download_parent)
    target_mod = child_dir(runfd, 'modcache')
    try:
        on_mount(source_mod, device, mount)
        copy_tree(source_mod, target_mod, device, mount)
    finally:
        os.close(source_mod)
        os.close(target_mod)
    print(os.path.join(project, 'build', runname))
except (OSError, ValueError) as e:
    print('verified-worker build: unsafe or incomplete offline cache/project: %s' % e,
          file=sys.stderr)
    sys.exit(1)
PY
    )" || die 'offline cache preparation failed'
    SOURCE_FILE="$RUN/upstream.tar.gz"
    GO_ARCHIVE="$RUN/$GO_FILE"
    PATCH="$RUN/verified-worker.patch"
else
    ensure_dir "$BUILD"
    ensure_dir "$CACHE"
    ensure_dir "$CACHE/gopath"
    ensure_dir "$CACHE/modcache"
    ensure_dir "$CACHE/gocache"
    ensure_dir "$OUTPUT_DIR"
    RUN="$(mktemp -d "$BUILD/.verified-worker.XXXXXXXX")" || die 'cannot create isolated build directory'
    mkdir "$RUN/home" "$RUN/tmp" "$RUN/source" "$RUN/toolchain"
fi
export COPYFILE_DISABLE=1
unset TAR_OPTIONS || :

fetch() {
    curl -q --fail --silent --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 20 --max-time 600 --retry 3 --max-filesize "$3" \
        --output "$2" "$1" || die 'public artifact download failed'
}
verify_sha() {
    [[ -f "$1" && ! -L "$1" && -O "$1" ]] || die 'cached artifact is unsafe'
    python3 -I - "$1" "$2" <<'PY' || die 'artifact SHA-256 verification failed'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], 'rb') as f:
    for block in iter(lambda: f.read(1024 * 1024), b''):
        h.update(block)
sys.exit(0 if h.hexdigest() == sys.argv[2] else 1)
PY
}
cache_artifact() {
    local target="$1" url="$2" digest="$3" maxbytes="$4"
    local temp="$RUN/$(basename "$target").tmp"
    if [[ -e "$target" || -L "$target" ]]; then
        verify_sha "$target" "$digest"
    else
        fetch "$url" "$temp" "$maxbytes"
        verify_sha "$temp" "$digest"
        /bin/link "$temp" "$target" || die 'artifact cache destination already exists or is unsafe'
    fi
}

if [[ "$OFFLINE" == 0 ]]; then
    printf '%s\n' 'Checking the pinned Go archive against official go.dev release metadata...' >&2
    fetch "$GO_METADATA" "$RUN/go-releases.json" 10000000
    python3 -I - "$RUN/go-releases.json" "$GO_VERSION" "$GO_FILE" "$GO_SHA256" "$GO_OS" "$GO_ARCH" <<'PY' || die 'official Go metadata does not match the pinned release'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    releases = json.load(f)
matching = [item for item in releases if item.get('version') == sys.argv[2]]
if len(matching) != 1:
    sys.exit(1)
files = [item for item in matching[0].get('files', []) if item.get('filename') == sys.argv[3]]
if len(files) != 1:
    sys.exit(1)
f = files[0]
if (f.get('sha256'), f.get('os'), f.get('arch'), f.get('kind')) != (sys.argv[4], sys.argv[5], sys.argv[6], 'archive'):
    sys.exit(1)
PY
    cache_artifact "$SOURCE_FILE" "$SOURCE_URL" "$SOURCE_SHA256" 5000000
    cache_artifact "$GO_ARCHIVE" "https://go.dev/dl/$GO_FILE" "$GO_SHA256" 100000000
fi
verify_sha "$SOURCE_FILE" "$SOURCE_SHA256"
verify_sha "$GO_ARCHIVE" "$GO_SHA256"

printf '%s\n' 'Extracting the verified upstream and Go archives into build/...' >&2
tar -xzf "$SOURCE_FILE" --strip-components 1 -C "$RUN/source"
tar -xzf "$GO_ARCHIVE" -C "$RUN/toolchain"
cp "$RUN/source/go.mod" "$RUN/original.go.mod"
cp "$RUN/source/go.sum" "$RUN/original.go.sum"
if [[ "$OFFLINE" == 1 ]]; then
    # Only simple unified diffs rooted inside the extracted source are accepted.
    # In particular, a mutable project patch must not redirect patch(1) writes
    # through ../, absolute paths, or an already-existing source symlink.
    python3 -I - "$PATCH" "$RUN/source" <<'PY' || die 'unsafe source patch'
import os, re, stat, sys

patch, root = sys.argv[1:]
lines = open(patch, encoding='utf-8').read().splitlines()
header = re.compile(r'^@@ -[0-9]+(?:,([0-9]+))? \+[0-9]+(?:,([0-9]+))? @@(?: .*)?$')

def target(line, marker, prefix):
    if not line.startswith(marker + ' '):
        raise ValueError('expected unified diff header')
    name = line[len(marker) + 1:]
    if name == '/dev/null':
        return None
    if not name.startswith(prefix + '/') or not re.fullmatch(r'[A-Za-z0-9_./-]+', name):
        raise ValueError('unsafe patch path')
    parts = name.split('/')[1:]
    if not parts or any(p in ('', '.', '..') for p in parts):
        raise ValueError('unsafe patch path component')
    path = root
    for component in parts:
        path = os.path.join(path, component)
        try:
            s = os.lstat(path)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(s.st_mode) or not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)):
            raise ValueError('unsafe existing patch target')
    return '/'.join(parts)

try:
    i = blocks = 0
    while i < len(lines):
        if i + 2 >= len(lines):
            raise ValueError('truncated patch')
        old = target(lines[i], '---', 'a')
        new = target(lines[i + 1], '+++', 'b')
        if old is None and new is None or old and new and old != new:
            raise ValueError('unsafe patch destination')
        i += 2
        hunks = 0
        while i < len(lines) and lines[i].startswith('@@ '):
            match = header.fullmatch(lines[i])
            if not match:
                raise ValueError('invalid unified hunk')
            before = int(match.group(1) or '1')
            after = int(match.group(2) or '1')
            i += 1
            while before or after:
                if i >= len(lines) or not lines[i] or lines[i][0] not in ' +-':
                    raise ValueError('invalid unified hunk body')
                kind = lines[i][0]
                before -= kind in ' -'
                after -= kind in ' +'
                if before < 0 or after < 0:
                    raise ValueError('invalid unified hunk length')
                i += 1
                if i < len(lines) and lines[i] == r'\ No newline at end of file':
                    i += 1
            hunks += 1
        if not hunks:
            raise ValueError('patch block has no hunks')
        blocks += 1
    if not blocks:
        raise ValueError('empty patch')
except (OSError, UnicodeError, ValueError) as e:
    print('verified-worker build: unsafe source patch: %s' % e, file=sys.stderr)
    sys.exit(1)
PY
fi
(cd "$RUN/source" && patch -p1 -s < "$PATCH") || die 'upstream source patch did not apply cleanly'

# No global PATH changes, installs, user Go env, workspaces, or direct VCS fetches.
# Offline has no checksum-database access: the pinned upstream go.sum is
# authoritative. The local cache is rebuilt from zips, not cached ziphashes or
# extracted source, and modules are reverified before and after compilation.
GO="$RUN/toolchain/go/bin/go"
GO_PROXY_ENV=("HOME=$RUN/home")
GO_PATH="$CACHE/gopath"
GO_MODCACHE="$CACHE/modcache"
GO_BUILDCACHE="$CACHE/gocache"
GO_MODULE_PROXY="$MODULE_PROXY"
GO_SUMDB=sum.golang.org
if [[ "$OFFLINE" == 1 ]]; then
    GO_PATH="$RUN/gopath"
    GO_MODCACHE="$RUN/modcache"
    GO_BUILDCACHE="$RUN/gocache"
    GO_MODULE_PROXY=off
    GO_SUMDB=off
else
    # Preserve only transport proxy settings for public downloads.
    for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy; do
        if value="$(printenv "$name" 2>/dev/null)"; then
            GO_PROXY_ENV+=("$name=$value")
        fi
    done
fi
run_go() {
    env -i "${GO_PROXY_ENV[@]}" XDG_CONFIG_HOME="$RUN/home" XDG_CACHE_HOME="$RUN/home" \
        TMPDIR="$RUN/tmp" GOPATH="$GO_PATH" GOMODCACHE="$GO_MODCACHE" \
        GOCACHE="$GO_BUILDCACHE" GOENV=off GOWORK=off GOTOOLCHAIN=local \
        GOFLAGS=-mod=readonly GOOS="$GO_OS" GOARCH="$GO_ARCH" CGO_ENABLED=0 \
        GOPROXY="$GO_MODULE_PROXY" GOSUMDB="$GO_SUMDB" GONOSUMDB= GONOPROXY= \
        GOPRIVATE= GOINSECURE= GOVCS='*:off' GOEXPERIMENT= GOMAXPROCS=4 \
        "$GO" "$@"
}
if [[ "$OFFLINE" == 1 ]]; then
    # Go 1.25 otherwise starts a telemetry sidecar, even with a new HOME.
    # This special command is handled before telemetry starts and writes only
    # to RUN/home; it does not change the user's Go telemetry preferences.
    run_go telemetry off || die 'cannot disable isolated Go telemetry'
fi
printf '%s\n' 'Preparing and checking upstream Go modules...' >&2
(cd "$RUN/source" && run_go mod download && \
    cmp -s "$RUN/original.go.mod" go.mod && cmp -s "$RUN/original.go.sum" go.sum && \
    run_go mod verify && \
    run_go build -p 4 -trimpath -buildvcs=false -o "$RUN/clash-speedtest" ./cmd/verified-worker && \
    run_go mod verify) || die 'Go module verification or compilation failed'
cmp -s "$RUN/original.go.mod" "$RUN/source/go.mod" || die 'upstream go.mod was modified'
cmp -s "$RUN/original.go.sum" "$RUN/source/go.sum" || die 'upstream go.sum was modified'
[[ -f "$RUN/clash-speedtest" && ! -L "$RUN/clash-speedtest" && -x "$RUN/clash-speedtest" ]] || die 'worker binary was not created'

# Do not replace an unknown existing binary. Repeated identical builds are safe.
if [[ -e "$OUTPUT" || -L "$OUTPUT" ]]; then
    [[ -f "$OUTPUT" && ! -L "$OUTPUT" && -O "$OUTPUT" && -x "$OUTPUT" ]] || die 'existing worker output is unsafe'
    cmp -s "$RUN/clash-speedtest" "$OUTPUT" || die 'existing worker differs; inspect and remove it manually before rebuilding'
else
    /bin/link "$RUN/clash-speedtest" "$OUTPUT" || die 'worker output destination already exists or is unsafe'
fi
printf 'Verified worker: %s\nIsolated build: %s\n' "$OUTPUT" "$RUN"
