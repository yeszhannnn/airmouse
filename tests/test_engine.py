import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import airmouse as am  # noqa: E402


def test_one_euro_is_steady_at_rest():
    f = am.OneEuro(1.4, 5.0, 1.5)
    rng = np.random.default_rng(0)
    out = [f(np.array([100.0, 100.0]) + rng.normal(0, 1, 2), i / 30, scale=50) for i in range(200)]
    out = np.array(out[50:])
    assert out.std(axis=0).max() < 0.6


def test_one_euro_follows_motion():
    f = am.OneEuro(1.4, 5.0, 1.5)
    for i in range(60):
        y = f(np.array([i * 5.0, 0.0]), i / 30, scale=50)
    assert abs(y[0] - 59 * 5.0) < 15


def test_finger_hysteresis():
    p = np.zeros((21, 2))
    p[6] = (0, -10)              # index PIP
    p[8] = (0, -11.4)            # ratio 1.14: between thresholds
    assert am.finger_states(p, prev=[True, False, False, False])[0] is True
    assert am.finger_states(p, prev=[False, False, False, False])[0] is False
