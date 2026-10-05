"""Finding the newest model by itself, and saying so in red when the CLI is the thing holding it
back (user, 2026-10-06).

The repo ran Sonnet 5 for three weeks after Sonnet 5.5 shipped, because the tier list is data a
human edits, and Opus 5.5 was refused by a CLI two months old without a word on the console.
"""
from __future__ import annotations

import asyncio
import json
import types

from backend import config, constants, model_tiers as mt, orchestrator, terminal
from backend.tools import doctor as d

TOO_OLD_MSG = ("API Error: 400 Claude Code 2.1.159 does not support this model; "
               "version 2.1.280 or newer is required. Run 'claude update', or update the Claude "
               "desktop app, then try again.")
NO_SUCH_MSG = "There's an issue with the selected model (claude-sonnet-9). It may not exist."
TIERS = {"sonnet": ["claude-sonnet-5-5", "claude-sonnet-5", "sonnet"]}


def resolver(tmp_path, answers: dict[str, bool], messages: dict[str, str] | None = None, now=1000.0):
    """A resolver whose probe answers from a table, so no CLI is involved."""
    messages = messages or {}
    asked: list[str] = []

    def probe(model: str):
        asked.append(model)
        ok = bool(answers.get(model))
        return mt.Probe(ok, "" if ok else messages.get(model, NO_SUCH_MSG))

    r = mt.Resolver(tmp_path / "tiers.json", probe, tiers=TIERS, clock=lambda: now)
    return r, asked


# ----------------------------------------------------------------- reading what a refusal means
def test_a_refusal_says_whether_the_cli_or_the_model_is_the_problem():
    assert mt.Probe(False, TOO_OLD_MSG).needs_newer_cli == "2.1.280"
    assert mt.Probe(False, NO_SUCH_MSG).needs_newer_cli == ""
    assert mt.Probe(True).needs_newer_cli == ""          # it answered; nothing to report
    assert not mt.Probe(False, TOO_OLD_MSG) and mt.Probe(True)
    # The wording this depends on is pinned, so a CLI that changes it fails here, not in silence.
    assert constants.CLAUDE_CLI_TOO_OLD_FOR_MODEL in TOO_OLD_MSG


def test_the_guesses_follow_the_naming_scheme_and_invent_nothing():
    assert mt.forward_candidates("claude-sonnet-5-5") == ["claude-sonnet-6", "claude-sonnet-5-6"]
    assert mt.forward_candidates("claude-opus-5-5") == ["claude-opus-6", "claude-opus-5-6"]
    # The dated form resolves to the dateless next ids, which is what ids have used since 4.6.
    assert mt.forward_candidates("claude-haiku-4-5-20251001") == ["claude-haiku-5", "claude-haiku-4-6"]
    for not_an_id in ("sonnet", "", "gpt-5", "claude", "claude-sonnet"):
        assert mt.forward_candidates(not_an_id) == []


# ----------------------------------------------------------------- the CLI being too old
def test_a_cli_too_old_for_the_newest_model_is_recorded_not_swallowed(tmp_path):
    r, asked = resolver(tmp_path, {"claude-sonnet-5": True}, {"claude-sonnet-5-5": TOO_OLD_MSG})
    assert r.resolve("sonnet") == "claude-sonnet-5"      # the lesson still happens
    assert r.cli_too_old == ("claude-sonnet-5-5", "2.1.280")
    assert asked == ["claude-sonnet-5-5", "claude-sonnet-5"]
    kept = json.loads((tmp_path / "tiers.json").read_text(encoding="utf-8"))["cli_too_old"]
    assert kept["model"] == "claude-sonnet-5-5" and kept["required"] == "2.1.280"


def test_a_plain_missing_model_is_not_reported_as_a_cli_problem(tmp_path):
    r, _ = resolver(tmp_path, {"claude-sonnet-5": True})
    assert r.resolve("sonnet") == "claude-sonnet-5"
    assert r.cli_too_old is None
    assert "cli_too_old" not in json.loads((tmp_path / "tiers.json").read_text(encoding="utf-8"))


def test_the_launch_says_it_in_red_with_the_command(capsys):
    tiers = types.SimpleNamespace(cli_too_old=("claude-opus-5-5", "2.1.280"))
    orchestrator.Lesson.warn_if_cli_is_old(tiers, using="claude-opus-5")
    out = capsys.readouterr().out
    assert terminal.RED in out and "claude-opus-5-5" in out and "2.1.280" in out
    assert constants.CLAUDE_CLI_UPDATE_COMMAND in out and "claude-opus-5" in out
    orchestrator.Lesson.warn_if_cli_is_old(types.SimpleNamespace(cli_too_old=None))
    assert capsys.readouterr().out == ""                 # nothing to say when nothing is wrong


