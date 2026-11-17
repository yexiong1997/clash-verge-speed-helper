"""Windows pywebview entry point."""

from pathlib import Path
import os
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webview
from Windows.desktop import Desktop


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
    def export_tsv(self): return self._desktop.export_tsv()
    def copy_node_name(self, value): return self._desktop.copy_node_name(value)


def main():
    frozen = getattr(sys, "frozen", False)
    base = Path(sys._MEIPASS) if frozen else Path(__file__).resolve().parent.parent
    worker = Path(os.environ.get("CLASH_SPEED_HELPER_WORKER", "")) if os.environ.get("CLASH_SPEED_HELPER_WORKER") else (
        Path(sys.executable).parent / "clash-speedtest.exe" if frozen
        else base / "build" / "verified-worker" / "clash-speedtest.exe")
    if "--smoke-test" in sys.argv:
        return 0 if worker.is_file() and (base / "Windows" / "ui" / "index.html").is_file() else 1
    root = None
    if "--profile-root" in sys.argv:
        index = sys.argv.index("--profile-root")
        if index + 1 >= len(sys.argv):
            return 2
        root = sys.argv[index + 1]
    app = Desktop(worker, root=root)
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    window = webview.create_window("Clash 测速工作台", str(base / "Windows" / "ui" / "index.html"),
                                   js_api=PublicApi(app), width=1240, height=800, min_size=(1080, 700),
                                   background_color="#eef2f9")
    app.window = window
    window.events.closing += app.close
    webview.start(gui="edgechromium", debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
