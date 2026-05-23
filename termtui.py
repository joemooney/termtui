#!/usr/bin/python3
"""TUI for browsing terminal processes and focusing their windows."""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

TAGS_FILE = Path.home() / ".config" / "termtui_tags.json"
WS_NAMES_FILE = Path.home() / ".config" / "termtui_ws_names.json"
WS_STATE_FILE = Path.home() / ".config" / "termtui_ws.txt"  # read by GNOME extension
SETTINGS_FILE = Path.home() / ".config" / "termtui_settings.json"

DEFAULTS: dict = {
    "preview_secs": 1.5,
}


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text())
        return {**DEFAULTS, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def _save_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def _load_tags() -> dict[str, str]:
    try:
        return json.loads(TAGS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tags(tags: dict[str, str]) -> None:
    TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TAGS_FILE.write_text(json.dumps(tags, indent=2))


def _load_ws_names() -> dict[str, str]:
    try:
        return json.loads(WS_NAMES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_ws_names(names: dict[str, str]) -> None:
    WS_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    WS_NAMES_FILE.write_text(json.dumps(names, indent=2))

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, OptionList, Static
from textual.widgets.option_list import Option


TERMINAL_PROCS = {"bash", "zsh", "fish", "sh", "gnome-terminal-server",
                  "xterm", "kitty", "alacritty", "tilix", "konsole", "terminator",
                  "x-terminal-emul"}  # kernel truncates comm to 15 chars

SHELL_PROCS = {"bash", "zsh", "fish", "sh"}

REQUIRED_TOOLS = {
    "wmctrl":   "sudo apt install wmctrl",
    "xdotool":  "sudo apt install xdotool",
    "xwininfo": "sudo apt install x11-utils",
}


def _tag_key(comm: str, cwd: str) -> str:
    """Stable key for tagging: comm + shell cwd."""
    return f"{comm}:{cwd}"


def _wid_to_desktop() -> dict[str, str]:
    """Return mapping of window ID -> desktop number from wmctrl."""
    result = subprocess.run(["wmctrl", "-lp"], capture_output=True, text=True)
    mapping = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 2:
            mapping[parts[0]] = str(int(parts[1]) + 1)  # wid -> 1-based desktop number
    return mapping


def check_dependencies() -> list[str]:
    """Return install instructions for any missing required tools."""
    missing = []
    for tool, install_cmd in REQUIRED_TOOLS.items():
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            missing.append(f"  {tool}: {install_cmd}")
    return missing


def _read_cmdline(pid: str) -> str:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return " ".join(
        part.decode("utf-8", errors="replace")
        for part in raw.split(b"\x00") if part
    )


def _build_child_map() -> dict[str, list[str]]:
    """Return a mapping of ppid -> [child pids] for all readable /proc entries."""
    children: dict[str, list[str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            after_paren = stat.split(") ", 1)[-1]
            parts = after_paren.split()
            ppid = parts[1] if len(parts) > 1 else "0"
            children.setdefault(ppid, []).append(entry.name)
        except (PermissionError, FileNotFoundError, OSError):
            continue
    return children


def _tpgid(pid: str) -> str:
    """Return the foreground process group ID for the terminal this pid is attached to."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        after_paren = stat.split(") ", 1)[-1]
        parts = after_paren.split()
        # stat fields after state: ppid pgrp session tty_nr tpgid
        return parts[4] if len(parts) > 4 else ""
    except (FileNotFoundError, OSError):
        return ""


def _leaf_proc(pid: str, child_map: dict[str, list[str]]) -> str:
    """Walk down the process tree to find the active foreground process.

    With a single child, follow it. With multiple children, pick the one
    whose process group matches the terminal foreground group (tpgid).
    """
    visited = set()
    while pid not in visited:
        visited.add(pid)
        kids = child_map.get(pid, [])
        if not kids:
            break
        if len(kids) == 1:
            pid = kids[0]
        else:
            # Pick the child whose pgrp matches the foreground pgrp (tpgid)
            fg = _tpgid(pid)
            fg_child = next(
                (k for k in kids if _tpgid(k) == fg and k != pid),
                None,
            )
            pid = fg_child if fg_child else kids[-1]
    return pid


def get_terminal_procs() -> list[dict]:
    """Scan /proc for shell processes and resolve the active leaf child.

    WS/wid are left as '?' and resolved lazily via resolve_wids().
    """
    child_map = _build_child_map()
    procs = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = entry.name
        try:
            comm = (entry / "comm").read_text().strip()
            if comm not in SHELL_PROCS:
                continue
            stat = (entry / "stat").read_text()
            after_paren = stat.split(") ", 1)[-1]
            parts = after_paren.split()
            ppid = parts[1] if len(parts) > 1 else "0"

            # Walk up the tree to confirm this shell is descended from a terminal emulator
            check = ppid
            has_terminal_ancestor = False
            for _ in range(8):
                try:
                    anc_comm = Path(f"/proc/{check}/comm").read_text().strip()
                except (FileNotFoundError, OSError):
                    break
                if anc_comm in TERMINAL_PROCS:
                    has_terminal_ancestor = True
                    break
                try:
                    anc_stat = Path(f"/proc/{check}/stat").read_text()
                    anc_after = anc_stat.split(") ", 1)[-1]
                    anc_parts = anc_after.split()
                    check = anc_parts[1] if len(anc_parts) > 1 else "0"
                    if check in ("0", "1"):
                        break
                except (FileNotFoundError, OSError):
                    break
            if not has_terminal_ancestor:
                continue

            cwd = os.readlink(entry / "cwd")

            # Find the active leaf process in this shell's subtree
            leaf_pid = _leaf_proc(pid, child_map)
            leaf_comm = Path(f"/proc/{leaf_pid}/comm").read_text().strip()
            leaf_cwd = os.readlink(f"/proc/{leaf_pid}/cwd")
            leaf_cmdline = _read_cmdline(leaf_pid) or leaf_comm

            procs.append({
                "pid": pid,
                "leaf_pid": leaf_pid,
                "comm": leaf_comm,
                "cwd": leaf_cwd,
                "shell_cwd": cwd,
                "ppid": ppid,
                "cmdline": leaf_cmdline,
                "desktop": "?",   # resolved lazily
                "tag_key": _tag_key(comm, cwd),
                "wid": "",        # resolved lazily
            })
        except (PermissionError, FileNotFoundError, OSError):
            continue
    return sorted(procs, key=lambda p: int(p["pid"]))


def resolve_wids(procs: list[dict],
                 progress_cb=None, progress_every: int = 5) -> None:
    """Resolve wid and desktop for each proc in-place (slow — run in background thread).

    Re-fetches the wid->desktop map after every sentinel round-trip so that the
    desktop is never stale (the old code captured the snapshot once up-front,
    before the sentinel title change had propagated to wmctrl).

    If progress_cb is provided, it is called every progress_every resolved procs
    so the UI can update incrementally rather than waiting for the full batch.
    """
    for i, p in enumerate(procs):
        try:
            wid = _find_wid(p["pid"], p["shell_cwd"])
            p["wid"] = wid or ""
            if wid:
                # Fresh snapshot so the sentinel's title change is captured
                p["desktop"] = _wid_to_desktop().get(wid, "?")
            else:
                p["desktop"] = "?"
        except Exception:
            p["wid"] = ""
            p["desktop"] = "?"
        if progress_cb and (i + 1) % progress_every == 0:
            try:
                progress_cb()
            except Exception:
                pass
    # Always call once at the end for any remainder
    if progress_cb:
        try:
            progress_cb()
        except Exception:
            pass


def _get_window_title(wid: str) -> str:
    result = subprocess.run(["wmctrl", "-lp"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if parts and parts[0] == wid:
            return parts[4] if len(parts) > 4 else ""
    return ""


HIGHLIGHT_BORDER_PX: int = 20
HIGHLIGHT_SECS: float = 5.0


import queue as _queue

# ---------------------------------------------------------------------------
# Single-thread tkinter border manager
# ---------------------------------------------------------------------------
# All tkinter calls MUST happen on one thread. We keep a hidden root alive
# permanently and process "show border" requests via an after()-polled queue.

_tk_queue: "_queue.Queue[dict]" = _queue.Queue()
_tk_thread_started = False
_tk_thread_lock = threading.Lock()


def _tk_worker() -> None:
    """Long-lived daemon thread that owns all tkinter windows."""
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()  # hidden master window keeps the mainloop alive

    _strips: list[tk.Tk] = []
    _cancel_after_id: list = [None]  # mutable cell for scheduled destroy id

    def _destroy_strips():
        nonlocal _strips
        if _cancel_after_id[0] is not None:
            try:
                root.after_cancel(_cancel_after_id[0])
            except Exception:
                pass
            _cancel_after_id[0] = None
        for s in _strips:
            try:
                s.destroy()
            except Exception:
                pass
        _strips = []

    def _show(x, y, w, h, border, flash_ms):
        _destroy_strips()
        strips_geo = [
            (x,               y,               w,      border),
            (x,               y + h - border,  w,      border),
            (x,               y,               border, h),
            (x + w - border,  y,               border, h),
        ]
        for sx, sy, sw, sh in strips_geo:
            s = tk.Toplevel(root)
            s.overrideredirect(True)
            s.attributes("-topmost", True)
            s.geometry(f"{sw}x{sh}+{sx}+{sy}")
            s.configure(bg="#FFD700")
            _strips.append(s)
        _cancel_after_id[0] = root.after(flash_ms, _destroy_strips)

    _osd_win: list = [None]  # mutable cell for current OSD window
    _osd_after: list = [None]

    def _destroy_osd():
        if _osd_after[0] is not None:
            try:
                root.after_cancel(_osd_after[0])
            except Exception:
                pass
            _osd_after[0] = None
        if _osd_win[0] is not None:
            try:
                _osd_win[0].destroy()
            except Exception:
                pass
            _osd_win[0] = None

    def _show_osd(text, duration_ms):
        _destroy_osd()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.88)
        except Exception:
            pass
        win.configure(bg="#1a1a1a")
        lbl = tk.Label(
            win, text=text,
            font=("Sans", 64, "bold"),
            fg="#FFD700", bg="#1a1a1a",
            padx=48, pady=28,
        )
        lbl.pack()
        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")
        _osd_win[0] = win
        _osd_after[0] = root.after(duration_ms, _destroy_osd)

    def _poll_queue():
        try:
            while True:
                msg = _tk_queue.get_nowait()
                if msg.get("cmd") == "show":
                    _show(msg["x"], msg["y"], msg["w"], msg["h"],
                          msg["border"], msg["flash_ms"])
                elif msg.get("cmd") == "cancel":
                    _destroy_strips()
                elif msg.get("cmd") == "osd":
                    _show_osd(msg["text"], msg.get("duration_ms", 1500))
        except _queue.Empty:
            pass
        root.after(30, _poll_queue)

    root.after(30, _poll_queue)
    root.mainloop()


def _ensure_tk_thread() -> None:
    global _tk_thread_started
    with _tk_thread_lock:
        if not _tk_thread_started:
            t = threading.Thread(target=_tk_worker, daemon=True)
            t.start()
            _tk_thread_started = True


_ATSPI_SCRIPT = r"""
import sys, os, time
import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi
Atspi.init()

pid, pts, sentinel = sys.argv[1], sys.argv[2], sys.argv[3]

def find_terminal_in(node, depth=0):
    if depth > 6 or node is None: return None
    try:
        if node.get_role().value_nick == 'terminal':
            return node
        for i in range(node.get_child_count()):
            r = find_terminal_in(node.get_child_at_index(i), depth+1)
            if r: return r
    except: pass
    return None

def find_label(node, sentinel, depth=0):
    if depth > 22 or node is None: return None
    try:
        name = node.get_name() or ''
        if node.get_role().value_nick == 'label' and sentinel in name:
            return node
        for i in range(node.get_child_count()):
            r = find_label(node.get_child_at_index(i), sentinel, depth+1)
            if r: return r
    except: pass
    return None

desktop = Atspi.get_desktop(0)
for i in range(desktop.get_child_count()):
    app = desktop.get_child_at_index(i)
    if not app: continue
    lbl = find_label(app, sentinel)
    if not lbl: continue
    node = lbl
    for _ in range(15):
        parent = node.get_parent()
        if not parent: break
        for j in range(parent.get_child_count()):
            sib = parent.get_child_at_index(j)
            if sib is node: continue
            term = find_terminal_in(sib)
            if term:
                comp = term.get_component_iface()
                ext = comp.get_extents(Atspi.CoordType.SCREEN)
                if ext.width > 0 and ext.height > 0:
                    print(f'{ext.x},{ext.y},{ext.width},{ext.height}')
                    sys.exit(0)
        node = parent
"""


def _find_vte_bbox(pid: str, shell_cwd: str) -> tuple[int, int, int, int] | None:
    """Use AT-SPI to find the exact screen bbox of the VTE pane for this shell."""
    pts = _pts_of(pid)
    if not pts or "(deleted)" in pts:
        return None
    sentinel = f"__TERMTUI_{pid}__"
    original = shell_cwd.replace(str(Path.home()), "~") if shell_cwd else ""
    try:
        with open(pts, "wb") as tty:
            tty.write(f"\033]2;{sentinel}\007".encode())
        time.sleep(0.2)
        r = subprocess.run(
            ["/usr/bin/python3", "-c", _ATSPI_SCRIPT, pid, pts, sentinel],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split(",")
            return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
    except Exception:
        pass
    finally:
        try:
            with open(pts, "wb") as tty:
                tty.write(f"\033]2;{original}\007".encode())
        except OSError:
            pass
    return None


def _window_bbox(wid_dec: str) -> tuple[int, int, int, int]:
    """Return (x, y, w, h) for the given X11 window."""
    geo = subprocess.run(
        ["xwininfo", "-id", wid_dec], capture_output=True, text=True
    ).stdout
    x, y, w, h = 0, 0, 800, 600
    for line in geo.splitlines():
        line = line.strip()
        if line.startswith("Absolute upper-left X:"):
            x = int(line.split(":")[1].strip())
        elif line.startswith("Absolute upper-left Y:"):
            y = int(line.split(":")[1].strip())
        elif line.startswith("Width:"):
            w = int(line.split(":")[1].strip())
        elif line.startswith("Height:"):
            h = int(line.split(":")[1].strip())
    return x, y, w, h


def _highlight_window(wid: str, wid_dec: str,
                      flash_secs: float = HIGHLIGHT_SECS,
                      border: int = HIGHLIGHT_BORDER_PX,
                      cancel: threading.Event | None = None,
                      pid: str = "",
                      shell_cwd: str = "") -> None:
    """Show a gold border outline around the target window (thread-safe).

    When pid is provided, attempts AT-SPI pane detection first so that
    the border targets just the Terminator split-pane, not the whole window.
    """
    _ensure_tk_thread()

    if cancel and cancel.is_set():
        return

    bbox = None
    if pid:
        try:
            bbox = _find_vte_bbox(pid, shell_cwd)
        except Exception:
            pass

    if cancel and cancel.is_set():
        return

    x, y, w, h = bbox if bbox else _window_bbox(wid_dec)

    if cancel and cancel.is_set():
        return

    _tk_queue.put({
        "cmd": "show",
        "x": x, "y": y, "w": w, "h": h,
        "border": border,
        "flash_ms": int(flash_secs * 1000),
    })


def _read_environ(pid: str) -> dict[str, str]:
    """Read environment variables from /proc/<pid>/environ."""
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
        env: dict[str, str] = {}
        for item in data.split(b"\x00"):
            if b"=" in item:
                key, _, val = item.partition(b"=")
                env[key.decode(errors="replace")] = val.decode(errors="replace")
        return env
    except (PermissionError, FileNotFoundError, OSError):
        return {}


def _pts_of(pid: str) -> str | None:
    """Return the /dev/pts/N path for the given pid's controlling tty."""
    # Try fd/0,1,2 in order — shells may redirect stdin
    for fd in ("0", "1", "2"):
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
            if target.startswith("/dev/pts/"):
                return target
        except OSError:
            continue
    return None


def _terminal_ancestor_pid(pid: str) -> str | None:
    """Walk the process tree upward from pid; return PID of the first terminal-emulator ancestor.

    Excludes shells (SHELL_PROCS) — we want the emulator process itself, e.g. terminator.
    """
    emulator_procs = TERMINAL_PROCS - SHELL_PROCS
    check = pid
    for _ in range(12):
        try:
            comm = Path(f"/proc/{check}/comm").read_text().strip()
            if comm in emulator_procs:
                return check
            stat = Path(f"/proc/{check}/stat").read_text()
            after = stat.split(") ", 1)[-1]
            parts = after.split()
            check = parts[1] if len(parts) > 1 else "0"
            if check in ("0", "1"):
                break
        except (FileNotFoundError, OSError):
            break
    return None


def _find_wid(pid: str, shell_cwd: str | None = None) -> str | None:
    """Find the wmctrl window ID for the given shell pid.

    Strategy:
    1. Walk up the process tree matching by PID (works when terminal
       emulator reports per-window PIDs).
    2. Write a unique sentinel title to the shell's pts, then scan
       wmctrl output to find which window picked it up, then restore.
    """
    result = subprocess.run(["wmctrl", "-lp"], capture_output=True, text=True)
    lines = result.stdout.splitlines()

    # pid -> list of wids (tabs share one emulator pid)
    pid_to_wids: dict[str, list[str]] = {}
    all_windows: list[tuple[str, str, str]] = []
    for line in lines:
        parts = line.split(None, 4)
        if len(parts) >= 3:
            wid, wpid = parts[0], parts[2]
            title = parts[4] if len(parts) > 4 else ""
            pid_to_wids.setdefault(wpid, []).append(wid)
            all_windows.append((wid, wpid, title))

    # Strategy 1: walk up process tree by PID — only use if pid maps to exactly one window
    check = pid
    for _ in range(6):
        if check in pid_to_wids:
            wids = pid_to_wids[check]
            if len(wids) == 1:
                return wids[0]
            break  # multiple windows share this pid — fall through to sentinel
        try:
            stat = Path(f"/proc/{check}/stat").read_text()
            after_paren = stat.split(") ", 1)[-1]
            parts = after_paren.split()
            check = parts[1] if len(parts) > 1 else "0"
            if check in ("0", "1"):
                break
        except (FileNotFoundError, OSError):
            break

    # Strategy 2: write a unique sentinel title to the pts, find the window, restore.
    # Uses xdotool search --name which checks _NET_WM_NAME — Terminator updates this
    # for inactive panes whereas wmctrl's WM_NAME scan misses them.
    pts = _pts_of(pid)
    if pts:
        sentinel = f"__TERMTUI_{pid}__"
        original_title = next(
            (title for wid, _, title in all_windows
             if shell_cwd and (shell_cwd.replace(str(Path.home()), "~") in title
                               or shell_cwd in title)),
            ""
        )
        found_wid: str | None = None
        try:
            with open(pts, "wb") as tty:
                tty.write(f"\033]2;{sentinel}\007".encode())
            time.sleep(0.8)
            xdt = subprocess.run(
                ["xdotool", "search", "--name", sentinel],
                capture_output=True, text=True,
            )
            desktop_map = _wid_to_desktop()
            for wid_dec_str in xdt.stdout.splitlines():
                wid_dec_str = wid_dec_str.strip()
                if not wid_dec_str:
                    continue
                try:
                    target_int = int(wid_dec_str)
                    # Direct match
                    for hex_wid in desktop_map:
                        if int(hex_wid, 16) == target_int:
                            found_wid = hex_wid
                            break
                    # If xdotool returned a sub-window not in wmctrl, walk up to parent
                    if not found_wid:
                        parent = subprocess.run(
                            ["xdotool", "getwindowgeometry", "--shell", wid_dec_str],
                            capture_output=True, text=True,
                        )
                        pw = subprocess.run(
                            ["xprop", "-id", wid_dec_str, "WM_TRANSIENT_FOR"],
                            capture_output=True, text=True,
                        )
                        # Try querying the X parent via xwininfo
                        xi = subprocess.run(
                            ["xwininfo", "-id", wid_dec_str],
                            capture_output=True, text=True,
                        )
                        for ln in xi.stdout.splitlines():
                            if "Parent window id:" in ln:
                                parts = ln.split()
                                for part in parts:
                                    if part.startswith("0x"):
                                        try:
                                            par_int = int(part, 16)
                                            for hex_wid in desktop_map:
                                                if int(hex_wid, 16) == par_int:
                                                    found_wid = hex_wid
                                                    break
                                        except ValueError:
                                            pass
                                        if found_wid:
                                            break
                                if found_wid:
                                    break
                except ValueError:
                    pass
                if found_wid:
                    break
        except OSError:
            pass
        # Always restore the pts title before returning
        try:
            with open(pts, "wb") as tty:
                tty.write(f"\033]2;{original_title}\007".encode())
        except OSError:
            pass
        if found_wid:
            return found_wid

    # Strategy 3: find the terminal emulator ancestor and locate its window(s) via
    # xdotool --pid.  Only works unambiguously when there is exactly one window for
    # that emulator (common case); skip if multiple windows exist to avoid returning
    # the wrong workspace.
    term_pid = _terminal_ancestor_pid(pid)
    if term_pid:
        xdt = subprocess.run(
            ["xdotool", "search", "--pid", term_pid],
            capture_output=True, text=True,
        )
        wids_dec = [w.strip() for w in xdt.stdout.splitlines() if w.strip()]
        if len(wids_dec) == 1:
            desktop_map = _wid_to_desktop()   # hex WID -> desktop string
            try:
                target_int = int(wids_dec[0])
                for hex_wid, _desktop in desktop_map.items():
                    if int(hex_wid, 16) == target_int:
                        return hex_wid
            except ValueError:
                pass

    return None


def _current_desktop() -> int:
    """Return the index of the currently active desktop."""
    result = subprocess.run(["wmctrl", "-d"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "*":
            return int(parts[0])
    return 0


def _raise_and_focus(wid: str, wid_dec: str, visual_hint: bool = True,
                     pid: str = "", shell_cwd: str = "") -> None:
    """Unminimize, raise, focus, click to activate keyboard, and optionally highlight."""
    has_xdotool = subprocess.run(["which", "xdotool"], capture_output=True).returncode == 0

    # Remove hidden/minimized state via EWMH before trying to raise
    subprocess.run(["wmctrl", "-ir", wid, "-b", "remove,hidden"], capture_output=True)

    if has_xdotool:
        # Get window centre for the click
        geo = subprocess.run(
            ["xdotool", "getwindowgeometry", wid_dec],
            capture_output=True, text=True
        ).stdout
        cx, cy = 400, 400
        for line in geo.splitlines():
            line = line.strip()
            if line.startswith("Geometry:"):
                w, h = (int(v) for v in line.split(":", 1)[1].strip().split("x"))
                cx, cy = w // 2, h // 2
                break

        subprocess.run([
            "xdotool",
            "windowactivate", "--sync", wid_dec,
            "windowraise", wid_dec,
            "windowfocus", "--sync", wid_dec,
            "mousemove", "--window", wid_dec, str(cx), str(cy),
            "click", "--window", wid_dec, "1",
        ], capture_output=True)
    else:
        subprocess.run(["wmctrl", "-ia", wid])

    if visual_hint:
        threading.Thread(
            target=_highlight_window,
            args=(wid, wid_dec),
            kwargs={"pid": pid, "shell_cwd": shell_cwd},
            daemon=True,
        ).start()


def do_focus(pid: str, shell_cwd: str = "", visual_hint: bool = True) -> str:
    if subprocess.run(["which", "wmctrl"], capture_output=True).returncode != 0:
        return "wmctrl not found — install with: sudo apt install wmctrl"
    wid = _find_wid(pid, shell_cwd)
    if wid is None:
        return f"No window found for PID {pid}"
    wid_dec = str(int(wid, 16))
    _raise_and_focus(wid, wid_dec, visual_hint, pid=pid, shell_cwd=shell_cwd)
    return f"Focused PID {pid}"


def do_move_here(pid: str, shell_cwd: str = "", visual_hint: bool = True) -> str:
    """Move the window to the current desktop, then focus it."""
    if subprocess.run(["which", "wmctrl"], capture_output=True).returncode != 0:
        return "wmctrl not found — install with: sudo apt install wmctrl"
    wid = _find_wid(pid, shell_cwd)
    if wid is None:
        return f"No window found for PID {pid}"
    desktop = _current_desktop()
    subprocess.run(["wmctrl", "-ir", wid, "-t", str(desktop)])
    wid_dec = str(int(wid, 16))
    _raise_and_focus(wid, wid_dec, visual_hint, pid=pid, shell_cwd=shell_cwd)
    return f"Moved PID {pid} to desktop {desktop} and focused"


# Menu actions: (label, id)
ACTIONS = [
    ("Focus window",        "focus"),
    ("Move here + focus",   "move_here"),
    ("Set tag",             "tag"),
    ("Clear tag",           "clear_tag"),
]


class TagScreen(ModalScreen[str]):
    """Modal input for setting a tag on a terminal."""

    BINDINGS = [Binding("escape", "dismiss('')", "Cancel", show=False)]

    CSS = """
    TagScreen {
        align: center middle;
    }
    #tag_box {
        width: 50;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #tag_label {
        height: 1;
        margin-bottom: 1;
    }
    """

    def __init__(self, current_tag: str = ""):
        super().__init__()
        self._current = current_tag

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="tag_box"):
            yield Label("Set tag (Enter to confirm, Esc to cancel):", id="tag_label")
            yield Input(value=self._current, id="tag_input")

    def on_mount(self) -> None:
        self.query_one("#tag_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


class DetailScreen(ModalScreen[None]):
    """Modal popup showing full details for a terminal process, with ↑↓ navigation."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Close",    show=False),
        Binding("space",  "dismiss(None)", "Close",    show=False),
        Binding("enter",  "dismiss(None)", "Close",    show=False),
        Binding("e",      "show_env",      "Env vars", show=True),
        Binding("up",     "navigate(-1)",  "Prev",     show=False),
        Binding("down",   "navigate(1)",   "Next",     show=False),
    ]

    CSS = """
    DetailScreen {
        align: center middle;
    }
    #detail_box {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 85vh;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    .detail_row {
        height: auto;
    }
    .detail_label {
        color: $text-muted;
        width: 12;
    }
    .detail_value {
        color: $text;
    }
    .detail_value_wrap {
        color: $text;
        width: 1fr;
    }
    """

    # Field definitions: (widget_id, label, proc_key_or_None, wrap)
    _FIELDS = [
        ("dv_pid",  "PID",     "pid",     False),
        ("dv_comm", "COMM",    "comm",    False),
        ("dv_ws",   "WS",      "desktop", False),
        ("dv_tag",  "TAG",     None,      False),
        ("dv_wid",  "WID",     "wid",     False),
        ("dv_cwd",  "CWD",     "cwd",     True),
        ("dv_cmd",  "COMMAND", "cmdline", True),
    ]

    def __init__(self, procs: list[dict], idx: int, tags: dict[str, str]):
        super().__init__()
        self._procs = procs
        self._idx   = idx
        self._tags  = tags

    # ── helpers ──────────────────────────────────────────────────────────────

    def _cur(self) -> dict:
        return self._procs[self._idx]

    def _field_value(self, fid: str, proc: dict) -> str:
        home = str(Path.home())
        if fid == "dv_tag":
            return self._tags.get(proc.get("tag_key", ""), "") or ""
        if fid == "dv_cwd":
            return proc.get("cwd", "").replace(home, "~", 1)
        entry = next((e for e in self._FIELDS if e[0] == fid), None)
        key = entry[2] if entry and entry[2] else None
        if not key:
            return ""
        return proc.get(key, "") or ""

    def _update_title(self) -> None:
        p = self._cur()
        n = len(self._procs)
        self.query_one("#detail_box").border_title = (
            f"Details — PID {p['pid']}  [{self._idx + 1}/{n}]"
            f"  (↑↓ navigate · E env · Esc close)"
        )

    # ── compose / mount ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical, Horizontal
        p = self._cur()
        with Vertical(id="detail_box"):
            for fid, lbl, _, wrap in self._FIELDS:
                with Horizontal(classes="detail_row"):
                    yield Label(f"{lbl}:", classes="detail_label")
                    css = "detail_value_wrap" if wrap else "detail_value"
                    yield Label(self._field_value(fid, p), id=fid,
                                classes=css, markup=False)

    def on_mount(self) -> None:
        self._update_title()

    # ── actions ──────────────────────────────────────────────────────────────

    def action_navigate(self, delta: int) -> None:
        new_idx = self._idx + delta
        if not (0 <= new_idx < len(self._procs)):
            return
        self._idx = new_idx
        p = self._cur()
        for fid, _, _, _ in self._FIELDS:
            self.query_one(f"#{fid}", Label).update(self._field_value(fid, p))
        self._update_title()

    def action_show_env(self) -> None:
        self.app.push_screen(EnvScreen(self._procs, self._idx, self._tags))




class EnvScreen(ModalScreen[None]):
    """Env-var viewer with n/p process navigation and / search.

    * = var present in leaf but not parent shell (e.g. from FOO=BAR cmd).
    """

    BINDINGS = [
        Binding("e",     "dismiss(None)",    "Close",  show=False),
        Binding("escape","close_or_dismiss", "Close",  show=False),
        Binding("n",     "navigate(1)",      "Next",   show=False),
        Binding("p",     "navigate(-1)",     "Prev",   show=False),
        Binding("slash", "open_search",      "Search", show=True),
    ]

    CSS = """
    EnvScreen { align: center middle; }
    #env_box {
        width: 95%;
        max-width: 180;
        height: 85vh;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    #env_search {
        height: 1;
        margin-bottom: 1;
        display: none;
    }
    #env_table { height: 1fr; }
    #env_proc_info {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, procs: list[dict], idx: int, tags: dict[str, str]):
        super().__init__()
        self._procs     = procs
        self._idx       = idx
        self._tags      = tags
        self._env_cache: dict[str, tuple[dict[str, str], dict[str, str]]] = {}

    # ── helpers ──────────────────────────────────────────────────────────────

    def _cur(self) -> dict:
        return self._procs[self._idx]

    def _get_envs(self, proc: dict) -> tuple[dict[str, str], dict[str, str]]:
        pid = proc["pid"]
        if pid not in self._env_cache:
            leaf_pid  = proc.get("leaf_pid") or pid
            leaf_env  = _read_environ(leaf_pid)
            shell_env = _read_environ(pid) if leaf_pid != pid else {}
            self._env_cache[pid] = (leaf_env, shell_env)
        return self._env_cache[pid]

    def _refresh(self, pattern: str = "") -> None:
        proc = self._cur()
        leaf_env, shell_env = self._get_envs(proc)

        table = self.query_one("#env_table", DataTable)
        table.clear()

        lp = pattern.lower()
        for key in sorted(leaf_env):
            val = leaf_env[key]
            display_key = f"* {key}" if key not in shell_env else key
            if lp and lp not in key.lower() and lp not in val.lower():
                continue
            table.add_row(display_key, val)

        new_count = sum(1 for k in leaf_env if k not in shell_env)
        note = f"  (* = {new_count} inline)" if new_count else ""
        n = len(self._procs)
        self.query_one("#env_box").border_title = (
            f"Env [{self._idx + 1}/{n}]{note}  (n/p nav · / search · E close)"
        )
        cwd = proc.get("cwd") or ""
        cmd = proc.get("cmdline") or proc.get("comm") or ""
        info = f"PID {proc['pid']}  {proc['comm']}  WS {proc.get('desktop','?')}  {cwd}  {cmd}"
        try:
            self.query_one("#env_proc_info").update(info)
        except Exception:
            pass

    # ── compose / mount ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="env_box"):
            yield Input(placeholder="/filter...", id="env_search")
            yield DataTable(id="env_table")
            yield Label("", id="env_proc_info")

    def on_mount(self) -> None:
        table = self.query_one("#env_table", DataTable)
        table.add_columns("Variable", "Value")
        table.cursor_type = "row"
        self._refresh()
        table.focus()

    # ── actions ──────────────────────────────────────────────────────────────

    def action_navigate(self, delta: int) -> None:
        new_idx = self._idx + delta
        if not (0 <= new_idx < len(self._procs)):
            return
        self._idx = new_idx
        self._refresh(self.query_one("#env_search", Input).value)
        self.query_one("#env_table", DataTable).focus()

    def action_open_search(self) -> None:
        search = self.query_one("#env_search", Input)
        search.display = True
        self.set_timer(0.05, search.focus)

    def action_close_or_dismiss(self) -> None:
        search = self.query_one("#env_search", Input)
        if search.display:
            search.value = ""
            search.display = False
            self.query_one("#env_table", DataTable).focus()
            self._refresh()
        else:
            self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "env_search":
            self._refresh(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "env_search":
            self.query_one("#env_table", DataTable).focus()


class SettingsScreen(ModalScreen[dict | None]):
    """Modal for editing user-configurable settings."""

    BINDINGS = [Binding("escape", "dismiss(None)", show=False)]

    CSS = """
    SettingsScreen { align: center middle; }
    #settings_box {
        width: 52; height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    .setting_row { height: auto; margin-bottom: 1; }
    .setting_label { height: 1; color: $text-muted; }
    """

    def __init__(self, settings: dict):
        super().__init__()
        self._settings = dict(settings)

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="settings_box"):
            yield Label("Preview duration (seconds):", classes="setting_label")
            yield Input(
                value=str(self._settings.get("preview_secs", 3.0)),
                id="preview_secs",
                classes="setting_row",
            )
            yield Label("Enter to save · Esc to cancel", classes="setting_label")

    def on_mount(self) -> None:
        self.query_one("#settings_box").border_title = "Settings"
        self.query_one("#preview_secs", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            self._settings["preview_secs"] = max(0.5, float(event.value))
        except ValueError:
            pass
        self.dismiss(self._settings)


class ActionMenu(ModalScreen[str]):
    """Modal popup menu for selecting an action on a process."""

    BINDINGS = [
        Binding("escape", "dismiss('')", "Cancel", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    CSS = """
    ActionMenu {
        align: center middle;
    }
    #menu {
        width: 36;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 0 1;
    }
    """

    def __init__(self, pid: str, cwd: str):
        super().__init__()
        self._pid = pid
        self._cwd = cwd

    def compose(self) -> ComposeResult:
        yield OptionList(
            *[Option(label, id=action_id) for label, action_id in ACTIONS],
            id="menu",
        )

    def on_mount(self) -> None:
        self.query_one(OptionList).border_title = f"PID {self._pid}  {self._cwd}"

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cursor_down(self) -> None:
        self.query_one(OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(OptionList).action_cursor_up()


class TermTUI(App):
    """Browse terminal processes and focus their windows."""

    CSS = """
    DataTable {
        height: 1fr;
    }
    #search {
        height: 1;
        display: none;
        border: none;
        padding: 0 1;
        background: $surface;
        color: $text;
    }
    #search:focus {
        border: none;
    }
    #status {
        height: 1;
        padding: 0 1;
        color: $success;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("S", "enter_col_mode", "Columns"),
        Binding("v", "toggle_hint", "Visual hint"),
        Binding("slash", "open_search", "Search"),
        Binding("space", "show_detail",     "Detail",   show=True),
        Binding("e",     "show_env_direct", "Env Vars", show=True),
        Binding("p", "preview", "Preview", show=True),
        Binding("P", "preview_long", "Preview 3x", show=True),
        Binding("N", "name_ws", "Name WS", show=True),
        Binding("ctrl+s", "settings", "Settings", show=True),
        Binding("escape", "close_search", "Clear search", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        *[Binding(f"m{n}", f"move_to_ws_{n}", f"→WS{n}", show=False) for n in range(10)],
    ]

    TITLE = "Terminal Window Finder"

    # field -> sort label (all possible sort fields)
    SORT_FIELDS = {
        "desktop": "WS", "pid": "PID", "comm": "COMM", "cwd": "DIR",
        "tag": "TAG", "cmdline": "CMD", "cpu": "CPU%", "mem": "MEM%",
    }

    # key -> (field, header label) — order determines display order
    COLUMNS = {
        "%": ("cpu",      "CPU%"),
        "m": ("mem",      "MEM%"),
        "w": ("desktop",  "WS"),
        "t": ("tag",      "TAG"),
        "p": ("pid",      "PID"),
        "C": ("comm",     "COMM"),
        "d": ("cwd",      "DIRECTORY"),
        "c": ("cmdline",  "COMMAND"),
    }

    STATS_REFRESH_SECS = 3
    WS_REFRESH_SECS = 0.5

    def __init__(self):
        super().__init__()
        self._all_procs: list[dict] = []
        self._visible: list[dict] = []     # current filtered+sorted display order
        self._visual_hint: bool = True
        self._tags: dict[str, str] = _load_tags()
        self._sort_field: str = "desktop"  # current sort field
        self._col_mode: bool = False
        self._stats_timer = None
        self._peek_cancel: threading.Event = threading.Event()
        self._current_ws: int = 0
        # Visible columns (ordered): field key -> visible
        self._visible_cols: dict[str, bool] = {
            "%": False, "m": False,
            "w": True, "t": True, "p": True, "C": True, "d": True, "c": True,
        }
        self._ws_names: dict[str, str] = _load_ws_names()
        self._settings: dict = _load_settings()

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable()
        yield Input(placeholder="/search...", id="search")
        yield Label("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        self._rebuild_columns()
        self._load_procs()
        self._update_ws_display()
        self.set_interval(self.WS_REFRESH_SECS, self._update_ws_display)
        self.set_interval(15.0, self._resolve_pending_wids)
        missing = check_dependencies()
        if missing:
            tools = ", ".join(REQUIRED_TOOLS.keys() - {
                t for t in REQUIRED_TOOLS
                if subprocess.run(["which", t], capture_output=True).returncode == 0
            })
            self.query_one("#status", Label).update(
                f"Missing: {tools} — run: sudo apt install {tools}"
            )

    def _update_ws_display(self) -> None:
        """Refresh current WS number in title bar, terminal title, and state file."""
        try:
            ws = _current_desktop()
        except Exception:
            return
        if ws == self._current_ws and hasattr(self, "_ws_initialized"):
            return
        self._current_ws = ws
        self._ws_initialized = True
        self._write_ws_state(osd=True)

    def _resolve_pending_wids(self) -> None:
        """Background drip: resolve one unresolved (WS=?) proc per tick."""
        pending = [p for p in self._all_procs if p.get("desktop") == "?" and not p.get("wid")]
        if not pending:
            return
        proc = pending[0]

        def _run():
            wid = _find_wid(proc["pid"], proc.get("shell_cwd"))
            if wid:
                proc["wid"] = wid
                proc["desktop"] = _wid_to_desktop().get(wid, "?")
            else:
                proc["desktop"] = "?"   # stays ?, will retry next tick
            self.call_from_thread(
                self._apply_filter,
                self.query_one("#search", Input).value,
            )

        threading.Thread(target=_run, daemon=True).start()

    def _write_ws_state(self, osd: bool = False) -> None:
        """Write the full WS label (number + optional name) to the state file."""
        ws = self._current_ws
        num = ws + 1  # 1-based for display
        name = self._ws_names.get(str(num), "")
        label = f"WS {num}" + (f" · {name}" if name else "")
        # number + optional name for the extension (it prepends its own "WS ")
        file_content = str(num) + (f" · {name}" if name else "")
        # 1. Textual title bar
        self.title = f"Terminal Window Finder  [{label}]"
        # 2. Terminal window title (OSC 2)
        import sys
        sys.stderr.write(f"\033]2;termtui  {label}\007")
        sys.stderr.flush()
        # 3. State file for GNOME extension
        try:
            WS_STATE_FILE.write_text(file_content)
        except Exception:
            pass
        # 4. OSD overlay on actual workspace change
        if osd:
            _ensure_tk_thread()
            _tk_queue.put({"cmd": "osd", "text": label, "duration_ms": 1500})

    def _rebuild_columns(self) -> None:
        from rich.text import Text
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        headers = []
        for k, (field, label) in self.COLUMNS.items():
            if not self._visible_cols.get(k):
                continue
            if field == self._sort_field:
                t = Text(label, style="underline bold")
            else:
                t = Text(label)
            headers.append(t)
        table.add_columns(*headers)

    def _get_proc_values(self, p: dict, home: str) -> list[str]:
        """Return ordered cell values for visible columns."""
        tag = self._tags.get(p["tag_key"], "")
        if not tag:
            ws_num = p.get("desktop", "?")
            tag = self._ws_names.get(ws_num, "")
        cwd_display = p["cwd"].replace(home, "~", 1)
        vals = []
        for k, (field, _) in self.COLUMNS.items():
            if not self._visible_cols.get(k):
                continue
            if field == "desktop":
                vals.append(p["desktop"])
            elif field == "tag":
                vals.append(tag)
            elif field == "pid":
                vals.append(p["pid"])
            elif field == "comm":
                vals.append(p["comm"])
            elif field == "cwd":
                vals.append(cwd_display)
            elif field == "cmdline":
                vals.append(p["cmdline"])
            elif field == "cpu":
                vals.append(p.get("cpu", ""))
            elif field == "mem":
                vals.append(p.get("mem", ""))
        return vals

    def _load_procs(self) -> None:
        self._all_procs = get_terminal_procs()
        self._apply_filter(self.query_one("#search", Input).value)
        # Resolve wids/desktops in background with periodic UI refresh
        procs = self._all_procs

        def _refresh():
            self.call_from_thread(self._apply_filter,
                                  self.query_one("#search", Input).value)

        def _bg_resolve():
            resolve_wids(procs, progress_cb=_refresh, progress_every=5)

        threading.Thread(target=_bg_resolve, daemon=True).start()

    def _apply_filter(self, pattern: str) -> None:
        table = self.query_one(DataTable)
        # Remember current row's PID so we can restore cursor position after rebuild
        current_pid: str | None = None
        if table.cursor_row >= 0:
            try:
                current_pid = table.get_row_at(table.cursor_row)[0]  # first visible col
                # Find PID from the actual pid column, not assuming index 0
                vis_keys = [k for k in self.COLUMNS if self._visible_cols.get(k)]
                pid_idx = next((i for i, k in enumerate(vis_keys) if self.COLUMNS[k][0] == "pid"), None)
                if pid_idx is not None:
                    current_pid = str(table.get_row_at(table.cursor_row)[pid_idx])
                else:
                    current_pid = None
            except Exception:
                current_pid = None
        table.clear()
        if pattern:
            try:
                rx = re.compile(pattern, re.IGNORECASE)
                procs = [p for p in self._all_procs
                         if rx.search(p["pid"]) or rx.search(p["comm"])
                         or rx.search(p["cwd"]) or rx.search(p["cmdline"])
                         or rx.search(self._tags.get(p["tag_key"], ""))
                         or rx.search(p["desktop"])]
            except re.error:
                procs = [p for p in self._all_procs
                         if pattern.lower() in
                         (p["pid"] + p["comm"] + p["cwd"] + p["cmdline"]
                          + self._tags.get(p["tag_key"], "") + p["desktop"]).lower()]
        else:
            procs = list(self._all_procs)

        sf = self._sort_field
        if sf == "desktop":
            procs.sort(key=lambda p: (int(p["desktop"]) if p["desktop"] not in ("?", "") else 9999, int(p["pid"])))
        elif sf == "pid":
            procs.sort(key=lambda p: int(p["pid"]))
        elif sf == "comm":
            procs.sort(key=lambda p: p["comm"].lower())
        elif sf == "cwd":
            procs.sort(key=lambda p: p["cwd"].lower())
        elif sf == "tag":
            procs.sort(key=lambda p: (self._tags.get(p["tag_key"], ""), p["desktop"]))
        elif sf == "cmdline":
            procs.sort(key=lambda p: p["cmdline"].lower())
        elif sf == "cpu":
            procs.sort(key=lambda p: float(p.get("cpu") or 0), reverse=True)
        elif sf == "mem":
            procs.sort(key=lambda p: float(p.get("mem") or 0), reverse=True)

        self._visible = procs   # snapshot for DetailScreen navigation

        home = str(Path.home())
        for p in procs:
            table.add_row(*self._get_proc_values(p, home), key=p["pid"])

        # Restore cursor to the same PID's row if it's still in the table
        if current_pid is not None:
            for i, p in enumerate(procs):
                if p["pid"] == current_pid:
                    table.move_cursor(row=i, animate=False)
                    break

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self._apply_filter(event.value)
            self.query_one("#status", Label).update(
                f"/{event.value}" if event.value else ""
            )

    def action_open_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.value = ""
        search.focus()

    def action_close_search(self) -> None:
        search = self.query_one("#search", Input)
        search.value = ""
        search.display = False
        self._apply_filter("")
        self.query_one("#status", Label).update("")
        self.query_one(DataTable).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.query_one("#search", Input).display = False
            self.query_one(DataTable).focus()

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def _move_selected_to_ws(self, desktop: int) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row < 0:
            return
        row = table.get_row_at(table.cursor_row)
        vis_keys = [k for k in self.COLUMNS if self._visible_cols.get(k)]
        pid_idx = next((i for i, k in enumerate(vis_keys) if self.COLUMNS[k][0] == "pid"), 2)
        pid = str(row[pid_idx])
        proc = next((p for p in self._all_procs if p["pid"] == pid), {})
        shell_cwd = proc.get("shell_cwd", "")
        wid = proc.get("wid") or _find_wid(pid, shell_cwd)
        if wid is None:
            self.query_one("#status", Label).update(f"No window found for PID {pid}")
            return
        subprocess.run(["wmctrl", "-ir", wid, "-t", str(desktop)])
        self.query_one("#status", Label).update(f"Moved PID {pid} to workspace {desktop}")
        self._load_procs()

    # Dynamically handle action_move_to_ws_N for N in 0-9
    def action_move_to_ws_0(self) -> None: self._move_selected_to_ws(0)
    def action_move_to_ws_1(self) -> None: self._move_selected_to_ws(1)
    def action_move_to_ws_2(self) -> None: self._move_selected_to_ws(2)
    def action_move_to_ws_3(self) -> None: self._move_selected_to_ws(3)
    def action_move_to_ws_4(self) -> None: self._move_selected_to_ws(4)
    def action_move_to_ws_5(self) -> None: self._move_selected_to_ws(5)
    def action_move_to_ws_6(self) -> None: self._move_selected_to_ws(6)
    def action_move_to_ws_7(self) -> None: self._move_selected_to_ws(7)
    def action_move_to_ws_8(self) -> None: self._move_selected_to_ws(8)
    def action_move_to_ws_9(self) -> None: self._move_selected_to_ws(9)

    def _stats_visible(self) -> bool:
        return self._visible_cols.get("%") or self._visible_cols.get("m")

    def _fetch_and_update_stats(self) -> None:
        """Fetch CPU/MEM for all procs and refresh the table in-place."""
        if not self._all_procs:
            return
        try:
            ps = subprocess.run(
                ["ps", "-o", "pid=,pcpu=,pmem=", "-p",
                 ",".join(p["pid"] for p in self._all_procs)],
                capture_output=True, text=True
            ).stdout
            stats = {}
            for line in ps.splitlines():
                parts = line.split()
                if len(parts) == 3:
                    stats[parts[0]] = (parts[1], parts[2])
            for p in self._all_procs:
                cpu, mem = stats.get(p["pid"], ("", ""))
                p["cpu"] = cpu
                p["mem"] = mem
        except Exception:
            return
        # Update visible rows in-place without clearing the table
        table = self.query_one(DataTable)
        vis_keys = [k for k in self.COLUMNS if self._visible_cols.get(k)]
        cpu_idx = next((i for i, k in enumerate(vis_keys) if k == "%"), None)
        mem_idx = next((i for i, k in enumerate(vis_keys) if k == "m"), None)
        if cpu_idx is None and mem_idx is None:
            return
        for p in self._all_procs:
            try:
                if cpu_idx is not None:
                    table.update_cell(p["pid"], table.columns[cpu_idx].key, p.get("cpu", ""))
                if mem_idx is not None:
                    table.update_cell(p["pid"], table.columns[mem_idx].key, p.get("mem", ""))
            except Exception:
                pass

    def _update_stats_timer(self) -> None:
        """Start or stop the stats refresh timer based on column visibility."""
        if self._stats_visible():
            if self._stats_timer is None:
                self._stats_timer = self.set_interval(
                    self.STATS_REFRESH_SECS, self._fetch_and_update_stats
                )
        else:
            if self._stats_timer is not None:
                self._stats_timer.stop()
                self._stats_timer = None

    def action_enter_col_mode(self) -> None:
        self._col_mode = True
        hints = "  ".join(f"{k}:{self.COLUMNS[k][1]}" for k in self.COLUMNS)
        self.query_one("#status", Label).update(f"S + key to toggle column: {hints}")

    def on_key(self, event: Key) -> None:
        if self._col_mode:
            self._col_mode = False
            key = event.character or ""
            if key in self.COLUMNS:
                self._visible_cols[key] = not self._visible_cols[key]
                field, label = self.COLUMNS[key]
                state = "on" if self._visible_cols[key] else "off"
                self._rebuild_columns()
                self._apply_filter(self.query_one("#search", Input).value)
                self._update_stats_timer()
                self.query_one("#status", Label).update(f"Column {label}: {state}")
            else:
                self.query_one("#status", Label).update("Column toggle cancelled")
            event.stop()

    def action_cycle_sort(self) -> None:
        # Only cycle through fields whose column is currently visible
        visible_fields = [
            self.COLUMNS[k][0] for k in self.COLUMNS if self._visible_cols.get(k)
        ]
        if not visible_fields:
            return
        if self._sort_field not in visible_fields:
            self._sort_field = visible_fields[0]
        else:
            idx = (visible_fields.index(self._sort_field) + 1) % len(visible_fields)
            self._sort_field = visible_fields[idx]
        self._rebuild_columns()
        self._apply_filter(self.query_one("#search", Input).value)
        label = self.SORT_FIELDS.get(self._sort_field, self._sort_field)
        self.query_one("#status", Label).update(f"Sort: {label}")

    def action_toggle_hint(self) -> None:
        self._visual_hint = not self._visual_hint
        state = "on" if self._visual_hint else "off"
        self.query_one("#status", Label).update(f"Visual hint {state}")

    def _name_ws_num(self, num: int) -> None:
        """Open the tag input to name workspace `num` (1-based)."""
        current = self._ws_names.get(str(num), "")

        def save_name(new_name: str) -> None:
            if new_name:
                self._ws_names[str(num)] = new_name
            elif str(num) in self._ws_names:
                del self._ws_names[str(num)]
            _save_ws_names(self._ws_names)
            if num == self._current_ws + 1:
                self._write_ws_state()
            self._apply_filter(self.query_one("#search", Input).value)
            self.query_one("#status", Label).update(
                f"WS {num} named: {new_name}" if new_name else f"WS {num} name cleared"
            )

        self.push_screen(TagScreen(current), save_name)

    def action_name_ws(self) -> None:
        """N: name the selected row's WS, falling back to the current WS."""
        proc = self._proc_at_cursor()
        num: int | None = None
        if proc:
            d = proc.get("desktop", "?")
            if d not in ("?", ""):
                try:
                    num = int(d)
                except ValueError:
                    pass
        if num is None:
            num = self._current_ws + 1
        self._name_ws_num(num)

    def action_refresh(self) -> None:
        self._load_procs()
        self.query_one("#status", Label).update("Refreshed.")

    def _proc_at_cursor(self) -> dict | None:
        """Return the proc dict for the currently highlighted row, or None."""
        table = self.query_one(DataTable)
        if table.cursor_row < 0:
            return None
        try:
            row = table.get_row_at(table.cursor_row)
        except Exception:
            return None
        vis_keys = [k for k in self.COLUMNS if self._visible_cols.get(k)]
        pid_idx = next((i for i, k in enumerate(vis_keys) if self.COLUMNS[k][0] == "pid"), None)
        if pid_idx is None:
            return None
        pid = str(row[pid_idx])
        return next((p for p in self._all_procs if p["pid"] == pid), None)

    def _detail_idx(self, proc: dict) -> int:
        """Return the index of proc in the current visible list."""
        return next((i for i, p in enumerate(self._visible) if p["pid"] == proc["pid"]), 0)

    def action_show_detail(self) -> None:
        """Space: show full detail modal and resolve WS/wid for this proc immediately."""
        proc = self._proc_at_cursor()
        if proc is None:
            return

        if not proc.get("wid"):
            self.query_one("#status", Label).update("Resolving window…")

            def _resolve_one():
                wid = _find_wid(proc["pid"], proc.get("shell_cwd"))
                if wid:
                    proc["wid"] = wid
                    proc["desktop"] = _wid_to_desktop().get(wid, "?")
                self.call_from_thread(_after_resolve)

            def _after_resolve():
                self._apply_filter(self.query_one("#search", Input).value)
                self.push_screen(DetailScreen(self._visible, self._detail_idx(proc), self._tags))
                self.query_one("#status", Label).update("")

            threading.Thread(target=_resolve_one, daemon=True).start()
        else:
            self.push_screen(DetailScreen(self._visible, self._detail_idx(proc), self._tags))

    def action_show_env_direct(self) -> None:
        """e: open Env Vars screen directly for the selected process."""
        proc = self._proc_at_cursor()
        if proc is None:
            return
        idx = self._detail_idx(proc)
        self.push_screen(EnvScreen(self._visible, idx, self._tags))

    def action_settings(self) -> None:
        """ctrl+s: open settings."""
        def save(result: dict | None) -> None:
            if result is None:
                return
            self._settings = result
            _save_settings(result)
            self.query_one("#status", Label).update(
                f"Settings saved  (preview: {result['preview_secs']}s)"
            )
        self.push_screen(SettingsScreen(self._settings), save)

    def action_preview_long(self) -> None:
        """P: preview at 3× the configured duration."""
        self._do_preview(multiplier=3)

    def action_preview(self) -> None:
        """p: jump to the window's workspace for preview_secs, then return."""
        self._do_preview(multiplier=1)

    def _do_preview(self, multiplier: int = 1) -> None:
        proc = self._proc_at_cursor()
        if proc is None:
            return

        desktop = proc.get("desktop", "?")
        if desktop in ("?", ""):
            self.query_one("#status", Label).update(
                "WS not resolved — press Space to resolve first"
            )
            return

        target_ws_0 = int(desktop) - 1
        current_ws_0 = self._current_ws

        wid = proc.get("wid", "")
        if not wid:
            wid = _find_wid(proc["pid"], proc.get("shell_cwd")) or ""
            if wid:
                proc["wid"] = wid

        # Save the currently active window so we can return focus to it
        orig_r = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True
        )
        orig_wid_dec = orig_r.stdout.strip()

        comm = proc.get("comm", "")
        preview_secs = self._settings["preview_secs"] * multiplier
        secs = preview_secs

        # Build OSD labels now (while still on main thread)
        target_num = int(desktop)
        target_name = self._ws_names.get(str(target_num), "")
        target_label = f"WS {target_num}" + (f" · {target_name}" if target_name else "")
        origin_num = current_ws_0 + 1
        origin_name = self._ws_names.get(str(origin_num), "")
        origin_label = f"WS {origin_num}" + (f" · {origin_name}" if origin_name else "")

        self.query_one("#status", Label).update(
            f"Previewing {comm}  WS {desktop} — returning in {secs}s…"
        )

        def _run() -> None:
            try:
                # Show target WS OSD immediately, before wmctrl even completes
                _ensure_tk_thread()
                _tk_queue.put({"cmd": "osd", "text": target_label,
                               "duration_ms": int(preview_secs * 1000)})
                subprocess.run(["wmctrl", "-s", str(target_ws_0)], capture_output=True)
                if wid:
                    subprocess.run(["wmctrl", "-ia", wid], capture_output=True)
                time.sleep(preview_secs)
                # Show origin WS OSD immediately on return
                _tk_queue.put({"cmd": "osd", "text": origin_label, "duration_ms": 1500})
                subprocess.run(["wmctrl", "-s", str(current_ws_0)], capture_output=True)
                if orig_wid_dec:
                    subprocess.run(
                        ["xdotool", "windowactivate", "--sync", orig_wid_dec],
                        capture_output=True,
                    )
            except Exception:
                pass

            def _done() -> None:
                self.query_one("#status", Label).update(f"Returned from WS {desktop}")

            self.call_from_thread(_done)

        threading.Thread(target=_run, daemon=True).start()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Briefly show a narrow gold border around windows on the same workspace."""
        table = self.query_one(DataTable)
        if event.cursor_row < 0:
            return
        try:
            row = table.get_row_at(event.cursor_row)
        except Exception:
            return
        vis_keys = [k for k in self.COLUMNS if self._visible_cols.get(k)]
        pid_idx = next((i for i, k in enumerate(vis_keys) if self.COLUMNS[k][0] == "pid"), None)
        if pid_idx is None:
            return
        pid = str(row[pid_idx])
        proc = next((p for p in self._all_procs if p["pid"] == pid), None)
        if proc is None:
            return
        wid = proc.get("wid", "")
        desktop = proc.get("desktop", "?")
        if not wid or desktop == "?":
            return
        try:
            cur_desktop = str(_current_desktop() + 1)
        except Exception:
            return
        if desktop != cur_desktop:
            return
        # Cancel any previous peek (also tell tk to tear down immediately)
        self._peek_cancel.set()
        _tk_queue.put({"cmd": "cancel"})
        cancel = threading.Event()
        self._peek_cancel = cancel
        wid_dec = str(int(wid, 16))
        def _peek():
            if cancel.is_set():
                return
            _highlight_window(wid, wid_dec, flash_secs=0.8, border=5, cancel=cancel)
        threading.Thread(target=_peek, daemon=True).start()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = self.query_one(DataTable)
        row = table.get_row_at(event.cursor_row)
        # Find PID and CWD column indices dynamically based on visible columns
        vis_keys = [k for k in self.COLUMNS if self._visible_cols.get(k)]
        pid_idx = next((i for i, k in enumerate(vis_keys) if self.COLUMNS[k][0] == "pid"), None)
        cwd_idx = next((i for i, k in enumerate(vis_keys) if self.COLUMNS[k][0] == "cwd"), None)
        if pid_idx is None:
            return
        pid = str(row[pid_idx])
        cwd = str(row[cwd_idx]) if cwd_idx is not None else ""
        visual_hint = self._visual_hint
        proc = next((p for p in self._all_procs if p["pid"] == pid), {})
        shell_cwd = proc.get("shell_cwd", cwd)
        tag_key = proc.get("tag_key", "")

        def handle_action(action: str) -> None:
            if not action:
                return
            if action == "focus":
                msg = do_focus(pid, shell_cwd, visual_hint)
                self.query_one("#status", Label).update(msg)
            elif action == "move_here":
                msg = do_move_here(pid, shell_cwd, visual_hint)
                self.query_one("#status", Label).update(msg)
            elif action == "tag":
                current = self._tags.get(tag_key, "")
                def save_tag(new_tag: str) -> None:
                    if new_tag:
                        self._tags[tag_key] = new_tag
                    elif tag_key in self._tags:
                        del self._tags[tag_key]
                    _save_tags(self._tags)
                    self._apply_filter(self.query_one("#search", Input).value)
                    self.query_one("#status", Label).update(
                        f"Tag set: {new_tag}" if new_tag else "Tag cleared"
                    )
                self.push_screen(TagScreen(current), save_tag)
            elif action == "clear_tag":
                if tag_key in self._tags:
                    del self._tags[tag_key]
                    _save_tags(self._tags)
                    self._apply_filter(self.query_one("#search", Input).value)
                self.query_one("#status", Label).update("Tag cleared")
            else:
                self.query_one("#status", Label).update(f"Unknown action: {action}")

        self.push_screen(ActionMenu(pid, cwd), handle_action)

    def on_unmount(self) -> None:
        # Restore terminal title and clean up state file
        import sys
        sys.stderr.write("\033]2;\007")
        sys.stderr.flush()
        try:
            WS_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    missing = check_dependencies()
    if missing:
        print("Missing required tools — install with:\n" + "\n".join(missing))
        print()
    TermTUI().run()
