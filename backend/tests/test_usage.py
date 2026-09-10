"""The status bar's numbers (user request 2026-09-10): turns this month, and the context meter.

Hermetic. The context figures are the ones a live probe returned (constants.py, 2026-09-10).
No dollars anywhere: the subscription runs through `claude -p` and is not billed per turn.
"""
from __future__ import annotations

import datetime as dt

from backend import usage
from backend.brain import claude_cli

TODAY = dt.date(2026, 9, 10)
#: The launcher class, whatever its name: the one that owns _meters.
CLI = next(v for v in vars(claude_cli).values() if isinstance(v, type) and hasattr(v, "_meters"))


# ------------------------------------------------------------------ the ledger
def test_turns_add_up_and_survive_a_restart(tmp_path):
    usage.Ledger(tmp_path).add_turn(TODAY)
    assert usage.Ledger(tmp_path).add_turn(TODAY) == {"turns": 2}
    assert usage.Ledger(tmp_path).month(TODAY) == {"turns": 2}


def test_months_are_kept_apart(tmp_path):
    ledger = usage.Ledger(tmp_path)
    ledger.add_turn(TODAY)
    ledger.add_turn(dt.date(2026, 10, 1))
    assert ledger.month(TODAY) == {"turns": 1} and ledger.month(dt.date(2026, 10, 1)) == {"turns": 1}


def test_an_empty_month_is_zero_not_an_error(tmp_path):
    assert usage.Ledger(tmp_path).month(TODAY) == {"turns": 0}


def test_a_damaged_file_starts_the_month_again(tmp_path):
    ledger = usage.Ledger(tmp_path)
    ledger.path(TODAY).parent.mkdir(parents=True, exist_ok=True)
    ledger.path(TODAY).write_text("{not json", encoding="utf-8")
    assert ledger.add_turn(TODAY) == {"turns": 1}


def test_no_money_is_recorded():
    """Dollars made no sense for a subscription (user, 2026-09-10); nothing may bring them back."""
    assert not any("cost" in name or "usd" in name for name in vars(usage.Ledger))


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
    assert CLI._meters(PROBE_USAGE, PROBE_MODEL_USAGE) == {"context_tokens": 6204, "context_window": 200000}


def test_a_turn_with_a_tool_call_counts_its_last_request():
    usage_ = dict(PROBE_USAGE, iterations=[
        {"input_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
        {"input_tokens": 5, "cache_read_input_tokens": 7000, "cache_creation_input_tokens": 300}])
    assert CLI._meters(usage_, PROBE_MODEL_USAGE)["context_tokens"] == 7305


def test_missing_fields_are_unknown_not_zero_or_an_error():
    assert CLI._meters({}, {}) == {"context_tokens": None, "context_window": None}
