"""Watcher — inotify file-system observer.

Responsibility (Master Part II §4.2, Part III §4.1, RFC-0004; CONTRACTS §4):
watch the TicketRepository via `inotify-simple`, debounce raw events per
`config.yaml:debounce_window_seconds`, and translate low-level file events into
domain triggers using each manifest's `watch:` rules.

TODO(Phase C): implement inotify registration, debounce and low-level -> domain
event translation.
"""
