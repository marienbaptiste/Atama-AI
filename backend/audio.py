"""Audio devices, playback and capture (spec §8/§9, M2).

Thin wrapper over PortAudio (`sounddevice`). Two jobs:

* enumerate input/output devices so the settings interface can offer a picker (ADR-022), and
  resolve a saved choice back to a device — by name, so a saved setting survives the index
  shuffling that Windows does when devices come and go;
* play a synthesised WAV, and capture microphone frames as 16 kHz mono PCM16 for the VAD
  (spec §9 — raw frames, never encoded audio).

    python -m backend.audio            # list devices
    python -m backend.audio --test     # play a tone on the configured output
    python -m backend.audio --meter    # live input level + speech probability
"""
from __future__ import annotations

import io
import sys
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import numpy as np

#: The pipeline speaks PCM16 mono at this rate end to end (spec §9).
SAMPLE_RATE = 16000
FRAME_MS = 32
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


class AudioUnavailable(RuntimeError):
    """No usable audio backend or device. The app should degrade, not crash."""


def _sd():
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:  # PortAudio missing on headless boxes
        raise AudioUnavailable(f"audio backend unavailable: {exc}") from None
    return sd


#: Windows publishes the SAME physical device under every host API, so a name match can land on
#: a duplicate that cannot be opened at all. Both rates this app uses are fixed — 16 kHz capture
#: for the VAD and Whisper, 24 kHz playback for VOICEVOX — and that is what decides the list.
#: Verified on this machine 2026-09-09, opening a stream at each rate:
#:   MME                 in 16k OK          out 24k OK
#:   Windows DirectSound in 16k OK          out 24k OK
#:   Windows WASAPI      in 16k FAIL        out 24k FAIL   "Invalid sample rate" — shared mode
#:                                                         plays at the device rate and will not
#:                                                         resample. Do not "restore" it here
#:                                                         without a resampler.
#:   Windows WDM-KS      in 16k FAIL        out 24k FAIL   "Blocking API not supported yet" —
#:                                                         sounddevice's blocking read/write is
#:                                                         unsupported on this host API.
#: Ranked best-first; anything absent is unusable and is never auto-selected.
USABLE_HOSTAPIS = ("MME", "Windows DirectSound")
CAPTURE_HOSTAPIS = USABLE_HOSTAPIS
PLAYBACK_HOSTAPIS = USABLE_HOSTAPIS

#: A lost or missing device is looked for again this often.
RETRY_S = 2.0
#: While on the system default because the chosen device is missing, how often to check whether
#: it has come back. Each check closes and reopens the stream (a gap of a fraction of a second),
#: so it only runs between turns — never while push-to-talk is held.
PROBE_S = 5.0
#: Windows' MME "Sound Mapper" entries route to whatever the OS default device is, so they are
#: the best meaning of "system default". Verified 2026-09-10: the output mapper opens at 24 kHz.
#: That an open mapper stream FOLLOWS a live change of the OS default is Windows' wave-mapper
#: behaviour and has not been observed here yet.
MAPPER_PREFIX = "Microsoft Sound Mapper"
#: DirectSound's own alias for the default device; like the mapper, not a device to pick by name.
DEFAULT_ALIASES = ("Primary Sound Driver", "Primary Sound Capture Driver")


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    kind: str                # "input" | "output"
    channels: int
    samplerate: int
    is_default: bool = False
    hostapi: str = ""
    usable: bool = True      # False = enumerable but cannot be opened (see CAPTURE_HOSTAPIS)

    def as_option(self) -> dict[str, Any]:
        """One entry for the settings picker."""
        return {"value": self.name, "index": self.index, "label": self.name,
                "kind": self.kind, "default": self.is_default,
                "hostapi": self.hostapi, "usable": self.usable}


def _hostapi_names(sd) -> list[str]:
    """Host API names by index; empty list when the backend does not report them."""
    try:
        return [str(h.get("name", "")) for h in sd.query_hostapis()]
    except (AttributeError, TypeError, ValueError):
        return []


