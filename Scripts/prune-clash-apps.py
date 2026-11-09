#!/usr/bin/env python3
"""Prune this project's generated apps without deleting official Clash bundles."""
import argparse
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess


APP_NAME = 'Clash 多订阅测速助手.app'
STOCK_APP = Path('/Applications/Clash Verge.app')
STOCK_ID = 'io.github.clash-verge-rev.clash-verge-rev'
DIRECT_ID = 'io.github.clash-verge-rev.clash-verge-direct'
HELPER_IDS = {
    'local.clashverge.multispeedhelper', 'local.clashverge.uipreview',
    'local.clashverge.speedhelper', 'local.clashverge.speedhelper.development',
    'local.clash.node-speed-helper', 'local.clash.node-speed-helper.fast',
    'local.clash.node-speed-helper.auto',
}
REGISTERED_IDS = HELPER_IDS | {STOCK_ID, DIRECT_ID, 'local.synthetic.clash-smoke'}
TMP_PREFIXES = ('clash-runtime-', 'clashmulti-', 'clash-multi-')
LSREGISTER = Path('/System/Library/Frameworks/CoreServices.framework/Frameworks/'
                  'LaunchServices.framework/Support/lsregister')


def physical(path):
    return Path(os.path.realpath(path))


def family(name, base):
    return name == base or name.startswith(base + '.')


def scoped(path, roots):
    return any(path == root or root in path.parents for root in roots)


def bundle_id(path):
    contents = path / 'Contents'
    info = contents / 'Info.plist'
    if path.is_symlink() or contents.is_symlink() or info.is_symlink():
        return None
    try:
        with info.open('rb') as stream:
            data = plistlib.load(stream)
        if isinstance(data, dict) and data.get('CFBundlePackageType') == 'APPL':
            identifier = data.get('CFBundleIdentifier')
            return identifier if isinstance(identifier, str) else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    return None


def is_adhoc(path):
    result = subprocess.run(['/usr/bin/codesign', '-dv', str(path)],
                            capture_output=True, text=True, timeout=15)
    return result.returncode == 0 and 'Signature=adhoc' in result.stderr.splitlines()


def generated(path, identifier):
    if identifier in HELPER_IDS:
        return True
    if identifier == DIRECT_ID:
        return family(path.name, 'Clash Verge Direct.app')
    if identifier != STOCK_ID:
        return False
    # Same-ID experiments must be explicitly named, or be a Cargo bundle output,
    # AND ad-hoc signed. Never remove a Developer ID signed official copy.
    named = (family(path.name, 'Clash Verge Patched.app') or
             path.name.startswith('Clash Verge.app.patched.'))
    cargo_output = (path.name == 'Clash Verge.app' and
                    path.parent.parts[-3:] == ('release', 'bundle', 'macos'))
    return (named or cargo_output) and is_adhoc(path)


def bundle_paths(root):
    for current, directories, _ in os.walk(root, followlinks=False):
        kept = []
        for name in directories:
            path = Path(current) / name
            if path.is_symlink() or name in {'.git', 'node_modules', 'DerivedData'}:
                continue
            if '.app' in name:
                yield path
            else:
                kept.append(name)
        directories[:] = kept


def registrations(dump):
    for block in re.split(r'\n-{20,}\n', dump):
        identifier = re.search(r'^identifier:\s+([^\n]+)$', block, re.M)
        path = re.search(r'^path:\s+(.+) \(0x[0-9a-f]+\)\s*$', block, re.M)
        if identifier and path and identifier.group(1).strip() in REGISTERED_IDS:
            yield Path(path.group(1)), identifier.group(1).strip()


def refresh_registration(removed, roots, protected, custom, dry_run, register_apps=()):
    if not LSREGISTER.is_file():
        return
    dump = subprocess.run([str(LSREGISTER), '-dump'], check=True,
                          capture_output=True, text=True, timeout=30).stdout
    removed = set(removed)
    stale = set()
    for path, _ in registrations(dump):
        if (physical(path) not in protected and
                (path in removed or not os.path.lexists(path)) and
                (not custom or scoped(path, roots))):
            stale.add(path)
    for path in sorted(stale):
        if dry_run:
            print(f'would unregister: {path}')
        else:
            subprocess.run([str(LSREGISTER), '-u', str(path)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
            print(f'unregistered: {path}')
    if not dry_run and not custom:
        for path in sorted(register_apps):
            if path.is_dir():
                subprocess.run([str(LSREGISTER), '-f', str(path)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)


def cleanup(roots, protected, dry_run, custom=False, register_apps=()):
    candidates = set()
    for root in roots:
        if not root.exists() and not root.is_symlink():
            continue
        if root != physical(root) or not root.is_dir():
            raise RuntimeError(f'cleanup root must be a physical directory: {root}')
        if root != Path('/Applications') and root.stat().st_uid != os.geteuid():
            raise RuntimeError(f'cleanup root must be owned by the current user: {root}')
        for path in bundle_paths(root):
            if physical(path) not in protected and generated(path, bundle_id(path)):
                candidates.add(path)

    # Preflight all candidates before deleting any. A running old app blocks
    # cleanup; it is never terminated just to remove a duplicate icon.
    commands = subprocess.run(['/bin/ps', '-axo', 'command='], check=True,
                              capture_output=True, text=True, timeout=15).stdout.splitlines()
    for path in sorted(candidates):
        if path != physical(path) or path.stat().st_uid != os.geteuid():
            raise RuntimeError(f'candidate is not an owned physical bundle: {path}')
        prefix = str(path) + '/Contents/'
        if any(line.strip().startswith(prefix) for line in commands):
            raise RuntimeError(f'refusing to remove a running app: {path}')
    for path in sorted(candidates):
        if dry_run:
            print(f'would remove: {path}')
        else:
            shutil.rmtree(path)
            print(f'removed: {path}')
    refresh_registration(candidates, roots, protected, custom, dry_run, register_apps)
    return len(candidates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='report only; do not delete or unregister')
    parser.add_argument('--keep', action='append', default=[], metavar='APP',
                        help='also protect a newly staged bundle during installation')
    args = parser.parse_args()
    home = Path.home()
    project = Path(__file__).resolve().parent.parent
    install = Path(os.environ.get('CLASH_MULTI_INSTALL_ROOT', str(home / 'Applications')))
    register_apps = {physical(STOCK_APP), physical(home / 'Applications' / APP_NAME),
                     physical(install / APP_NAME)}
    protected = register_apps | {physical(Path(p)) for p in args.keep}
    custom = 'CLASH_LEGACY_CLEANUP_ROOTS' in os.environ
    if custom:
        roots = [Path(p).expanduser() for p in os.environ['CLASH_LEGACY_CLEANUP_ROOTS'].split(':') if p]
    else:
        roots = [Path('/Applications'), home / 'Applications', project / 'build',
                 home / 'Documents/Codex', home / '.Trash', home / 'Library/Mobile Documents/.Trash']
        # Limit temporary scanning to this project's build/acceptance directory
        # families. Other temporary applications are outside the cleanup scope.
        roots += [p for p in Path('/private/tmp').iterdir()
                  if p.name.startswith(TMP_PREFIXES) and p.is_dir() and
                  not p.is_symlink() and p.stat().st_uid == os.geteuid()]
    try:
        count = cleanup(roots, protected, args.dry_run, custom, register_apps)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'prune-clash-apps: {error}\n')
    print(f'Stale Clash app cleanup complete: {count} bundle(s).')


if __name__ == '__main__':
    main()
