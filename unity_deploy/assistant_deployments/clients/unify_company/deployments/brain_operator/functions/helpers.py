"""Shared helper surface for the brain_operator deployment.

Reserved for cross-function helpers if/when the wrappers grow them.
Today every wrapper imports brain entrypoints directly inside its
function body to satisfy FunctionManager's isolation rule, so this
module is intentionally empty.
"""

from __future__ import annotations
