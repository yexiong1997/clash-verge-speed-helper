"""Exercise Windows AF_UNIX using synthetic sockets and HTTP only."""

import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.name == "nt":
    from Windows.controller import (AssistedSwitch, ControllerError, SwitchLatch,
                                    _connect_unix, _pin_unix_socket, read_settings)
    from Windows.profile_registry import load_registry, snapshot_profile


@unittest.skipUnless(os.name == "nt", "Windows AF_UNIX")
class UnixControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clash-unix-fixture-")
        self.root = Path(self.tmp.name)
        (self.root / "profiles").mkdir()
        (self.root / "profiles.yaml").write_text(
            "current: u\nitems:\n- {uid: u, name: Test, type: local, file: p.yaml}\n", encoding="utf-8")
        (self.root / "profiles" / "p.yaml").write_text("proxies:\n- {name: A}\n- {name: B}\n", encoding="utf-8")
        self.path = self.root / "ctrl.sock"
        if len(str(self.path).encode()) > 107:
            self.skipTest("synthetic AF_UNIX path too long")
        self.listener = socket.socket(1, socket.SOCK_STREAM)
        address = (ctypes.c_ubyte * 110)()
        address[0] = 1
        raw = str(self.path).encode("utf-8")
        address[2:2 + len(raw)] = raw
        self.winsock = ctypes.windll.ws2_32
        self.winsock.bind.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        self.winsock.bind.restype = ctypes.c_int
        self.winsock.accept.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        self.winsock.accept.restype = ctypes.c_size_t
        if self.winsock.bind(self.listener.fileno(), address, ctypes.sizeof(address)) != 0:
            self.listener.close()
            self.tmp.cleanup()
            self.skipTest("Windows AF_UNIX unavailable")
        self.listener.listen(8)
        (self.root / "clash-verge.yaml").write_text(
            "external-controller-unix: '" + str(self.path) + "'\nsecret: synthetic-secret\n", encoding="utf-8")
        self.now = "A"
        self.puts = 0
        self.bad_put_status = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.server, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stop.set()
        try:
            client = _connect_unix(str(self.path), time.monotonic() + 2)
            client.close()
        except Exception:
            pass
        self.thread.join(3)
        self.listener.close()
        self.tmp.cleanup()

    def server(self):
        while not self.stop.is_set():
            accepted = self.winsock.accept(self.listener.fileno(), None, None)
            if accepted == ctypes.c_size_t(-1).value:
                return
            with socket.socket(1, socket.SOCK_STREAM, fileno=accepted) as client:
                if self.stop.is_set():
                    return
                try:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    if b"\r\n\r\n" not in data:
                        continue
                    header, body = data.split(b"\r\n\r\n", 1)
                    lines = header.split(b"\r\n")
                    method, target, _ = lines[0].split(b" ")
                    length = next((int(line.split(b":", 1)[1]) for line in lines
                                   if line.lower().startswith(b"content-length:")), 0)
                    while len(body) < length:
                        body += client.recv(65536)
                    if b"Authorization: Bearer synthetic-secret" not in lines:
                        status, response = 401, b""
                    elif method == b"PUT":
                        self.puts += 1
                        self.now = json.loads(body)["name"]
                        status, response = (500 if self.bad_put_status else 204), b""
                    else:
                        group = {"name": "Test", "type": "Selector", "now": self.now, "all": ["A", "B"]}
                        obj = {"proxies": {"Test": group}} if target == b"/proxies" else group
                        status, response = 200, json.dumps(obj).encode()
                    client.sendall(f"HTTP/1.1 {status} OK\r\nContent-Length: {len(response)}\r\nConnection: close\r\n\r\n".encode() + response)
                except (OSError, ValueError):
                    pass

    def coordinator(self):
        registry = load_registry(self.root)
        snapshot = snapshot_profile(registry, registry.profiles[0])
        return AssistedSwitch(registry, snapshot, "B", latch=SwitchLatch())

    def test_get_put_and_confirm(self):
        coordinator = self.coordinator()
        self.assertEqual([group.name for group in coordinator.prepare()], ["Test"])
        self.assertEqual(coordinator.apply("Test", lambda: True), "switched")
        self.assertEqual((self.now, self.puts), ("B", 1))

    def test_changed_current_is_not_written(self):
        coordinator = self.coordinator()
        coordinator.prepare()
        self.now = "B"
        self.assertEqual(coordinator.apply("Test", lambda: True), "not_attempted")
        self.assertEqual(self.puts, 0)

    def test_uncertain_result_remains_locked(self):
        coordinator = self.coordinator()
        coordinator.prepare()
        self.bad_put_status = True
        self.assertEqual(coordinator.apply("Test", lambda: True), "outcome_unknown")
        with self.assertRaises(ControllerError):
            coordinator.prepare()
        self.assertEqual(self.puts, 1)

    def test_renamed_socket_is_not_reaccepted(self):
        with _pin_unix_socket(str(self.path)):
            moved = self.root / "moved.sock"
            os.rename(self.path, moved)
            try:
                with self.assertRaises(ControllerError):
                    with _pin_unix_socket(str(self.path)):
                        pass
            finally:
                os.rename(moved, self.path)
        self.assertTrue(self.path.exists())

    def test_changed_path_between_connect_and_send_fails_closed(self):
        coordinator = self.coordinator()
        moved = self.root / "moved.sock"
        def connect_then_rename(path, deadline):
            stream = _connect_unix(path, deadline)
            os.rename(path, moved)
            return stream
        try:
            with patch("Windows.controller._connect_unix", side_effect=connect_then_rename):
                with self.assertRaises(ControllerError):
                    coordinator.prepare()
            self.assertEqual(self.puts, 0)
        finally:
            if moved.exists():
                os.rename(moved, self.path)

    def test_replaced_socket_identity_is_rejected(self):
        coordinator = self.coordinator()
        coordinator.prepare()
        moved = self.root / "old.sock"
        replacement = socket.socket(1, socket.SOCK_STREAM)
        os.rename(self.path, moved)
        bound = False
        try:
            address = (ctypes.c_ubyte * 110)()
            address[0] = 1
            raw = str(self.path).encode("utf-8")
            address[2:2 + len(raw)] = raw
            self.assertEqual(self.winsock.bind(replacement.fileno(), address, ctypes.sizeof(address)), 0)
            bound = True
            replacement.listen(1)
            with self.assertRaises(ControllerError):
                coordinator.controller.group("Test")
            self.assertEqual(self.puts, 0)
        finally:
            replacement.close()
            if bound and self.path.exists():
                os.unlink(self.path)
            os.rename(moved, self.path)

    def test_non_socket_and_ambiguous_endpoints_rejected(self):
        plain = self.root / "plain.sock"
        plain.write_text("synthetic", encoding="utf-8")
        with self.assertRaises(ControllerError):
            with _pin_unix_socket(str(plain)):
                pass
        registry = load_registry(self.root)
        (self.root / "clash-verge.yaml").write_text(
            "external-controller-unix: '" + str(self.path) + "'\n"
            "external-controller-pipe: '\\\\.\\pipe\\synthetic'\n", encoding="utf-8")
        with self.assertRaises(ControllerError):
            read_settings(registry)
        (self.root / "clash-verge.yaml").write_text(
            "external-controller-unix: '" + str(self.path) + "'\nexternal-controller-pipe: false\n", encoding="utf-8")
        with self.assertRaises(ControllerError):
            read_settings(registry)

    def test_socket_ancestor_junction_is_rejected(self):
        alias = self.root / "alias"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias),
                                 str(self.root)], capture_output=True)
        if result.returncode != 0:
            self.skipTest("creating synthetic junctions is not permitted")
        try:
            with self.assertRaises(ControllerError):
                with _pin_unix_socket(str(alias / self.path.name)):
                    pass
        finally:
            os.rmdir(alias)


if __name__ == "__main__":
    unittest.main()
