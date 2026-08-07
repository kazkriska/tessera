"""Dispatcher — event bus routing and hook resolution.

Responsibility (Master Part VII, RFC-0004): receive domain events, resolve them
against manifest `hooks:`, apply the recursion depth guard
(`recursion_max_depth`, Part VII R.A.7) and enqueue descriptors onto the
scheduler queues keyed per CONTRACTS §6.

TODO(Phase C): implement subscription registry, routing and depth guard.
"""