def list_devices(kind: str | None = None) -> list[Device]:
    """Every device, each flagged `usable` for its direction (see CAPTURE_HOSTAPIS)."""
    sd = _sd()
    try:
        defaults = tuple(sd.default.device)
    except (TypeError, ValueError):
        defaults = (-1, -1)
    apis = _hostapi_names(sd)
    out: list[Device] = []
    for index, info in enumerate(sd.query_devices()):
        api = apis[info["hostapi"]] if apis and isinstance(info.get("hostapi"), int) else ""
        for want, channels_key, default_index, allowed in (
                ("input", "max_input_channels", defaults[0], CAPTURE_HOSTAPIS),
                ("output", "max_output_channels", defaults[1], PLAYBACK_HOSTAPIS)):
            channels = int(info.get(channels_key) or 0)
            if channels <= 0 or (kind and kind != want):
                continue
            # No host-api information (a stub, or a platform that does not report them) means we
            # cannot rule anything out — assume usable rather than hiding every device.
            usable = (not api) or (api in allowed)
            out.append(Device(index=index, name=str(info.get("name", f"device {index}")), kind=want,
                              channels=channels, samplerate=int(info.get("default_samplerate") or 0),
                              is_default=(index == default_index), hostapi=api, usable=usable))
    return out


def _rank(device: Device) -> int:
    """Best-first ordering among devices that match the same name (lower wins)."""
    allowed = CAPTURE_HOSTAPIS if device.kind == "input" else PLAYBACK_HOSTAPIS
    try:
        return allowed.index(device.hostapi)
    except ValueError:
        return len(allowed)


def resolve_device(spec: str | int | None, kind: str) -> int | None:
    """A saved setting -> a device index. None means "use the system default".

    Accepts an index or a name (exact, then case-insensitive substring). Names are preferred in
    settings because indices are not stable across reboots or device plug/unplug.

    One name matches several devices on Windows, because the same microphone is published under
    every host API — and some of those cannot be opened at all (see CAPTURE_HOSTAPIS). Matching
    used to return whichever came first, which is how "Realtek" resolved to a WDM-KS entry that
    failed with "Blocking API not supported yet" (2026-09-09). Unusable entries are now skipped
    and the rest are ranked, so a name resolves to a device that actually opens.
    """
    if spec is None or str(spec).strip() == "":
        return None
    text = str(spec).strip()
    devices = list_devices(kind)
    if text.lstrip("-").isdigit():
        index = int(text)
        # An explicit index is the user overriding us; honour it even if we think it is unusable.
        return index if any(d.index == index for d in devices) else None
    usable = [d for d in devices if d.usable]
    exact = [d for d in usable if d.name == text]
    lowered = text.lower()
    partial = [d for d in usable if lowered in d.name.lower()]
    for candidates in (exact, partial):
        if candidates:
            return min(candidates, key=_rank).index
    return None


def _system_default(kind: str) -> int | None:
    """The MME Sound Mapper when there is one (it follows the OS default), else PortAudio's."""
    try:
        devices = list_devices(kind)
    except AudioUnavailable:
        return None
    for d in devices:
        if d.usable and d.name.startswith(MAPPER_PREFIX):
            return d.index
    return None


def pick(spec: str | int | None, kind: str) -> tuple[int | None, bool]:
    """(device index, fell_back). The chosen device when it is connected; otherwise the system
    default, with `fell_back` True so the caller can say so and look for it again later."""
    wanted = "" if spec is None else str(spec).strip()
    index = resolve_device(wanted, kind) if wanted else None
    if index is not None:
        return index, False
    return _system_default(kind), bool(wanted)


def device_name(index: int | None, kind: str) -> str:
    try:
        sd = _sd()
        if index is None:
            index = sd.default.device[0 if kind == "input" else 1]
        return str(sd.query_devices(index)["name"])
    except Exception:  # noqa: BLE001 - a name is for display; never fail over it
        return "system default"


