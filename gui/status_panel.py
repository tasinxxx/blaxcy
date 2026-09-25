"""Status and sequence progress (specification sections 65, 66.1, 71).

Section 71 asks the window to show connection state, current activity,
verification state, error state -- and, once batching exists, a **sequence
progress indicator** so a human watching never sees an opaque multi-second batch
with no idea which step is running or why it stopped.

Every value here comes from ``BlaxcyApplication.status()``, whose two rules this
panel inherits:

* **A fact the Body has not established is shown as ``UNKNOWN``**, never as an
  empty string or a plausible default. ``None`` is not zero and it is not success.
* **A refusal is shown as a refusal.** If the mode selector asked for AUTONOMOUS
  and the controller forced OBSERVE, the panel shows OBSERVE with the reason,
  because that is what the Body did.
"""

from __future__ import annotations

from typing import Any, Final

from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

#: Shown for any fact the Body has not established. Never "" and never "0".
UNKNOWN: Final[str] = "UNKNOWN"

#: The status fields the panel renders, in display order.
_STATUS_ROWS: Final[tuple[tuple[str, str], ...]] = (
    ("started", "started"),
    ("session", "session_type"),
    ("mode", "mode"),
    ("brain", "brain"),
    ("activity", "activity"),
    ("verification", "verification"),
    ("error", "error"),
    ("state", "state"),
)


def _humanise(value: Any) -> str:
    """Render one status value, honestly.

    ``None``/missing becomes ``UNKNOWN``; booleans become ``yes``/``no`` (so
    ``False`` is visibly a fact rather than an absence); everything else is
    stringified verbatim so the panel never paraphrases a verdict.
    """
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).strip()
    return text or UNKNOWN


def summarise_status(report: dict[str, Any] | None) -> dict[str, str]:
    """Flatten a ``status()`` report into the panel's display rows.

    Kept a pure function so the rendering rules above are testable without Qt.
    """
    if report is None:
        return {key: UNKNOWN for key, _ in _STATUS_ROWS}

    state = report.get("state") or {}
    brain = report.get("brain") or {}
    stop = report.get("emergency_stop") or {}
    takeover = report.get("takeover") or {}
    startup = report.get("startup") or {}

    brain_text: Any
    if brain.get("adapter"):
        brain_text = brain["adapter"]
    else:
        brain_text = brain.get("reason") or "not connected"

    activity_text: Any = None
    if report.get("sequence"):
        sequence = report["sequence"]
        activity_text = (
            f"sequence {sequence.get('sequence_id', '?')} "
            f"step {sequence.get('current_index', '?')} of "
            f"{sequence.get('total_steps', '?')}"
        )
    elif state.get("active_app"):
        activity_text = f"observing {state['active_app']}"

    verification = state.get("verification")
    if verification is None:
        verification = report.get("verification")

    error_text: Any = None
    if stop.get("latched"):
        error_text = "EMERGENCY_STOP_ACTIVE"
    elif takeover.get("active"):
        error_text = f"HUMAN_TAKEOVER: {takeover.get('reason') or 'human took control'}"

    age = state.get("age_ms")
    freshness = state.get("fresh")
    if age is None:
        state_text = UNKNOWN
    else:
        state_text = (
            f"frame {state.get('frame_id', UNKNOWN)} "
            f"v{state.get('state_version', UNKNOWN)} "
            f"gen {state.get('generation', UNKNOWN)} "
            f"age {age:.0f}ms "
            f"{'fresh' if freshness else 'STALE'} "
            f"elements {state.get('element_count', UNKNOWN)}"
        )

    rows = {
        "started": _humanise(report.get("started")),
        "session": _humanise(report.get("session_type")),
        "mode": _humanise(report.get("mode")),
        "brain": _humanise(brain_text),
        "activity": _humanise(activity_text),
        "verification": _humanise(verification),
        "error": _humanise(error_text),
        "state": _humanise(state_text),
    }
    if startup.get("notes"):
        rows["notes"] = "; ".join(str(note) for note in startup["notes"])
    return rows


class SequenceProgressIndicator(QWidget):
    """Section 71's sequence progress: current step / total, and any halt reason."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel("no sequence running", self)
        self._label.setObjectName("sequence_progress_label")
        self._bar = QProgressBar(self)
        self._bar.setObjectName("sequence_progress_bar")
        self._bar.setTextVisible(True)
        layout.addWidget(self._label)
        layout.addWidget(self._bar)

    def update_sequence(self, progress: dict[str, Any] | None) -> None:
        """Render the live ``SequenceProgress`` record, or its absence."""
        if not progress:
            self._label.setText("no sequence running")
            self._bar.setRange(0, 1)
            self._bar.setValue(0)
            return
        total = int(progress.get("total_steps") or 0)
        completed = len(progress.get("completed_indices") or ())
        current = int(progress.get("current_index") or 0)
        halt = progress.get("halt_reason")
        sequence_id = progress.get("sequence_id") or UNKNOWN
        self._bar.setRange(0, max(total, 1))
        self._bar.setValue(min(completed, max(total, 1)))
        if halt:
            self._label.setText(
                f"sequence {sequence_id} HALTED after {completed}/{total} steps "
                f"at step {current}: {halt}"
            )
        elif total and completed == total:
            self._label.setText(f"sequence {sequence_id} complete ({completed}/{total})")
        else:
            self._label.setText(
                f"sequence {sequence_id} step {current + 1} of {total} "
                f"({completed} completed)"
            )

    def label_text(self) -> str:
        """The rendered label (used by tests)."""
        return self._label.text()


class StatusPanel(QWidget):
    """Section 71's status read over the assembled Body."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        box = QGroupBox("Body status", self)
        form = QFormLayout(box)
        self._rows: dict[str, QLabel] = {}
        for key, _source in _STATUS_ROWS:
            value = QLabel(UNKNOWN, box)
            value.setObjectName(f"status_{key}")
            value.setWordWrap(True)
            form.addRow(f"{key}:", value)
            self._rows[key] = value
        layout.addWidget(box)

        self.sequence = SequenceProgressIndicator(self)
        sequence_box = QGroupBox("Sequence progress", self)
        sequence_layout = QVBoxLayout(sequence_box)
        sequence_layout.addWidget(self.sequence)
        layout.addWidget(sequence_box)

    def update_status(self, report: dict[str, Any] | None) -> None:
        """Render a ``BlaxcyApplication.status()`` report (or its absence)."""
        rows = summarise_status(report)
        for key, label in self._rows.items():
            label.setText(rows.get(key, UNKNOWN))
        self.sequence.update_sequence(
            None if report is None else report.get("sequence")
        )

    def value(self, key: str) -> str:
        """Read one rendered row (used by tests)."""
        label = self._rows.get(key)
        return "" if label is None else label.text()


__all__ = [
    "UNKNOWN",
    "SequenceProgressIndicator",
    "StatusPanel",
    "summarise_status",
]
