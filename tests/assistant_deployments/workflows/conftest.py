"""Configuration for the workflow-bundle tests.

Purely symbolic: they read the authored bundles off disk and check them
against the authoring contract. No Unify backend, no LLM calls, no network.
The lifecycle proof — install, hold, arm, uninstall against real managers —
lives in unify, where the managers are; what belongs here is whether the
bundles *this repo authors* are well-formed.
"""

import os

os.environ["SKIP_UNIFY_TEST_INIT"] = "1"
