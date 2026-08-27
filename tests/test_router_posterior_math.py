"""The Beta arithmetic, checked against closed forms rather than against itself."""

from __future__ import annotations

import math

import pytest
from router_evidence_core.betamath import (
    beta_mean,
    beta_quantile,
    beta_variance,
    log_beta,
    moment_match,
    regularized_incomplete_beta,
)


def test_the_uniform_case_is_the_identity() -> None:
    for x in (0.0, 0.1, 0.5, 0.9, 1.0):
        assert regularized_incomplete_beta(x, 1.0, 1.0) == pytest.approx(x, abs=1e-12)


def test_beta_two_two_matches_its_polynomial() -> None:
    """I_x(2,2) = 3x^2 - 2x^3, so a wrong continued fraction cannot hide."""

    for x in (0.05, 0.2, 0.3, 0.5, 0.77, 0.95):
        expected = 3 * x**2 - 2 * x**3
        assert regularized_incomplete_beta(x, 2.0, 2.0) == pytest.approx(expected, abs=1e-12)


def test_integer_parameters_match_the_binomial_sum() -> None:
    """I_x(a,b) with integer a,b equals the tail of a Binomial(a+b-1, x)."""

    a, b = 4, 6
    n = a + b - 1
    for x in (0.15, 0.4, 0.62, 0.88):
        expected = sum(math.comb(n, k) * x**k * (1 - x) ** (n - k) for k in range(a, n + 1))
        assert regularized_incomplete_beta(x, a, b) == pytest.approx(expected, abs=1e-10)


def test_the_symmetric_median_is_a_half() -> None:
    for a in (0.5, 1.0, 5.0, 50.0):
        assert beta_quantile(0.5, a, a) == pytest.approx(0.5, abs=1e-9)


def test_the_quantile_inverts_the_cdf() -> None:
    for a, b in ((0.5, 0.5), (2.0, 7.0), (30.0, 3.0), (120.0, 400.0)):
        for probability in (0.01, 0.1, 0.5, 0.9, 0.99):
            x = beta_quantile(probability, a, b)
            assert regularized_incomplete_beta(x, a, b) == pytest.approx(probability, abs=1e-9)


def test_the_lower_quantile_is_below_the_mean_for_a_thin_cell() -> None:
    """The property the whole conservative router rests on."""

    thin_lower = beta_quantile(0.10, 4.5, 1.5)
    thick_lower = beta_quantile(0.10, 450.0, 150.0)
    assert beta_mean(4.5, 1.5) == pytest.approx(beta_mean(450.0, 150.0))
    assert thin_lower < thick_lower < beta_mean(450.0, 150.0)


def test_more_data_narrows_the_interval() -> None:
    widths = []
    for scale in (1, 4, 16, 64):
        a, b = 3.0 * scale, 2.0 * scale
        widths.append(beta_quantile(0.9, a, b) - beta_quantile(0.1, a, b))
    assert widths == sorted(widths, reverse=True)


def test_the_quantile_is_bit_for_bit_reproducible() -> None:
    """A replay report that cannot be reproduced is not evidence."""

    first = [beta_quantile(0.1, 7.0, 3.0) for _ in range(5)]
    assert len(set(first)) == 1


def test_a_skewed_beta_can_have_its_mean_outside_its_own_interval() -> None:
    """Why the stored posterior is constrained on its quantiles and not its mean.

    With b below one the density has a pole at 1 and the distribution is
    nearly a point mass there, while the mean is dragged down by a thin left
    tail. Both numbers are correct; they are just not ordered the way
    intuition expects, and a database constraint that assumed otherwise
    rejected arithmetic rather than catching a bug.
    """

    a, b = 36.0, 0.01
    mean = beta_mean(a, b)
    lower = beta_quantile(0.10, a, b)
    assert lower > mean
    assert lower <= beta_quantile(0.90, a, b)


def test_degenerate_parameters_are_refused_not_clamped() -> None:
    with pytest.raises(ValueError, match="positive"):
        regularized_incomplete_beta(0.5, 0.0, 1.0)
    with pytest.raises(ValueError, match="positive"):
        beta_quantile(0.5, 1.0, -1.0)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        beta_quantile(1.5, 1.0, 1.0)


def test_log_beta_matches_the_gamma_identity() -> None:
    expected = math.log(math.factorial(2) * math.factorial(3) / math.factorial(6))
    assert log_beta(3.0, 4.0) == pytest.approx(expected)


def test_moment_matching_returns_the_mean_and_variance_it_was_given() -> None:
    a, b = moment_match(0.72, 0.01)
    assert beta_mean(a, b) == pytest.approx(0.72)
    assert beta_variance(a, b) == pytest.approx(0.01)


def test_an_impossible_variance_raises_rather_than_widening_silently() -> None:
    with pytest.raises(ValueError, match="not attainable"):
        moment_match(0.5, 0.5)
    with pytest.raises(ValueError, match="strictly inside"):
        moment_match(0.0, 0.01)


def test_the_percentile_is_nearest_rank_at_every_sample_count() -> None:
    """`round(x + 0.5)` is not `ceil`: it breaks halves to even.

    The old form returned the right rank for twenty samples and the wrong one
    for ten and fifty — an off-by-one that depended on nothing but parity, and
    at p90 of ten samples returned the maximum.
    """

    import math

    from router_evidence_core.posterior import CostLatencySummary

    for count in range(1, 60):
        values = [float(index) for index in range(count)]
        for fraction in (0.5, 0.9, 0.95):
            expected = max(0, min(count - 1, math.ceil(fraction * count) - 1))
            assert CostLatencySummary._percentile(values, fraction) == float(expected), (
                count,
                fraction,
            )
