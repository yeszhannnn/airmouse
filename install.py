#!/usr/bin/env python3
"""
One-command setup for AirMouse.

    python3 install.py               install / update, start on login, turn it on now
    python3 install.py --no-autostart
    python3 install.py --uninstall   stop it and remove it from login items
"""

from __future__ import annotations

import glob
import os
import platform
import plistlib
import subprocess
import sys
import time

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
APP = os.path.join(HERE, "airmouse.py")
MAC_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.airmouse.launcher.plist")
LINUX_DESKTOP = os.path.expanduser("~/.config/autostart/airmouse.desktop")
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PORT = 47615
OLD_PACKAGES = ["mediapipe", "opencv-contrib-python", "matplotlib", "contourpy", "cycler",
                "fonttools", "kiwisolver", "pillow", "pyparsing", "python-dateutil",
                "sounddevice", "cffi", "pycparser", "absl-py", "tzdata"]


def say(msg):
    print("\n==> " + msg, flush=True)


def send(cmd, timeout=1.0):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout) as s:
            s.sendall(cmd.encode() + b"\n")
            return s.recv(256).decode().strip()
    except OSError:
        return None


def venv_python(gui=False):
    if IS_WIN:
        return os.path.join(VENV, "Scripts", "pythonw.exe" if gui else "python.exe")
    return os.path.join(VENV, "bin", "python")


def pip(*args, check=True):
    r = subprocess.run([venv_python(), "-m", "pip", "install", "--disable-pip-version-check",
                        "-q", *args])
    if check and r.returncode != 0:
        sys.exit("\n[error] pip install failed: %s" % " ".join(args))
    return r.returncode == 0


# ---------------------------------------------------------------------------
#  Autostart (starts switched off; triple-tap Ctrl to turn on)
# ---------------------------------------------------------------------------
def autostart_add():
    if IS_WIN:
        import winreg
        cmd = '"%s" "%s" --background' % (venv_python(gui=True), APP)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "AirMouse", 0, winreg.REG_SZ, cmd)
    elif IS_MAC:
        os.makedirs(os.path.dirname(MAC_PLIST), exist_ok=True)
        log = os.path.expanduser("~/.airmouse/launchd.log")
        with open(MAC_PLIST, "wb") as f:
            plistlib.dump({
                "Label": "com.airmouse.launcher",
                "ProgramArguments": [venv_python(), APP, "--background"],
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
                    'Exec="%s" "%s" --background\nNoDisplay=true\n'
                    "X-GNOME-Autostart-enabled=true\n" % (venv_python(), APP))


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
#  Running instance
# ---------------------------------------------------------------------------
def stop_running():
    """Stops a running AirMouse (also the old separate launcher)."""
    if send("exit") is None:
        return
    for _ in range(50):
        if send("ping", timeout=0.2) is None:
            time.sleep(0.3)             # let the process release its files
            return
        time.sleep(0.1)


def start_now():
    args = [venv_python(gui=True), APP]
    if IS_WIN:
        subprocess.Popen(args, cwd=HERE, close_fds=True,
                         creationflags=0x00000008 | 0x00000200,  # DETACHED | NEW_GROUP
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(args, cwd=HERE, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if send("status", timeout=0.2) == "on":
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if sys.version_info < (3, 9):
        sys.exit("AirMouse needs Python 3.9 or newer (you have %s)." % sys.version.split()[0])

    if "--uninstall" in args:
        stop_running()
        autostart_remove()
        print("AirMouse stopped and removed from login items. "
              "Delete this folder to remove it completely.")
        return

    stop_running()                      # update in place: stop the old version first

    # versions before 2.0 used the big mediapipe package: start from a clean env
    old = glob.glob(os.path.join(VENV, "Lib", "site-packages", "mediapipe")) + \
        glob.glob(os.path.join(VENV, "lib", "python*", "site-packages", "mediapipe"))
    if old:
        # uninstall in place (deleting .venv fails on Windows if you run this from it)
        say("Removing the old heavy packages (mediapipe, matplotlib, ...)")
        subprocess.run([venv_python(), "-m", "pip", "uninstall", "-y", "-q", *OLD_PACKAGES])

    say("Creating a private Python environment (.venv)")
    if not os.path.exists(venv_python()):
        r = subprocess.run([sys.executable, "-m", "venv", VENV])
        if r.returncode != 0:
            hint = "" if (IS_WIN or IS_MAC) else "  On Ubuntu/Debian run: sudo apt install python3-venv"
            sys.exit("[error] could not create the environment.\n" + hint)

    say("Installing dependencies")
    pip("--upgrade", "pip", check=False)
    pip("-r", os.path.join(HERE, "requirements.txt"))
    if old:   # opencv-contrib shared the cv2 folder: restore opencv-python's files
        pip("--force-reinstall", "--no-deps", "opencv-python")
    intel_mac = IS_MAC and platform.machine() == "x86_64"
    if not intel_mac and not pip("ai-edge-litert", check=False):
        print("    (fast runtime not available for this Python - using OpenCV, still works)")

    say("Downloading the hand-tracking model")
    r = subprocess.run([venv_python(), "-c", "import airmouse; airmouse.ensure_model()"], cwd=HERE)
    if r.returncode != 0:
        sys.exit("[error] could not download the model. Check your internet connection.")

    say("Starting AirMouse")
    if "--no-autostart" in args:
        autostart_remove()
    else:
        autostart_add()
    if not start_now():
        print("\n[!] AirMouse did not start. See ~/.airmouse/airmouse.log")
        print("    You can also run it directly:  %s airmouse.py" % venv_python())
        sys.exit(1)

    print("\nDone! AirMouse is ON. Raise your index finger to move the cursor.")
    print("Triple-tap Ctrl to turn it off or on again.")
    if "--no-autostart" not in args:
        print("It will be ready (switched off) every time you log in.")
    if IS_MAC:
        print("\nmacOS: when asked, allow Camera, Accessibility and Input Monitoring")
        print("for Python (System Settings > Privacy & Security).")
    if not (IS_WIN or IS_MAC) and os.environ.get("XDG_SESSION_TYPE") == "wayland":
        print("\n[!] Wayland detected: global hotkeys and mouse control need an X11 session.")


if __name__ == "__main__":
    main()
