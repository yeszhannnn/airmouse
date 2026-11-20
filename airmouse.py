#!/usr/bin/env python3
"""
AirMouse - control your computer with hand gestures through a webcam.

Gestures
  index finger up (others curled) ....... move the cursor
  pinch thumb + index ................... left click (twice = double click,
                                          hold and move = drag)
  two fingers up + pinch ................ right click
  open palm, move up / down ............. scroll (with inertia)
  fist .................................. release the cursor / stop scrolling

Hotkeys (work everywhere)
  Ctrl+Alt+M  pause     Ctrl+Alt+V  show/hide camera window     Ctrl+Alt+Q  quit
  (triple-tap Ctrl to start/stop is handled by launcher.py)

Threads
  camera    keeps only the newest frame (no queue = no lag)
  tracking  MediaPipe + filters + gestures (independent of the preview window)
  output    240 Hz: moves the cursor smoothly between camera frames
  main      preview window only
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
import traceback
import urllib.request
from collections import deque
from dataclasses import dataclass

import numpy as np

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "hand_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/latest/hand_landmarker.task")


# =============================================================================
#  SETTINGS
# =============================================================================
@dataclass
class Config:
    # camera
    camera: str = "0"              # camera index, or a path to a video file
    cam_width: int = 640
    cam_height: int = 480
    cam_fps: int = 60              # the camera will give the max it supports
    mirror: bool = True
    preview: bool = True

    # cursor
    #   "follow"   - the cursor repeats your finger's motion 1:1 (scaled up),
    #                independent of how far you sit from the camera (default)
    #   "absolute" - a point in the camera frame maps to a point on screen
    mode: str = "follow"
    gain: float = 1000.0           # screen px (on a 1920 px wide screen) per palm length
    speed: float = 1.0             # global multiplier (+/- keys in the preview)

    # absolute mode: active zone inside the frame (fractions of the frame)
    zone_cx: float = 0.5
    zone_cy: float = 0.42
    zone_w: float = 0.42
    zone_h: float = 0.40

    # smoothing
    min_cutoff: float = 1.4        # One Euro filter, Hz. Lower = steadier at rest
    beta: float = 5.0              # higher = less lag on fast moves
    d_cutoff: float = 1.5
    deadband: float = 0.010        # "rope" radius in palm lengths: kills micro-jitter
    deadband_auto: float = 2.2     # the radius grows if the camera is noisy (dark room)
    track_tip_weight: float = 0.6  # tracked point = 0.6 * fingertip + 0.4 * next joint

    # output thread
    output_hz: int = 240
    output_tau: float = 0.014      # s, soft easing toward the target (removes steps)
    prediction: float = 0.8        # 0..1, motion extrapolation between camera frames

    # click (thumb-index distance divided by palm size)
    pinch_on: float = 0.30
    pinch_off: float = 0.44
    freeze_ratio: float = 0.60     # while the fingers close, the cursor freezes so
    freeze_speed: float = 1.2      #   the click lands where you were aiming
    freeze_rewind: float = 0.06    # s, jump back to where the finger was before the pinch
    freeze_max: float = 0.45
    unfreeze_frac: float = 0.90
    drag_delay: float = 0.30       # hold the pinch this long -> drag
    drag_move: float = 0.35        # ...or move this many palm lengths
    dbl_click_time: float = 0.50
    dbl_click_radius: float = 40.0

    # pose recognition
    pose_frames: int = 3           # frames in a row needed to switch gesture
    lost_grace: float = 0.15       # s, ignore short tracking dropouts

    # scroll
    scroll_style: str = "swipe"    # "swipe" (drag the page) | "joystick"
    scroll_natural: bool = True    # palm up -> page moves up (like a phone)
    scroll_gain: float = 450.0     # scroll px per palm length of motion
    scroll_accel: float = 0.10     # fast swipes scroll further
    scroll_max: float = 6000.0     # px/s cap
    scroll_deadzone: float = 0.22  # palm lengths/s; slower = "hand is still"
    scroll_inertia: float = 0.22   # s, glide after a swipe (0 = none)
    scroll_reverse_ratio: float = 0.65  # a slower move back = returning the hand, ignored
    scroll_return_window: float = 0.9   # s of stillness after which any move counts
    joystick_deadzone: float = 0.25
    joystick_gain: float = 1400.0
    wheel_steps: bool = False      # send whole wheel notches (for old apps)


# =============================================================================
#  FILTERS
# =============================================================================
def _alpha(dt: float, cutoff: float) -> float:
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuro:
    """One Euro filter (Casiez et al. 2012). Speed is normalized by `scale`."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self):
        self.x = None
        self.dx = None
        self.t = None

    def __call__(self, x, t: float, scale: float = 1.0):
        x = np.asarray(x, dtype=float)
        if self.x is None:
            self.x, self.dx, self.t = x.copy(), np.zeros_like(x), t
            return self.x.copy()
        dt = t - self.t
        if dt <= 1e-6:
            return self.x.copy()
        self.t = t
        dx = (x - self.x) / dt
        ad = _alpha(dt, self.d_cutoff)
        self.dx = ad * dx + (1.0 - ad) * self.dx
        speed = float(np.linalg.norm(self.dx)) / max(scale, 1e-6)
        a = _alpha(dt, self.min_cutoff + self.beta * speed)
        self.x = a * x + (1.0 - a) * self.x
        return self.x.copy()


# =============================================================================
#  WINDOWS TUNING
# =============================================================================
def windows_tuning():
    """Removes the usual causes of stutter on Windows."""
    if not IS_WIN:
        return
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    H = wintypes.HANDLE
    k.GetCurrentProcess.restype = H
    k.GetStdHandle.restype = H
    k.SetPriorityClass.argtypes = [H, wintypes.DWORD]
    k.GetConsoleMode.argtypes = [H, ctypes.POINTER(wintypes.DWORD)]
    k.SetConsoleMode.argtypes = [H, wintypes.DWORD]
    try:  # higher priority = fewer hiccups when other apps are busy
        k.SetPriorityClass(k.GetCurrentProcess(), 0x8000)  # ABOVE_NORMAL
    except Exception:
        pass
    try:  # opt out of EcoQoS: Windows 11 slows down apps whose window is hidden
        class PPTS(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]
        st = PPTS(1, 0x1 | 0x4, 0)  # EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION -> off
        k.SetProcessInformation(k.GetCurrentProcess(), 4,  # ProcessPowerThrottling
                                ctypes.byref(st), ctypes.sizeof(st))
    except Exception:
        pass
    try:  # 1 ms timer
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
    try:  # disable console QuickEdit: a stray click into the console pauses the app
        h = k.GetStdHandle(-10)
        mode = wintypes.DWORD()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
    except Exception:
        pass


