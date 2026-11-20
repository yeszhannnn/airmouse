#!/usr/bin/env python3
"""
One-command setup for AirMouse.

    python3 install.py               install / update, start on login, run now
    python3 install.py --no-autostart
    python3 install.py --uninstall   stop it and remove it from login items
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
LAUNCHER = os.path.join(HERE, "launcher.py")
MAC_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.airmouse.launcher.plist")
LINUX_DESKTOP = os.path.expanduser("~/.config/autostart/airmouse.desktop")
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

sys.path.insert(0, HERE)
from launcher import send  # noqa: E402  (stdlib only)


def say(msg):
    print("\n==> " + msg, flush=True)


def venv_python(gui=False):
    if IS_WIN:
        return os.path.join(VENV, "Scripts", "pythonw.exe" if gui else "python.exe")
    return os.path.join(VENV, "bin", "python")


def run(cmd):
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit("\n[error] command failed: %s" % " ".join(cmd))


# ---------------------------------------------------------------------------
#  Autostart
# ---------------------------------------------------------------------------
def autostart_add():
    if IS_WIN:
        import winreg
        cmd = '"%s" "%s"' % (venv_python(gui=True), LAUNCHER)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "AirMouse", 0, winreg.REG_SZ, cmd)
    elif IS_MAC:
        os.makedirs(os.path.dirname(MAC_PLIST), exist_ok=True)
        log = os.path.expanduser("~/.airmouse/launchd.log")
        with open(MAC_PLIST, "wb") as f:
            plistlib.dump({
                "Label": "com.airmouse.launcher",
                "ProgramArguments": [venv_python(), LAUNCHER],
                "WorkingDirectory": HERE,
                "RunAtLoad": True,
                "StandardOutPath": log,
                "StandardErrorPath": log,
            }, f)
    else:
        os.makedirs(os.path.dirname(LINUX_DESKTOP), exist_ok=True)
        with open(LINUX_DESKTOP, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=AirMouse\n"
                    "Comment=Hand gesture mouse - triple-tap Ctrl to toggle\n"
                    'Exec="%s" "%s"\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n'
                    % (venv_python(), LAUNCHER))


def autostart_remove():
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "AirMouse")
        except OSError:
            pass
    else:
        path = MAC_PLIST if IS_MAC else LINUX_DESKTOP
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
#  Background launcher
# ---------------------------------------------------------------------------
def launcher_stop():
    if send("exit"):
        for _ in range(30):
            if send("ping", timeout=0.2) is None:
                return
            time.sleep(0.1)


def launcher_start():
    if IS_WIN:
        subprocess.Popen([venv_python(gui=True), LAUNCHER], cwd=HERE, close_fds=True,
                         creationflags=0x00000008 | 0x00000200,  # DETACHED | NEW_GROUP
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen([venv_python(), LAUNCHER], cwd=HERE, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    for _ in range(50):
        if send("ping", timeout=0.2) == "pong":
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if sys.version_info < (3, 9):
        sys.exit("AirMouse needs Python 3.9 or newer (you have %s)." % sys.version.split()[0])

    if "--uninstall" in args:
        launcher_stop()
        autostart_remove()
        print("AirMouse stopped and removed from login items. "
              "Delete this folder to remove it completely.")
        return

    say("Creating a private Python environment (.venv)")
    if not os.path.exists(venv_python()):
        r = subprocess.run([sys.executable, "-m", "venv", VENV])
        if r.returncode != 0:
            hint = "  On Ubuntu/Debian run: sudo apt install python3-venv" if not (IS_WIN or IS_MAC) else ""
            sys.exit("[error] could not create the environment.\n" + hint)

    say("Installing dependencies (first time takes a minute)")
    run([venv_python(), "-m", "pip", "install", "--disable-pip-version-check", "-q",
         "--upgrade", "pip"])
    run([venv_python(), "-m", "pip", "install", "--disable-pip-version-check",
         "-r", os.path.join(HERE, "requirements.txt")])

    say("Downloading the hand-tracking model")
    run([venv_python(), "-c", "import airmouse; airmouse.ensure_model()"])

    say("Starting the background launcher")
    launcher_stop()                      # restart if it was already running (update)
    if "--no-autostart" in args:
        autostart_remove()
    else:
        autostart_add()
    ok = launcher_start()

    print()
    if not ok:
        print("[!] The launcher did not start. Check ~/.airmouse/launcher.log")
        print("    You can always run AirMouse directly:  %s airmouse.py" % venv_python())
        sys.exit(1)
    print("Done! Triple-tap Ctrl to turn AirMouse on or off.")
    if "--no-autostart" not in args:
        print("It will also start automatically when you log in.")
    if IS_MAC:
        print("\nmacOS: when asked, allow Camera, Accessibility and Input Monitoring")
        print("for Python (System Settings > Privacy & Security).")
    if not (IS_WIN or IS_MAC) and os.environ.get("XDG_SESSION_TYPE") == "wayland":
        print("\n[!] Wayland detected: global hotkeys and mouse control need an X11 session.")


if __name__ == "__main__":
    main()
