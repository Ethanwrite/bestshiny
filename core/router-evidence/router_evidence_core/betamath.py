"""Beta distribution arithmetic, without adding a scientific stack.

The posterior this package computes is Beta, and a Beta posterior is only
useful if you can ask it for a lower quantile — the whole point of a
conservative router is that it acts on the bottom of the interval rather than
the middle. That needs the inverse regularised incomplete beta function, which
is the one piece of mathematics the standard library does not provide.

Adding SciPy for it would pull a large binary dependency into a service whose
runtime need is three functions, so they are implemented here: the regularised
incomplete beta by the standard Lentz continued fraction, and its inverse by
bisection. Bisection is slower than Newton and completely immune to the
overshoot that makes Newton fail near a=0 or b=0, which is exactly the region a
model with two observations lives in.

Accuracy is checked against known closed forms in
``tests/test_router_posterior_math.py``: the uniform case, the symmetric case,
and the integer-parameter case where the CDF is a binomial sum.
"""

from __future__ import annotations

import math

_MAX_ITERATIONS = 300
_EPSILON = 1e-14
_TINY = 1e-300


def log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _beta_continued_fraction(x: float, a: float, b: float) -> float:
    """Lentz's algorithm for the continued fraction of I_x(a, b).

    Converges for x < (a+1)/(a+b+2); the caller reflects when it does not.
    """

    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITERATIONS + 1):
        m2 = 2 * m
        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + numerator / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        h *= d * c
        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + numerator / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPSILON:
            break
    return h


def regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """I_x(a, b) — the CDF of Beta(a, b) at x."""

    if a <= 0 or b <= 0:
        raise ValueError(f"Beta parameters must be positive; got a={a}, b={b}")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(a * math.log(x) + b * math.log1p(-x) - log_beta(a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(x, a, b) / a
    return 1.0 - front * _beta_continued_fraction(1.0 - x, b, a) / b


def beta_quantile(probability: float, a: float, b: float) -> float:
    """The inverse of :func:`regularized_incomplete_beta`, by bisection.

    Returns a value in [0, 1]. The loop is bounded by iteration count rather
    than driven by a tolerance, so the result is bit-for-bit reproducible —
    which a replay report needs it to be.
    """

    if not 0.0 <= probability <= 1.0:
        raise ValueError(f"probability must be in [0, 1]; got {probability}")
    if a <= 0 or b <= 0:
        raise ValueError(f"Beta parameters must be positive; got a={a}, b={b}")
    if probability == 0.0:
        return 0.0
    if probability == 1.0:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(200):
        middle = 0.5 * (low + high)
        if regularized_incomplete_beta(middle, a, b) < probability:
            low = middle
        else:
            high = middle
        if high - low < 1e-15:
            break
    return 0.5 * (low + high)


def beta_mean(a: float, b: float) -> float:
    return a / (a + b)


def beta_variance(a: float, b: float) -> float:
    total = a + b
    return (a * b) / (total * total * (total + 1.0))


def moment_match(mean: float, variance: float) -> tuple[float, float]:
    """Beta parameters with a given mean and variance.

    Used to express an external prior as pseudo-counts: a benchmark that says
    "0.82, and we are about this sure" becomes a Beta whose mass sits where the
    benchmark says and whose width says how much it is allowed to matter.
    Raises rather than clamping when the variance is impossible for the mean,
    because silently widening a prior changes what it claims.
    """

    if not 0.0 < mean < 1.0:
        raise ValueError(f"mean must be strictly inside (0, 1); got {mean}")
    maximum = mean * (1.0 - mean)
    if not 0.0 < variance < maximum:
        raise ValueError(f"variance {variance} is not attainable for mean {mean} (must be < {maximum})")
    strength = maximum / variance - 1.0
    return mean * strength, (1.0 - mean) * strength


__all__ = [
    "beta_mean",
    "beta_quantile",
    "beta_variance",
    "log_beta",
    "moment_match",
    "regularized_incomplete_beta",
]
