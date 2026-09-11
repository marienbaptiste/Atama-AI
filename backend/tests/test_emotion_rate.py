"""Gate M3f's measure: the share of her turns carrying an emotion tag, read from the turn log."""
from __future__ import annotations

import json

from backend.tools import emotion_rate


def turn(*emotions):
    return {"tutor": {"sentences": [{"text": "はい。", "emotion": e} for e in emotions]}}


def test_a_turn_counts_once_whatever_its_number_of_tags():
    tagged, spoken, seen = emotion_rate.rate([turn("happy", "happy", None), turn(None), turn("serious")])
    assert (tagged, spoken) == (2, 3)
    assert seen == {"happy": 2, "serious": 1}


def test_turns_she_did_not_speak_in_are_not_counted():
    assert emotion_rate.rate([{"tutor": {"sentences": []}}, {"student": {}}])[:2] == (0, 0)


def test_a_half_written_last_line_is_skipped(tmp_path):
    log = tmp_path / "s.jsonl"
    log.write_text(json.dumps(turn("happy")) + "\n" + '{"tutor": {"sent', encoding="utf-8")
    assert len(emotion_rate.read(log)) == 1
