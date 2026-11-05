#!/usr/bin/env python3
"""Smoke a staged macOS bundle with network and all user homes denied.

This deliberately does not exercise real subscriptions, proxies, or PUTs.
The app opens its real window briefly and is then terminated by this test.
Only a privately staged /private/tmp/*.app is accepted; no installed app is run.
"""
import ctypes
import errno
import os
import pwd
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def literal(path):
    return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"') + '"'


def window_count(pid):
    cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    cg.CGWindowListCopyWindowInfo.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cf.CFArrayGetCount.argtypes = (ctypes.c_void_p,)
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetValueAtIndex.argtypes = (ctypes.c_void_p, ctypes.c_long)
    cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    cf.CFDictionaryGetValue.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    cf.CFDictionaryGetValue.restype = ctypes.c_void_p
    cf.CFNumberGetValue.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFRelease.argtypes = (ctypes.c_void_p,)
    key = ctypes.c_void_p.in_dll(cg, 'kCGWindowOwnerPID').value
    array = cg.CGWindowListCopyWindowInfo(0, 0)
    require(array and key, 'window server metadata is unavailable')
    try:
        found = 0
        for i in range(cf.CFArrayGetCount(array)):
            item = cf.CFArrayGetValueAtIndex(array, i)
            number = cf.CFDictionaryGetValue(item, key)
            owner = ctypes.c_int32()
            if number and cf.CFNumberGetValue(number, 3, ctypes.byref(owner)) and owner.value == pid:
                found += 1
        return found
    finally:
        cf.CFRelease(array)


def worker_children(pid):
    proc = subprocess.run(['/bin/ps', '-A', '-o', 'ppid=', '-o', 'comm='],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, timeout=10, check=True)
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) == pid:
            if Path(parts[1]).name == 'clash-speedtest':
                return True
    return False


def main():
    require(sys.platform == 'darwin' and len(sys.argv) == 2, 'supply one staged macOS .app path')
    original = Path(sys.argv[1])
    require(original.is_absolute() and original.name.endswith('.app'), 'app path must be absolute')
    app = original.resolve(strict=True)
    require(app == original and app.is_relative_to(Path('/private/tmp')), 'app must be physically staged under /private/tmp')
    info = app.lstat()
    parent = app.parent.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(), 'unsafe staged app')
    require(parent.st_uid == os.geteuid() and parent.st_mode & 0o077 == 0, 'staging parent must be private')
    binary = app / 'Contents/MacOS/ClashMultiSpeedHelper'
    require(binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK), 'missing staged executable')
    require(Path('/usr/bin/sandbox-exec').is_file(), 'sandbox-exec is unavailable')
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    require(home.is_relative_to(Path('/Users')) and home != Path('/Users'), 'cannot safely identify real user home')

    with tempfile.TemporaryDirectory(prefix='multi-package-smoke-', dir='/private/tmp') as folder:
        root = Path(folder).resolve()
        (root / 'home').mkdir(mode=0o700)
        (root / 'tmp').mkdir(mode=0o700)
        (root / 'denied').mkdir(mode=0o700)
        (root / 'allowed').write_bytes(b'synthetic allowed')
        (root / 'denied/profile.yaml').write_bytes(b'synthetic denied')
        policy = '\n'.join([
            '(version 1)', '(allow default)', '(deny network-outbound)',
            '(deny file-write*)', '(allow file-write* (subpath ' + literal(root) + '))',
            '(allow file-write* (literal "/dev/null"))',
            '(deny file-read* (subpath "/Users"))',
            '(deny file-read* (subpath "/System/Volumes/Data/Users"))',
            '(deny file-read* (subpath ' + literal(home) + '))',
            '(deny file-read* (subpath ' + literal(root / 'denied') + '))',
        ])
        env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(root / 'home'),
               'CFFIXED_USER_HOME': str(root / 'home'), 'TMPDIR': str(root / 'tmp'),
               'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'NO_PROXY': '*', 'no_proxy': '*'}
        unix_path = root / 'denied.sock'
        with socket.socket(socket.AF_INET) as tcp, socket.socket(socket.AF_UNIX) as unix:
            tcp.bind(('127.0.0.1', 0)); tcp.listen()
            unix.bind(str(unix_path)); unix.listen()
            canary = r'''import errno, os, socket, sys
port, unix, allowed, denied, home = sys.argv[1:]
for kind, address in ((socket.AF_INET, ('127.0.0.1', int(port))), (socket.AF_UNIX, unix)):
    s = socket.socket(kind); s.settimeout(3)
    try:
        s.connect(address)
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.EACCES): raise
    else: raise SystemExit(1)
    finally: s.close()
with open(allowed, 'rb') as f:
    if f.read() != b'synthetic allowed': raise SystemExit(2)
for path in (denied, home):
    try: fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.EACCES): raise
    else: os.close(fd); raise SystemExit(3)
'''
            probe = subprocess.run(['/usr/bin/sandbox-exec', '-p', policy, '/usr/bin/python3', '-c', canary,
                                    str(tcp.getsockname()[1]), str(unix_path), str(root / 'allowed'),
                                    str(root / 'denied/profile.yaml'), str(home)],
                                   cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=15)
            require(probe.returncode == 0, 'network/home sandbox canary failed; app not launched')
        print('Packaged smoke sandbox canaries PASS', flush=True)
        verify = subprocess.run(['/usr/bin/sandbox-exec', '-p', policy, '/usr/bin/codesign',
                                 '--verify', '--deep', '--strict', str(app)], cwd=root, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        require(verify.returncode == 0, 'staged app signature verification failed')
        process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', policy, str(binary)],
                                   cwd=root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            seen_window = False
            while time.monotonic() < deadline:
                require(process.poll() is None, 'staged app exited during launch')
                require(not worker_children(process.pid), 'worker started without user action')
                if window_count(process.pid):
                    seen_window = True
                    break
                time.sleep(0.25)
            require(seen_window, 'no app window appeared within the smoke-test timeout')
            for _ in range(8):
                time.sleep(0.25)
                require(process.poll() is None, 'staged app exited while idle')
                require(not worker_children(process.pid), 'worker started while idle')
            print('Signed packaged app window and idle defaults PASS', flush=True)
        finally:
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait(timeout=5)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print('Packaged smoke FAILED: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