def raise_thread_priority():
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            k.GetCurrentThread.restype = wintypes.HANDLE
            k.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
            k.SetThreadPriority(k.GetCurrentThread(), 2)  # HIGHEST
        except Exception:
            pass


# =============================================================================
#  MOUSE BACKENDS (native per OS)       scroll(px): positive = scroll up
# =============================================================================
class WindowsMouse:
    def __init__(self, cfg: Config):
        import ctypes
        from ctypes import wintypes
        self._ct, self._wt = ctypes, wintypes
        try:  # real pixels on 125% / 150% scaled screens
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        self.u = ctypes.windll.user32
        self.w = self.u.GetSystemMetrics(0)
        self.h = self.u.GetSystemMetrics(1)
        self._wheel_acc = 0.0
        self._quantum = 120 if cfg.wheel_steps else 20

    def screen_size(self):
        return self.w, self.h

    def get_pos(self):
        pt = self._wt.POINT()
        self.u.GetCursorPos(self._ct.byref(pt))
        return float(pt.x), float(pt.y)

    def move(self, x, y):
        nx = int(round(x * 65535.0 / max(1, self.w - 1)))
        ny = int(round(y * 65535.0 / max(1, self.h - 1)))
        self.u.mouse_event(0x8001, nx, ny, 0, 0)  # MOVE | ABSOLUTE

    def left_down(self):
        self.u.mouse_event(0x0002, 0, 0, 0, 0)

    def left_up(self):
        self.u.mouse_event(0x0004, 0, 0, 0, 0)

    def right_click(self):
        self.u.mouse_event(0x0008, 0, 0, 0, 0)
        self.u.mouse_event(0x0010, 0, 0, 0, 0)

    def scroll(self, px):
        self._wheel_acc += px * 1.2       # 120 units = 1 notch ~ 100 px in a browser
        q = self._quantum
        if abs(self._wheel_acc) >= q:
            units = int(self._wheel_acc / q) * q
            self._wheel_acc -= units
            self.u.mouse_event(0x0800, 0, 0, units, 0)


class MacMouse:
    def __init__(self, cfg: Config):
        import Quartz
        self.Q = Quartz
        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        self.w, self.h = int(b.size.width), int(b.size.height)
        self.down = False
        self._x, self._y = self.get_pos()
        self._last_click_t = 0.0
        self._last_click_xy = (0.0, 0.0)
        self._click_state = 1
        self._acc = 0.0

    def screen_size(self):
        return self.w, self.h

    def get_pos(self):
        p = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        return float(p.x), float(p.y)

    def _post(self, etype, button, clicks=1):
        Q = self.Q
        ev = Q.CGEventCreateMouseEvent(None, etype, (self._x, self._y), button)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, clicks)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    def move(self, x, y):
        Q = self.Q
        self._x, self._y = float(x), float(y)
        t = Q.kCGEventLeftMouseDragged if self.down else Q.kCGEventMouseMoved
        self._post(t, Q.kCGMouseButtonLeft)

    def _next_click_state(self):
        now = time.perf_counter()
        lx, ly = self._last_click_xy
        if now - self._last_click_t < 0.5 and math.hypot(self._x - lx, self._y - ly) < 8:
            self._click_state += 1
        else:
            self._click_state = 1
        self._last_click_t, self._last_click_xy = now, (self._x, self._y)
        return self._click_state

    def left_down(self):
        self.down = True
        self._post(self.Q.kCGEventLeftMouseDown, self.Q.kCGMouseButtonLeft,
                   self._next_click_state())

    def left_up(self):
        self.down = False
        self._post(self.Q.kCGEventLeftMouseUp, self.Q.kCGMouseButtonLeft, self._click_state)

    def right_click(self):
        Q = self.Q
        self._post(Q.kCGEventRightMouseDown, Q.kCGMouseButtonRight)
        self._post(Q.kCGEventRightMouseUp, Q.kCGMouseButtonRight)

    def scroll(self, px):
        self._acc += px
        n = int(self._acc)
        if n:
            self._acc -= n
            ev = self.Q.CGEventCreateScrollWheelEvent(None, self.Q.kCGScrollEventUnitPixel, 1, n)
            self.Q.CGEventPost(self.Q.kCGHIDEventTap, ev)


class PynputMouse:
    """Linux (X11) and fallback."""
    PX_PER_NOTCH = 55.0

    def __init__(self, cfg: Config):
        from pynput.mouse import Button, Controller
        self.m, self.B = Controller(), Button
        self.w, self.h = self._detect_size()
        self._acc = 0.0

    @staticmethod
    def _detect_size():
        try:
            from Xlib import display
            s = display.Display().screen()
            return s.width_in_pixels, s.height_in_pixels
        except Exception:
            pass
        try:
            import tkinter
            r = tkinter.Tk()
            r.withdraw()
            w, h = r.winfo_screenwidth(), r.winfo_screenheight()
            r.destroy()
            return w, h
        except Exception:
            return 1920, 1080

    def screen_size(self):
        return self.w, self.h

    def get_pos(self):
        x, y = self.m.position
        return float(x), float(y)

    def move(self, x, y):
        self.m.position = (int(x), int(y))

    def left_down(self):
        self.m.press(self.B.left)

    def left_up(self):
        self.m.release(self.B.left)

    def right_click(self):
        self.m.click(self.B.right)

    def scroll(self, px):
        self._acc += px / self.PX_PER_NOTCH
        n = int(self._acc)
        if n:
            self._acc -= n
            self.m.scroll(0, n)


