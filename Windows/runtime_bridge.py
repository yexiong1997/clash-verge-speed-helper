"""Popup-free Edge WebView2 CDP bridge for official Windows Clash Verge.

Architecture (REA-verified on official Clash Verge 2.5.10 + EdgeWebView2 157):
- Never modifies, patches, re-signs, or replaces official clash-verge.exe.
- Discovers the loopback-only ephemeral CDP port written by Edge WebView2 to
  %LOCALAPPDATA%\\io.github.clash-verge-rev.clash-verge-rev\\EBWebView\\DevToolsActivePort
  when launched with --remote-debugging-port=0 (via per-exe WebView2 policy or
  process-scoped WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS).
- Strictly validates no reparse points, loopback 127.0.0.1 binding, and
  official Tauri localhost page target before injecting the bearer-token
  protected RuntimeBroker bridge over local CDP Runtime.evaluate.
- Works silently with zero DevTools popups, zero UI flicker, and even when
  Clash Verge runs elevated (High IL) or hidden in the system tray.
"""

import base64
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import struct
import threading
import time
from urllib.parse import urlparse
import winreg

import win32con
import win32file

from .profile_registry import _open, _valid_filename
from .runtime_broker import (
    RuntimeBridgeUnavailable,
    RuntimeBroker,
    RuntimeBrokerError,
    RuntimeProfileSwitch,
    RuntimeSwitchCancelled,
    RuntimeSwitchOutcomeUnknown,
    valid_profile_uid,
)

CLASH_VERGE_EXE_NAME = "clash-verge.exe"
WEBVIEW2_POLICY_SUBKEY = r"SOFTWARE\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments"
WEBVIEW2_CDP_ARG = "--remote-debugging-port=0"
_TAURI_PAGE_PREFIXES = ("http://tauri.localhost/", "https://tauri.localhost/")
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_ACTIVE_PORT_BYTES = 4096
_MAX_CDP_JSON_BYTES = 512 * 1024
_MAX_WS_FRAME_BYTES = 512 * 1024


def default_webview_data_dir():
    override = os.environ.get("CLASH_SPEED_HELPER_WEBVIEW_DIR", "").strip()
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    return Path(local_app_data) / "io.github.clash-verge-rev.clash-verge-rev" / "EBWebView"


def _read_active_port_file(webview_dir):
    """Safely read DevToolsActivePort without following any reparse points."""
    if not webview_dir:
        return None
    try:
        target_dir = Path(os.path.abspath(str(webview_dir)))
        drive, tail = os.path.splitdrive(str(target_dir))
        if not drive or drive.startswith("\\") or not tail.startswith("\\"):
            return None
        parts = [p for p in Path(tail).parts if p not in ("\\", "/")]
        if any(not _valid_filename(p) for p in parts):
            return None
        with ExitStack() as stack:
            cur = Path(drive + "\\")
            stack.enter_context(_open(cur, True))
            for part in parts:
                cur = cur / part
                stack.enter_context(_open(cur, True))
            port_file = cur / "DevToolsActivePort"
            with _open(port_file, False, read=True) as handle:
                chunks = []
                total = 0
                while True:
                    _rc, data = win32file.ReadFile(handle, min(4096, _MAX_ACTIVE_PORT_BYTES + 1 - total))
                    if not data:
                        break
                    chunks.append(data)
                    total += len(data)
                    if total > _MAX_ACTIVE_PORT_BYTES:
                        return None
        text = b"".join(chunks).decode("utf-8", "strict")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None
        port = int(lines[0])
        if not 1 <= port <= 65535:
            return None
        browser_path = lines[1] if len(lines) >= 2 else ""
        return port, browser_path
    except Exception:
        return None


