"""Native Win32 system tray icon, menu model, and single-instance wake guard."""

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import sys
import threading

CMD_SHOW_HIDE = 1001
CMD_TOGGLE_MINIMIZE = 1002
CMD_STOP_RUN = 1003
CMD_STOP_SCHEDULE = 1004
CMD_STOP_PATROL = 1005
CMD_EXIT = 1006


def build_tray_menu_model(snapshot: dict) -> dict:
    """Build deterministic tray menu items and tooltip text from desktop tray snapshot."""
    primary = snapshot.get("primary_status") or "待命就绪"
    detail = snapshot.get("detail_summary") or "● 活跃探测 待命"
    tooltip = snapshot.get("tooltip") or f"Clash 测速工作台 · {primary}"
    if len(tooltip) > 120:
        tooltip = tooltip[:119] + "…"
    visible = bool(snapshot.get("window_visible", True))
    minimize_to_tray = bool(snapshot.get("minimize_to_tray", True))
    running = bool(snapshot.get("running", False))
    schedule_armed = snapshot.get("schedule_minutes") is not None
    patrol_armed = bool(snapshot.get("patrol_armed", False))

    items = [
        {"id": None, "label": f"状态：{primary}", "enabled": False, "checked": False, "separator": False},
        {"id": None, "label": detail, "enabled": False, "checked": False, "separator": False},
        {"id": None, "label": "", "enabled": False, "checked": False, "separator": True},
        {
            "id": CMD_SHOW_HIDE,
            "label": "隐藏到托盘" if visible else "显示主窗口",
            "enabled": True,
            "checked": False,
            "separator": False,
        },
        {
            "id": CMD_TOGGLE_MINIMIZE,
            "label": "关闭窗口时最小化到托盘",
            "enabled": True,
            "checked": minimize_to_tray,
            "separator": False,
        },
        {"id": None, "label": "", "enabled": False, "checked": False, "separator": True},
        {
            "id": CMD_STOP_RUN,
            "label": "停止当前测速",
            "enabled": running,
            "checked": False,
            "separator": False,
        },
        {
            "id": CMD_STOP_SCHEDULE,
            "label": "停止定时测速",
            "enabled": schedule_armed,
            "checked": False,
            "separator": False,
        },
        {
            "id": CMD_STOP_PATROL,
            "label": "停止智能巡检",
            "enabled": patrol_armed,
            "checked": False,
            "separator": False,
        },
        {"id": None, "label": "", "enabled": False, "checked": False, "separator": True},
        {"id": CMD_EXIT, "label": "退出应用", "enabled": True, "checked": False, "separator": False},
    ]
    return {"tooltip": tooltip, "items": items}


def instance_key_for_root(profile_root=None) -> str:
    if profile_root is None or str(profile_root).strip() == "":
        return "default"
    norm = os.path.abspath(str(profile_root)).lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