def make_mouse(cfg: Config):
    if IS_WIN:
        return WindowsMouse(cfg)
    if IS_MAC:
        try:
            return MacMouse(cfg)
        except Exception as e:
            print("[!] Quartz unavailable (%s), falling back to pynput" % e)
    return PynputMouse(cfg)


# =============================================================================
#  OUTPUT THREAD - 240 Hz smooth cursor and scroll
# =============================================================================
class Output(threading.Thread):
    def __init__(self, mouse, cfg: Config):
        super().__init__(daemon=True, name="output")
        self.mouse, self.cfg = mouse, cfg
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.cmds: deque = deque()
        self.running = True
        self.active = False
        self.target = None
        self.lead = None
        self.vel = np.zeros(2)
        self.pub_t = 0.0
        self.frame_dt = 1 / 30
        self.cur = None
        self.last_sent = None
        self.button = False
        self.s_drive = 0.0
        self.s_drive_t = -1.0
        self.s_vel = 0.0

    # ---- API used by the tracking thread ----
    def set_active(self, on: bool):
        with self.lock:
            if on and not self.active:
                self.cmds.append(("activate",))
            self.active = on
            if not on:
                self.target = None
        self.wake.set()

    def deactivate_at(self, xy):
        """Put the cursor back at xy and stop driving it."""
        with self.lock:
            if self.active and xy is not None:
                self.cmds.append(("snap", float(xy[0]), float(xy[1])))
            self.active = False
            self.target = None
        self.wake.set()

    def set_target(self, xy, vel, lead, frame_dt):
        with self.lock:
            self.target = np.array(xy, dtype=float)
            self.vel = np.array(vel, dtype=float)
            self.lead = np.array(lead, dtype=float)
            self.pub_t = time.perf_counter()
            self.frame_dt = min(0.1, max(1 / 120, frame_dt))

    def left_down(self, xy):
        with self.lock:
            self.cmds.append(("snap", float(xy[0]), float(xy[1])))
            self.cmds.append(("ldown",))
        self.wake.set()

    def left_up(self):
        with self.lock:
            self.cmds.append(("lup",))
        self.wake.set()

    def right_click(self, xy):
        with self.lock:
            self.cmds.append(("snap", float(xy[0]), float(xy[1])))
            self.cmds.append(("rclick",))
        self.wake.set()

    def drive_scroll(self, v):
        m = self.cfg.scroll_max
        with self.lock:
            self.s_drive = float(min(m, max(-m, v)))
            self.s_drive_t = time.perf_counter()
        self.wake.set()

    def stop_scroll(self):
        with self.lock:
            self.s_drive, self.s_drive_t, self.s_vel = 0.0, -1.0, 0.0

    def cursor_pos(self):
        with self.lock:
            if self.cur is not None:
                return self.cur.copy()
        return np.array(self.mouse.get_pos(), dtype=float)

    def scroll_velocity(self):
        return self.s_vel

    def stop(self):
        self.running = False
        self.wake.set()

    # ---- loop ----
    def run(self):
        raise_thread_priority()
        cfg = self.cfg
        period = 1.0 / cfg.output_hz
        last = time.perf_counter()
        next_t = last
        while self.running:
            busy = False
            try:
                now = time.perf_counter()
                dt = min(0.05, max(1e-4, now - last))
                last = now
                with self.lock:
                    cmds = list(self.cmds)
                    self.cmds.clear()
                    active = self.active
                    target = None if self.target is None else self.target.copy()
                    lead = None if self.lead is None else self.lead.copy()
                    vel, pub_t, fdt = self.vel.copy(), self.pub_t, self.frame_dt
                    s_drive, s_drive_t = self.s_drive, self.s_drive_t

                for c in cmds:
                    self._exec(c)

                # cursor: extrapolate between frames, but never past where the
                # finger really is (no overshoot when you stop)
                if active and target is not None and self.cur is not None:
                    busy = True
                    ahead = min(now - pub_t, 1.2 * fdt) * cfg.prediction
                    ext = vel * ahead
                    if lead is not None:
                        lim = float(np.linalg.norm(lead - target))
                        n = float(np.linalg.norm(ext))
                        if n > lim:
                            ext *= lim / max(n, 1e-9)
                    goal = target + ext
                    a = 1.0 - math.exp(-dt / max(1e-4, cfg.output_tau))
                    self.cur = self.cur + (goal - self.cur) * a
                    self._send(self.cur)

                # scroll: follow the hand quickly, decay slowly (inertia)
                fresh = (now - s_drive_t) < 0.09
                if fresh and (s_drive * self.s_vel < 0 or abs(s_drive) > abs(self.s_vel)):
                    self.s_vel += (s_drive - self.s_vel) * (1.0 - math.exp(-dt / 0.03))
                elif cfg.scroll_inertia > 0:
                    self.s_vel *= math.exp(-dt / cfg.scroll_inertia)
                else:
                    self.s_vel = s_drive if fresh else 0.0
                if abs(self.s_vel) < 4.0 and not fresh:
                    self.s_vel = 0.0
                if self.s_vel:
                    busy = True
                    self.mouse.scroll(self.s_vel * dt)
            except Exception as e:  # the output thread must never die
                print("[output] error:", e)
                time.sleep(0.05)

            if busy:
                next_t += period
                delay = next_t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.perf_counter()
            else:  # nothing to move: sleep until woken, no CPU burn
                self.wake.wait(0.05)
                self.wake.clear()
                next_t = time.perf_counter()
                last = next_t
        if self.button:
            try:
                self.mouse.left_up()
            except Exception:
                pass

    def _send(self, p):
        w, h = self.mouse.screen_size()
        ix = int(round(min(max(p[0], 0), w - 1)))
        iy = int(round(min(max(p[1], 0), h - 1)))
        if (ix, iy) != self.last_sent:
            self.mouse.move(ix, iy)
            self.last_sent = (ix, iy)

    def _exec(self, c):
        k = c[0]
        if k == "activate":
            self.cur = np.array(self.mouse.get_pos(), dtype=float)
            self.last_sent = None
        elif k == "snap":
            self.cur = np.array([c[1], c[2]], dtype=float)
            self._send(self.cur)
        elif k == "ldown":
            if not self.button:
                self.mouse.left_down()
                self.button = True
        elif k == "lup":
            if self.button:
                self.mouse.left_up()
                self.button = False
        elif k == "rclick":
            self.mouse.right_click()


