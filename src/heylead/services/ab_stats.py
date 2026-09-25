"""When one arm of an A/B test has beaten the other, and when it has not yet.

Shared, byte for byte, by heylead-api (app/services/ab_stats.py) and the
client (src/heylead/services/ab_stats.py). A test in each repo hashes this
file and compares the digest with the same constant, so a change on one side
fails the other's suite until it is mirrored. Pure: no imports from either
product, no I/O.

Until 24 Sep 2026 the client's evaluate_ab_tests declared a winner on a fixed
gap, 2 percentage points of reply rate or 3 of acceptance, at 15 invitations
per arm. 3 replies in 10 against 5 in 10 is a 20-point gap and a coin flip
(p = 0.36): it crowned B and completed the test, and every campaign after it
inherited a message that had never been shown to be better.

The rule here is the ordinary one. A two-proportion z-test on the pooled
rate says how surprising the observed gap would be if the arms were the
same; the sample size for the observed effect (two-sided alpha, power) says
whether either arm has seen enough people for that answer to mean anything.
A winner needs both.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

ALPHA = 0.05
POWER = 0.8

_NORMAL = NormalDist()


@dataclass(frozen=True)
class TwoProportion:
    """The test on one pair of arms, with the numbers it was computed from."""

    a_rate: float
    b_rate: float
    z: float
    p: float
    # People each arm needs before the observed gap can be told from noise at
    # ALPHA with POWER. None when the arms are level: no sample size detects
    # an effect of zero.
    min_n: int | None


def two_proportion_test(
    a_success: int, a_n: int, b_success: int, b_n: int,
    *, alpha: float = ALPHA, power: float = POWER,
) -> TwoProportion:
    """Pooled two-proportion z-test of A against B, two-sided.

    ``z`` is positive when A's rate is the higher. An arm with no people has
    a rate of 0 and the test answers p = 1: nothing has been observed.
    """
    a_n, b_n = max(0, int(a_n)), max(0, int(b_n))
    a_success = min(max(0, int(a_success)), a_n)
    b_success = min(max(0, int(b_success)), b_n)
    a_rate = a_success / a_n if a_n else 0.0
    b_rate = b_success / b_n if b_n else 0.0
    if not a_n or not b_n:
        return TwoProportion(a_rate, b_rate, 0.0, 1.0, _min_n(a_rate, b_rate, alpha, power))

    pooled = (a_success + b_success) / (a_n + b_n)
    se = math.sqrt(pooled * (1 - pooled) * (1 / a_n + 1 / b_n))
    if se == 0:
        z, p = 0.0, 1.0
    else:
        z = (a_rate - b_rate) / se
        p = 2 * (1 - _NORMAL.cdf(abs(z)))
    return TwoProportion(a_rate, b_rate, z, p, _min_n(a_rate, b_rate, alpha, power))


def _min_n(a_rate: float, b_rate: float, alpha: float, power: float) -> int | None:
    """Per-arm sample size to detect a_rate against b_rate (two-sided)."""
    effect = abs(a_rate - b_rate)
    if effect == 0:
        return None
    z_alpha = _NORMAL.inv_cdf(1 - alpha / 2)
    z_power = _NORMAL.inv_cdf(power)
    mean = (a_rate + b_rate) / 2
    numerator = (
        z_alpha * math.sqrt(2 * mean * (1 - mean))
        + z_power * math.sqrt(a_rate * (1 - a_rate) + b_rate * (1 - b_rate))
    )
    return math.ceil(numerator ** 2 / effect ** 2)


def winner(
    a_success: int, a_n: int, b_success: int, b_n: int,
    *, alpha: float = ALPHA, power: float = POWER,
) -> str | None:
    """"A" or "B" when the gap passes the test and both arms met min_n; else None.

    None means keep running, not "inconclusive": an underpowered test that is
    stopped early answers a question nobody asked.
    """
    t = two_proportion_test(a_success, a_n, b_success, b_n, alpha=alpha, power=power)
    if t.min_n is None or t.p >= alpha:
        return None
    if a_n < t.min_n or b_n < t.min_n:
        return None
    return "A" if t.z > 0 else "B"
