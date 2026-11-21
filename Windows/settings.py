"""Persistent desktop preferences with reparse-point safety and test isolation."""

import json
import os
from pathlib import Path
import tempfile
import threading

if os.name == "nt":
    import win32con
    import win32file

ALLOWED_DOWNLOAD_BYTES = frozenset((
    262_144,
    524_288,
    1_048_576,
    2_097_152,
    3_145_728,
    5_242_880,
    20_971_520,
    52_428_800,
))
ALLOWED_MODES = frozenset(("fast", "accurate"))
ALLOWED_INTERVALS = frozenset((0, 15, 30, 60, 120, 360, 720, 1440))
ALLOWED_PATROL_QUOTAS = frozenset((
    50 * 1_048_576,
    80 * 1_048_576,
    150 * 1_048_576,
    300 * 1_048_576,
))

DEFAULT_PREFERENCES = {
    "parallel": 10,
    "download_bytes": 3_145_728,
    "mode": "fast",
    "interval": 0,
    "patrol_quota": 80 * 1_048_576,
    "top_n": 10,
}
ALLOWED_PREFERENCE_KEYS = frozenset(DEFAULT_PREFERENCES.keys())


def _is_strict_int(value) -> bool:
    return type(value) is int


def validate_preference_patch(patch: dict) -> dict:
    """Strictly validate a user preference update; rejects auto_switch or unknown keys."""
    if not isinstance(patch, dict) or not patch:
        raise ValueError("偏好参数无效")
    unknown = set(patch.keys()) - ALLOWED_PREFERENCE_KEYS
    if unknown:
        raise ValueError("包含不允许持久化的参数")
    validated = {}
    if "parallel" in patch:
        val = patch["parallel"]
        if not _is_strict_int(val) or not (1 <= val <= 10):
            raise ValueError("并发数须为 1 至 10 的整数")
        validated["parallel"] = val
    if "download_bytes" in patch:
        val = patch["download_bytes"]
        if not _is_strict_int(val) or val not in ALLOWED_DOWNLOAD_BYTES:
            raise ValueError("下载包大小无效")
        validated["download_bytes"] = val
    if "mode" in patch:
        val = patch["mode"]
        if not isinstance(val, str) or val not in ALLOWED_MODES:
            raise ValueError("测速模式无效")
        validated["mode"] = val
    if "interval" in patch:
        val = patch["interval"]
        if not _is_strict_int(val) or val not in ALLOWED_INTERVALS:
            raise ValueError("定时间隔选项无效")
        validated["interval"] = val
    if "patrol_quota" in patch:
        val = patch["patrol_quota"]
        if not _is_strict_int(val) or val not in ALLOWED_PATROL_QUOTAS:
            raise ValueError("巡检日配额无效")
        validated["patrol_quota"] = val
    if "top_n" in patch:
        val = patch["top_n"]
        if not _is_strict_int(val) or not (1 <= val <= 2000):
            raise ValueError("前 N 项须为 1 至 2000 的整数")
        validated["top_n"] = val
    return validated


def sanitize_preferences(raw) -> dict:
    """Extract only safe ergonomic preferences from persisted JSON, dropping disallowed fields."""
    result = dict(DEFAULT_PREFERENCES)
    if not isinstance(raw, dict):
        return result
    for key in ALLOWED_PREFERENCE_KEYS:
        if key not in raw:
            continue
        try:
            result.update(validate_preference_patch({key: raw[key]}))
        except ValueError:
            pass
    return result


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

    def _write_dict_locked(self, updater) -> bool:
        cfg_path = self._validated_target(create_dir=True)
        if cfg_path is None:
            return False
        try:
            existing = self._read_dict_locked()
        except SettingsSecurityError:
            raise
        except Exception:
            existing = {}
        # Strip any disallowed keys that could have been written manually.
        for forbidden in ("auto_switch", "schedule_armed", "patrol_armed", "running", "group"):
            existing.pop(forbidden, None)
        updater(existing)
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
            return self._write_dict_locked(lambda existing: existing.__setitem__("minimize_to_tray", enabled))

    def load_preferences(self) -> dict:
        with self._lock:
            try:
                data = self._read_dict_locked()
            except SettingsSecurityError:
                return dict(DEFAULT_PREFERENCES)
            except Exception:
                return dict(DEFAULT_PREFERENCES)
            return sanitize_preferences(data.get("preferences"))

    def save_preferences(self, patch: dict) -> dict:
        validated = validate_preference_patch(patch)
        with self._lock:
            merged = [dict(DEFAULT_PREFERENCES)]

            def _apply(existing: dict):
                current = sanitize_preferences(existing.get("preferences"))
                current.update(validated)
                existing["preferences"] = current
                merged[0] = current

            self._write_dict_locked(_apply)
            return dict(merged[0])