#: Open streams, so `rescan` never re-initialises PortAudio underneath one.
_pa_lock = threading.Lock()
_open_streams = 0


def _track(delta: int) -> None:
    global _open_streams
    with _pa_lock:
        _open_streams += delta


def rescan() -> bool:
    """Re-read the device list from the OS. Returns False when it was not safe to.

    PortAudio snapshots the devices when it initialises, so a headset plugged in after launch is
    invisible until this runs. Terminating PortAudio under an open stream would tear that stream
    down mid-read, so this refuses while any stream is open; callers re-scan after closing theirs.
    """
    sd = _sd()
    with _pa_lock:
        if _open_streams > 0:
            return False
        try:
            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001 - an unscannable system keeps the old list
            return False
    return True


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """WAV bytes -> (float32 samples in [-1, 1], samplerate). Mono or stereo in, mono out."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise AudioUnavailable(f"expected 16-bit PCM WAV, got {width * 8}-bit")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def play(wav_bytes: bytes, device: str | int | None = None, blocking: bool = True) -> float:
    """Play a WAV. Returns its duration in seconds."""
    sd = _sd()
    samples, rate = decode_wav(wav_bytes)
    sd.play(samples, samplerate=rate, device=resolve_device(device, "output"), blocking=blocking)
    return len(samples) / float(rate or SAMPLE_RATE)


def stop() -> None:
    """Cut playback immediately — this is what barge-in calls (spec §8)."""
    try:
        _sd().stop()
    except AudioUnavailable:
        pass


class Player:
    """One output stream, held open for the whole session.

    `sd.play()` opens a fresh stream per call, and the device drops roughly its first 100 ms
    while it spins up — so EVERY sentence lost its opening syllable and faded in (reported and
    reproduced 2026-09-09). Keeping the stream running means audio starts the instant we write.

    Writing in blocks also makes barge-in immediate: `cancel()` is noticed between blocks
    instead of after the sentence finishes.
    """

    #: VOICEVOX synthesises at 24 kHz mono (verified 0.25.2).
    DEFAULT_RATE = 24000
    BLOCK = 1024
    #: Silence written while idle, small enough that a sentence never waits long for
    #: the lock (~11 ms at 24 kHz).
    KEEPALIVE_BLOCK = 256

    def __init__(self, device: str | int | None = None, samplerate: int = DEFAULT_RATE):
        self._sd = _sd()
        self._spec = device
        self._device: int | None = None
        self._rate = samplerate
        self._stream = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._keeper: threading.Thread | None = None

    def _ensure(self, rate: int) -> None:
        if self._stream is not None and self._rate == rate:
            return
        # NOT close(): this runs with the lock held, and close() joins the
        # keep-alive thread, which is itself waiting for that lock.
        self._close_stream()
        self._rate = rate
        # Resolved at every open, not once: after a device drops out, this is what lands the
        # reopened stream on whatever is there now — the chosen device or the system default.
        self._device, _ = pick(self._spec, "output")
        stream = self._sd.OutputStream(samplerate=rate, channels=1, dtype="float32",
                                       device=self._device, blocksize=self.BLOCK)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._stream = stream
        _track(+1)
        # Prime with a few blocks of silence so the very first sample of real audio lands in a
        # stream that is already running, not one that is still starting.
        self._stream.write(np.zeros(self.BLOCK * 2, dtype=np.float32))

    def open(self, rate: int | None = None) -> None:
        """Open the stream NOW and keep it fed, so the first sentence is not clipped.

        Opening early was not enough on its own. The stream sat with nothing written to it for
        the ~10 s the brain spends generating, underran, and the device went idle — so the first
        real write paid the spin-up all over again and ate こ off こんにちは (2026-09-10). With
        VOICEVOX_PRE_PHONEME at 0.0 there is only ~19 ms of lead-in to absorb that.

        A keep-alive thread writes silence whenever nothing else is playing, which holds the
        device open and also covers the gaps between sentences.
        """
        with self._lock:
            self._ensure(rate or self._rate)
        if self._keeper is None:
            self._stop.clear()
            self._keeper = threading.Thread(target=self._keepalive, name="player-keepalive",
                                            daemon=True)
            self._keeper.start()

    def _keepalive(self) -> None:
        """Write silence while idle. Blocking writes pace this at real time by themselves."""
        silence = np.zeros(self.KEEPALIVE_BLOCK, dtype=np.float32)
        while not self._stop.is_set():
            broken = False
            with self._lock:
                if self._stream is not None:
                    try:
                        self._stream.write(silence)
                        continue
                    except Exception:  # noqa: BLE001 - the device went away (PortAudioError)
                        # This used to `return`, which ended the keep-alive for good: one
                        # unplug and every later sentence paid the spin-up clip again.
                        self._close_stream()
                        broken = True
                else:
                    try:
                        self._ensure(self._rate)   # reopen after a loss, on whatever is there now
                        continue
                    except Exception:  # noqa: BLE001 - nothing to play on yet; try again soon
                        broken = True
            if broken:
                self._stop.wait(RETRY_S)
                rescan()

    def play(self, wav_bytes: bytes) -> float:
        """Play a WAV through the open stream. Returns seconds actually played."""
        samples, rate = decode_wav(wav_bytes)
        with self._lock:
            self._cancel.clear()
            for attempt in (1, 2):
                try:
                    self._ensure(rate)
                    written = 0
                    for start in range(0, len(samples), self.BLOCK):
                        if self._cancel.is_set():
                            break
                        block = samples[start:start + self.BLOCK]
                        self._stream.write(np.ascontiguousarray(block, dtype=np.float32))  # type: ignore[union-attr]
                        written += len(block)
                    return written / float(rate or SAMPLE_RATE)
                except AudioUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - PortAudioError: the device went away
                    # Drop the dead stream; the retry reopens on whatever is connected now and
                    # plays the sentence from the top. A second failure is reported, not raised
                    # as a PortAudioError nobody upstream knows to catch.
                    self._close_stream()
                    if attempt == 2:
                        raise AudioUnavailable(f"no playback device: {exc}") from None
                    rescan()
        return 0.0

    def switch(self, device: str | int | None) -> None:
        """Play on a different output from now on — the settings panel, live."""
        with self._lock:
            self._spec = device
            self._close_stream()      # the keep-alive (or the next sentence) reopens on the choice

    def cancel(self) -> None:
        """Stop the sentence in flight. Safe from another thread — that is the point."""
        self._cancel.set()

    def _close_stream(self) -> None:
        """Tear down just the stream. Caller holds the lock."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - closing audio must never raise on shutdown
                pass
            _track(-1)

    def close(self) -> None:
        # Stop the keeper FIRST and outside the lock, or the join waits on a thread
        # that is blocked acquiring it.
        self._stop.set()
        keeper, self._keeper = self._keeper, None
        if keeper is not None and keeper.is_alive():
            keeper.join(timeout=1.0)
        with self._lock:
            self._close_stream()


