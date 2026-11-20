#!/usr/bin/env python3
"""
AirMouse launcher - runs quietly in the background.
Triple-tap Ctrl to turn AirMouse on or off.

    python launcher.py            run the background launcher
    python launcher.py --toggle   turn AirMouse on/off (same as triple Ctrl)
    python launcher.py --exit     stop the background launcher
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import threading
import time

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "airmouse.py")
PORT = 47615                       # localhost only: single instance + commands
DATA_DIR = os.path.join(os.path.expanduser("~"), ".airmouse")
LAUNCHER_LOG = os.path.join(DATA_DIR, "launcher.log")
APP_LOG = os.path.join(DATA_DIR, "airmouse.log")
ARGS_FILE = os.path.join(DATA_DIR, "args.txt")   # your permanent options, e.g. --speed 1.3


# ---------------------------------------------------------------------------
#  Talking to a running launcher
# ---------------------------------------------------------------------------
def send(cmd: str, timeout: float = 1.0):
    """Send a command to the running launcher. Returns its reply or None."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout) as s:
            s.sendall(cmd.encode() + b"\n")
            return s.recv(256).decode().strip()
    except OSError:
        return None


# ---------------------------------------------------------------------------
#  Triple-tap detector (pure logic, easy to test)
# ---------------------------------------------------------------------------
class TapDetector:
    """Fires `callback` when Ctrl is tapped `taps` times quickly, alone.

    A tap = Ctrl pressed and released within `max_hold` seconds with no other
    key in between. Ctrl+C, Ctrl+V etc. never count.
    """

    def __init__(self, callback, taps=3, window=0.9, max_hold=0.35, cooldown=1.0,
                 clock=time.monotonic):
        self.callback, self.taps, self.window = callback, taps, window
        self.max_hold, self.cooldown, self.clock = max_hold, cooldown, clock
        self.times: list = []
        self.down_t = None
        self.dirty = False
        self.last_fire = -1e9

    def ctrl_down(self):
        if self.down_t is None:          # ignore key auto-repeat
            self.down_t = self.clock()
            self.dirty = False

    def ctrl_up(self):
        if self.down_t is None:
            return
        now = self.clock()
        held = now - self.down_t
        self.down_t = None
        if self.dirty or held > self.max_hold:
            self.times.clear()
            return
        self.times = [t for t in self.times if now - t <= self.window] + [now]
        if len(self.times) >= self.taps:
            self.times.clear()
            if now - self.last_fire >= self.cooldown:
                self.last_fire = now
                self.callback()

    def other_key(self):
        self.dirty = True
        self.times.clear()


# ---------------------------------------------------------------------------
#  The launcher
# ---------------------------------------------------------------------------
def child_python():
    """On Windows use pythonw.exe so no console window pops up."""
    exe = sys.executable
    if IS_WIN:
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def chime(on: bool):
    """Tiny sound so you know it switched, even with the camera window hidden."""
    def play():
        try:
            if IS_WIN:
                import winsound
                winsound.MessageBeep(0x40 if on else 0x0)
            elif IS_MAC:
                snd = "/System/Library/Sounds/%s.aiff" % ("Tink" if on else "Pop")
                subprocess.run(["afplay", snd], timeout=3)
            else:
                snd = "/usr/share/sounds/freedesktop/stereo/%s.oga" % (
                    "device-added" if on else "device-removed")
                if os.path.exists(snd):
                    subprocess.run(["paplay", snd], timeout=3,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    threading.Thread(target=play, daemon=True).start()


class Launcher:
    def __init__(self):
        self.child = None
        self.lock = threading.Lock()
        self.log = None

    def running(self):
        return self.child is not None and self.child.poll() is None

    def start(self):
        with self.lock:
            if self.running():
                return
            os.makedirs(DATA_DIR, exist_ok=True)
            self.log = open(APP_LOG, "a", buffering=1, encoding="utf-8", errors="replace")
            self.log.write("\n=== AirMouse started %s ===\n" % time.ctime())
            flags = 0x08000000 if IS_WIN else 0     # CREATE_NO_WINDOW
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            extra = []
            if os.path.exists(ARGS_FILE):
                with open(ARGS_FILE, encoding="utf-8") as f:
                    extra = shlex.split(f.read(), posix=not IS_WIN)
            self.child = subprocess.Popen(
                [child_python(), APP, "--managed"] + extra, cwd=HERE, env=env,
                stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT,
                creationflags=flags)
            print("[launcher] AirMouse ON (pid %d)" % self.child.pid)
        chime(True)

    def stop(self):
        with self.lock:
            if not self.running():
                return
            c = self.child
            try:                                # ask nicely: releases mouse buttons etc.
                c.stdin.write(b"quit\n")
                c.stdin.flush()
                c.stdin.close()
            except Exception:
                pass
            try:
                c.wait(timeout=4)
            except subprocess.TimeoutExpired:
                c.kill()
            self.child = None
            print("[launcher] AirMouse OFF")
        chime(False)

    def toggle(self):
        if self.running():
            self.stop()
        else:
            self.start()

    def handle(self, cmd):
        if cmd == "toggle":
            threading.Thread(target=self.toggle, daemon=True).start()
            return "ok"
        if cmd == "start":
            threading.Thread(target=self.start, daemon=True).start()
            return "ok"
        if cmd == "stop":
            threading.Thread(target=self.stop, daemon=True).start()
            return "ok"
        if cmd == "status":
            return "on" if self.running() else "off"
        if cmd == "ping":
            return "pong"
        return "?"


def start_keyboard(on_trigger):
    from pynput import keyboard
    K = keyboard.Key
    ctrl_keys = {K.ctrl, K.ctrl_l, K.ctrl_r}
    det = TapDetector(on_trigger)

    def on_press(key):
        if key in ctrl_keys:
            det.ctrl_down()
        else:
            det.other_key()

    def on_release(key):
        if key in ctrl_keys:
            det.ctrl_up()

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    if IS_MAC and getattr(listener, "IS_TRUSTED", True) is False:
        print("[launcher] macOS blocked keyboard access. Allow Python in System Settings > "
              "Privacy & Security > Input Monitoring and Accessibility, then log out and in.")
    return listener


def run_daemon():
    os.makedirs(DATA_DIR, exist_ok=True)
    if sys.stdout is None or not sys.stdout.isatty():       # pythonw / autostart
        f = open(LAUNCHER_LOG, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = f
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if not IS_WIN:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", PORT))
    except OSError:
        print("[launcher] already running")
        return
    srv.listen(4)
    print("[launcher] ready %s - triple-tap Ctrl to toggle AirMouse" % time.ctime())

    app = Launcher()
    try:
        start_keyboard(app.toggle)
    except Exception as e:
        print("[launcher] keyboard hook unavailable:", e)

    try:
        while True:
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(2)
                try:
                    cmd = conn.recv(64).decode().strip()
                except OSError:
                    continue
                if cmd == "exit":
                    conn.sendall(b"bye\n")
                    break
                conn.sendall((app.handle(cmd) + "\n").encode())
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        srv.close()
        print("[launcher] exit")


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--toggle", "--start", "--stop", "--status", "--exit"):
        reply = send(args[0][2:])
        print(reply if reply else "AirMouse launcher is not running (run: python install.py)")
        return
    run_daemon()


if __name__ == "__main__":
    main()
