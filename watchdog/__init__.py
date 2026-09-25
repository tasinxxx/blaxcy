"""BLAXCY crash-safety watchdog (Phase 9, specification section 64).

Two small pieces, no service and no broker (section 2):

* ``watchdog.protocol`` -- the heartbeat record and its atomic reader/writer. It
  carries the process id, session id, heartbeat count, the section 78 input
  ownership (``owned_keys_down`` / ``owned_buttons_down``) and whether the run
  reported a clean shutdown, so the next run can tell a clean exit from a crash
  that was holding input. It lives outside the project directory: it is runtime
  evidence, never repository content (section 22).
* ``watchdog.watchdog`` -- the minimal heartbeat thread plus :class:`CrashGuard`,
  which releases BLAXCY-held input and marks the run clean on
  SIGTERM/SIGINT/SIGHUP and on ``atexit``, then really terminates.

The watchdog is evidence, not a safety gate: losing its state file never takes
the process down, and a crash deliberately leaves the record dirty rather than
pretending the shutdown was clean.
"""
