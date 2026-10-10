"""Regression tests for the frontend polish wrapper."""

from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "custom_components" / "reminders" / "frontend"


def test_polish_wrapper_reduces_primary_navigation() -> None:
    source = (FRONTEND / "reminders-panel-polish.js").read_text(encoding="utf-8")

    for label in ("Needs attention", "Upcoming", "All reminders", "History"):
        assert f'"{label}"' in source
    for label in ("Recurring", "Triggered", "Failed", "Expired"):
        assert (
            f'["{label.lower() if label != "Triggered" else "triggered"}", "{label}"]'
            in source
        )
    secondary_views = (
        'SECONDARY_VIEWS = new Set(["all", "recurring", '
        '"triggered", "failed", "expired"])'
    )
    assert secondary_views in source
    assert 'select.className = "view-filter"' in source


def test_history_uses_incremental_pagination() -> None:
    source = (FRONTEND / "reminders-panel-polish.js").read_text(encoding="utf-8")

    assert "const HISTORY_PAGE_SIZE = 50" in source
    assert "data.offset = offset" in source
    assert "this._historyTotal = Number.isFinite(result.total)" in source
    assert 'button.textContent = loadingMore ? "Loading…" : "Load more"' in source
    assert "{ appendHistory: true }" in source


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
        Path(__file__).parents[1] / "custom_components" / "reminders" / "frontend.py"
    ).read_text(encoding="utf-8")

    assert 'module_url=f"{STATIC_URL}/reminders-panel-polish.js"' in source


def test_current_lists_use_incremental_pagination() -> None:
    source = (FRONTEND / "reminders-panel-polish.js").read_text(encoding="utf-8")

    assert "const LIST_PAGE_SIZE = 100" in source
    assert "data.offset = offset" in source
    assert "this._listTotal = Number.isFinite(result.total)" in source
    assert "{ appendList: true }" in source
    assert 'footer.className = "pagination-footer"' in source


def test_list_loads_ignore_stale_responses_and_do_not_drop_refreshes() -> None:
    source = (FRONTEND / "reminders-panel-polish.js").read_text(encoding="utf-8")

    assert "const requestId = (this._loadRequestId || 0) + 1" in source
    assert source.count("if (requestId !== this._loadRequestId) return true") == 3
    assert "if (requestId === this._loadRequestId)" in source
    assert "if (this._historyLoadingMore) return true" in source
    assert "if (this._listLoadingMore) return true" in source
    assert "_historyRequestInFlight" not in source