def capture(device: str | int | None = None, frame_samples: int = FRAME_SAMPLES) -> Iterator[np.ndarray]:
    """Yield mono float32 frames at SAMPLE_RATE until the caller stops iterating.

    PortAudio resamples to 16 kHz for us, so the VAD and Whisper both get exactly what spec §9
    asks for without a resampling step of our own.
    """
    sd = _sd()
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=frame_samples, device=resolve_device(device, "input")) as stream:
        while True:
            frame, overflowed = stream.read(frame_samples)
            yield frame[:, 0].copy()


def capture_resilient(device: Any = None, frame_samples: int = FRAME_SAMPLES,
                      stop: threading.Event | None = None,
                      on_status: Callable[[str, str], None] | None = None,
                      idle: Callable[[], bool] = lambda: True,
                      wake: threading.Event | None = None) -> Iterator[np.ndarray]:
    """`capture`, but it survives the microphone going away (spec §9).

    * The chosen device is not connected (at launch or later) -> the system default, reported as
      `fallback`; every PROBE_S, between turns only (`idle()`), it looks again and switches back.
    * No input device at all -> `missing`, retried every RETRY_S until one appears.
    * The device vanishes mid-read (PortAudioError) -> `lost`, then the same reopen path.

    Every reopen re-scans first, because PortAudio's device list is a snapshot taken at start-up
    (`rescan`). `on_status(state, detail)` fires on change only: ok | fallback | missing | lost.

    `device` may be a callable, read at every (re)open, so a new choice from the settings panel
    applies live. With `wake`, reopening is event-driven — set it and the stream reopens at the
    next idle moment (the device watcher saw a change, or the choice changed); without it, a
    timer probes every PROBE_S while on the fallback.
    """
    sd = _sd()
    stop = stop or threading.Event()
    last: list = [None]

    def report(state: str, detail: str) -> None:
        if on_status is not None and last[0] != (state, detail):
            last[0] = (state, detail)
            on_status(state, detail)

    first = True
    while not stop.is_set():
        if not first:
            rescan()
        first = False
        spec = device() if callable(device) else device
        index, fell_back = pick(spec, "input")
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                    blocksize=frame_samples, device=index)
            stream.start()
        except Exception:  # noqa: BLE001 - PortAudioError "Error querying device -1": none at all
            report("missing", "no microphone found - plug one in and it will be picked up")
            stop.wait(RETRY_S)
            continue
        _track(+1)
        name = device_name(index, "input")
        if fell_back:
            report("fallback", f"'{spec}' is not connected - using the system default ({name})")
        else:
            report("ok", name)
        probe_at = time.monotonic() + PROBE_S
        try:
            while not stop.is_set():
                frame, _overflowed = stream.read(frame_samples)
                yield frame[:, 0].copy()
                if wake is not None:
                    # Event-driven: the device watcher (a new device list) or the settings panel
                    # (a new choice) asks for a reopen. Never mid push-to-talk.
                    if wake.is_set() and idle():
                        wake.clear()
                        break
                elif fell_back and time.monotonic() >= probe_at:
                    if idle():
                        break                  # reopen: the chosen device may be back
                    probe_at = time.monotonic() + 1.0
        except Exception as exc:  # noqa: BLE001 - PortAudioError when the device is pulled out
            report("lost", f"microphone disconnected ({type(exc).__name__}) - reconnecting")
            stop.wait(RETRY_S / 4)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - a dead device may refuse even this
                pass
            _track(-1)


