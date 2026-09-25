"""``activate_element`` (specification section 66).

Two layers are covered. The **accessibility helpers** do the AT-SPI work: select
which action to invoke, descend the element path live, and perform the action.
The **executor path** is what makes the tool safe: it must refuse without a
wired backend, refuse an absent target, never inject synthetic pointer or key
input, and be blocked in OBSERVE exactly like any other input.

The point of the tool is that it is *not* a click: it asks the application to
perform its own accessibility action, so no coordinates are needed and pointer
occlusion cannot defeat it. That is only acceptable because it passes through the
identical policy -> lease -> revalidation -> verification pipeline, which the
executor tests below assert.
"""

from __future__ import annotations

from typing import Any

from core.accessibility import _descend_path, _invoke_action, _select_action_index
from schemas.actions import ActivationOutcome, PlannedAction, ToolName
from schemas.elements import ElementQuery, UIElement
from schemas.enums import ErrorCode, PolicyMode, VerificationState
from tests.harness.phase8 import ExecutorEnv, make_delta, make_element, make_state

_QUERY = ElementQuery(text="Send")


# -- The accessibility helpers ----------------------------------------------


class _Node:
    """A stand-in for an ``Atspi.Accessible`` node with children."""

    def __init__(self, children: list[Any] | None = None) -> None:
        self._children: list[Any] = list(children or [])

    def get_child_at_index(self, index: int) -> Any | None:
        if 0 <= index < len(self._children):
            return self._children[index]
        return None


class _ActionInterface:
    """A stand-in for ``Atspi.Action`` that records calls and can decline."""

    def __init__(self, *, performs: bool = True) -> None:
        self.performs = performs
        self.calls: list[tuple[Any, int]] = []

    def do_action(self, node: Any, index: int) -> bool:
        self.calls.append((node, index))
        return self.performs


class _RefusingActionInterface:
    """An ``Atspi.Action`` double whose interface call is simply not usable."""

    def do_action(self, _node: Any, _index: int) -> bool:
        raise RuntimeError("the interface is not usable here")


class _Atspi:
    """A stand-in for the ``Atspi`` module: desktops plus an Action interface."""

    def __init__(self, desktops: list[Any] | None = None, *, action: Any = None) -> None:
        self._desktops: list[Any] = list(desktops or [])
        if action is not None:
            self.Action = action

    def get_desktop(self, index: int) -> Any | None:
        if 0 <= index < len(self._desktops):
            return self._desktops[index]
        return None


def test_descend_path_follows_the_recorded_indices() -> None:
    """A traversal path (``desktop[0]/1/0``) resolves to the exact live node."""
    target = _Node()
    child = _Node(children=[target])
    root = _Node(children=[_Node(), child])

    assert _descend_path(_Atspi([root]), "desktop[0]/1/0") is target


def test_descend_path_returns_the_desktop_for_a_root_path() -> None:
    root = _Node()
    assert _descend_path(_Atspi([root]), "desktop[0]") is root


def test_descend_path_refuses_a_path_it_cannot_follow() -> None:
    """An unresolvable path means 'absent', never a guess at a near neighbour."""
    root = _Node(children=[_Node()])
    atspi = _Atspi([root])

    assert _descend_path(atspi, "frame:0/1") is None, "a non-traversal path is not descended"
    assert _descend_path(atspi, "desktop[0]/7") is None, "an absent child index is absent"
    assert _descend_path(atspi, "desktop[0]/x") is None, "a non-numeric segment is refused"
    assert _descend_path(atspi, "desktop[3]") is None, "an absent desktop is absent"
    assert _descend_path(atspi, "desktop[not-an-index]") is None


def test_action_selection_prefers_a_click_over_a_generic_activation() -> None:
    """Ordered most-specific-first, so the invoked action is the expected one."""
    assert _select_action_index(["activate", "click"], None) == 1
    assert _select_action_index(["press"], None) == 0
    assert _select_action_index(["expand", "collapse"], None) == 0


def test_action_selection_honours_an_explicit_request_exactly() -> None:
    assert _select_action_index(["activate", "click"], "CLICK") == 1
    assert _select_action_index(["activate", "click"], "expand") is None, (
        "a name that is not present is reported, never approximated"
    )
    assert _select_action_index([], None) is None


