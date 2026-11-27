"""Authenticated local Named Pipe/AF_UNIX controller and assisted switch."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import ctypes
from ctypes import wintypes
import http.client
import io
import json
import math
import os
from pathlib import Path
import select
import socket
import threading
import time
import unicodedata
from urllib.parse import quote

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32process
import win32security

from .profile_registry import (_identity, _open, _pinned, _read, _valid_filename,
                               parse_yaml, revalidate, MAX_REGISTRY, ProfileError)

MAX_RESPONSE = 8 * 1024 * 1024
MAX_TEXT = 8192
_PIPE_PREFIX = "\\\\.\\pipe\\"
_OVERLAPPED = 0x40000000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_AF_UNIX = 1
_AF_UNIX_REPARSE_TAG = 0x80000023
_READ_CONTROL = 0x00020000
_IO_UNRESOLVED = threading.Event()
_CONFIG_PARSE_LOCK = threading.Lock()
_CONFIG_PARSE_CACHE = None


class ControllerError(Exception):
    """Never contains raw responses, pipe names, secrets, or OS diagnostics."""


class ProbeError(ControllerError):
    """Read-only proxy delay probe returned an unexpected HTTP status or payload."""


def _valid_text(value, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty):
        return False
    try:
        return (len(value.encode("utf-8")) <= MAX_TEXT and
                not any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in value))
    except UnicodeError:
        return False


def _valid_test_url(url):
    return (_valid_text(url) and len(url.encode("utf-8")) <= 1024 and
            (url.startswith("http://") or url.startswith("https://")))


def _valid_group(name):
    return (_valid_text(name) and name not in (".", "..") and
            not any(c in name for c in "/\\%?#;"))


@dataclass(frozen=True)
class Settings:
    pipe: str = field(repr=False)
    secret: str = field(repr=False)
    config_data: bytes = field(repr=False)
    config_version: tuple = field(repr=False)
    unix: str = field(default="", repr=False)


@dataclass(frozen=True)
class ProxyGroup:
    name: str
    now: str
    all: tuple


def _parse_verge_config_cached(data, version):
    """Memoize deterministic strict YAML parsing for identical clash-verge.yaml bytes."""
    global _CONFIG_PARSE_CACHE
    with _CONFIG_PARSE_LOCK:
        cached = _CONFIG_PARSE_CACHE
        if cached is not None and cached[0] == version and cached[1] == data:
            return cached[2]
    doc = parse_yaml(data)
    with _CONFIG_PARSE_LOCK:
        _CONFIG_PARSE_CACHE = (version, data, doc)
    return doc


def read_settings(registry):
    try:
        with _pinned(registry.root) as (root, root_id, profiles_id):
            if (root_id, profiles_id) != (registry.root_identity, registry.profiles_identity):
                raise ControllerError("controller changed")
            data, version = _read(root / "clash-verge.yaml", MAX_REGISTRY)
        document = _parse_verge_config_cached(data, version)
        if not isinstance(document, dict):
            raise ControllerError("controller unavailable")
        pipe = document.get("external-controller-pipe")
        unix = document.get("external-controller-unix")
        if pipe is None:
            pipe = ""
        if unix is None:
            unix = ""
        secret = document.get("secret", "")
        if (not isinstance(pipe, str) or not isinstance(unix, str) or
                not _valid_text(secret, allow_empty=True) or bool(pipe) == bool(unix)):
            raise ControllerError("controller unavailable")
        if pipe:
            if (not _valid_text(pipe) or not pipe.lower().startswith(_PIPE_PREFIX.lower()) or
                    not pipe[len(_PIPE_PREFIX):] or "\\" in pipe[len(_PIPE_PREFIX):] or
                    "/" in pipe[len(_PIPE_PREFIX):]):
                raise ControllerError("controller unavailable")
        else:
            unix = _validate_unix_path(unix)
        return Settings(pipe, secret, data, version, unix)
    except ControllerError:
        raise
    except Exception:
        raise ControllerError("controller unavailable") from None


def read_tun_enabled(registry):
    """Safely inspect clash-verge.yaml tun.enable as bool | None.

    Missing or malformed config degrades to None without blocking refresh;
    reparse-point, path-pinning, and TOCTOU failures propagate as ProfileError.
    """
    with _pinned(registry.root) as (root, root_id, profiles_id):
        if (root_id, profiles_id) != (registry.root_identity, registry.profiles_identity):
            raise ProfileError("profile changed")
        try:
            data, version = _read(root / "clash-verge.yaml", MAX_REGISTRY)
        except pywintypes.error as exc:
            if exc.winerror in (2, 3):
                return None
            raise ProfileError("profile unavailable") from None
    try:
        document = _parse_verge_config_cached(data, version)
    except ProfileError:
        return None
    if not isinstance(document, dict):
        return None
    tun = document.get("tun")
    if not isinstance(tun, dict):
        return None
    enabled = tun.get("enable")
    return enabled if type(enabled) is bool else None


def _validate_unix_path(value):
    if not _valid_text(value):
        raise ControllerError("controller unavailable")
    value = value.replace("/", "\\")
    drive, tail = os.path.splitdrive(value)
    parts = Path(tail).parts
    if (not drive or drive.startswith("\\") or not tail.startswith("\\") or
            value != os.path.normpath(value) or
            not parts or any(not _valid_filename(part) for part in parts if part != "\\")):
        raise ControllerError("controller unavailable")
    try:
        raw = value.encode("utf-8", "strict")
    except UnicodeError:
        raise ControllerError("controller unavailable") from None
    if not 0 < len(raw) <= 107:
        raise ControllerError("controller unavailable")
    return value


@contextmanager
def _pin_unix_socket(value):
    """Pin local ancestors and the same-user AF_UNIX reparse object, not symlinks."""
    value = _validate_unix_path(value)
    drive, tail = os.path.splitdrive(value)
    parts = [part for part in Path(tail).parts if part != "\\"]
    if not parts:
        raise ControllerError("controller unavailable")
    try:
        with ExitStack() as stack:
            path = Path(drive + "\\")
            stack.enter_context(_open(path, True))
            for part in parts[:-1]:
                path /= part
                stack.enter_context(_open(path, True))
            path /= parts[-1]
            handle = win32file.CreateFile(str(path), _READ_CONTROL,
                                         win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                                         None, win32con.OPEN_EXISTING,
                                         win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
            try:
                info = win32file.GetFileInformationByHandleEx(handle, win32file.FileAttributeTagInfo)
                if (not info["FileAttributes"] & win32con.FILE_ATTRIBUTE_REPARSE_POINT or
                        info["ReparseTag"] != _AF_UNIX_REPARSE_TAG):
                    raise ControllerError("controller unavailable")
                owner = win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT,
                                                      win32security.OWNER_SECURITY_INFORMATION).GetSecurityDescriptorOwner()
                token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
                try:
                    current = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
                    if win32security.ConvertSidToStringSid(owner) != win32security.ConvertSidToStringSid(current):
                        raise ControllerError("controller unavailable")
                finally:
                    token.Close()
                yield str(path), ("unix",) + _identity(handle)
            finally:
                handle.Close()
    except ControllerError:
        raise
    except Exception:
        raise ControllerError("controller unavailable") from None


def _connect_unix(path, deadline):
    raw = path.encode("utf-8", "strict")
    address = (ctypes.c_ubyte * 110)()
    address[0] = _AF_UNIX
    address[2:2 + len(raw)] = raw
    stream = socket.socket(_AF_UNIX, socket.SOCK_STREAM)
    try:
        stream.setblocking(False)
        winsock = ctypes.windll.ws2_32
        winsock.connect.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        winsock.connect.restype = ctypes.c_int
        winsock.WSAGetLastError.restype = ctypes.c_int
        if winsock.connect(stream.fileno(), address, ctypes.sizeof(address)) != 0:
            if winsock.WSAGetLastError() not in (10035, 10036, 10037):
                raise ControllerError("controller unavailable")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControllerError("controller timeout")
            _, writable, exceptional = select.select([], [stream], [stream], remaining)
            if not writable or exceptional or stream.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                raise ControllerError("controller unavailable")
        return stream
    except Exception:
        stream.close()
        raise


def _pipe_identity(handle):
    pid = wintypes.ULONG()
    function = ctypes.windll.kernel32.GetNamedPipeServerProcessId
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    function.restype = wintypes.BOOL
    if not function(int(handle), ctypes.byref(pid)) or not pid.value:
        raise ControllerError("controller unavailable")
    process = win32api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    try:
        token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        current = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            own_sid = win32security.GetTokenInformation(current, win32security.TokenUser)[0]
            if win32security.ConvertSidToStringSid(sid) != win32security.ConvertSidToStringSid(own_sid):
                raise ControllerError("controller unavailable")
        finally:
            current.Close()
            token.Close()
        created = win32process.GetProcessTimes(process)["CreationTime"]
        return pid.value, created
    finally:
        process.Close()


def _overlapped_io(handle, deadline, data=None, amount=65536):
    if _IO_UNRESOLVED.is_set():
        raise ControllerError("controller unavailable")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ControllerError("controller timeout")
    operation = pywintypes.OVERLAPPED()
    event = win32event.CreateEvent(None, True, False, None)
    operation.hEvent = event
    try:
        if data is None:
            rc, buffer = win32file.ReadFile(handle, amount, operation)
        else:
            rc, buffer = win32file.WriteFile(handle, data, operation)
        if rc not in (0, 997):
            raise ControllerError("controller unavailable")
        if rc == 997:
            wait = win32event.WaitForSingleObject(event, max(1, int(remaining * 1000)))
            if wait != win32event.WAIT_OBJECT_0:
                try:
                    win32file.CancelIoEx(handle, operation)
                except pywintypes.error as exc:
                    if exc.winerror != 1168:  # ERROR_NOT_FOUND: already completed
                        raise
                if win32event.WaitForSingleObject(event, 1000) != win32event.WAIT_OBJECT_0:
                    # Keep the OVERLAPPED and its data buffer alive if a driver
                    # delays cancellation. Block new requests for this session.
                    _IO_UNRESOLVED.set()
                    pending_event = event
                    def finish_later(_operation=operation, _buffer=buffer, _data=data):
                        try:
                            win32event.WaitForSingleObject(pending_event, win32event.INFINITE)
                        finally:
                            pending_event.Close()
                    threading.Thread(target=finish_later, daemon=True).start()
                    event = None
                raise ControllerError("controller timeout")
        count = win32file.GetOverlappedResult(handle, operation, False)
        return bytes(buffer[:count]) if data is None else count
    except pywintypes.error as exc:
        if data is None and exc.winerror in (109, 232, 233):
            return b""
        raise ControllerError("controller unavailable") from None
    finally:
        if event is not None:
            event.Close()


class _PipeReader(io.RawIOBase):
    def __init__(self, handle, deadline):
        self.handle = handle
        self.deadline = deadline
        self.total = 0

    def readable(self):
        return True

    def readinto(self, destination):
        data = _overlapped_io(self.handle, self.deadline, amount=min(len(destination), 65536))
        self.total += len(data)
        if self.total > MAX_RESPONSE + 128 * 1024:
            raise ControllerError("controller response too large")
        destination[:len(data)] = data
        return len(data)


class _SocketView:
    def __init__(self, handle, deadline):
        self.reader = _PipeReader(handle, deadline)

    def makefile(self, *_args, **_kwargs):
        return io.BufferedReader(self.reader)


class _UnixReader(io.RawIOBase):
    def __init__(self, stream, deadline):
        self.stream, self.deadline, self.total = stream, deadline, 0

    def readable(self):
        return True

    def readinto(self, destination):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ControllerError("controller timeout")
        self.stream.settimeout(remaining)
        count = self.stream.recv_into(destination, min(len(destination), 65536))
        self.total += count
        if self.total > MAX_RESPONSE + 128 * 1024:
            raise ControllerError("controller response too large")
        return count


class _UnixSocketView:
    def __init__(self, stream, deadline):
        self.reader = _UnixReader(stream, deadline)

    def makefile(self, *_args, **_kwargs):
        return io.BufferedReader(self.reader)


def _request_bytes(settings, method, path, body):
    request = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
    if settings.secret:
        request += "Authorization: Bearer " + settings.secret + "\r\n"
    payload = b"" if body is None else json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if body is not None:
        request += f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
    return request.encode("utf-8") + b"\r\n" + payload


def _response(view):
    response = http.client.HTTPResponse(view)
    response.begin()
    lengths = response.headers.get_all("Content-Length", [])
    transfers = response.headers.get_all("Transfer-Encoding", [])
    if len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers):
        raise ControllerError("invalid response")
    result = response.read(MAX_RESPONSE + 1)
    if len(result) > MAX_RESPONSE:
        raise ControllerError("invalid response")
    return response.status, result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        key.encode("utf-8", "strict")
        result[key] = value
    return result


def _strict_json(raw):
    if len(raw) > MAX_RESPONSE:
        raise ControllerError("invalid response")
    try:
        def bad_constant(_value):
            raise ValueError("invalid constant")
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique,
                           parse_constant=bad_constant)
        def check(item, depth=0):
            if depth > 64:
                raise ValueError("too deep")
            if isinstance(item, str):
                item.encode("utf-8", "strict")
            elif isinstance(item, list):
                for part in item:
                    check(part, depth + 1)
            elif isinstance(item, dict):
                for part in item.values():
                    check(part, depth + 1)
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("invalid number")
        check(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ControllerError("invalid response") from None


class PipeController:
    def __init__(self, settings, timeout=5):
        self.settings = settings
        self.timeout = max(.05, min(5, float(timeout)))
        self.identity = None

    def _request(self, method, path, body=None, timeout=None):
        try:
            effective_timeout = self.timeout if timeout is None else max(.05, min(35, float(timeout)))
            deadline = time.monotonic() + effective_timeout
            data = _request_bytes(self.settings, method, path, body)
            if self.settings.unix:
                with _pin_unix_socket(self.settings.unix) as (socket_path, identity):
                    if self.identity is not None and self.identity != identity:
                        raise ControllerError("controller changed")
                    self.identity = identity
                    with _connect_unix(socket_path, deadline) as stream:
                        # Windows can rename an AF_UNIX reparse object even while
                        # it is open without FILE_SHARE_DELETE. Reopen the name
                        # after connect and before sending any credentials/body.
                        with _pin_unix_socket(socket_path) as (_name, current):
                            if current != identity:
                                raise ControllerError("controller changed")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ControllerError("controller timeout")
                        stream.settimeout(remaining)
                        stream.sendall(data)
                        response = _response(_UnixSocketView(stream, deadline))
                        with _pin_unix_socket(socket_path) as (_name, current):
                            if current != identity:
                                raise ControllerError("controller changed")
                        return response
            win32pipe.WaitNamedPipe(self.settings.pipe, max(1, min(2000, int(effective_timeout * 1000))))
            handle = win32file.CreateFile(self.settings.pipe, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                         0, None, win32con.OPEN_EXISTING, _OVERLAPPED, None)
            try:
                identity = ("pipe",) + _pipe_identity(handle)
                if self.identity is not None and self.identity != identity:
                    raise ControllerError("controller changed")
                self.identity = identity
                position = 0
                while position < len(data):
                    written = _overlapped_io(handle, deadline, data=data[position:])
                    if not written:
                        raise ControllerError("controller unavailable")
                    position += written
                return _response(_SocketView(handle, deadline))
            finally:
                handle.Close()
        except ControllerError:
            raise
        except Exception:
            raise ControllerError("controller unavailable") from None

    def groups(self, node_names):
        status, raw = self._request("GET", "/proxies")
        if status != 200:
            raise ControllerError("unexpected status")
        root = _strict_json(raw)
        proxies = root.get("proxies") if isinstance(root, dict) else None
        if not isinstance(proxies, dict):
            raise ControllerError("invalid response")
        groups = []
        for key, proxy in proxies.items():
            if not isinstance(proxy, dict) or proxy.get("type") != "Selector":
                continue
            name, now, members = proxy.get("name"), proxy.get("now"), proxy.get("all")
            if not isinstance(name, str) or key != name or not isinstance(now, str) or not isinstance(members, list) or not all(isinstance(x, str) for x in members):
                raise ControllerError("invalid response")
            if _valid_group(name) and any(name in node_names for name in members):
                groups.append(ProxyGroup(name, now, tuple(members)))
        return sorted(groups, key=lambda group: group.name.encode("utf-8"))

    def group(self, name):
        if not _valid_group(name):
            raise ControllerError("invalid group")
        status, raw = self._request("GET", "/proxies/" + quote(name, safe="-._~"))
        if status != 200:
            raise ControllerError("unexpected status")
        value = _strict_json(raw)
        if not isinstance(value, dict) or value.get("name") != name or value.get("type") != "Selector":
            raise ControllerError("invalid selector")
        now, members = value.get("now"), value.get("all")
        if not isinstance(now, str) or not isinstance(members, list) or not all(isinstance(x, str) for x in members):
            raise ControllerError("invalid selector")
        return ProxyGroup(name, now, tuple(members))

    def probe_node_delay(self, node, timeout_ms=3500, test_url="http://www.gstatic.com/generate_204"):
        if (not _valid_text(node) or node in (".", "..") or
                type(timeout_ms) is not int or not 100 <= timeout_ms <= 30_000 or
                not _valid_test_url(test_url)):
            raise ControllerError("invalid probe request")
        path = f"/proxies/{quote(node, safe='-._~')}/delay?timeout={timeout_ms}&url={quote(test_url, safe='-._~')}"
        max_time = max(5.0, math.ceil(timeout_ms / 1000.0) + 2.0)
        status, raw = self._request("GET", path, timeout=max_time)
        if status != 200:
            raise ProbeError("probe failed")
        if len(raw) > 4096:
            raise ProbeError("invalid probe response")
        try:
            value = _strict_json(raw)
        except ControllerError:
            raise ProbeError("invalid probe response") from None
        delay = value.get("delay") if isinstance(value, dict) else None
        if (type(delay) not in (int, float) or isinstance(delay, bool) or
                not math.isfinite(delay) or delay <= 0 or delay > 60_000):
            raise ProbeError("invalid probe response")
        return float(delay)


class SwitchLatch:
    """Shared by all coordinators in an app session; never auto-cleared."""
    def __init__(self):
        self.lock = threading.RLock()
        self.outcome_unknown = False


_SESSION_LATCH = SwitchLatch()


class AssistedSwitch:
    """One preparation per apply; an uncertain PUT permanently locks this instance."""

    def __init__(self, registry, snapshot, node, measurement=None, latch=None):
        if not _valid_text(node) or node not in snapshot.names:
            raise ControllerError("invalid node")
        if measurement is not None:
            lowered = node.lower()
            metadata = ("剩余流量", "到期", "流量重置", "重置时间", "过期时间", "订阅信息",
                        "traffic remaining", "remaining traffic", "subscription expires", "expiration date")
            if (measurement.node_name != node or measurement.score is None or
                    any(term in lowered for term in metadata)):
                raise ControllerError("invalid measurement")
        self.registry = registry
        self.snapshot = snapshot
        self.node = node
        self.settings = read_settings(registry)
        self.controller = PipeController(self.settings)
        self.prepared = None
        self.latch = latch or _SESSION_LATCH
        self.lock = self.latch.lock

    def _validate(self):
        if not self.snapshot.profile.current:
            raise ControllerError("manual profile required")
        try:
            revalidate(self.registry, [self.snapshot])
        except ProfileError:
            raise ControllerError("profile changed") from None
        if read_settings(self.registry) != self.settings:
            raise ControllerError("controller changed")

    def prepare(self):
        with self.lock:
            if self.latch.outcome_unknown:
                raise ControllerError("previous outcome unknown")
            self.prepared = None
            self._validate()
            groups = self.controller.groups([self.node])
            self._validate()
            self.prepared = tuple(groups)
            return self.prepared

    def apply(self, group_name, authorized):
        with self.lock:
            if self.latch.outcome_unknown:
                return "previous_outcome_unknown"
            prepared, self.prepared = self.prepared, None
            if not callable(authorized) or not authorized() or prepared is None:
                return "not_attempted"
            selected = next((group for group in prepared if group.name == group_name), None)
            if selected is None:
                return "not_attempted"
            try:
                self._validate()
                before = self.controller.group(selected.name)
                if self.node not in before.all or before.now != selected.now:
                    return "not_attempted"
                self._validate()
                if not authorized():
                    return "not_attempted"
                if before.now == self.node:
                    return "confirmed_noop"
            except Exception:
                return "not_attempted"
            try:
                status, _ = self.controller._request("PUT", "/proxies/" + quote(group_name, safe="-._~"), {"name": self.node})
                if status != 204:
                    raise ControllerError("uncertain")
                after = self.controller.group(group_name)
                if after.now != self.node:
                    raise ControllerError("uncertain")
                return "switched"
            except Exception:
                self.latch.outcome_unknown = True
                return "outcome_unknown"
