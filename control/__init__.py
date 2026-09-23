"""BLAXCY control: the Executor, input backends, verification and recovery.

Phase 7 provides the physical input layer:

* ``control.backends`` -- input backends (XTEST implemented; each selected only
  after a passing functional probe, section 28 rule 12);
* ``control.mouse`` -- the section 48 mouse sequence (transform, clamp, inject,
  synchronise, readback-correct, settle) and section 49 drag;
* ``control.keyboard`` -- section 50 typing (direct/clipboard), the section 51
  focus guard and section 52 modifier hygiene.

Nothing here decides *whether* to act: policy, leases and target revalidation are
the executor's job (sections 13, 44, 45), which arrives in Phase 8.
"""
