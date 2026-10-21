"""Exact Clopper-Pearson binomial intervals, no SciPy needed."""

from __future__ import annotations

from math import comb
from typing import Callable


def _cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(0, k + 1))


def _bisect_increasing(g: Callable[[float], float], target: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if g(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Two-sided exact interval for a proportion k out of n."""
    if n <= 0 or not 0 <= k <= n:
        raise ValueError("need 0 <= k <= n and n > 0")
    tail = (1 - confidence) / 2
    # lower: P(X >= k | p) = tail, and P(X >= k | p) rises with p
    lower = 0.0 if k == 0 else _bisect_increasing(lambda p: 1 - _cdf(k - 1, n, p), tail)
    # upper: P(X <= k | p) = tail, and 1 - P(X <= k | p) rises with p
    upper = 1.0 if k == n else _bisect_increasing(lambda p: 1 - _cdf(k, n, p), 1 - tail)
    return lower, upper
