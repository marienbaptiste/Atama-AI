"""The status bar's numbers (user request 2026-09-10): the month ledger and the context meter.

Hermetic. The figures are the ones a live two-turn probe returned (constants.py, 2026-09-10).
"""
from __future__ import annotations

import datetime as dt

from backend import usage
from backend.brain import claude_cli

TODAY = dt.date(2026, 9, 10)
#: The launcher class, whatever its name: the one that owns _meters.
CLI = next(v for v in vars(claude_cli).values() if isinstance(v, type) and hasattr(v, "_meters"))


# ------------------------------------------------------------------ the ledger
def test_session_totals_become_per_turn_additions(tmp_path):
    """The CLI's cost is cumulative per session: adding the raw totals would double-count."""
    ledger = usage.Ledger(tmp_path)
    ledger.add_session_total(0.02865575, TODAY)
    month = ledger.add_session_total(0.330596, TODAY)
    assert month["turns"] == 2 and abs(month["cost_usd"] - 0.330596) < 1e-6


def test_a_lower_total_is_a_new_session_and_counts_in_full(tmp_path):
    ledger = usage.Ledger(tmp_path)
    ledger.add_session_total(0.33, TODAY)
    month = ledger.add_session_total(0.05, TODAY)          # restarted: a fresh session
    assert abs(month["cost_usd"] - 0.38) < 1e-6


def test_a_tutor_switch_starts_counting_afresh(tmp_path):
    ledger = usage.Ledger(tmp_path)
    ledger.add_session_total(0.33, TODAY)
    ledger.new_session()
    month = ledger.add_session_total(0.50, TODAY)          # higher, but a different session
    assert abs(month["cost_usd"] - 0.83) < 1e-6


def test_months_are_kept_apart_and_survive_a_restart(tmp_path):
    usage.Ledger(tmp_path).add_session_total(1.0, TODAY)
    usage.Ledger(tmp_path).add_session_total(2.0, dt.date(2026, 10, 1))
    assert usage.Ledger(tmp_path).month(TODAY) == {"cost_usd": 1.0, "turns": 1}
    assert usage.Ledger(tmp_path).month(dt.date(2026, 10, 1)) == {"cost_usd": 2.0, "turns": 1}


def test_an_unknown_cost_records_nothing(tmp_path):
    ledger = usage.Ledger(tmp_path)
    assert ledger.add_session_total(None, TODAY) == {"cost_usd": 0.0, "turns": 0}
    assert not ledger.path(TODAY).exists()


def test_a_damaged_file_starts_the_month_again(tmp_path):
    ledger = usage.Ledger(tmp_path)
    ledger.path(TODAY).parent.mkdir(parents=True, exist_ok=True)
    ledger.path(TODAY).write_text("{not json", encoding="utf-8")
    assert ledger.add_session_total(0.1, TODAY) == {"cost_usd": 0.1, "turns": 1}


# ----------------------------------------------------------- the context meter
PROBE_USAGE = {"input_tokens": 3, "cache_read_input_tokens": 2131, "cache_creation_input_tokens": 4070,
               "output_tokens": 4}
PROBE_MODEL_USAGE = {
    "claude-haiku-4-5-20251001": {"inputTokens": 443, "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 0, "contextWindow": 200000},
    "claude-sonnet-4-6": {"inputTokens": 3, "cacheReadInputTokens": 2131,
                          "cacheCreationInputTokens": 4070, "contextWindow": 200000},
}


def test_context_is_what_the_turn_read_and_the_window_is_the_main_models():
    m = CLI._meters(PROBE_USAGE, PROBE_MODEL_USAGE, 0.0164738)
    assert m == {"context_tokens": 6204, "context_window": 200000, "cost_total_usd": 0.0164738}


def test_a_turn_with_a_tool_call_counts_its_last_request():
    usage_ = dict(PROBE_USAGE, iterations=[
        {"input_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
        {"input_tokens": 5, "cache_read_input_tokens": 7000, "cache_creation_input_tokens": 300}])
    assert CLI._meters(usage_, PROBE_MODEL_USAGE, None)["context_tokens"] == 7305


def test_missing_fields_are_unknown_not_zero_or_an_error():
    assert CLI._meters({}, {}, None) == {"context_tokens": None, "context_window": None,
                                         "cost_total_usd": None}
