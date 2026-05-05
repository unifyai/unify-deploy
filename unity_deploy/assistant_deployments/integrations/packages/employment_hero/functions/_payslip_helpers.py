"""Payslip-related helpers for the Employment Hero package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  Imported
from inside ``list_employmenthero_employee_payslips`` and
``get_employmenthero_payslip`` so FunctionManager's isolation rule is
preserved.

Provides:

* ``mask_currency`` — render a masked currency string (e.g. ``"£***.**"``)
  used by the live payslip return path so exact figures never surface
  in actor-visible output.
"""

from __future__ import annotations


def mask_currency(amount: float | None, currency: str = "GBP") -> str | None:
    """Return a masked currency representation (e.g. ``"£***.**"``).

    Returns ``None`` when ``amount`` is ``None`` so callers can
    distinguish "no value" from "value masked".
    """
    if amount is None:
        return None
    sym = "£" if currency.upper() == "GBP" else currency
    return f"{sym}***.**"
