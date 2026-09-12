"""The REPL's split (2026-09-12): the page's buttons (`page_control`), the console rendering
(`terminal`) and the entry point (`repl`) — hermetic, with a fake loop and a fake page."""
from __future__ import annotations

import asyncio

import numpy as np

from backend import audio as audio_mod
from backend import orchestrator, page_control, repl, terminal
from backend.status import StatusRegistry
from backend.voice_loop import TurnTiming, vad_frame_samples

# Long enough to register with the sanitiser, not shaped like any real token (as test_redaction).
WK = "wk-fake-secret-value-0123456789"
BP = "bp-fake-secret-value-9876543210"


class FakeVad:
    min_speech_ms = 300


class FakeVoice:
    def __init__(self):
        self.cancelled = 0
        self.device = None

    def cancel(self):
        self.cancelled += 1

    def set_device(self, device):
        self.device = device


class FakeLoop:
    """Only what `PageControl` touches of a VoiceLoop."""

    def __init__(self, ptt: bool = True, starts_turn: bool = True):
        self.ptt = ptt
        self.starts_turn = starts_turn
        self._ptt_open = False
        self._ptt_buf: list = []
        self._turn_task = None
        self._mic = None
        self.frames_seen = 0
        self.capture_error = ""
        self.vad = FakeVad()
        self.voice = FakeVoice()
        self.brain = object()
        self.calls: list[str] = []
        self.asked: list[str] = []
        self.on_state = None
        self.on_transcript = None

    def stop(self):
        self.calls.append("stop")

    def ask(self, text):
        self.asked.append(text)

    def ptt_begin(self):
        self.calls.append("ptt_begin")
        self._ptt_open = True

    def ptt_cancel(self):
        self.calls.append("ptt_cancel")
        return True

    def ptt_end(self):
        self.calls.append("ptt_end")
        self._ptt_open = False
        if self.starts_turn:
            self._turn_task = object()          # "a new turn task exists"

    def reopen_mic(self, device=None, *, changed=False):
        self.calls.append(f"reopen_mic:{device}")


class FakeHub:
    def __init__(self):
        self.statuses: list[tuple] = []
        self.levels: list[tuple[float, float]] = []
        self.timings: list[dict] = []
        self.bargeins = 0
        self.states: list[str] = []
        self.transcripts: list[tuple] = []
        self.quit_requested = False
        self.on_control = None
        self.on_settings = None

    async def status(self, service, state, detail="", last_error="", remember=True):
        self.statuses.append((service, state, detail, remember))

    async def level(self, level, speech):
        self.levels.append((level, speech))

    async def timing(self, **fields):
        self.timings.append(fields)

    async def bargein(self):
        self.bargeins += 1

    async def state(self, name):
        self.states.append(name)

    async def transcript(self, text, accepted=True, reason=""):
        self.transcripts.append((text, accepted, reason))


async def settle():
    for _ in range(3):
        await asyncio.sleep(0)


def frames(n: int, amplitude: float) -> list[np.ndarray]:
    rng = np.random.default_rng(1)
    return [(rng.standard_normal(vad_frame_samples()) * amplitude).astype("float32") for _ in range(n)]


# ---------------------------------------------------------------- the page's buttons
def test_the_stop_button_is_a_full_stop_and_new_topic_is_the_nudge():
    async def run():
        hub, loop = FakeHub(), FakeLoop()
        page = page_control.PageControl(hub, loop, sanitize=lambda s: s)
        page.attach()
        assert hub.on_control == page.from_browser and hub.on_settings == page.settings_saved
        hub.on_control("new_topic")
        hub.on_control("quit")
        await settle()
        return hub, loop

    hub, loop = asyncio.run(run())
    assert loop.asked == [page_control.TOPIC_NUDGE]
    assert hub.quit_requested is True and loop.calls == ["stop"]
    assert ("ptt", "topic", "finding a new topic...", False) in hub.statuses


def test_start_and_cancel_are_the_push_to_talk_edges_with_an_answer_for_every_press():
    async def run():
        hub, loop = FakeHub(), FakeLoop(ptt=True)
        page = page_control.PageControl(hub, loop, sanitize=lambda s: s)
        page.from_browser("start")
        page.from_browser("cancel")
        await settle()
        free = FakeLoop(ptt=False)
        page_free = page_control.PageControl(hub, free, sanitize=lambda s: s)
        page_free.from_browser("start")
        await settle()
        return hub, loop, free

    hub, loop, free = asyncio.run(run())
    assert loop.calls == ["ptt_begin", "ptt_cancel"]
    states = [s[1] for s in hub.statuses if s[0] == "ptt"]
    assert states == ["recording", "cancelled", "off"]
    assert free.calls == []                              # hands-free: the press does nothing
    assert all(remember is False for (_, _, _, remember) in hub.statuses)