if os.name == "nt":
    LRESULT = ctypes.c_ssize_t
    HCURSOR = wintypes.HANDLE
    HBRUSH = wintypes.HANDLE

    ERROR_ALREADY_EXISTS = 183
    EVENT_MODIFY_STATE = 0x0002
    WAIT_OBJECT_0 = 0x00000000
    ASFW_ANY = 0xFFFFFFFF

    WS_POPUP = 0x80000000
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_NULL = 0x0000
    WM_APP = 0x8000
    WM_TRAYICON = WM_APP + 1
    WM_UPDATE_TIP = WM_APP + 2
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B

    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004

    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040
    SM_CXSMICON = 49
    SM_CYSMICON = 50
    IDI_APPLICATION = 32512

    MIIM_STATE = 0x00000001
    MIIM_ID = 0x00000002
    MIIM_FTYPE = 0x00000100
    MIIM_STRING = 0x00000040
    MFT_STRING = 0x00000000
    MFT_SEPARATOR = 0x00000800
    MFS_ENABLED = 0x00000000
    MFS_DISABLED = 0x00000003
    MFS_CHECKED = 0x00000008
    MFS_UNCHECKED = 0x00000000

    TPM_LEFTALIGN = 0x0000
    TPM_RIGHTBUTTON = 0x0002
    TPM_BOTTOMALIGN = 0x0020
    TPM_NONOTIFY = 0x0080
    TPM_RETURNCMD = 0x0100

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", ctypes.c_wchar * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", ctypes.c_wchar * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", ctypes.c_wchar * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", HCURSOR),
            ("hbrBackground", HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class MENUITEMINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("fMask", wintypes.UINT),
            ("fType", wintypes.UINT),
            ("fState", wintypes.UINT),
            ("wID", wintypes.UINT),
            ("hSubMenu", wintypes.HMENU),
            ("hbmpChecked", wintypes.HBITMAP),
            ("hbmpUnchecked", wintypes.HBITMAP),
            ("dwItemData", ctypes.c_size_t),
            ("dwTypeData", wintypes.LPWSTR),
            ("cch", wintypes.UINT),
            ("hbmpItem", wintypes.HBITMAP),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", wintypes.POINT),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.OpenEventW.restype = wintypes.HANDLE
    _kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    _kernel32.SetEvent.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
    _user32.RegisterWindowMessageW.restype = wintypes.UINT
    _user32.RegisterClassExW.argtypes = (ctypes.POINTER(WNDCLASSEXW),)
    _user32.RegisterClassExW.restype = wintypes.ATOM
    _user32.UnregisterClassW.argtypes = (wintypes.LPCWSTR, wintypes.HINSTANCE)
    _user32.UnregisterClassW.restype = wintypes.BOOL
    _user32.CreateWindowExW.argtypes = (
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    )
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.DefWindowProcW.restype = LRESULT
    _user32.DestroyWindow.argtypes = (wintypes.HWND,)
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.PostQuitMessage.argtypes = (ctypes.c_int,)
    _user32.PostQuitMessage.restype = None
    _user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = (ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = (ctypes.POINTER(MSG),)
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = (ctypes.POINTER(MSG),)
    _user32.DispatchMessageW.restype = LRESULT
    _user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _user32.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT)
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadIconW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR)
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.DestroyIcon.argtypes = (wintypes.HICON,)
    _user32.DestroyIcon.restype = wintypes.BOOL
    _user32.CreatePopupMenu.argtypes = ()
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.InsertMenuItemW.argtypes = (wintypes.HMENU, wintypes.UINT, wintypes.BOOL, ctypes.POINTER(MENUITEMINFOW))
    _user32.InsertMenuItemW.restype = wintypes.BOOL
    _user32.TrackPopupMenuEx.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID)
    _user32.TrackPopupMenuEx.restype = ctypes.c_int
    _user32.DestroyMenu.argtypes = (wintypes.HMENU,)
    _user32.DestroyMenu.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
    _user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.ShowWindow.restype = wintypes.BOOL

    _shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _shell32.ExtractIconExW.argtypes = (wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT)
    _shell32.ExtractIconExW.restype = wintypes.UINT


def _load_tray_icon(icon_path: Path | None):
    cx = max(16, _user32.GetSystemMetrics(SM_CXSMICON))
    cy = max(16, _user32.GetSystemMetrics(SM_CYSMICON))
    if icon_path is not None and Path(icon_path).is_file():
        handle = _user32.LoadImageW(None, str(icon_path), IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        if handle:
            return handle, True
    if getattr(sys, "frozen", False):
        small = wintypes.HICON()
        count = _shell32.ExtractIconExW(sys.executable, 0, None, ctypes.byref(small), 1)
        if count > 0 and small.value:
            return small.value, True
    fallback = _user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))
    return fallback, False