def _http_get_loopback_json(port, path, timeout=2.0):
    """Direct loopback HTTP GET bypassing Windows system proxy settings."""
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{int(port)}", "Connection": "close"})
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        raw = resp.read(_MAX_CDP_JSON_BYTES + 1)
        if len(raw) > _MAX_CDP_JSON_BYTES:
            return None
        return json.loads(raw.decode("utf-8", "strict"))
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _find_clash_tauri_ws_target(port, timeout=2.0):
    """Return validated loopback (port, ws_path) for Clash Verge's Tauri page."""
    targets = _http_get_loopback_json(port, "/json/list", timeout=timeout)
    if not isinstance(targets, list):
        return None
    for item in targets:
        if not isinstance(item, dict) or item.get("type") != "page":
            continue
        url = item.get("url")
        ws_url = item.get("webSocketDebuggerUrl")
        if not isinstance(url, str) or not isinstance(ws_url, str):
            continue
        if not any(url.startswith(prefix) for prefix in _TAURI_PAGE_PREFIXES):
            continue
        parsed = urlparse(ws_url)
        if parsed.scheme != "ws":
            continue
        if parsed.hostname not in ("127.0.0.1", "localhost"):
            continue
        if parsed.port != int(port):
            continue
        ws_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if not ws_path.startswith("/devtools/page/"):
            continue
        return int(port), ws_path
    return None


def _ws_send_frame(sock, opcode, payload):
    mask = os.urandom(4)
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _cdp_runtime_evaluate(port, ws_path, expression, timeout=5.0):
    """Execute Runtime.evaluate over a minimal loopback RFC 6455 WebSocket."""
    deadline = time.monotonic() + max(1.0, float(timeout))
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    ).decode("ascii")

    sock = socket.create_connection(("127.0.0.1", int(port)), timeout=max(0.5, deadline - time.monotonic()))
    try:
        req = (
            f"GET {ws_path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{int(port)}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(req)

        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeBridgeUnavailable("cdp handshake timeout")
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeBridgeUnavailable("cdp handshake closed")
            buf.extend(chunk)
            if len(buf) > 16384:
                raise RuntimeBridgeUnavailable("cdp handshake too large")

        sep = buf.index(b"\r\n\r\n")
        head = bytes(buf[:sep]).decode("latin-1", "replace")
        rest = bytearray(buf[sep + 4:])
        head_lines = head.split("\r\n")
        if not head_lines or " 101 " not in head_lines[0]:
            raise RuntimeBridgeUnavailable("cdp handshake rejected")
        headers = {}
        for line in head_lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if headers.get("sec-websocket-accept") != expected_accept:
            raise RuntimeBridgeUnavailable("cdp accept mismatch")

        req_id = 1
        body = json.dumps(
            {
                "id": req_id,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": False,
                },
            },
            ensure_ascii=True,
        ).encode("utf-8")
        _ws_send_frame(sock, 0x1, body)

        def read_exact(n):
            while len(rest) < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeBridgeUnavailable("cdp read timeout")
                sock.settimeout(remaining)
                chunk = sock.recv(max(4096, n - len(rest)))
                if not chunk:
                    raise RuntimeBridgeUnavailable("cdp connection closed")
                rest.extend(chunk)
            out = bytes(rest[:n])
            del rest[:n]
            return out

        fragments = []
        while True:
            b1, b2 = read_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", read_exact(8))[0]
            if length > _MAX_WS_FRAME_BYTES:
                raise RuntimeBridgeUnavailable("cdp frame too large")
            mask_key = read_exact(4) if masked else b""
            payload = read_exact(length)
            if masked:
                payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

            if opcode == 0x8:  # Close
                raise RuntimeBridgeUnavailable("cdp closed")
            if opcode == 0x9:  # Ping
                _ws_send_frame(sock, 0xA, payload)
                continue
            if opcode == 0xA:  # Pong
                continue
            if opcode in (0x1, 0x0):
                fragments.append(payload)
                if not fin:
                    continue
                full = b"".join(fragments)
                fragments.clear()
                msg = json.loads(full.decode("utf-8", "strict"))
                if isinstance(msg, dict) and msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeBridgeUnavailable("cdp evaluate error")
                    result_obj = msg.get("result") or {}
                    if "exceptionDetails" in result_obj:
                        raise RuntimeBridgeUnavailable("cdp script exception")
                    return result_obj.get("result", {}).get("value")
    except RuntimeBrokerError:
        raise
    except Exception:
        raise RuntimeBridgeUnavailable("cdp communication failed") from None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _has_cdp_arg(raw):
    return isinstance(raw, str) and "--remote-debugging-port=" in raw