def test_invoking_an_action_uses_the_interface_first() -> None:
    node = _Node()
    interface = _ActionInterface()

    assert _invoke_action(_Atspi(action=interface), node, 0) is True
    assert interface.calls == [(node, 0)]


def test_invoking_an_action_falls_back_to_the_node_method() -> None:
    """The deprecated shim does not share a signature across PyGObject versions."""
    called: list[int] = []

    class _FallbackNode:
        def do_action(self, index: int) -> bool:
            called.append(index)
            return True

    atspi = _Atspi(action=_RefusingActionInterface())
    assert _invoke_action(atspi, _FallbackNode(), 2) is True
    assert called == [2]


def test_invoking_an_action_reports_a_real_refusal() -> None:
    atspi = _Atspi(action=_ActionInterface(performs=False))
    assert _invoke_action(atspi, _Node(), 0) is False


# -- The executor path ------------------------------------------------------


def _env(**kwargs: Any) -> ExecutorEnv:
    """An executor environment in ASSIST by default."""
    kwargs.setdefault("mode", PolicyMode.ASSIST)
    return ExecutorEnv(**kwargs)


def test_activation_goes_through_accessibility_and_injects_nothing() -> None:
    """The whole point of the tool: an AT-SPI action, not a synthetic click."""
    element = make_element()
    state = make_state(frame_id=5, elements=(element,))
    seen: list[UIElement] = []

    def hook(target: UIElement) -> ActivationOutcome:
        seen.append(target)
        return ActivationOutcome(ok=True, action="click")

    env = _env(state=state, activate=hook)
    after = make_state(frame_id=6, elements=(element,))
    env.perceiver.script(after, make_delta(before=state, after=after))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.ACTIVATE_ELEMENT, target=_QUERY))

    assert envelope.ok is True
    assert envelope.verification is VerificationState.VERIFIED
    assert [item.element_id for item in seen] == [element.element_id]
    assert env.backend.events == [], "activation must not inject pointer or key input"


def test_activation_without_a_wired_backend_is_honestly_unavailable() -> None:
    """No accessibility backend is UNAVAILABLE, never a silently substituted click."""
    element = make_element()
    env = _env(state=make_state(frame_id=5, elements=(element,)))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.ACTIVATE_ELEMENT, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE
    assert env.backend.events == []


def test_a_refused_accessibility_action_is_reported_as_unavailable() -> None:
    element = make_element()

    def refusing(_target: UIElement) -> ActivationOutcome:
        return ActivationOutcome(ok=False, reason="the application refused it")

    env = _env(state=make_state(frame_id=5, elements=(element,)), activate=refusing)

    envelope = env.executor.execute(PlannedAction(tool=ToolName.ACTIVATE_ELEMENT, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE
    assert "refused" in (envelope.message or "")


def test_activation_never_runs_on_an_absent_target() -> None:
    """Resolution still happens first; a missing target is a NOT_FOUND."""
    invoked: list[UIElement] = []

    def hook(target: UIElement) -> ActivationOutcome:
        invoked.append(target)
        return ActivationOutcome(ok=True)

    env = _env(state=make_state(elements=()), activate=hook)

    envelope = env.executor.execute(PlannedAction(tool=ToolName.ACTIVATE_ELEMENT, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_NOT_FOUND
    assert invoked == [], "a target that was never resolved cannot be activated"


def test_observe_mode_refuses_before_the_action_is_invoked() -> None:
    """Section 56/85: OBSERVE blocks activation exactly like any other input."""
    invoked: list[UIElement] = []
    element = make_element()

    def hook(target: UIElement) -> ActivationOutcome:
        invoked.append(target)
        return ActivationOutcome(ok=True)

    env = _env(mode=PolicyMode.OBSERVE, state=make_state(frame_id=5, elements=(element,)), activate=hook)

    envelope = env.executor.execute(PlannedAction(tool=ToolName.ACTIVATE_ELEMENT, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is not None
    assert envelope.verification is not VerificationState.VERIFIED
    assert invoked == [], "OBSERVE must refuse before the accessibility action runs"
    assert env.backend.events == []
