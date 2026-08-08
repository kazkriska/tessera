"""Language runner plugins (Master Part III §4.1, RFC-0004).

Each runner adapts a hook/action descriptor to a concrete interpreter
invocation under the Executor's isolation rules.

Runners are registered through :data:`tessera_runtime.runtime.executor.RUNNERS`
(keyed by ``shell`` name) and resolved by :func:`...dispatcher.resolve_runner`.
"""