def check_webview2_cdp_policy():
    """Read-only check whether clash-verge.exe has WebView2 CDP args configured."""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, WEBVIEW2_POLICY_SUBKEY, 0, winreg.KEY_READ) as key:
                val, _kind = winreg.QueryValueEx(key, CLASH_VERGE_EXE_NAME)
                if _has_cdp_arg(val):
                    return True
        except OSError:
            continue
    if _has_cdp_arg(os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")):
        return True
    return False


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _run_elevated_reg(args_str, timeout_ms=30000):
    """Invoke fixed reg.exe arguments via Windows UAC runas and wait for exit."""
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "reg.exe"
    info.lpParameters = args_str
    info.nShow = SW_HIDE
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return False, "已取消或无法获取管理员授权"
    if not info.hProcess:
        return False, "无法等待策略配置结果"
    try:
        wait_res = kernel32.WaitForSingleObject(info.hProcess, int(timeout_ms))
        if wait_res != 0:
            return False, "策略配置超时"
        code = wintypes.DWORD(1)
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value == 0, "" if code.value == 0 else "策略写入失败"
    finally:
        kernel32.CloseHandle(info.hProcess)


def enable_webview2_cdp_policy():
    """Configure per-exe official Edge WebView2 CDP policy strictly for clash-verge.exe.

    Writes only HKLM\\SOFTWARE\\Policies\\Microsoft\\Edge\\WebView2\\AdditionalBrowserArguments
    value name `clash-verge.exe` = `--remote-debugging-port=0`. Never touches
    clash-verge.exe binary or any other WebView2 application.
    """
    params = (
        f'add "HKLM\\{WEBVIEW2_POLICY_SUBKEY}" '
        f'/v "{CLASH_VERGE_EXE_NAME}" /t REG_SZ /d "{WEBVIEW2_CDP_ARG}" /f'
    )
    ok, err = _run_elevated_reg(params)
    if not ok:
        return {"ok": False, "error": err or "未能写入 WebView2 策略"}
    configured = check_webview2_cdp_policy()
    return {
        "ok": configured,
        "policy_configured": configured,
        "message": (
            "已仅为 clash-verge.exe 启用 Edge WebView2 本机无感热切策略；重启一次 Clash Verge 后永久自动就绪"
            if configured
            else "策略写入后未检测到生效项"
        ),
    }


def disable_webview2_cdp_policy():
    """Remove the per-exe clash-verge.exe Edge WebView2 CDP policy value."""
    params = f'delete "HKLM\\{WEBVIEW2_POLICY_SUBKEY}" /v "{CLASH_VERGE_EXE_NAME}" /f'
    ok, err = _run_elevated_reg(params)
    if not ok and check_webview2_cdp_policy():
        return {"ok": False, "error": err or "未能移除 WebView2 策略"}
    configured = check_webview2_cdp_policy()
    return {
        "ok": not configured,
        "policy_configured": configured,
        "message": "已移除 clash-verge.exe 的 WebView2 策略；重启 Clash Verge 后生效",
    }


class WindowsRuntimeBridge:
    """Manages silent CDP attach + bearer-token broker for cross-profile hot switch."""

    def __init__(self, webview_dir=None, cdp_port=None, policy_checker=None):
        self._webview_dir = Path(webview_dir) if webview_dir is not None else None
        self._cdp_port_override = int(cdp_port) if cdp_port is not None else None
        self._policy_checker = policy_checker or check_webview2_cdp_policy
        self._lock = threading.RLock()
        self._attach_lock = threading.Lock()
        self._broker = RuntimeBroker()
        self._closed = False
        self._last_status_ts = 0.0
        self._cached_status = None

    def _resolve_cdp_port(self):
        if self._cdp_port_override is not None:
            return self._cdp_port_override
        data_dir = self._webview_dir if self._webview_dir is not None else default_webview_data_dir()
        found = _read_active_port_file(data_dir)
        return found[0] if found else None

    def _discover_target(self, timeout=1.5):
        port = self._resolve_cdp_port()
        if not port:
            return None
        return _find_clash_tauri_ws_target(port, timeout=timeout)

    def status(self, force_probe=False):
        """Return sanitized UI-safe bridge status (no tokens, ports, or paths)."""
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return {
                    "state": "not_configured",
                    "ready": False,
                    "attached": False,
                    "cdp_ready": False,
                    "policy_configured": False,
                    "label": "跨订阅热切 已停止",
                }
            attached = self._broker.is_attached
            if attached:
                res = {
                    "state": "attached",
                    "ready": True,
                    "attached": True,
                    "cdp_ready": True,
                    "policy_configured": True,
                    "label": "跨订阅热切 已连接",
                }
                self._cached_status = res
                self._last_status_ts = now
                return dict(res)
            if not force_probe and self._cached_status is not None and (now - self._last_status_ts) < 3.0:
                return dict(self._cached_status)

        cdp_ready = self._discover_target(timeout=0.45) is not None
        policy_configured = bool(self._policy_checker())
        if cdp_ready:
            state, label = "cdp_ready", "跨订阅热切 就绪"
        elif policy_configured:
            state, label = "needs_restart", "跨订阅热切 待重启生效"
        else:
            state, label = "not_configured", "跨订阅热切 未启用"
        res = {
            "state": state,
            "ready": cdp_ready,
            "attached": False,
            "cdp_ready": cdp_ready,
            "policy_configured": policy_configured,
            "label": label,
        }
        with self._lock:
            self._cached_status = res
            self._last_status_ts = time.monotonic()
        return dict(res)

    def is_ready(self):
        return bool(self.status()["ready"])

    def ensure_attached(self, timeout=8.0, is_cancelled=None):
        with self._attach_lock:
            if self._closed:
                raise RuntimeBridgeUnavailable("bridge closed")
            if is_cancelled is not None and is_cancelled():
                raise RuntimeSwitchCancelled("cancelled")
            if self._broker.is_attached:
                return True
            target = self._discover_target(timeout=2.0)
            if target is None:
                raise RuntimeBridgeUnavailable("clash verge cdp unavailable")
            port, ws_path = target
            loader = self._broker.make_loader_script()
            _cdp_runtime_evaluate(port, ws_path, loader, timeout=min(5.0, float(timeout)))
            attached = self._broker.wait_until_attached(timeout=timeout, is_cancelled=is_cancelled)
            if not attached:
                raise RuntimeBridgeUnavailable("bridge failed to attach")
            with self._lock:
                self._cached_status = None
            return True

    def switch_profile(self, uid, timeout=25.0, is_cancelled=None):
        if not valid_profile_uid(uid):
            raise RuntimeBrokerError("invalid profile uid")
        if self._closed:
            raise RuntimeBridgeUnavailable("bridge closed")
        if is_cancelled is not None and is_cancelled():
            raise RuntimeSwitchCancelled("cancelled")

        for attempt in range(2):
            self.ensure_attached(timeout=min(8.0, float(timeout)), is_cancelled=is_cancelled)
            try:
                result = self._broker.enqueue_switch(
                    uid,
                    pickup_timeout=3.0,
                    execution_timeout=float(timeout),
                    is_cancelled=is_cancelled,
                )
                with self._lock:
                    self._cached_status = None
                return result
            except RuntimeBridgeUnavailable:
                # Command was never picked up; invalidate stale client and retry once.
                self._broker.invalidate_client()
                with self._lock:
                    self._cached_status = None
                if attempt == 1:
                    raise
            except RuntimeSwitchOutcomeUnknown:
                with self._lock:
                    self._cached_status = None
                raise
        raise RuntimeBridgeUnavailable("bridge unavailable")

    def close(self):
        with self._lock:
            self._closed = True
            self._cached_status = None
        self._broker.stop()
