import pytest

from countersign.stats import clopper_pearson


@pytest.mark.parametrize("k,n,lo,hi", [(4, 5, 0.2836, 0.9949), (0, 10, 0.0, 0.3085), (10, 10, 0.6915, 1.0)])
def test_known_values(k, n, lo, hi):
    a, b = clopper_pearson(k, n)
    assert round(a, 4) == lo and round(b, 4) == hi
