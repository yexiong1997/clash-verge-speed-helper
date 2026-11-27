"""Read-only Windows profile snapshots; no profile content appears in errors."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import unicodedata

import yaml
import win32con
import win32file

MAX_REGISTRY = 4 * 1024 * 1024
MAX_PROFILE = 1024 * 1024
MAX_TEXT = 8192
_REPARSE = win32con.FILE_ATTRIBUTE_REPARSE_POINT
_DIRECTORY = win32con.FILE_ATTRIBUTE_DIRECTORY
_OPEN_REPARSE_POINT = 0x00200000
_YAML12_FLOW_SCALAR_RE = re.compile(
    r'(?<=:\s)([^\s,{}\[\]"\'#&*!]+:[^\s,{}\[\]"\'#&*!]+)(?=\s*[,}])'
)


class ProfileError(Exception):
    """An intentionally generic profile/registry validation failure."""


_YAML_BASE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _StrictLoader(_YAML_BASE_LOADER):
    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise ProfileError("invalid YAML")
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ProfileError("invalid YAML")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _strict_parse_yaml_text(text):
    scanner = _YAML_BASE_LOADER(text)
    try:
        while scanner.check_event():
            ev = scanner.get_event()
            if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None) or getattr(ev, "tag", None):
                raise ProfileError("invalid YAML")
    finally:
        scanner.dispose()
    return yaml.load(text, Loader=_StrictLoader)


def _normalize_yaml12_flow_scalars(text):
    def _quote(match):
        val = match.group(1).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{val}"'
    return _YAML12_FLOW_SCALAR_RE.sub(_quote, text)


def parse_yaml(raw):
    try:
        text = raw.decode("utf-8", "strict")
        try:
            return _strict_parse_yaml_text(text)
        except yaml.YAMLError:
            # YAML 1.2 (Go yaml.v3 / Mihomo) allows unquoted flow-mapping scalars
            # containing non-spaced colons (e.g. SS2022 base64:base64 passwords
            # or IPv6 literals), while PyYAML (YAML 1.1) rejects them. Quote only
            # those plain values on fallback and enforce the exact same strict
            # anchor/alias/tag/duplicate-key validation.
            normalized = _normalize_yaml12_flow_scalars(text)
            if normalized == text:
                raise
            return _strict_parse_yaml_text(normalized)
    except (yaml.YAMLError, UnicodeError, RecursionError, ValueError):
        raise ProfileError("invalid YAML") from None


def _valid_text(value, maximum=MAX_TEXT):
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum and not any(
            unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value)
    except UnicodeError:
        return False


def _valid_filename(value):
    if not _valid_text(value) or value in (".", ".."):
        return False
    # Colon also prevents NTFS alternate streams and drive-relative paths.
    return not any(c in value for c in '/\\:*?"<>|') and not value.endswith((".", " "))


def default_root():
    override = os.environ.get("CLASH_SPEED_HELPER_ROOT")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ProfileError("registry unavailable")
    return Path(appdata) / "io.github.clash-verge-rev.clash-verge-rev"


def _identity(handle):
    info = win32file.GetFileInformationByHandle(handle)
    return info[4], info[8], info[9]


def _version(handle):
    info = win32file.GetFileInformationByHandle(handle)
    basic = win32file.GetFileInformationByHandleEx(handle, win32file.FileBasicInfo)
    return (_identity(handle), info[5], info[6], basic["CreationTime"],
            basic["LastWriteTime"], basic["ChangeTime"])


@contextmanager
def _open(path, directory, read=False):
    flags = _OPEN_REPARSE_POINT
    if directory:
        flags |= win32con.FILE_FLAG_BACKUP_SEMANTICS
    handle = win32file.CreateFile(
        str(path), win32con.GENERIC_READ if read else 0,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
        None, win32con.OPEN_EXISTING, flags, None)
    try:
        attrs = win32file.GetFileInformationByHandle(handle)[0]
        if attrs & _REPARSE or bool(attrs & _DIRECTORY) != directory:
            raise ProfileError("unsafe profile path")
        if not directory and win32file.GetFileType(handle) != win32file.FILE_TYPE_DISK:
            raise ProfileError("unsafe profile path")
        yield handle
    finally:
        handle.Close()


@contextmanager
def _pinned(root):
    """Hold every ancestor open without FILE_SHARE_DELETE until validation ends."""
    try:
        root = Path(os.path.abspath(root))
        drive, tail = os.path.splitdrive(str(root))
        if not drive or drive.startswith("\\") or not tail.startswith("\\"):
            raise ProfileError("unsafe profile path")
        components = Path(tail).parts
        with ExitStack() as stack:
            path = Path(drive + "\\")
            handle = stack.enter_context(_open(path, True))
            for part in components:
                if part in ("\\", "/"):
                    continue
                if not _valid_filename(part):
                    raise ProfileError("unsafe profile path")
                path = path / part
                handle = stack.enter_context(_open(path, True))
            root_identity = _identity(handle)
            profiles = stack.enter_context(_open(path / "profiles", True))
            yield path, root_identity, _identity(profiles)
    except ProfileError:
        raise
    except Exception:
        raise ProfileError("profile unavailable") from None


def _read(path, limit):
    with _open(path, False, True) as handle:
        before = _version(handle)
        chunks = []
        size = 0
        while True:
            _rc, data = win32file.ReadFile(handle, min(65536, limit + 1 - size))
            if not data:
                break
            chunks.append(data)
            size += len(data)
            if size > limit:
                raise ProfileError("profile too large")
        after = _version(handle)
        if before != after or (after[1] << 32 | after[2]) != size:
            raise ProfileError("profile changed")
        with _open(path, False) as named:
            if _version(named) != after:
                raise ProfileError("profile changed")
        return b"".join(chunks), after


@dataclass(frozen=True)
class Profile:
    uid: str
    name: str
    filename: str
    current: bool
    identity: tuple


@dataclass(frozen=True)
class Registry:
    root: Path
    root_identity: tuple
    profiles_identity: tuple
    registry_data: bytes
    registry_version: tuple
    profiles: tuple


@dataclass(frozen=True)
class ProfileSnapshot:
    profile: Profile
    data: bytes
    version: tuple
    names: tuple
    node_types: tuple = ()


def load_registry(root=None):
    try:
        with _pinned(root or default_root()) as (root, root_id, profiles_id):
            raw, version = _read(root / "profiles.yaml", MAX_REGISTRY)
            document = parse_yaml(raw)
            if not isinstance(document, dict) or not isinstance(document.get("items"), list):
                raise ProfileError("invalid registry")
            current = document.get("current")
            if current is not None and not _valid_text(current):
                raise ProfileError("invalid registry")
            eligible, ignored_ids, ignored_names = [], set(), set()
            ids, names, identities = set(), set(), set()
            for item in document["items"]:
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    raise ProfileError("invalid registry")
                kind = item["type"]
                uid, name, filename = item.get("uid"), item.get("name"), item.get("file")
                if kind in ("script", "merge", "rules", "proxies", "groups"):
                    if _valid_text(uid):
                        ignored_ids.add(uid.encode())
                    if _valid_filename(filename):
                        ignored_names.add(filename.encode())
                    continue
                if kind not in ("remote", "local") or not _valid_text(uid) or not _valid_text(name) or not _valid_filename(filename):
                    raise ProfileError("invalid registry")
                uid_bytes, file_bytes = uid.encode(), filename.encode()
                with _open(root / "profiles" / filename, False) as handle:
                    identity = _identity(handle)
                if uid_bytes in ids or file_bytes in names or identity in identities:
                    raise ProfileError("invalid registry")
                ids.add(uid_bytes)
                names.add(file_bytes)
                identities.add(identity)
                eligible.append(Profile(uid, name, filename, uid == current, identity))
            if ids & ignored_ids or names & ignored_names:
                raise ProfileError("invalid registry")
            if current is not None and sum(p.current for p in eligible) != 1:
                raise ProfileError("invalid registry")
            if _read(root / "profiles.yaml", MAX_REGISTRY) != (raw, version):
                raise ProfileError("registry changed")
            return Registry(root, root_id, profiles_id, raw, version, tuple(eligible))
    except ProfileError:
        raise
    except Exception:
        raise ProfileError("registry unavailable") from None


def _inspect_nodes_and_types(raw):
    if not raw or len(raw) > MAX_PROFILE:
        raise ProfileError("invalid profile")
    document = parse_yaml(raw)
    if not isinstance(document, dict) or "proxy-providers" in document:
        raise ProfileError("invalid profile")
    proxies = document.get("proxies")
    if not isinstance(proxies, list) or not 1 <= len(proxies) <= 2000:
        raise ProfileError("invalid profile")
    names, types, seen = [], [], set()
    for item in proxies:
        name = item.get("name") if isinstance(item, dict) else None
        if not _valid_text(name, 1024) or name.encode() in seen:
            raise ProfileError("invalid profile")
        seen.add(name.encode())
        names.append(name)
        raw_type = item.get("type") if isinstance(item, dict) else ""
        ntype = raw_type.strip() if isinstance(raw_type, str) and len(raw_type.strip()) <= 64 else ""
        types.append(ntype)
    return tuple(names), tuple(types)


def inspect_nodes(raw):
    return _inspect_nodes_and_types(raw)[0]


def snapshot_profile(registry, profile):
    if profile not in registry.profiles:
        raise ProfileError("profile changed")
    with _pinned(registry.root) as (root, root_id, profiles_id):
        if (root_id, profiles_id) != (registry.root_identity, registry.profiles_identity):
            raise ProfileError("profile changed")
        if _read(root / "profiles.yaml", MAX_REGISTRY) != (registry.registry_data, registry.registry_version):
            raise ProfileError("registry changed")
        raw, version = _read(root / "profiles" / profile.filename, MAX_PROFILE)
        if version[0] != profile.identity:
            raise ProfileError("profile changed")
        names, types = _inspect_nodes_and_types(raw)
        return ProfileSnapshot(profile, raw, version, names, types)


def revalidate(registry, snapshots):
    try:
        with _pinned(registry.root) as (root, root_id, profiles_id):
            if (root_id, profiles_id) != (registry.root_identity, registry.profiles_identity):
                raise ProfileError("registry changed")
            if _read(root / "profiles.yaml", MAX_REGISTRY) != (registry.registry_data, registry.registry_version):
                raise ProfileError("registry changed")
            known = set(registry.profiles)
            for snapshot in snapshots:
                if snapshot.profile not in known:
                    raise ProfileError("profile changed")
                raw, version = _read(root / "profiles" / snapshot.profile.filename, MAX_PROFILE)
                if version[0] != snapshot.profile.identity or (raw, version) != (snapshot.data, snapshot.version):
                    raise ProfileError("profile changed")
            if _read(root / "profiles.yaml", MAX_REGISTRY) != (registry.registry_data, registry.registry_version):
                raise ProfileError("registry changed")
    except ProfileError:
        raise
    except Exception:
        raise ProfileError("registry changed") from None
