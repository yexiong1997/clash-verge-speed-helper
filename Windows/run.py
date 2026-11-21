"""Windows pywebview entry point."""

from pathlib import Path
import ctypes
import os
import sys
import threading

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Windows.desktop import Desktop
from Windows.tray import SingleInstanceGuard, Win32TrayIcon


class PublicApi:
    """Expose only intended bridge calls, never the desktop object or window."""
    def __init__(self, desktop):
        self._desktop = desktop

    def get_state(self): return self._desktop.get_state()
    def refresh(self): return self._desktop.refresh()
    def set_selection(self, value): return self._desktop.set_selection(value)
    def start(self, value): return self._desktop.start(value)
    def retest_node(self, value): return self._desktop.retest_node(value)
    def start_funnel(self, value): return self._desktop.start_funnel(value)
    def stop(self): return self._desktop.stop()
    def refresh_groups(self): return self._desktop.refresh_groups()
    def switch_manual(self, value): return self._desktop.switch_manual(value)
    def undo_switch(self): return self._desktop.undo_switch()
    def continue_pending(self, value): return self._desktop.continue_pending(value)
    def set_schedule(self, value): return self._desktop.set_schedule(value)
    def set_patrol(self, value): return self._desktop.set_patrol(value)
    def get_history(self, value=None): return self._desktop.get_history(value)
    def clear_history(self): return self._desktop.clear_history()
    def export_tsv(self): return self._desktop.export_tsv()
    def copy_node_name(self, value): return self._desktop.copy_node_name(value)
    def hide_to_tray(self): return self._desktop.hide_to_tray()
    def set_minimize_to_tray(self, value): return self._desktop.set_minimize_to_tray(value)


def resolve_worker(base=None, frozen=None):
    override = os.environ.get("CLASH_SPEED_HELPER_WORKER", "").strip()
    if override:
        return Path(override)
    if frozen is None:
        frozen = getattr(sys, "frozen", False)
    if frozen:
        return Path(sys.executable).parent / "clash-speedtest.exe"
    if base is None:
        base = Path(__file__).resolve().parent.parent
    primary = base / "build" / "verified-worker" / "clash-speedtest.exe"
    if primary.is_file():
        return primary
    localappdata = os.environ.get("LOCALAPPDATA", "").strip()
    if localappdata:
        fallback = Path(localappdata) / "ClashSpeedHelper" / "verified-worker" / "clash-speedtest.exe"
        if fallback.is_file():
            return fallback
    return primary


def _bring_window_to_front(window):
    if window is None:
        return
    try:
        window.show()
    except Exception:
        return
    try:
        from System import Func, Type
        import System.Windows.Forms as WinForms
        from webview.platforms.winforms import BrowserView
        view = BrowserView.instances.get(window.uid)
        if view is not None:
            def _activate():
                if view.WindowState == WinForms.FormWindowState.Minimized:
                    view.WindowState = WinForms.FormWindowState.Normal
                view.Show()
                view.Activate()
                hwnd = view.Handle.ToInt64()
                if hwnd:
                    user32 = ctypes.windll.user32
                    user32.ShowWindow(ctypes.c_void_p(hwnd), 5)  # SW_SHOW
                    if not user32.SetForegroundWindow(ctypes.c_void_p(hwnd)):
                        was_top = bool(view.TopMost)
                        view.TopMost = True
                        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
                        view.TopMost = was_top
                        view.Activate()
            if view.InvokeRequired:
                view.Invoke(Func[Type](_activate))
            else:
                _activate()
    except Exception:
        try:
            window.restore()
        except Exception:
            pass


def main():
    frozen = getattr(sys, "frozen", False)
    base = Path(sys._MEIPASS) if frozen else Path(__file__).resolve().parent.parent
    worker = resolve_worker(base=base, frozen=frozen)
    icon_path = base / "Windows" / "ui" / "app.ico"
    if "--smoke-test" in sys.argv:
        return 0 if worker.is_file() and (base / "Windows" / "ui" / "index.html").is_file() and icon_path.is_file() else 1
    root = None
    if "--profile-root" in sys.argv:
        index = sys.argv.index("--profile-root")
        if index + 1 >= len(sys.argv):
            return 2
        root = sys.argv[index + 1]
    instance_guard = SingleInstanceGuard(profile_root=root)
    if not instance_guard.acquire():
        return 0
    import webview
    app = Desktop(worker, root=root)
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    window = webview.create_window("Clash 测速工作台", str(base / "Windows" / "ui" / "index.html"),
                                   js_api=PublicApi(app), width=1240, height=800, min_size=(1080, 700),
                                   background_color="#eef2f9")
    app.window = window
    app._window_show_cb = lambda: threading.Thread(target=_bring_window_to_front, args=(window,), daemon=True).start()
    app._window_hide_cb = lambda: threading.Thread(target=window.hide, daemon=True).start()
    app._window_destroy_cb = lambda: threading.Thread(target=window.destroy, daemon=True).start()
    tray = Win32TrayIcon(
        icon_path=icon_path,
        get_state_cb=app.get_tray_snapshot,
        on_show=app.show_from_tray,
        on_hide=app.hide_to_tray,
        on_toggle_minimize=lambda: app.set_minimize_to_tray(not app.minimize_to_tray),
        on_stop_run=app.stop,
        on_stop_schedule=lambda: app.set_schedule({"minutes": 0}),
        on_stop_patrol=lambda: app.set_patrol({"enabled": False}),
        on_exit=app.request_exit,
    )
    app.tray_icon = tray
    app._tray_notify_cb = tray.update_tooltip
    tray.start()
    instance_guard.start_listener(app.show_from_tray)
    window.events.closing += app.handle_window_closing
    try:
        webview.start(gui="edgechromium", debug=False)
    finally:
        app.close()
        tray.stop()
        instance_guard.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
