"""Dedupe Studio desktop app.

Runs dedupe_studio.py (unchanged) in the background and shows its UI in a
native window instead of a browser tab. Finds the UI address by catching the
studio's webbrowser.open() call, or a localhost URL it prints. Set
DEDUPE_STUDIO_URL to force an address.
"""
import io
import json
import os
import re
import runpy
import sys
import threading
import time
import urllib.request
import webbrowser

_real_open = webbrowser.open


def _unblock_self():
    """Windows marks files from a downloaded zip as 'from the internet' (a hidden
    Zone.Identifier stream). .NET then refuses to load pythonnet's Python.Runtime.dll
    and the window never opens. Strip the mark from our own files before loading it."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    base = os.path.dirname(os.path.abspath(sys.executable))
    for d, _, files in os.walk(base):
        for f in files:
            try:
                os.remove(os.path.join(d, f) + ":Zone.Identifier")
            except OSError:
                pass


_unblock_self()

import webview  # noqa: E402  (after the unblock on purpose)

if os.environ.get("__PYINSTALLER_ANALYSIS_ONLY__"):
    import dedupe_studio  # noqa: F401  (never runs; lets PyInstaller bundle its imports)

URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?[^\s'\"<>]*")
_found = threading.Event()
_url = {}


def _resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _capture(url):
    if not _found.is_set():
        _url["value"] = url.replace("0.0.0.0", "127.0.0.1")
        _found.set()


def _fake_open(url, *args, **kwargs):
    _capture(url)
    return True


class _Watch(io.TextIOBase):
    """Passes output through and watches it for a localhost URL."""

    def __init__(self, inner):
        self.inner = inner

    def write(self, s):
        m = URL_RE.search(s)
        if m:
            _capture(m.group(0))
        if self.inner is not None:
            try:
                self.inner.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        if self.inner is not None:
            try:
                self.inner.flush()
            except Exception:
                pass


def _run_studio():
    script = _resource("dedupe_studio.py")
    sys.argv = [script]
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit:
        pass
    except Exception as exc:  # show the error instead of a blank window
        _url["error"] = repr(exc)
        _found.set()


def _wait_until_up(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _bind_drop(win):
    """Drag-and-drop: a browser page never sees full paths, but pywebview's drop event
    does (pywebviewFullPath). A dropped file adds its folder."""
    try:
        from webview.dom import DOMEventHandler
    except Exception:  # noqa: BLE001
        return

    def on_drop(e):
        for f in ((e or {}).get("dataTransfer") or {}).get("files") or []:
            p = f.get("pywebviewFullPath")
            if not p:
                continue
            if not os.path.isdir(p):
                p = os.path.dirname(p)
            win.evaluate_js("addRoot(%s)" % json.dumps(p))

    def on_loaded():
        try:
            win.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:  # noqa: BLE001
            pass

    win.events.loaded += on_loaded


def main():
    forced = os.environ.get("DEDUPE_STUDIO_URL")
    if forced:
        _capture(forced)
    webbrowser.open = webbrowser.open_new = webbrowser.open_new_tab = _fake_open
    sys.stdout = _Watch(sys.stdout)
    sys.stderr = _Watch(sys.stderr)

    threading.Thread(target=_run_studio, daemon=True).start()

    if not _found.wait(30) or "error" in _url:
        msg = _url.get("error", "Dedupe Studio did not report its address within 30 s.")
        webview.create_window("Dedupe Studio - error", html=f"<pre>{msg}</pre>")
    else:
        url = _url["value"]
        _wait_until_up(url)
        win = webview.create_window("Dedupe Studio", url, width=1280, height=820, min_size=(900, 600))
        _bind_drop(win)
    try:
        webview.start()
    except Exception:  # noqa: BLE001
        # The desktop window couldn't start on this PC (.NET / WebView2 problem).
        # Fall back to the default browser so the app still works.
        url = _url.get("value")
        if url:
            _real_open(url)
            threading.Event().wait()  # keep the studio running until the console is closed
    os._exit(0)  # stop the studio server when the window closes


if __name__ == "__main__":
    main()
