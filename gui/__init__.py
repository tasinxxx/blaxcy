"""BLAXCY GUI (Phase 12, specification sections 56, 63, 65, 71).

A single Qt window over the *assembled* Body (``core.application``): it observes
and controls, and it never becomes a second decision path. Concretely:

* ``gui.app`` -- the composition entry point. It builds the one
  :class:`~core.application.BlaxcyApplication`, hands it a real confirmation
  callback, shows the window and shuts the Body down on exit.
* ``gui.main_window`` -- the window: mode requests, safety operations, the
  live status/capability/log views and the refresh timer.
* ``gui.status_panel`` -- section 71's status read, including the section 66.1
  sequence-progress indicator.
* ``gui.capability_panel`` -- the section 28 verdicts as the probe actually
  measured them (status, backend, latency, reason, fix hint).
* ``gui.action_log`` -- the section 65 event bus rendered as a bounded log.
* ``gui.confirmation`` -- section 56's confirmation: the dialog, plus the
  thread bridge that lets a blocked executor thread ask the GUI thread.
* ``gui.emergency_stop_ui`` -- section 63's stop button, its shortcut and the
  re-arm control.

Two rules hold across every module here. First, **the GUI reports what the Body
did, not what it was asked to do**: a mode request that the policy engine refuses
is shown as refused, and a status field that is unknown is shown as unknown
rather than as an empty success. Second, **the GUI never invents an action**: it
calls the same ``dispatch``/``set_mode``/``trigger_emergency_stop`` entry points
the Brain does, so every click by an operator passes through policy -> resolve ->
lease -> revalidate -> execute -> verify exactly like a Brain tool call.

This package intentionally imports **nothing** from its own submodules. Every GUI
module needs a Qt binding, and BLAXCY's non-GUI work (``main.py probe``, the test
suite's core tests, the composition root) must not require one; import
``gui.main_window`` or ``gui.app`` explicitly when Qt is wanted.
"""

from typing import Final

#: The package's modules, named rather than imported (see the note above).
#: ``gui.app.run`` is the entry point; ``gui.main_window`` holds the window.
MODULES: Final[tuple[str, ...]] = (
    "gui.app",
    "gui.main_window",
    "gui.status_panel",
    "gui.capability_panel",
    "gui.action_log",
    "gui.confirmation",
    "gui.emergency_stop_ui",
)

__all__ = ["MODULES"]
