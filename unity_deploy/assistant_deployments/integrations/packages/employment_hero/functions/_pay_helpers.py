"""Pay-related helpers for the Employment Hero package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  Imported
from inside ``sync_employmenthero_pay`` and friends so FunctionManager's
isolation rule is preserved.

Provides:

* ``band_rate`` — convert an exact pay rate to a coarse band string,
  used by the snapshot path so DataManager queries surface
  distribution buckets without exposing exact figures.
"""

from __future__ import annotations


def band_rate(amount: float | None, currency: str = "GBP") -> str:
    """Convert exact rate to coarse band (e.g. ``"<45,000 GBP"``).

    The band scale is inlined here rather than module-level so future
    changes are visible in one diff alongside the function that uses
    them.
    """
    bands = [
        25_000,
        35_000,
        45_000,
        60_000,
        80_000,
        100_000,
        130_000,
        170_000,
        220_000,
    ]
    if amount is None:
        return "unknown"
    for upper in bands:
        if amount < upper:
            return f"<{upper:,} {currency}"
    return f">={bands[-1]:,} {currency}"