def to_pcm16(frame: np.ndarray) -> bytes:
    """float32 [-1, 1] -> PCM16 bytes, the wire format of spec §8's `audio_chunk`."""
    return (np.clip(frame, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


#: Below this RMS, the microphone is delivering *digital silence* — muted at the OS or hardware
#: level, or blocked by privacy settings. Measured 2026-09-09 on the same machine:
#:   muted headset        mean rms 0.000015
#:   live but idle room   mean rms 0.000150, peaks 0.003
#: An order of magnitude separates them, so the bar sits between — set too high (0.0005) this
#: cried wolf at a working microphone that simply had nobody talking into it.
SILENT_RMS = 0.00005


def input_level(device: str | int | None = None, seconds: float = 1.0) -> float:
    """RMS of a short capture. 0.0 means nothing is arriving at all."""
    sd = _sd()
    frames = []
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=FRAME_SAMPLES, device=pick(device, "input")[0]) as stream:
        for _ in range(max(1, int(seconds * SAMPLE_RATE / FRAME_SAMPLES))):
            frames.append(stream.read(FRAME_SAMPLES)[0][:, 0])
    audio = np.concatenate(frames) if frames else np.zeros(1, dtype=np.float32)
    return float(np.sqrt(np.mean(np.square(audio))))


def meter(device: str | int | None = None, seconds: float = 30.0) -> int:
    """Live input level and speech probability, so a dead microphone is visible, not mysterious."""
    from backend import config
    from backend.vad import VoiceActivityDetector

    sd = _sd()
    resolved = resolve_device(device, "input")
    name = sd.query_devices(resolved if resolved is not None else sd.default.device[0])["name"]
    print(f"listening on: {name}")
    print("speak — the bar should move. Ctrl+C to stop.\n")
    try:
        vad = VoiceActivityDetector.from_config(config.load())
    except Exception:
        vad = None
    peak_seen = 0.0
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=FRAME_SAMPLES, device=resolved) as stream:
            for _ in range(int(seconds * SAMPLE_RATE / FRAME_SAMPLES)):
                frame = stream.read(FRAME_SAMPLES)[0][:, 0]
                level = float(np.sqrt(np.mean(np.square(frame))))
                peak_seen = max(peak_seen, level)
                prob = vad.probability(frame) if vad is not None else 0.0
                bars = int(min(1.0, level * 20) * 40)
                flag = " SPEECH" if prob >= 0.5 else ""
                # Six decimals: a muted mic reads 0.00002, which four decimals rounds to 0.0000
                # and makes indistinguishable from a device delivering literally nothing.
                print(f"\r|{'#' * bars:<40}| rms {level:.6f}  speech {prob:.2f}{flag}   ",
                      end="", flush=True)
    except KeyboardInterrupt:
        pass          # Ctrl+C is how this tool is meant to end, not a crash
    print()
    if peak_seen < SILENT_RMS:
        print(f"\nNothing arrived (peak rms {peak_seen:.5f}). The stream opened, so the device exists —")
        print("it is muted or blocked, not missing. Check, in order:")
        print("  1. the physical mute on the headset/mic")
        print("  2. Windows Settings > Privacy & security > Microphone > 'Let desktop apps access'")
        print("  3. Windows Sound settings > Input > the device level is not 0")
        return 1
    return 0


