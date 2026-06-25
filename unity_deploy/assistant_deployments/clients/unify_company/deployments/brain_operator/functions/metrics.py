"""Metric function surface for the brain_operator deployment.

Reserved for ``@custom_function``-decorated metric helpers (success
counts, last-run timestamps, etc.) that surface scheduled-job health
to the operator.  Empty at bootstrap; populated as the deployed
brain_operator accrues operational signal we want to expose.
"""

from __future__ import annotations