# =============================================================================
#  GESTURES
# =============================================================================
NONE, POINT, POINT2, PALM, FIST, OTHER = range(6)
POSE_NAMES = {NONE: "no hand", POINT: "POINT", POINT2: "TWO FINGERS",
              PALM: "PALM / SCROLL", FIST: "FIST", OTHER: "-"}
POINTER_POSES = (POINT, POINT2)

FINGERS = ((8, 6), (12, 10), (16, 14), (20, 18))   # (tip, PIP joint): index..pinky
EXT_ON, EXT_OFF = 1.20, 1.08                       # hysteresis: extended / curled


def _d(a, b):
    return float(math.hypot(a[0] - b[0], a[1] - b[1]))


def finger_ratios(p):
    w = p[0]
    return [_d(w, p[tip]) / max(_d(w, p[pip]), 1e-6) for tip, pip in FINGERS]


def finger_states(p, prev=None):
    """Is each finger extended? (with hysteresis so it doesn't flicker)."""
    out = []
    for i, r in enumerate(finger_ratios(p)):
        if r > EXT_ON:
            out.append(True)
        elif r < EXT_OFF:
            out.append(False)
        else:
            out.append(prev[i] if prev is not None else r > 0.5 * (EXT_ON + EXT_OFF))
    return out


def classify_states(s):
    idx, mid, ring, pinky = s
    n = idx + mid + ring + pinky
    if idx and not mid and not ring and not pinky:
        return POINT
    if idx and mid and not ring and not pinky:
        return POINT2
    if n >= 3:
        return PALM
    if n == 0:
        return FIST
    return OTHER


def classify(p):
    return classify_states(finger_states(p))


def palm_size(p):
    # robust to wrist rotation: max of palm length and (scaled) palm width
    return max(_d(p[0], p[9]), 1.3 * _d(p[5], p[17]), 1e-3)