def _main(argv: list[str]) -> int:
    try:
        devices = list_devices()
    except AudioUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    for kind in ("input", "output"):
        print(f"\n{kind.upper()}")
        for d in (x for x in devices if x.kind == kind):
            mark = "*" if d.is_default else " "
            flag = "" if d.usable else "  <- cannot be opened"
            print(f"  [{d.index:>2}] {mark} {d.name[:42]:<42} {d.hostapi[:20]:<20} "
                  f"{d.samplerate or 0:>5} Hz{flag}")
    unusable = [d for d in devices if not d.usable]
    print("\n* = system default. Put a NAME (or a substring) in AUDIO_INPUT_DEVICE /"
          "\nAUDIO_OUTPUT_DEVICE — names survive reboots, indices do not. The same device"
          "\nappears once per host API; a name resolves to the best one that actually opens.")
    if unusable:
        print(f"\n{len(unusable)} entries cannot be opened. Capture is fixed at 16 kHz for the VAD and"
              f"\nWhisper: WASAPI refuses to resample and WDM-KS has no blocking API. A device that"
              f"\nappears ONLY there is not selectable — enable it in Windows Sound settings so it"
              f"\nalso shows up under MME or DirectSound.")
    if "--meter" in argv:
        from backend import config
        return meter(config.load().AUDIO_INPUT_DEVICE)
    if "--test" in argv:
        from backend import config
        cfg = config.load()
        tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(SAMPLE_RATE) / SAMPLE_RATE)).astype(np.float32)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(SAMPLE_RATE)
            w.writeframes((tone * 32767).astype("<i2").tobytes())
        print(f"\nplaying a 1 s tone on {cfg.AUDIO_OUTPUT_DEVICE or 'the system default'}…")
        play(buf.getvalue(), cfg.AUDIO_OUTPUT_DEVICE)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