def test_the_doctor_fails_red_on_it_rather_than_warning_about_a_version(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"cache_dir": str(tmp_path)}), encoding="utf-8")
    (tmp_path / "model_tiers.json").write_text(json.dumps(
        {"cli_too_old": {"model": "claude-opus-5-5", "required": "2.1.280", "at": 1}}), encoding="utf-8")
    assert d._cli_too_old_for(config.load(settings, env={})) == ("claude-opus-5-5", "2.1.280")
    monkeypatch.setattr(d, "_cli_too_old_for", lambda *a, **k: ("claude-opus-5-5", "2.1.280"))
    r = d.check_claude_cli(run=lambda *a, **k: (0, f"{constants.CLAUDE_CLI_VERSION_VERIFIED} (Claude Code)\n", ""),
                           which=lambda *a, **k: "claude")
    assert r.level == d.FAIL and "2.1.280" in r.message and constants.CLAUDE_CLI_UPDATE_COMMAND in r.message


# ----------------------------------------------------------------- finding a newer model
def test_a_newer_model_is_found_and_used_from_then_on(tmp_path):
    r, asked = resolver(tmp_path, {"claude-sonnet-6": True, "claude-sonnet-5-5": True})
    assert r.discover("sonnet") == "claude-sonnet-6"
    assert asked == ["claude-sonnet-6"]                  # the newest guess first, and it answered
    # From now on it is simply the newest option, ahead of everything the file lists.
    assert r.options_for("sonnet")[0] == "claude-sonnet-6"
    assert r.resolve("sonnet") == "claude-sonnet-6"


def test_nothing_newer_means_nothing_said_and_no_second_look_this_week(tmp_path):
    r, asked = resolver(tmp_path, {"claude-sonnet-5-5": True})
    assert r.discover("sonnet") is None
    assert asked == ["claude-sonnet-6", "claude-sonnet-5-6"]   # both guesses refused
    asked.clear()
    assert r.discover("sonnet") is None and asked == []        # a week's silence, not a probe
    assert r.resolve("sonnet") == "claude-sonnet-5-5"          # the file's newest still wins


def test_a_week_later_it_looks_again(tmp_path):
    r, asked = resolver(tmp_path, {"claude-sonnet-5-5": True})
    assert r.discover("sonnet") is None and asked
    later = mt.Resolver(tmp_path / "tiers.json", r.probe, tiers=TIERS,
                        clock=lambda: 1000.0 + mt.CACHE_TTL_S + 1)
    asked.clear()
    assert later.discover("sonnet") is None
    assert asked == ["claude-sonnet-6", "claude-sonnet-5-6"]


def test_discovery_while_the_cli_is_too_old_reports_the_cli(tmp_path):
    """The guess exists but this CLI cannot run it: that is the CLI's fault, and it gets said."""
    r, _ = resolver(tmp_path, {}, {"claude-sonnet-6": TOO_OLD_MSG})
    assert r.discover("sonnet") is None
    assert r.cli_too_old == ("claude-sonnet-6", "2.1.280")


def test_a_pinned_model_id_is_never_guessed_about(tmp_path):
    r, asked = resolver(tmp_path, {})
    assert r.discover("claude-sonnet-5") is None and asked == []
    assert r.resolve("claude-sonnet-5") == "claude-sonnet-5"


# ----------------------------------------------------------------- the lesson's own wiring
def test_the_lesson_looks_for_newer_models_off_the_launch_path(capsys):
    looked = []

    class Tiers:
        cli_too_old = None

        def discover(self, spec):
            looked.append(spec)
            return "claude-sonnet-6" if spec == "sonnet" else None

    lesson = orchestrator.Lesson.__new__(orchestrator.Lesson)
    lesson._tiers = Tiers()
    lesson.cfg = types.SimpleNamespace(CLAUDE_MODEL="sonnet", MEMORY_SUMMARY_MODEL="sonnet")
    asyncio.run(lesson.discover_models())
    assert looked == ["sonnet"]                          # the same spec twice is asked once
    out = capsys.readouterr().out
    assert "claude-sonnet-6" in out and "model_tiers.txt" in out
