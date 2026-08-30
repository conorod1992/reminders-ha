"""Regression tests for the frontend polish wrapper."""

from __future__ import annotations

from pathlib import Path


FRONTEND = (
    Path(__file__).parents[1]
    / "custom_components"
    / "reminders"
    / "frontend"
)


def test_polish_wrapper_reduces_primary_navigation() -> None:
    source = (FRONTEND / "reminders-panel-polish.js").read_text(encoding="utf-8")

    for label in ("Needs attention", "Upcoming", "All reminders", "History"):
        assert f'"{label}"' in source
    for label in ("Recurring", "Triggered", "Failed", "Expired"):
        assert f'["{label.lower() if label != "Triggered" else "triggered"}", "{label}"]' in source
    assert 'SECONDARY_VIEWS = new Set(["all", "recurring", "triggered", "failed", "expired"])' in source
    assert 'select.className = "view-filter"' in source


def test_history_uses_incremental_pagination() -> None:
    source = (FRONTEND / "reminders-panel-polish.js").read_text(encoding="utf-8")

    assert "const HISTORY_PAGE_SIZE = 50" in source
    assert "offset: append ? this._history.length : 0" in source
    assert "this._historyTotal = Number.isFinite(result.total)" in source
    assert 'button.textContent = this._historyLoadingMore ? "Loading…" : "Load more"' in source
    assert "this._load({ appendHistory: true })" in source


def test_complex_cards_use_scannable_metadata() -> None:
    source = (FRONTEND / "reminders-panel-polish.js").read_text(encoding="utf-8")

    assert 'row.className = "title-row"' in source
    assert 'list.className = "card-meta-list"' in source
    assert 'item.className = "card-meta-item"' in source
    assert 'card.classList.add("complex-card")' in source


def test_startup_retry_detects_failed_initial_load_and_backs_off() -> None:
    source = (FRONTEND / "reminders-panel-robust.js").read_text(encoding="utf-8")

    assert "const RETRY_DELAYS_MS = [1000, 3000, 10000, 30000]" in source
    assert "if (this._startupPhase) this._startupLoadError = error" in source
    assert "if (loaded === false || this._startupLoadError)" in source
    assert 'retry.textContent = "Retry now"' in source
    assert "_scheduleRetry(this)" in source
    assert "_clearRetryTimer(this)" in source


def test_registered_panel_loads_polish_wrapper() -> None:
    source = (
        Path(__file__).parents[1]
        / "custom_components"
        / "reminders"
        / "frontend.py"
    ).read_text(encoding="utf-8")

    assert 'module_url=f"{STATIC_URL}/reminders-panel-polish.js"' in source
