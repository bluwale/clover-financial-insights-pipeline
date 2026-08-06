"""utils/money.py — dollars_to_cents parses human-typed money without float drift."""
from __future__ import annotations

import pytest

from utils.money import dollars_to_cents


@pytest.mark.parametrize("text,cents", [
    ("3500", 350000),
    ("3500.10", 350010),
    ("3,500.10", 350010),
    ("$3,500.10", 350010),
    ("0", 0),
    ("0.01", 1),
    ("  42.50  ", 4250),
])
def test_parses_common_forms(text, cents):
    assert dollars_to_cents(text) == cents


def test_no_float_rounding_drift():
    """The classic float trap: 0.1 + 0.2 style errors must not survive money parsing."""
    assert dollars_to_cents("19.99") == 1999
    assert dollars_to_cents("100.30") == 10030


def test_invalid_amount_raises():
    with pytest.raises(ValueError, match="not a valid dollar amount"):
        dollars_to_cents("not-a-number")
