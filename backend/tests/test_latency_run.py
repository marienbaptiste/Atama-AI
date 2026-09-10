"""The latency report's maths (spec §10). Hermetic: no brain, no audio — just the numbers."""
from __future__ import annotations

from backend.tools import latency_run as lr


def row(v2v, stt=300, claude=1500, tts=400, ttft=900, thinking=0):
    return {"stt_ms": stt, "claude_ms": claude, "tts_ms": tts, "v2v_ms": v2v,
            "ttft_ms": ttft, "thinking_chars": thinking}


def test_p90_is_the_nearest_rank():
    """"p90 over 20 turns" is the 18th of 20, sorted — not an interpolation between two turns."""
    values = list(range(1, 21))
    assert lr.percentile(values, 90) == 18
    assert lr.percentile(values, 50) == 10


def test_missing_values_are_skipped_and_nothing_is_none():
    assert lr.percentile([None, 5, None, 7], 50) == 5
    assert lr.percentile([None, None], 90) is None


def test_the_gate_passes_only_when_p90_is_inside_the_budget():
    inside = [row(2350)] * 20
    assert lr.summarise(inside)["pass"]
    slow_tail = [row(2350)] * 17 + [row(5000)] * 3      # the 18th sorted value is 5000
    assert not lr.summarise(slow_tail)["pass"]
    assert lr.summarise(slow_tail)["v2v_ms"]["p50"] == 2350


def test_a_run_with_no_complete_turns_fails_rather_than_passing_empty():
    assert not lr.summarise([row(None)])["pass"]


def test_the_report_names_every_stage_and_the_verdict():
    text = lr.format_report(lr.summarise([row(3400, claude=2600)] * 20), {"claude_ms": 8800, "v2v_ms": 9700})
    for word in ("stt", "claude first sentence", "tts first audio", "VOICE->VOICE", "OVER",
                 "opening turn", "GATE M3d", "FAIL"):
        assert word in text


def test_twenty_distinct_student_lines():
    assert len(lr.STUDENT_LINES) == 20 and len(set(lr.STUDENT_LINES)) == 20