def test_stop_reports_what_the_press_produced_in_seconds_of_real_frames():
    """Empty, silent, sent, too short, busy — measured with the VAD's frame size, not a literal."""
    def stop_with(buf, *, starts_turn):
        async def run():
            hub, loop = FakeHub(), FakeLoop(starts_turn=starts_turn)
            loop._ptt_buf = buf
            page_control.PageControl(hub, loop, sanitize=lambda s: s).from_browser("stop")
            await settle()
            assert loop.calls == ["ptt_end"]
            return [(s[1], s[2]) for s in hub.statuses if s[0] == "ptt"]
        return asyncio.run(run())

    assert stop_with([], starts_turn=False) == [("empty", "no audio arrived from the microphone")]
    one_second = round(audio_mod.SAMPLE_RATE / vad_frame_samples())
    [(state, detail)] = stop_with(frames(one_second, 0.0), starts_turn=True)
    assert state == "silent" and "1.0s" in detail
    [(state, detail)] = stop_with(frames(one_second, 0.01), starts_turn=True)
    assert state == "sent" and detail.startswith("sent 1.0s")
    [(state, _)] = stop_with(frames(3, 0.01), starts_turn=False)          # ~0.1 s < min_speech_ms
    assert state == "short"
    [(state, _)] = stop_with(frames(one_second, 0.01), starts_turn=False)
    assert state == "busy"


def test_the_settings_panel_changes_devices_live_and_the_tutor_in_the_background(monkeypatch):
    from backend import config

    class Fresh:
        AUDIO_INPUT_DEVICE = "mic-2"
        AUDIO_OUTPUT_DEVICE = "spk-2"
        TUTOR_PERSONA = "haru"

    monkeypatch.setattr(config, "load", lambda *a, **k: Fresh())
    switched: list[str] = []

    async def switch_persona(name):
        switched.append(name)
        return object()

    async def run():
        hub, loop = FakeHub(), FakeLoop()
        page = page_control.PageControl(hub, loop, switch_persona=switch_persona, sanitize=lambda s: s)
        page.settings_saved(["AUDIO_INPUT_DEVICE", "AUDIO_OUTPUT_DEVICE", "TUTOR_PERSONA"])
        for _ in range(10):
            await asyncio.sleep(0)
        return hub, loop

    hub, loop = asyncio.run(run())
    assert loop.calls == ["reopen_mic:mic-2"] and loop.voice.device == "spk-2"
    assert switched == ["haru"] and loop.asked == [page_control.TUTOR_NUDGE]
    assert [s[1] for s in hub.statuses if s[0] == "tutor"] == ["switching", "ok"]


# ---------------------------------------------------------------- sanitised at the boundary
def test_a_failed_refresh_reaches_the_page_without_the_token(monkeypatch):
    """Spec §11: the error string goes through the registry's sanitiser, once, before hub.status."""
    reg = StatusRegistry()
    reg.register_secret(WK)

    async def resync():
        raise RuntimeError(f"WaniKani said no: Authorization: Bearer {WK}")

    async def run():
        hub = FakeHub()
        page = page_control.PageControl(hub, FakeLoop(), resync=resync, sanitize=reg.sanitize)
        await page.do_resync()
        return hub

    hub = asyncio.run(run())
    failed = [s for s in hub.statuses if s[0] == "resync" and s[1] == "failed"]
    assert len(failed) == 1
    detail = failed[0][2]
    assert WK not in detail and "Bearer ***" in detail
    assert failed[0][3] is False                                  # an event, not a state


def test_refresh_without_a_resync_says_so_and_a_good_one_rotates():
    class Rotator:
        def __init__(self):
            self.reasons: list[str] = []

        async def rotate_next_turn(self, reason):
            self.reasons.append(reason)

    async def ok():
        pass

    async def run():
        hub, rot = FakeHub(), Rotator()
        await page_control.PageControl(hub, FakeLoop(), sanitize=lambda s: s).do_resync()
        await page_control.PageControl(hub, FakeLoop(), resync=ok, rotator=rot, sanitize=lambda s: s).do_resync()
        return [s[1:3] for s in hub.statuses if s[0] == "resync"], rot.reasons

    statuses, reasons = asyncio.run(run())
    assert statuses[:2] == [("syncing", "fetching your WaniKani and Bunpro progress..."),
                            ("failed", "refresh is not available in this mode")]
    assert statuses[-1][0] == "done" and reasons == ["study data refreshed"]


