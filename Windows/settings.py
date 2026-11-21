"""Persistent desktop preferences with reparse-point safety and test isolation."""

import json
import os
from pathlib import Path
import tempfile
import threading

if os.name == "nt":
    import win32con
    import win32file


class SettingsSecurityError(Exception):
    pass


def _is_reparse(path: Path) -> bool:
    if os.name != "nt":
        return path.is_symlink()
    attrs = win32file.GetFileAttributesW(str(path))
    if attrs == -1:
        return False
    return bool(attrs & win32con.FILE_ATTRIBUTE_REPARSE_POINT)


def _safe_prepare_directory(directory: Path) -> Path:
    target = Path(os.path.abspath(str(directory)))
    drive = os.path.splitdrive(str(target))[0]
    if os.name == "nt" and (not drive or drive.startswith("\\")):
        raise SettingsSecurityError("Settings path must be on a local drive.")
    probe = target
    to_create = []
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            raise SettingsSecurityError("Invalid settings path.")
        to_create.append(probe)
        probe = parent
    curr = probe
    while True:
        if _is_reparse(curr):
            raise SettingsSecurityError("Reparse point is not allowed in settings directory.")
        if curr.parent == curr:
            break
        curr = curr.parent
    if not probe.is_dir():
        raise SettingsSecurityError("Settings ancestor is not a directory.")
    for part in reversed(to_create):
        part.mkdir(exist_ok=True)
        if _is_reparse(part) or not part.is_dir():
            raise SettingsSecurityError("Created settings directory is unsafe.")
    return target


class AppSettingsStore:
    """Stores UI/tray preferences in settings.json without touching Clash Verge config."""

    def __init__(self, directory=None):
        self._lock = threading.RLock()
        if directory is None:
            directory = self._default_directory()
        self.directory = Path(directory) if directory is not None else None

    @staticmethod
    def _default_directory():
        for env_key in ("CLASH_SPEED_HELPER_SETTINGS_DIR", "CLASH_SPEED_HELPER_TELEMETRY_DIR"):
            override = os.environ.get(env_key, "").strip()
            if override:
                return Path(override)
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return Path(local) / "ClashSpeedHelper"
        return None

    @property
    def settings_path(self):
        if self.directory is None:
            return None
        return Path(os.path.abspath(str(self.directory))) / "settings.json"

    def _validated_target(self, create_dir: bool) -> Path | None:
        if self.directory is None:
            return None
        if create_dir:
            safe_dir = _safe_prepare_directory(self.directory)
        else:
            safe_dir = Path(os.path.abspath(str(self.directory)))
            if not safe_dir.exists():
                return None
            curr = safe_dir
            while True:
                if _is_reparse(curr):
                    raise SettingsSecurityError("Reparse point in settings directory.")
                if curr.parent == curr:
                    break
                curr = curr.parent
            if not safe_dir.is_dir():
                raise SettingsSecurityError("Settings path is not a directory.")
        cfg_path = safe_dir / "settings.json"
        if cfg_path.exists():
            if _is_reparse(cfg_path) or cfg_path.is_dir():
                raise SettingsSecurityError("Unsafe settings.json target.")
        return cfg_path

    def _read_dict_locked(self) -> dict:
        cfg_path = self._validated_target(create_dir=False)
        if cfg_path is None or not cfg_path.is_file():
            return {}
        raw = cfg_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def load_minimize_to_tray(self, default: bool = True) -> bool:
        with self._lock:
            try:
                data = self._read_dict_locked()
            except SettingsSecurityError:
                return default
            except Exception:
                return default
            val = data.get("minimize_to_tray", default)
            return val if type(val) is bool else default

    def save_minimize_to_tray(self, enabled: bool) -> bool:
        if type(enabled) is not bool:
            raise ValueError("minimize_to_tray must be a boolean")
        with self._lock:
            cfg_path = self._validated_target(create_dir=True)
            if cfg_path is None:
                return False
            try:
                existing = self._read_dict_locked()
            except SettingsSecurityError:
                raise
            except Exception:
                existing = {}
            existing["minimize_to_tray"] = enabled
            payload = json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
            fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(cfg_path.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                if _is_reparse(tmp_path):
                    raise SettingsSecurityError("Temporary settings file is unsafe.")
                if cfg_path.exists() and (_is_reparse(cfg_path) or cfg_path.is_dir()):
                    raise SettingsSecurityError("Target settings.json became unsafe.")
                os.replace(str(tmp_path), str(cfg_path))
            finally:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
            return True
