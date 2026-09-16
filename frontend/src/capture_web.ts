/** The browser behind capture.ts (ADR-040): getUserMedia, one AudioContext, the worklet.
 *
 *  Not unit-tested — there is no microphone in node — and kept thin for that reason: everything
 *  that can go wrong in a way worth reasoning about (denied, missing, fallback, lost, a device
 *  list that changes) is decided in capture.ts against a fake of this.
 *
 *  The constraints ask for the browser's echo cancellation — ADR-018's first layer against
 *  self-barge-in on speakers, which the sounddevice path could not have — and nothing else (see
 *  below). `channelCount: 1` is a request, not a guarantee; the worklet reads channel 0 either way. */
import workletUrl from "./capture_worklet.ts?worker&url";
import type { MicSource } from "./capture";
import { PROCESSOR, RATE } from "./pcm";

//: One AudioContext for capture. On the DESKTOP it is created inside the first touch
//: (`prepareAudio`) at 16 kHz and kept — that path works and is not touched (user, 2026-09-16).
//: On a PHONE it is created AFTER getUserMedia has opened the microphone, at the device's own
//: rate, and remade for every reopen: activating a microphone changes a phone's audio session —
//: iOS moves to a voice-processing rate, Android re-opens its output — and a context made earlier
//: keeps its old rate and delivers stretched, mangled audio. Either way it is resumed on every
//: talk press, because a phone browser suspends a context made outside a gesture, and a suspended
//: one never runs the worklet: the microphone opens and nothing arrives.
let ctx: AudioContext | null = null;
let phone = false;
//: Echo cancellation is the phone page's own switch (Settings → Sound in mobile mode), remembered
//: in that browser: on a loudspeaker the browser's canceller can gate the student's own voice
//: right after hers, so a lesson on headphones may turn it off (2026-09-16). The desktop always
//: has it on.
export const AEC_KEY = "atama.aec";
export function echoCancellation(): boolean {
  if (!phone) return true;
  try { return localStorage.getItem(AEC_KEY) !== "off"; } catch { return true; }
}
export function setEchoCancellation(on: boolean): void {
  try { localStorage.setItem(AEC_KEY, on ? "on" : "off"); } catch { /* private window */ }
}

/** On the desktop, a 16 kHz context: the browser resamples the microphone itself, with its own
 *  proper filter, and the worklet passes samples through (Chrome, Firefox and Safari all take the
 *  option; one that refuses it gets the default rate). On a PHONE, the device's own rate: a
 *  context forced to 16 kHz there came out mangled (user, 2026-09-16 — the phone's audio engine
 *  runs at 44.1/48 k and resamples a foreign-rate context badly, both ways), so the worklet takes
 *  the native rate and pcm.ts's own low-pass + interpolation brings it to 16 kHz. */
function makeContext(nativeRate: boolean): AudioContext {
  if (!nativeRate) {
    try {
      return new AudioContext({ sampleRate: RATE });
    } catch { /* fall through to the device's rate */ }
  }
  return new AudioContext();
}

export function prepareAudio(nativeRate = false): void {
  phone = nativeRate;
  if (phone) return;                        // made once the microphone is open (see `ctx`)
  try {
    ctx ??= makeContext(false);
    void ctx.resume().catch(() => { /* the next gesture */ });
  } catch { /* no Web Audio: capture reports it when it opens */ }
}

export function resumeAudio(): void {
  if (ctx && ctx.state !== "running") void ctx.resume().catch(() => { /* the next gesture */ });
}

