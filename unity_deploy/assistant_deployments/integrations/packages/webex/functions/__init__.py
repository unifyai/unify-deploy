"""Webex callable functions.

Underscore-prefixed modules (``_config``, ``_client``, ``_capabilities``,
``_sync_helpers``, ``_local_helpers``) are library code and skipped by
FunctionManager's discovery sweep.  All other modules expose
``@custom_function``-decorated callables for the actor.
"""