class Engine:
    """Takes 21 hand landmarks per frame and decides what the mouse does."""

    def __init__(self, cfg: Config, out: Output, screen_w: int, screen_h: int):
        self.cfg, self.out = cfg, out
        self.sw, self.sh = screen_w, screen_h
        self.f_ptr = OneEuro(cfg.min_cutoff, cfg.beta, cfg.d_cutoff)
        self.f_palm = OneEuro(1.2, 3.0, 1.0)
        self.palm_s = None
        self.fstate = None
        self.noise = 0.0                   # estimated fingertip noise, palm lengths
        self.pose = NONE
        self.cand, self.cand_n, self.cand_t = NONE, 0, 0.0
        self.last_seen = -1.0
        self.paused = False
        self.last_click_t, self.last_click_xy = -10.0, np.zeros(2)
        # scroll memory survives short tracking dropouts
        self.sc_last_dir, self.sc_last_peak, self.sc_last_t = 0, 0.0, -10.0
        self._reset_pointer()
        self._reset_scroll()
        self.status = {}

    # ---- helpers ----
    def _reset_pointer(self):
        self.f_ptr.reset()
        self.prev_f = None
        self.prev_t = None
        self.target = None
        self.vel = np.zeros(2)
        self.hist: deque = deque(maxlen=24)
        self.rope = None
        self.anchor_f = None
        self.anchor_s = None
        self.ref_palm = None
        self.offset = np.zeros(2)
        self.holding = False
        self.still_since = None
        self.frozen = False
        self.freeze_t = 0.0
        self.frozen_motion = 0.0
        self.pinched = False
        self.pinch_t = 0.0
        self.button = False
        self.dragging = False
        self.prev_r = None
        self.dr = 0.0
        self.r = 1.0
        self.r_base = None

    def _reset_scroll(self):
        self.f_palm.reset()
        self.prev_c = None
        self.prev_ct = None
        self.anchor_y = None
        self.stroke_dir = 0
        self.stroke_peak = 0.0
        self.stroke_return = False
        self.stroke_last_t = -10.0
        self.scroll_state = ""

    def release_all(self):
        if self.button:
            self.out.left_up()
        self.out.set_active(False)
        self.out.stop_scroll()
        self._reset_pointer()
        self._reset_scroll()
        self.pose = NONE
        self.cand, self.cand_n = NONE, 0

    def set_paused(self, p: bool):
        self.paused = p
        if p:
            self.release_all()

    def _set_pose(self, new, t):
        old = self.pose
        if new == old:
            return
        if old in POINTER_POSES and new not in POINTER_POSES:
            if self.button:
                self.out.left_up()
            # curling the finger moves its tip, so put the cursor back where it
            # was before the gesture change started
            self.out.deactivate_at(self._hist_at(self.cand_t - 0.08))
            self._reset_pointer()
        if new in POINTER_POSES and old not in POINTER_POSES:
            self.out.stop_scroll()
            self._reset_pointer()
            self.out.set_active(True)
        if new == PALM:
            self._reset_scroll()
        if new == FIST:
            self.out.stop_scroll()
        self.pose = new

    # ---- main entry ----
    def update(self, lm, t, fw, fh):
        """lm: (21, 2) array in normalized frame coords, or None."""
        cfg = self.cfg
        if self.paused:
            self.status = {"pose": "PAUSED"}
            return self.status
        if lm is None:
            grace = cfg.lost_grace * (2.5 if self.pose == PALM else 1.0)
            if self.pose != NONE and t - self.last_seen > grace:
                self._set_pose(NONE, t)
            self.status = {"pose": "no hand" if self.pose == NONE else "lost...",
                           "scroll": self.out.scroll_velocity()}
            return self.status
        self.last_seen = t

        p = np.asarray(lm, dtype=float)[:, :2] * (fw, fh)
        palm = palm_size(p)
        if self.palm_s is None:
            self.palm_s = palm
        else:
            self.palm_s += (palm - self.palm_s) * 0.15

        self.fstate = finger_states(p, self.fstate)
        raw = classify_states(self.fstate)
        if self.button or self.pinched:
            raw = self.pose                       # never switch gesture mid-pinch
        if raw == self.cand:
            self.cand_n += 1
        else:
            self.cand, self.cand_n, self.cand_t = raw, 1, t
        need = cfg.pose_frames
        if self.frozen or self.pose == PALM:
            need *= 2                              # leave scroll mode reluctantly
        if self.pose == NONE and raw in POINTER_POSES:
            need = 1
        if self.cand_n >= need:
            self._set_pose(self.cand, t)

        if self.pose in POINTER_POSES:
            self._pointer(p, palm, t, fw, fh)
        elif self.pose == PALM:
            self._scroll(p, t)

        self.status = {
            "pose": POSE_NAMES[self.pose], "pinch": self.r, "frozen": self.frozen,
            "pinched": self.pinched, "drag": self.dragging,
            "scroll": self.out.scroll_velocity(), "scroll_state": self.scroll_state,
        }
        return self.status

    # ---- cursor ----
    def _gain(self):
        return self.cfg.gain * self.cfg.speed * self.sw / 1920.0 / self.ref_palm

    def _map(self, pt, fw, fh):
        if self.cfg.mode == "absolute":
            return self._abs_map(pt, fw, fh) + self.offset
        return self.anchor_s + (pt - self.anchor_f) * self._gain()

    def _anchor(self, fw, fh):
        """Bind the current finger position to the current cursor position."""
        self.anchor_f = self.rope.copy()
        self.anchor_s = self.target.copy()
        self.ref_palm = self.palm_s
        if self.cfg.mode == "absolute":
            self.offset = self.target - self._abs_map(self.rope, fw, fh)

    def _pointer(self, p, palm, t, fw, fh):
        cfg = self.cfg
        k = cfg.track_tip_weight
        tp = k * p[8] + (1.0 - k) * p[7]
        f = self.f_ptr(tp, t, scale=self.palm_s)

        # estimate camera noise while the hand is nearly still
        if self.f_ptr.dx is not None and np.linalg.norm(self.f_ptr.dx) / self.palm_s < 0.5:
            res = float(np.linalg.norm(tp - f)) / self.palm_s
            self.noise += (res - self.noise) * 0.05

        # "rope" deadband: the point only follows once the finger moves past a radius
        if self.rope is None:
            self.rope = f.copy()
        dv = f - self.rope
        dn = float(np.linalg.norm(dv))
        rad = max(cfg.deadband, cfg.deadband_auto * self.noise) * self.palm_s
        rope_moved = dn > rad
        if rope_moved:
            self.rope = f - dv * (rad / dn)

        # pinch ratio (for right click the middle finger counts too)
        r = _d(p[4], p[8]) / palm
        if self.pose == POINT2:
            r = min(r, _d(p[4], p[12]) / palm)
        self.r = r

        if self.prev_t is None:              # first frame in pointer mode
            self.prev_f, self.prev_t, self.prev_r = f, t, r
            self.r_base = max(r, cfg.freeze_ratio)
            if cfg.mode == "absolute":
                self.target = self._abs_map(self.rope, fw, fh)
            else:
                self.target = self.out.cursor_pos()
            self._anchor(fw, fh)
            self.hist.append((t, self.target.copy()))
            self.out.set_target(self.target, (0, 0), self.target, 1 / 30)
            return

        dt = max(1e-3, t - self.prev_t)
        d_pu = float(np.linalg.norm(f - self.prev_f)) / self.palm_s
        self.prev_f, self.prev_t = f, t
        self.dr = 0.5 * self.dr + 0.5 * (r - self.prev_r) / dt
        self.prev_r = r
        if not self.frozen and not self.pinched and r > cfg.freeze_ratio:
            self.r_base += (r - self.r_base) * 0.1
        prev_target = self.target.copy()

        # hold the cursor while pinching or while the hand changes gesture
        pending = self.cand != self.pose and self.cand not in POINTER_POSES
        hold = self.frozen or pending
        lead = self.target
        if hold:
            self.frozen_motion += d_pu
            self.holding = True
        else:
            if self.holding:                  # resume from the same spot, no jump
                self.holding = False
                self._anchor(fw, fh)
            if cfg.mode == "absolute":
                self.offset *= math.exp(-dt / 0.3)
            m = self._map(self.rope, fw, fh)
            c = np.clip(m, (0, 0), (self.sw - 1, self.sh - 1))
            if cfg.mode != "absolute" and np.any(c != m):
                self.anchor_s = self.anchor_s + (c - m)   # hit the screen edge
            self.target = c
            lead = np.clip(self._map(tp, fw, fh), (0, 0), (self.sw - 1, self.sh - 1))
            # hand at rest: silently re-anchor (adapts if you lean back or forward)
            if rope_moved:
                self.still_since = None
            elif self.still_since is None:
                self.still_since = t
            elif t - self.still_since > 0.6 and cfg.mode != "absolute":
                self._anchor(fw, fh)
                self.still_since = t

        self._pinch_logic(r, t)

        self.hist.append((t, self.target.copy()))
        if self.frozen or hold:
            self.vel[:] = 0
            lead = self.target
        else:
            v = (self.target - prev_target) / dt
            self.vel = 0.4 * self.vel + 0.6 * v
        self.out.set_target(self.target, self.vel, lead, dt)

    def _hist_at(self, t):
        pos = self.target
        for ht, hp in reversed(self.hist):
            pos = hp
            if ht <= t:
                break
        return None if pos is None else pos.copy()

    def _abs_map(self, f, fw, fh):
        c = self.cfg
        nx = (f[0] / fw - (c.zone_cx - c.zone_w / 2)) / c.zone_w
        ny = (f[1] / fh - (c.zone_cy - c.zone_h / 2)) / c.zone_h
        nx, ny = min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)
        return np.array([nx * (self.sw - 1), ny * (self.sh - 1)])

    def _freeze(self, t):
        if self.frozen:
            return
        self.frozen, self.freeze_t, self.frozen_motion = True, t, 0.0
        self.target = self._hist_at(t - self.cfg.freeze_rewind)  # where you were aiming

    def _pinch_logic(self, r, t):
        cfg = self.cfg
        if not self.pinched:
            if r < cfg.pinch_on:
                self._freeze(t)
                self.pinched, self.pinch_t, self.frozen_motion = True, t, 0.0
                self.freeze_t = t
                pos = self.target
                # second click of a double click lands on exactly the same pixel
                if (t - self.last_click_t < cfg.dbl_click_time and
                        np.linalg.norm(pos - self.last_click_xy) < cfg.dbl_click_radius):
                    pos = self.last_click_xy.copy()
                    self.target = pos.copy()
                self.last_click_t, self.last_click_xy = t, pos.copy()
                if self.pose == POINT:
                    self.out.left_down(pos)
                    self.button, self.dragging = True, False
                else:
                    self.out.right_click(pos)
            elif not self.frozen and r < cfg.freeze_ratio and self.dr < -cfg.freeze_speed:
                self._freeze(t)
            elif self.frozen and ((r > max(cfg.freeze_ratio + 0.05, cfg.unfreeze_frac * self.r_base)
                                   and abs(self.dr) < 0.6)
                                  or t - self.freeze_t > cfg.freeze_max):
                self.frozen = False
        else:
            if r > cfg.pinch_off:
                self.pinched = False
                if self.button:
                    self.out.left_up()
                    self.button = False
                if self.dragging:
                    self.dragging = False
                else:
                    self.freeze_t = t   # keep holding while the fingers open
            elif self.button and not self.dragging and (
                    t - self.pinch_t > cfg.drag_delay or self.frozen_motion > cfg.drag_move):
                self.dragging = True
                self.frozen = False

    # ---- scroll ----
    def _scroll(self, p, t):
        cfg = self.cfg
        c = p[[0, 5, 9, 13, 17]].mean(axis=0)
        fc = self.f_palm(c, t, scale=self.palm_s)
        sign = 1.0 if cfg.scroll_natural else -1.0
        if self.prev_c is None or t - self.prev_ct > 0.3:
            self.prev_c, self.prev_ct, self.anchor_y = fc, t, fc[1]
            return
        dt = max(1e-3, t - self.prev_ct)
        vy = (fc[1] - self.prev_c[1]) / dt / self.palm_s       # palm lengths/s, + = down
        self.prev_c, self.prev_ct = fc, t

        if cfg.scroll_style == "joystick":
            off = (fc[1] - self.anchor_y) / self.palm_s
            mag = max(0.0, abs(off) - cfg.joystick_deadzone)
            self.out.drive_scroll(sign * math.copysign(mag, off) * cfg.joystick_gain)
            self.scroll_state = "joystick %+.2f" % off
            return

        speed = abs(vy)
        if speed > cfg.scroll_deadzone:
            d = 1 if vy > 0 else -1
            if self.stroke_dir != d:          # a new stroke begins
                self.stroke_dir, self.stroke_peak = d, 0.0
                # moving back soon after a swipe = returning the hand: don't scroll
                self.stroke_return = (d == -self.sc_last_dir and
                                      t - self.sc_last_t < cfg.scroll_return_window)
            self.stroke_peak = max(self.stroke_peak, speed)
            self.stroke_last_t = t
            self.sc_last_t = t                # the window extends while the hand moves
            if self.stroke_return and speed > cfg.scroll_reverse_ratio * self.sc_last_peak:
                self.stroke_return = False    # a fast swipe back is intentional
                self.out.stop_scroll()
            if self.stroke_return:
                self.scroll_state = "return (ignored)"
            else:
                s = sign * vy * cfg.scroll_gain * (1.0 + cfg.scroll_accel * speed)
                self.out.drive_scroll(s)
                self.sc_last_dir = d
                self.sc_last_peak = max(self.stroke_peak, 0.5 * self.sc_last_peak)
                self.scroll_state = "scroll " + ("down" if s < 0 else "up")
        elif self.stroke_dir and t - self.stroke_last_t > 0.08:
            self.stroke_dir = 0
            self.stroke_return = False
            self.scroll_state = ""


