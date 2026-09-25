"""BLAXCY control: the Executor, input backends, verification and recovery.

Phase 7 provides the physical input layer:

* ``control.backends`` -- input backends (XTEST implemented; each selected only
  after a passing functional probe, section 28 rule 12);
* ``control.mouse`` -- the section 48 mouse sequence (transform, clamp, inject,
  synchronise, readback-correct, settle) and section 49 drag;
* ``control.keyboard`` -- section 50 typing (direct/clipboard), the section 51
  focus guard and section 52 modifier hygiene.

Phase 8 adds the layer that decides *whether* an action may happen and proves it
afterwards:

* ``control.executor`` -- the section 59 state machine. It owns lease issue
  (section 44) and the full revalidation checklist (section 45), and it is the
  only caller of ``control.mouse``/``control.keyboard``;
* ``control.verifier`` -- section 60's ``VERIFIED`` / ``UNVERIFIED`` /
  ``CONTRADICTED`` outcomes, derived from real evidence;
* ``control.window_manager`` -- section 47 window activation over EWMH, verified
  by reading the active window back rather than assuming the request worked;
* ``control.action_tracker`` -- section 78 input-ownership tracking, cleared on
  every terminal path including emergency stop and shutdown.

Phase 9 adds the safety gate -- the mechanisms that stop automation and keep the
desktop's input safe while it is stopped:

* ``control.emergency_stop`` -- the latched section 63 stop (latch, release
  tracked buttons then keys, cancel the loop, force OBSERVE, emit), which
  exposes ``abort_code`` as the executor's hook;
* ``control.takeover`` -- section 62 human takeover: pause, release, force
  OBSERVE, then wait for an explicit resume that invalidates leases and
  re-perceives;
* ``control.recovery`` -- section 61 failure classification and bounded retry
  budgets, with destructive actions and anything that already injected input
  refused outright.

Section 50's paste path is real rather than a plug-in hole:

* ``control.clipboard`` -- :class:`X11ClipboardPaster` takes the X CLIPBOARD
  selection, *serves* it to whichever application requests it, and hands the
  selection back afterwards. The serve window is what makes a paste work, because
  an X selection is only read on demand, after the paste keystroke.

Still to come: the sequence runner (Phase 10.1).
"""
