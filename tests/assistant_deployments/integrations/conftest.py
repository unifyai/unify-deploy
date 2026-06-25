"""Configuration for integration framework tests.

Most tests in this directory are purely symbolic -- they validate Pydantic
models, file discovery, manifest parsing, and aggregation logic.  No Unify
backend, LLM calls, or network access is needed.

The ``test_sync_pipeline.py`` file overrides this by opting in to the full
Unity test infrastructure via ``@_handle_project``.
"""

import os

os.environ["SKIP_UNITY_TEST_INIT"] = "1"