def test_tool_records_are_sanitised_before_the_turn_log():
    reg = StatusRegistry()
    reg.register_secret(BP)
    tools = [{"name": "get_ghost_reviews", "args": {"q": f"Token token={BP}"},
              "ok": False, "error": f"HTTP 401 for {BP}", "ms": 12}]
    clean = orchestrator.sanitised(tools, reg.sanitize)
    assert clean[0]["ms"] == 12 and clean[0]["ok"] is False
    assert BP not in repr(clean) and clean[0]["args"]["q"] == "Token token=***"
    assert tools[0]["error"].endswith(BP)                          # the input is untouched


# ---------------------------------------------------------------- the page's meter
def test_the_level_sender_sends_the_newest_value_at_ten_hertz_not_a_task_per_frame():
    async def run():
        hub = FakeHub()
        sender = page_control.LevelSender(hub, every_s=0.02)
        sender.start()
        for burst in (1, 2):
            for i in range(50):                    # a burst of frames between two ticks
                sender.offer(burst + i / 1000, 0.5)
            await asyncio.sleep(0.06)              # at least one tick, however coarse the timer
            assert len(hub.levels) == burst        # ...and ONE send, of the newest value
            assert hub.levels[-1] == (burst + 0.049, 0.5)
        await asyncio.sleep(0.06)                  # nothing offered: nothing sent
        sender.stop()
        for _ in range(3):
            await asyncio.sleep(0)
        return hub.levels, sender

    levels, sender = asyncio.run(run())
    assert len(levels) == 2 and sender.latest is None and sender.sent == 2
    assert sender._task.cancelled()


# ---------------------------------------------------------------- console rendering
def test_the_timing_line_and_the_page_record_carry_play_beside_audio():
    t = TurnTiming(speech_end_at=0.0, stt_ms=200, first_chunk_ms=1500, first_audio_ms=1800,
                   first_play_ms=1950, total_ms=4000, ttft_ms=900, thinking_chars=40)
    t.session_p50_ms, t.session_p90_ms = 1950.0, 1950.0
    line = terminal.timing_line(t, [1950.0], warn_s=5.0)
    assert "audio 1800ms" in line and "play 1950ms" in line and "voice→voice 1950ms" in line
    assert "OVER" not in line and "thinking 40 chars" in line
    assert "OVER 1.0s" in terminal.timing_line(t, [1950.0], warn_s=1.0)

    async def run():
        hub = FakeHub()
        page_control.PageControl(hub, FakeLoop(), sanitize=lambda s: s).timing(t, 7)
        await settle()
        return hub.timings

    [sent] = asyncio.run(run())
    assert sent["first_play_ms"] == 1950 and sent["first_audio_ms"] == 1800 and sent["turns"] == 7


def test_the_session_report_uses_nearest_rank_percentiles_of_the_play_time():
    timings = [TurnTiming(speech_end_at=0.0, first_audio_ms=1000 + i, first_play_ms=2000 + i * 100)
               for i in range(10)]
    line = terminal.session_report(timings)
    assert line.startswith("\n") and "10 turns" in line
    assert "median 2.40s" in line and "p90 2.80s" in line and "budget 5.0s" in line   # ranks 5 and 9 of 10
    assert terminal.session_report([TurnTiming(speech_end_at=0.0)]) is None


def test_the_meter_holds_the_peak_between_redraws_and_labels_push_to_talk():
    drawn: list[str] = []
    meter = terminal.LevelMeter(out=drawn.append)
    meter.last = 0.0
    first = meter.update(0.02, 0.9, hot=True, ptt=None)            # redraws immediately
    assert first == 0.02 and "HEARING YOU" in drawn[-1]
    held = meter.update(0.001, 0.1, hot=False, ptt="open")           # too soon: no redraw
    assert len(drawn) == 1 and held == 0.02 * 0.45                   # the decayed hold, not 0.001
    assert "RECORDING" in terminal.LevelMeter.render(0.02, 0.1, hot=False, ptt="open")
    assert "SPACE to talk" in terminal.LevelMeter.render(0.0, 0.1, hot=False, ptt="closed")
    assert "listening" in terminal.LevelMeter.render(0.0, 0.1, hot=False, ptt=None)


# ---------------------------------------------------------------- the entry point
def test_the_command_line_keeps_its_flags_and_listen_implies_speak():
    args = repl.parse(["--listen", "--no-srs", "--refresh", "--no-open", "--show"])
    assert args.listen and args.speak and args.no_srs and args.refresh and args.no_open and args.show
    assert not repl.parse([]).speak and repl.parse(["--browser"]).speak
    assert repl._stop_containers is orchestrator.stop_containers   # test_app / up.py surface