# =============================================================================
#  CAMERA - own thread, newest frame only, auto-reconnect
# =============================================================================
class Camera(threading.Thread):
    def __init__(self, cfg: Config):
        super().__init__(daemon=True, name="camera")
        import cv2
        self.cv2, self.cfg = cv2, cfg
        self.is_file = not str(cfg.camera).isdigit()
        self.cap = self._open()
        if self.cap is None:
            raise RuntimeError("Could not open camera %s (is another app using it?)" % cfg.camera)
        self.cond = threading.Condition()
        self.frame, self.t, self.seq = None, 0.0, 0
        self.running = True
        self.fps = 0.0

    def _open(self):
        cv2, cfg = self.cv2, self.cfg
        if self.is_file:
            cap = cv2.VideoCapture(str(cfg.camera))
            return cap if cap.isOpened() else None
        if IS_WIN:
            apis = [cv2.CAP_DSHOW, cv2.CAP_MSMF]
        elif IS_MAC:
            apis = [cv2.CAP_AVFOUNDATION]
        else:
            apis = [cv2.CAP_V4L2]
        apis.append(cv2.CAP_ANY)
        for api in apis:
            try:
                cap = cv2.VideoCapture(int(cfg.camera), api)
            except Exception:
                continue
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.cam_width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.cam_height)
                cap.set(cv2.CAP_PROP_FPS, cfg.cam_fps)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ok, _ = cap.read()
                if ok:
                    return cap
            cap.release()
        return None

    def run(self):
        last = None
        fails = 0
        while self.running:
            ok, fr = self.cap.read()
            if self.is_file:
                if not ok:                       # loop the video
                    self.cap.set(self.cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                time.sleep(1 / 30)
            now = time.perf_counter()
            if not ok or fr is None:
                fails += 1
                if fails > 30:                   # camera dropped out: reconnect
                    print("[camera] no frames, reconnecting...")
                    try:
                        self.cap.release()
                    except Exception:
                        pass
                    cap = None
                    while self.running and cap is None:
                        cap = self._open()
                        if cap is None:
                            time.sleep(1.0)
                    if cap is not None:
                        self.cap = cap
                        print("[camera] back online")
                    fails = 0
                else:
                    time.sleep(0.01)
                continue
            fails = 0
            if last is not None:
                inst = 1.0 / max(1e-3, now - last)
                self.fps = inst if self.fps == 0 else 0.9 * self.fps + 0.1 * inst
            last = now
            with self.cond:
                self.frame, self.t = fr, now
                self.seq += 1
                self.cond.notify_all()

    def latest(self, after_seq, timeout=0.5):
        with self.cond:
            self.cond.wait_for(lambda: self.seq > after_seq or not self.running, timeout)
            return self.frame, self.t, self.seq

    def stop(self):
        self.running = False
        with self.cond:
            self.cond.notify_all()


# =============================================================================
#  HAND TRACKER (MediaPipe Tasks)
# =============================================================================
def ensure_model():
    if os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 1_000_000:
        return MODEL_PATH
    print("Downloading hand model (~8 MB)...")
    tmp = MODEL_PATH + ".part"
    urllib.request.urlretrieve(MODEL_URL, tmp)
    os.replace(tmp, MODEL_PATH)
    return MODEL_PATH


class HandTracker:
    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision
        self.mp = mp
        opts = vision.HandLandmarkerOptions(
            base_options=mpp.BaseOptions(model_asset_path=ensure_model()),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.6,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.lm = vision.HandLandmarker.create_from_options(opts)
        self.last_ts = -1

    def detect(self, rgb, t):
        ts = int(t * 1000)
        if ts <= self.last_ts:
            ts = self.last_ts + 1
        self.last_ts = ts
        img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        res = self.lm.detect_for_video(img, ts)
        if not res.hand_landmarks:
            return None
        return np.array([(q.x, q.y) for q in res.hand_landmarks[0]], dtype=float)

    def close(self):
        try:
            self.lm.close()
        except Exception:
            pass


# =============================================================================
#  TRACKING THREAD - runs independently of the preview window
# =============================================================================
class Processor(threading.Thread):
    def __init__(self, cfg, cam, tracker, engine):
        super().__init__(daemon=True, name="tracking")
        self.cfg, self.cam, self.tracker, self.engine = cfg, cam, tracker, engine
        self.running = True
        self.lock = threading.Lock()
        self.view = None            # (frame, landmarks, status) for the preview
        self.view_seq = 0
        self.proc_ms = 0.0
        self.want_view = cfg.preview

    def run(self):
        import cv2
        seq = 0
        err_t = 0.0
        while self.running:
            frame, t_cap, s = self.cam.latest(seq)
            if frame is None or s == seq:
                continue
            seq = s
            try:
                t0 = time.perf_counter()
                fh, fw = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                lm = self.tracker.detect(rgb, t_cap)
                if lm is not None and self.cfg.mirror:
                    lm[:, 0] = 1.0 - lm[:, 0]       # mirror the points, not the image
                st = self.engine.update(lm, t_cap, fw, fh)
                ms = (time.perf_counter() - t0) * 1000
                self.proc_ms = ms if self.proc_ms == 0 else 0.9 * self.proc_ms + 0.1 * ms
                if self.want_view:
                    with self.lock:
                        self.view = (frame, lm, dict(st))
                        self.view_seq += 1
            except Exception:
                now = time.perf_counter()
                if now - err_t > 2.0:             # don't spam, don't die
                    err_t = now
                    print("[tracking] error (still running):")
                    traceback.print_exc()


# =============================================================================
#  HOTKEYS
# =============================================================================
def start_hotkeys(actions):
    """actions: {'m': fn, 'v': fn, 'q': fn} -> Ctrl+Alt+<letter>."""
    if IS_WIN:
        # RegisterHotKey: system hotkeys without a keyboard hook (no input lag)
        def loop():
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            ids = {}
            for i, (ch, fn) in enumerate(actions.items(), start=1):
                if u.RegisterHotKey(None, i, 0x0002 | 0x0001 | 0x4000, ord(ch.upper())):
                    ids[i] = fn
                else:
                    print("[i] Ctrl+Alt+%s is taken by another app" % ch.upper())
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312 and msg.wParam in ids:
                    try:
                        ids[msg.wParam]()
                    except Exception as e:
                        print("[hotkey] error:", e)
        threading.Thread(target=loop, daemon=True, name="hotkeys").start()
        return
    try:
        from pynput import keyboard
        hk = keyboard.GlobalHotKeys({"<ctrl>+<alt>+" + ch: fn for ch, fn in actions.items()})
        hk.daemon = True
        hk.start()
    except Exception as e:
        print("[i] Global hotkeys unavailable:", e)


# =============================================================================
#  PREVIEW
# =============================================================================
HAND_EDGES = ((0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
              (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
              (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17))


def draw_hud(cv2, img, lm, st, cfg, cam_fps, proc_ms, paused):
    h, w = img.shape[:2]
    if cfg.mode == "absolute":
        x0 = int((cfg.zone_cx - cfg.zone_w / 2) * w)
        y0 = int((cfg.zone_cy - cfg.zone_h / 2) * h)
        cv2.rectangle(img, (x0, y0), (x0 + int(cfg.zone_w * w), y0 + int(cfg.zone_h * h)),
                      (255, 200, 0), 1)
    if lm is not None:
        pts = [(int(x * w), int(y * h)) for x, y in lm[:, :2]]
        for a, b in HAND_EDGES:
            cv2.line(img, pts[a], pts[b], (200, 200, 200), 1, cv2.LINE_AA)
        for i, q in enumerate(pts):
            cv2.circle(img, q, 3, (0, 255, 0) if i in (4, 8) else (0, 160, 255), -1)
        if st.get("pose") in (POSE_NAMES[POINT], POSE_NAMES[POINT2]):
            col = (0, 0, 255) if st.get("pinched") else (
                (0, 255, 255) if st.get("frozen") else (0, 255, 0))
            cv2.line(img, pts[4], pts[8], col, 2, cv2.LINE_AA)
    cv2.rectangle(img, (0, 0), (w, 54), (0, 0, 0), -1)
    pose = "PAUSED (Ctrl+Alt+M)" if paused else st.get("pose", "")
    extra = ""
    if st.get("drag"):
        extra = "  DRAG"
    elif st.get("pinched"):
        extra = "  CLICK"
    elif st.get("frozen"):
        extra = "  aim-lock"
    elif st.get("scroll_state"):
        extra = "  " + st["scroll_state"]
    cv2.putText(img, pose + extra, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    info = "cam %2.0f fps | track %4.1f ms | %s x%.2f" % (cam_fps, proc_ms, cfg.mode, cfg.speed)
    if "pinch" in st and st.get("pose") in (POSE_NAMES[POINT], POSE_NAMES[POINT2]):
        info += " | pinch %.2f" % st["pinch"]
    cv2.putText(img, info, (8, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 255, 180), 1)
    if cam_fps and cam_fps < 20:
        cv2.putText(img, "LOW CAMERA FPS: add light!", (8, h - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return img


# =============================================================================
#  MAIN
# =============================================================================
def parse_args(argv=None):
    c = Config()
    ap = argparse.ArgumentParser(description="AirMouse - control your computer with hand gestures")
    ap.add_argument("--camera", default=c.camera, help="camera index (0, 1, ...) or a video file")
    ap.add_argument("--mode", choices=["follow", "absolute"], default=c.mode,
                    help="follow = cursor repeats your finger's motion (default), "
                         "absolute = finger points at a spot on the screen")
    ap.add_argument("--speed", type=float, default=c.speed, help="cursor speed multiplier")
    ap.add_argument("--smooth", type=float, default=1.0,
                    help=">1 steadier but slightly slower, <1 snappier")
    ap.add_argument("--scroll", choices=["swipe", "joystick"], default=c.scroll_style)
    ap.add_argument("--scroll-speed", type=float, default=1.0, help="scroll speed multiplier")
    ap.add_argument("--invert-scroll", action="store_true",
                    help="palm up = scroll up (like a mouse wheel)")
    ap.add_argument("--wheel-steps", action="store_true",
                    help="scroll in whole wheel notches (if an app scrolls weirdly)")
    ap.add_argument("--no-preview", action="store_true", help="start without the camera window")
    ap.add_argument("--width", type=int, default=c.cam_width)
    ap.add_argument("--height", type=int, default=c.cam_height)
    ap.add_argument("--fps", type=int, default=c.cam_fps)
    ap.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)  # used by launcher.py
    a = ap.parse_args(argv)
    c.camera, c.mode, c.speed, c.scroll_style = str(a.camera), a.mode, a.speed, a.scroll
    c.scroll_gain *= a.scroll_speed
    c.joystick_gain *= a.scroll_speed
    c.scroll_natural = not a.invert_scroll
    c.wheel_steps = a.wheel_steps
    c.preview = not a.no_preview
    c.cam_width, c.cam_height, c.cam_fps = a.width, a.height, a.fps
    if a.smooth > 0:
        c.min_cutoff /= a.smooth
        c.deadband *= a.smooth
    return c, a.managed


def main(argv=None):
    cfg, managed = parse_args(argv)
    import cv2
    windows_tuning()

    mouse = make_mouse(cfg)
    sw, sh = mouse.screen_size()
    print("Screen: %dx%d" % (sw, sh))
    tracker = HandTracker()
    cam = Camera(cfg)
    cam.start()
    out = Output(mouse, cfg)
    out.start()
    engine = Engine(cfg, out, sw, sh)
    proc = Processor(cfg, cam, tracker, engine)
    proc.start()

    state = {"quit": False, "preview": cfg.preview, "shown": False}

    def toggle_pause():
        engine.set_paused(not engine.paused)
        print("Paused" if engine.paused else "Running")

    def toggle_preview():
        state["preview"] = not state["preview"]

    def do_quit():
        state["quit"] = True

    if managed:
        # launcher.py talks to us through stdin: "quit" (or a closed pipe) = exit
        def stdin_loop():
            try:
                for line in sys.stdin:
                    if line.strip() == "quit":
                        break
            except Exception:
                pass
            do_quit()
        threading.Thread(target=stdin_loop, daemon=True, name="stdin").start()

    start_hotkeys({"m": toggle_pause, "v": toggle_preview, "q": do_quit})
    print("AirMouse is running.")
    print("  Ctrl+Alt+M pause | Ctrl+Alt+V camera window | Ctrl+Alt+Q quit")
    print("  You can close the camera window - AirMouse keeps working.")

    win = "AirMouse"
    last_seq = 0
    try:
        while not state["quit"]:
            want = state["preview"]
            proc.want_view = want
            if not want:
                if state["shown"]:
                    cv2.destroyAllWindows()
                    cv2.waitKey(1)
                    state["shown"] = False
                time.sleep(0.05)
                continue

            with proc.lock:
                v, vs = proc.view, proc.view_seq
            if v is None or vs == last_seq:
                cv2.waitKey(5)          # keep the window responsive
                if state["shown"] and not _window_alive(cv2, win):
                    _on_window_closed(state)
                continue
            last_seq = vs
            frame, lm, st = v
            img = cv2.flip(frame, 1) if cfg.mirror else frame.copy()
            draw_hud(cv2, img, lm, st, cfg, cam.fps, proc.proc_ms, engine.paused)
            cv2.imshow(win, img)
            state["shown"] = True
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            elif key == ord("p"):
                toggle_pause()
            elif key == ord("h"):
                _on_window_closed(state)
            elif key == ord("c") and lm is not None:
                cfg.zone_cx, cfg.zone_cy = float(lm[8, 0]), float(lm[8, 1])
            elif key in (ord("+"), ord("=")):
                cfg.speed *= 1.15
                print("Speed: %.2f" % cfg.speed)
            elif key == ord("-"):
                cfg.speed /= 1.15
                print("Speed: %.2f" % cfg.speed)
            if not _window_alive(cv2, win):
                _on_window_closed(state)
    except KeyboardInterrupt:
        pass
    finally:
        proc.running = False
        engine.release_all()
        time.sleep(0.05)
        out.stop()
        cam.stop()
        proc.join(timeout=1.0)
        tracker.close()
        try:
            cam.cap.release()
        except Exception:
            pass
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        print("Bye.")


def _window_alive(cv2, win):
    try:
        return cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) >= 1
    except Exception:
        return False


def _on_window_closed(state):
    state["preview"] = False
    print("Camera window hidden - AirMouse keeps running. "
          "Ctrl+Alt+V to show it again, Ctrl+Alt+Q to quit.")


if __name__ == "__main__":
    main()
