"""Which kanji the student knows, for the chat's furigana (spec §8b). Synthetic data only."""
from __future__ import annotations

from backend.srs import wanikani as wk


def test_known_kanji_are_the_ones_passed_at_least_once():
    raw = {
        "user": {"data": {"level": 3}},
        "kanji_assignments": {"data": [
            {"data": {"subject_id": 1, "passed_at": "2026-01-01T00:00:00Z", "srs_stage": 3}},  # fell back: still passed
            {"data": {"subject_id": 2, "passed_at": None, "srs_stage": 4}},                   # never Guru yet
            {"data": {"subject_id": 9, "passed_at": "2026-01-02T00:00:00Z", "srs_stage": 5}},  # no subject: ignored
        ]},
        "kanji_subjects": {"data": [{"id": 1, "data": {"characters": "雨"}}, {"id": 2, "data": {"characters": "降"}}]},
    }
    assert wk.parse(raw).known_kanji == ["雨"]


def test_no_kanji_data_means_nothing_known():
    assert wk.parse({}).known_kanji == []


class RecordingClient:
    """GET only — the SRS client has nothing else (spec §0)."""

    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return {"data": {"level": 3}} if path == "/v2/user" else {"data": []}


def test_kanji_progress_is_read_with_documented_filters_only():
    client = RecordingClient()
    wk.fetch_raw(client)
    assert ("/v2/assignments", {"subject_types": "kanji", "started": "true"}) in client.calls
    assert ("/v2/subjects", {"types": "kanji", "levels": "1,2,3"}) in client.calls


def test_kanji_readings_come_from_the_subjects_primary_first_without_nanori():
    """The chat's per-kanji furigana split (backend/annotate.py) reads them from the snapshot."""
    raw = {"kanji_subjects": {"data": [
        {"id": 1, "data": {"characters": "日", "readings": [
            {"reading": "ひ", "primary": False, "accepted_answer": False, "type": "kunyomi"},
            {"reading": "にち", "primary": True, "accepted_answer": True, "type": "onyomi"},
            {"reading": "か", "primary": False, "accepted_answer": False, "type": "kunyomi"},
            {"reading": "あきら", "primary": False, "accepted_answer": False, "type": "nanori"},
            {"reading": "にち", "primary": False, "accepted_answer": True, "type": "onyomi"}]}},
        {"id": 2, "data": {"characters": "本", "readings": "garbage"}},
        {"id": 3, "data": {"characters": "", "readings": [{"reading": "x", "type": "onyomi"}]}},
    ]}}
    assert wk.parse(raw).kanji_readings == {"日": ["にち", "ひ", "か"]}
    assert wk.parse({}).kanji_readings == {}
