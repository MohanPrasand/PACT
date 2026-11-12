from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_minor(value: str | int | float | Decimal) -> int:
    try:
        d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {value!r}") from exc
    return int(d * 100)


def to_decimal_str(minor: int) -> str:
    return str((Decimal(minor) / 100).quantize(Decimal("0.01")))