export function webOpener(): (deviceId: string) => Promise<MicSource> {
  let module: Promise<void> | null = null;
  return async deviceId => {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      const e = new Error(window.isSecureContext
        ? "this browser has no getUserMedia (too old)"
        : "this page is not a secure context, so the browser will not share the microphone - open "
          + "it over https and accept the certificate (Settings → Remote on the computer)");
      e.name = "NotSupportedError";
      throw e;
    }
    // Echo cancellation yes (her voice comes out of the same device, ADR-018); noise suppression
    // and automatic gain NO: both are tuned for a human listener on a call and they smear the
    // consonants and pump the level, which a recogniser hears as a worse speaker (2026-09-16;
    // ADR-006's original constraints had gain control off for the same reason). Whisper and the
    // server's own room-floor measurement do better on the raw microphone.
    // Gain control stays OFF on the desktop (it pumps a headset's level, and that path works —
    // not touched, user 2026-09-16) and ON on a phone: a phone's raw microphone is quiet enough
    // that Whisper heard silence and produced its silence phrases (ご清聴…, ではまた) instead of
    // the sentence (session log, 2026-09-16). Noise suppression stays off everywhere.
    const audio: MediaTrackConstraints = {
      channelCount: 1, echoCancellation: echoCancellation(), noiseSuppression: false, autoGainControl: phone,
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
    };
    const stream = await navigator.mediaDevices.getUserMedia({ audio });
    try {
      if (phone) {                          // a fresh context for THIS microphone, now that it is open
        if (ctx) { const old = ctx; ctx = null; module = null; void old.close().catch(() => undefined); }
        ctx = makeContext(true);
        module = ctx.audioWorklet.addModule(workletUrl);
      } else {
        ctx ??= makeContext(false);
        module ??= ctx.audioWorklet.addModule(workletUrl);
      }
      await module;
    } catch (e) {
      stream.getTracks().forEach(t => t.stop());
      module = null;                        // try loading it again next time
      throw e;
    }
    void ctx.resume().catch(() => { /* it resumes on the next gesture */ });
    const source = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, PROCESSOR, { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
    source.connect(node);
    node.connect(ctx.destination);          // silent output; keeps the node rendering everywhere
    const track = stream.getAudioTracks()[0];
    const settings = track?.getSettings?.() || {};
    // Markers (see `flush`): the worklet echoes them behind every frame it posted before.
    let marks = 0;
    const waiting = new Map<number, () => void>();
    let onFrame: ((frame: ArrayBuffer) => void) | null = null;
    node.port.onmessage = e => {
      const data = e.data as ArrayBuffer | { mark: number };
      if (data instanceof ArrayBuffer) { onFrame?.(data); return; }
      const cb = waiting.get(data.mark);
      waiting.delete(data.mark);
      cb?.();
    };
    return {
      // The label says how the audio is being made, so a bad transcript can be traced: the
      // context's rate (16000 = the browser resamples; else pcm.ts does) and the track's own.
      label: (track?.label || "microphone") + ` · ${ctx.sampleRate} Hz context`
        + (settings.sampleRate ? `, mic ${settings.sampleRate} Hz` : "")
        + (echoCancellation() ? ", echo cancellation" : ", no echo cancellation")
        + (settings.autoGainControl ? ", gain control" : ""),
      onFrame: cb => { onFrame = cb; },
      onEnded: cb => { if (track) track.onended = cb; },
      flush: cb => { waiting.set(++marks, cb); node.port.postMessage({ mark: marks }); },
      stop: () => {
        node.port.onmessage = null;
        onFrame = null;
        waiting.clear();
        try { source.disconnect(); node.disconnect(); } catch { /* already gone */ }
        stream.getTracks().forEach(t => t.stop());
      },
    };
  };
}

/** The microphones the browser will name — labels appear only once permission has been given. */
export async function listMics(): Promise<[string, string][]> {
  try {
    const all = await navigator.mediaDevices.enumerateDevices();
    return all
      .filter(d => d.kind === "audioinput" && d.deviceId && d.deviceId !== "default")
      .map((d, i) => [d.deviceId, d.label || `Microphone ${i + 1}`] as [string, string]);
  } catch {
    return [];
  }
}

export function onDevicesChanged(fn: () => void): void {
  navigator.mediaDevices?.addEventListener?.("devicechange", fn);
}
