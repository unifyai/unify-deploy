"""UniLLM OpenAI-compatible proxy endpoints.

This module provides an OpenAI-compatible chat completions endpoint that routes
requests through UniLLM for caching, cost tracking, and multi-provider support.
"""
from communication.unillm.views import router

__all__ = ["router"]
