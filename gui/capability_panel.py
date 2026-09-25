"""Capability panel (specification sections 28, 71).

Section 28's rule -- every capability reports ``AVAILABLE`` / ``DEGRADED`` /
``UNAVAILABLE`` with evidence, and nothing is selected because a binary exists --
is only useful if a human can see the verdicts. This panel renders exactly what
the probe measured: status, backend, latency, the reason it is not available, and
the fix hint.

It never improves a verdict. A capability that was not probed shows as *not
probed* rather than as an empty success, and a row is never coloured green
because the row exists.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from schemas.capability import Capability, CapabilityReport
from schemas.enums import CapabilityStatus

#: Shown when no report exists yet. Never an empty string: "" reads like success.
NOT_PROBED: Final[str] = "not probed"

_COLUMNS: Final[tuple[str, ...]] = (
    "capability",
    "status",
    "backend",
    "latency",
    "reason",
    "fix",
)


def _latency_text(capability: Capability) -> str:
    """Measured latency, or an honest dash when no timed probe ran."""
    if capability.latency_ms is None:
        return "-"
    return f"{capability.latency_ms:.1f} ms"


class CapabilityPanel(QWidget):
    """The section 28 verdicts as a table, with the evidence behind each one."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._report: CapabilityReport | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._summary = QLabel(NOT_PROBED, self)
        self._summary.setObjectName("capability_summary")
        layout.addWidget(self._summary)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setObjectName("capability_table")
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(len(_COLUMNS) - 2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(len(_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table)

    # -- Rendering -------------------------------------------------------------

    def set_report(self, report: CapabilityReport | None) -> None:
        """Render a report. ``None`` means *not probed*, and says so."""
        self._report = report
        if report is None:
            self._table.setRowCount(0)
            self._summary.setText(NOT_PROBED)
            return
        self._table.setRowCount(len(report.capabilities))
        for row, capability in enumerate(report.capabilities):
            values = (
                capability.name.value,
                capability.status.value,
                capability.backend or "-",
                _latency_text(capability),
                capability.reason or "-",
                capability.fix_hint or "-",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setToolTip(capability.status.value)
                self._table.setItem(row, column, item)
        self._summary.setText(self.summary_text())

    def summary_text(self) -> str:
        """One-line tally, matching the CLI's probe footer wording."""
        report = self._report
        if report is None:
            return NOT_PROBED
        available = len(report.by_status(CapabilityStatus.AVAILABLE))
        degraded = len(report.by_status(CapabilityStatus.DEGRADED))
        unavailable = len(report.by_status(CapabilityStatus.UNAVAILABLE))
        return (
            f"AVAILABLE: {available}   DEGRADED: {degraded}   "
            f"UNAVAILABLE: {unavailable}"
        )

    def summary(self) -> dict[str, int]:
        """The tally as data, for tests and the action log."""
        report = self._report
        if report is None:
            return {}
        return {
            "AVAILABLE": len(report.by_status(CapabilityStatus.AVAILABLE)),
            "DEGRADED": len(report.by_status(CapabilityStatus.DEGRADED)),
            "UNAVAILABLE": len(report.by_status(CapabilityStatus.UNAVAILABLE)),
        }

    def row_count(self) -> int:
        """How many capabilities are displayed."""
        return self._table.rowCount()

    def cell(self, row: int, column: int) -> str:
        """Read one rendered cell (used by tests to assert what is shown)."""
        item = self._table.item(row, column)
        return "" if item is None else item.text()


__all__ = ["NOT_PROBED", "CapabilityPanel"]
