import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from airmouse import TapDetector  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make():
    fired = []
    clock = Clock()
    det = TapDetector(lambda: fired.append(clock.t), clock=clock)
    return det, clock, fired


def tap(det, clock, hold=0.05, gap=0.1):
    det.ctrl_down()
    clock.t += hold
    det.ctrl_up()
    clock.t += gap


def test_triple_tap_fires_once():
    det, clock, fired = make()
    for _ in range(3):
        tap(det, clock)
    assert len(fired) == 1


def test_two_taps_do_nothing():
    det, clock, fired = make()
    tap(det, clock)
    tap(det, clock)
    assert fired == []


def test_shortcuts_do_not_count():
    det, clock, fired = make()
    tap(det, clock)
    det.ctrl_down()
    det.other_key()          # Ctrl+C
    det.ctrl_up()
    tap(det, clock)
    tap(det, clock)
    assert fired == []


def test_slow_taps_do_not_count():
    det, clock, fired = make()
    for _ in range(3):
        tap(det, clock, gap=0.6)
    assert fired == []


def test_long_hold_does_not_count():
    det, clock, fired = make()
    tap(det, clock)
    tap(det, clock, hold=0.8)
    tap(det, clock)
    assert fired == []


def test_key_repeat_is_ignored():
    det, clock, fired = make()
    for _ in range(3):
        det.ctrl_down()
        det.ctrl_down()      # OS auto-repeat
        clock.t += 0.05
        det.ctrl_up()
        clock.t += 0.1
    assert len(fired) == 1
