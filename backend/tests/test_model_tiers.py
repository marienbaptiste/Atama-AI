"""Each tier resolves to the newest model the CLI accepts (user request, 2026-09-10).

Hermetic: the probe is a fake, so no real Claude call is made.
"""
from __future__ import annotations

from backend import model_tiers as mt

TIERS = {"sonnet": ["claude-sonnet-6", "claude-sonnet-5", "sonnet"], "haiku": ["claude-haiku-4-5", "haiku"]}


def resolver(tmp_path, accepts, clock=lambda: 1_000_000.0):
    asked = []

    def probe(model):
        asked.append(model)
        return model in accepts

    return mt.Resolver(tmp_path / "tiers.json", probe, TIERS, clock), asked


def test_the_newest_accepted_model_wins(tmp_path):
    r, asked = resolver(tmp_path, accepts={"claude-sonnet-5"})
    assert r.resolve("sonnet") == "claude-sonnet-5"
    assert asked == ["claude-sonnet-6", "claude-sonnet-5"]          # newest first, stops at a yes


def test_the_bare_alias_is_the_last_resort_and_never_probed(tmp_path):
    r, asked = resolver(tmp_path, accepts=set())
    assert r.resolve("sonnet") == "sonnet" and "sonnet" not in asked


def test_the_answer_is_remembered_for_a_week(tmp_path):
    r, asked = resolver(tmp_path, accepts={"claude-sonnet-5"})
    r.resolve("sonnet")
    again, asked2 = resolver(tmp_path, accepts={"claude-sonnet-5"}, clock=lambda: 1_000_000.0 + 3600)
    assert again.resolve("sonnet") == "claude-sonnet-5" and asked2 == []
    stale, asked3 = resolver(tmp_path, accepts={"claude-sonnet-6"}, clock=lambda: 1_000_000.0 + 8 * 86400)
    assert stale.resolve("sonnet") == "claude-sonnet-6" and asked3 == ["claude-sonnet-6"]


def test_forgetting_makes_the_next_launch_check_again(tmp_path):
    r, _ = resolver(tmp_path, accepts={"claude-sonnet-5"})
    r.resolve("sonnet")
    r.forget("sonnet")
    again, asked = resolver(tmp_path, accepts={"claude-sonnet-5"})
    again.resolve("sonnet")
    assert asked == ["claude-sonnet-6", "claude-sonnet-5"]


def test_a_pinned_full_id_is_used_as_given(tmp_path):
    r, asked = resolver(tmp_path, accepts=set())
    assert r.resolve("claude-sonnet-4-6") == "claude-sonnet-4-6" and asked == []


def test_tier_names_are_case_insensitive(tmp_path):
    r, _ = resolver(tmp_path, accepts={"claude-haiku-4-5"})
    assert r.resolve("Haiku") == "claude-haiku-4-5"


def test_the_shipped_file_parses_and_every_tier_ends_with_its_alias():
    tiers = mt.load_tiers()
    assert {"sonnet", "opus", "haiku"} <= set(tiers)
    for tier, ids in tiers.items():
        assert ids[-1] == tier and len(ids) >= 2
    assert tiers["sonnet"][0] == "claude-sonnet-5" and tiers["opus"][0] == "claude-opus-5"