class SingleInstanceGuard:
    """Local session mutex + wake event scoped by profile root."""

    def __init__(self, profile_root=None):
        self.key = instance_key_for_root(profile_root)
        self.mutex_name = f"Local\\ClashSpeedHelper-Mutex-{self.key}"
        self.event_name = f"Local\\ClashSpeedHelper-Wake-{self.key}"
        self._mutex = None
        self._wake_event = None
        self._stop_event = threading.Event()
        self._thread = None

    def acquire(self, wake_wait_ms: int = 2000) -> bool:
        if os.name != "nt":
            return True
        h_mutex = _kernel32.CreateMutexW(None, False, self.mutex_name)
        err = ctypes.get_last_error()
        if not h_mutex:
            return True
        if err == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(h_mutex)
            _user32.AllowSetForegroundWindow(ASFW_ANY)
            steps = max(1, wake_wait_ms // 50)
            for _ in range(steps):
                h_ev = _kernel32.OpenEventW(EVENT_MODIFY_STATE, False, self.event_name)
                if h_ev:
                    _kernel32.SetEvent(h_ev)
                    _kernel32.CloseHandle(h_ev)
                    break
                threading.Event().wait(0.05)
            return False
        self._mutex = h_mutex
        self._wake_event = _kernel32.CreateEventW(None, False, False, self.event_name)
        return True

    def start_listener(self, on_wake):
        if os.name != "nt" or not self._wake_event or self._thread is not None:
            return
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.is_set():
                res = _kernel32.WaitForSingleObject(self._wake_event, 400)
                if self._stop_event.is_set():
                    break
                if res == WAIT_OBJECT_0:
                    try:
                        on_wake()
                    except Exception:
                        pass

        self._thread = threading.Thread(target=_loop, daemon=True, name="clash-single-instance-wake")
        self._thread.start()

    def release(self):
        self._stop_event.set()
        if os.name == "nt" and self._wake_event:
            try:
                _kernel32.SetEvent(self._wake_event)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        if os.name == "nt":
            if self._wake_event:
                _kernel32.CloseHandle(self._wake_event)
                self._wake_event = None
            if self._mutex:
                _kernel32.CloseHandle(self._mutex)
                self._mutex = None


class Win32TrayIcon:
    """Native Shell_NotifyIconW tray icon with dynamic menu and Explorer restart recovery."""

    def __init__(
        self,
        icon_path,
        get_state_cb,
        on_show,
        on_hide,
        on_toggle_minimize,
        on_stop_run,
        on_stop_schedule,
        on_stop_patrol,
        on_exit,
    ):
        self.icon_path = Path(icon_path) if icon_path else None
        self.get_state_cb = get_state_cb
        self.callbacks = {
            "show": on_show,
            "hide": on_hide,
            "toggle_minimize": on_toggle_minimize,
            "stop_run": on_stop_run,
            "stop_schedule": on_stop_schedule,
            "stop_patrol": on_stop_patrol,
            "exit": on_exit,
        }
        self._thread = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._hwnd = None
        self._hicon = None
        self._owns_hicon = False
        self._last_tooltip = ""
        self._nid_added = False
        self._wndproc = None
        self._class_name = None
        self._hinstance = None

    def start(self) -> bool:
        if os.name != "nt" or self._thread is not None:
            return False
        self._ready.clear()
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="clash-win32-tray")
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self._hwnd is not None

    def update_tooltip(self):
        if os.name != "nt" or self._stopped.is_set() or not self._hwnd:
            return
        _user32.PostMessageW(self._hwnd, WM_UPDATE_TIP, 0, 0)

    def stop(self):
        if self._stopped.is_set():
            return
        self._stopped.set()
        if os.name == "nt" and self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread is not None and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _make_nid(self, tooltip: str = "") -> "NOTIFYICONDATAW":
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon
        nid.szTip = tooltip[:120]
        return nid

    def _add_icon(self):
        if not self._hwnd:
            return
        model = build_tray_menu_model(self.get_state_cb())
        self._last_tooltip = model["tooltip"]
        nid = self._make_nid(self._last_tooltip)
        self._nid_added = bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))

    def _sync_tooltip(self):
        if not self._hwnd or not self._nid_added or self._stopped.is_set():
            return
        model = build_tray_menu_model(self.get_state_cb())
        tip = model["tooltip"]
        if tip == self._last_tooltip:
            return
        self._last_tooltip = tip
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_TIP
        nid.szTip = tip[:120]
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _delete_icon(self):
        if self._hwnd and self._nid_added:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._nid_added = False

    def _dispatch_command(self, cmd_id: int):
        snap = self.get_state_cb()
        if cmd_id == CMD_SHOW_HIDE:
            target = self.callbacks["hide"] if snap.get("window_visible", True) else self.callbacks["show"]
        elif cmd_id == CMD_TOGGLE_MINIMIZE:
            target = self.callbacks["toggle_minimize"]
        elif cmd_id == CMD_STOP_RUN:
            target = self.callbacks["stop_run"]
        elif cmd_id == CMD_STOP_SCHEDULE:
            target = self.callbacks["stop_schedule"]
        elif cmd_id == CMD_STOP_PATROL:
            target = self.callbacks["stop_patrol"]
        elif cmd_id == CMD_EXIT:
            target = self.callbacks["exit"]
        else:
            return
        threading.Thread(target=target, daemon=True, name=f"clash-tray-cmd-{cmd_id}").start()

    def _show_context_menu(self):
        model = build_tray_menu_model(self.get_state_cb())
        hmenu = _user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            for pos, entry in enumerate(model["items"]):
                mii = MENUITEMINFOW()
                mii.cbSize = ctypes.sizeof(MENUITEMINFOW)
                if entry["separator"]:
                    mii.fMask = MIIM_FTYPE
                    mii.fType = MFT_SEPARATOR
                else:
                    mii.fMask = MIIM_FTYPE | MIIM_STATE | MIIM_ID | MIIM_STRING
                    mii.fType = MFT_STRING
                    state = MFS_ENABLED if entry["enabled"] else MFS_DISABLED
                    if entry["checked"]:
                        state |= MFS_CHECKED
                    mii.fState = state
                    mii.wID = entry["id"] or (9000 + pos)
                    mii.dwTypeData = entry["label"]
                _user32.InsertMenuItemW(hmenu, pos, True, ctypes.byref(mii))

            pt = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            _user32.SetForegroundWindow(self._hwnd)
            cmd = _user32.TrackPopupMenuEx(
                hmenu,
                TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                pt.x,
                pt.y,
                self._hwnd,
                None,
            )
            _user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            if cmd:
                self._dispatch_command(cmd)
        finally:
            _user32.DestroyMenu(hmenu)

    def _run_loop(self):
        taskbar_created_msg = _user32.RegisterWindowMessageW("TaskbarCreated")
        self._hinstance = _kernel32.GetModuleHandleW(None)
        self._hicon, self._owns_hicon = _load_tray_icon(self.icon_path)
        self._class_name = f"ClashSpeedHelperTray_{os.getpid()}_{id(self)}"

        def _wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAYICON:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    threading.Thread(target=self.callbacks["show"], daemon=True).start()
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_context_menu()
                return 0
            if msg == WM_UPDATE_TIP:
                self._sync_tooltip()
                return 0
            if taskbar_created_msg and msg == taskbar_created_msg:
                self._add_icon()
                return 0
            if msg == WM_CLOSE:
                self._delete_icon()
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._delete_icon()
                _user32.PostQuitMessage(0)
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(_wnd_proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._hinstance
        wc.lpszClassName = self._class_name
        atom = _user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            self._ready.set()
            return
        self._hwnd = _user32.CreateWindowExW(
            0,
            self._class_name,
            "ClashSpeedHelperTray",
            WS_POPUP,
            0,
            0,
            0,
            0,
            None,
            None,
            self._hinstance,
            None,
        )
        if self._hwnd:
            self._add_icon()
        self._ready.set()
        if not self._hwnd:
            _user32.UnregisterClassW(self._class_name, self._hinstance)
            return
        try:
            msg = MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._delete_icon()
            if self._owns_hicon and self._hicon:
                _user32.DestroyIcon(self._hicon)
                self._hicon = None
            _user32.UnregisterClassW(self._class_name, self._hinstance)
            self._hwnd = None
