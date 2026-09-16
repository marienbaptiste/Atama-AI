/** The browser behind capture.ts (ADR-040): getUserMedia, one AudioContext, the worklet.
 *
 *  Not unit-tested — there is no microphone in node — and kept thin for that reason: everything
 *  that can go wrong in a way worth reasoning about (denied, missing, fallback, lost, a device
 *  list that changes) is decided in capture.ts against a fake of this.
 *
 *  The constraints ask for the browser's echo cancellation, noise suppression and gain control:
 *  ADR-018's first layer against self-barge-in on speakers, which the sounddevice path could not
 *  have. `channelCount: 1` is a request, not a guarantee; the worklet reads channel 0 either way. */
import workletUrl from "./capture_worklet.ts?worker&url";
import type { MicSource } from "./capture";
import { PROCESSOR, RATE } from "./pcm";

//: One AudioContext for capture, created INSIDE a user gesture (`prepareAudio` from the first
//: touch) and resumed on every talk press: a phone browser suspends a context made outside one,
//: and a suspended context never runs the worklet — the microphone opens and nothing arrives.
let ctx: AudioContext | null = null;

/** A 16 kHz context when the browser will give one — then the browser resamples the microphone
 *  itself, with its own proper filter, and the worklet passes samples through. Chrome, Firefox
 *  and Safari all take the option; one that refuses it gets the default rate and pcm.ts's own
 *  low-pass + interpolation (2026-09-16: an unfiltered downsample made Whisper markedly worse). */
function makeContext(): AudioContext {
  try {
    return new AudioContext({ sampleRate: RATE });
  } catch {
    return new AudioContext();
  }
}

export function prepareAudio(): void {
  try {
    ctx ??= makeContext();
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
    const audio: MediaTrackConstraints = {
      channelCount: 1, echoCancellation: true, noiseSuppression: false, autoGainControl: false,
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
    };
    const stream = await navigator.mediaDevices.getUserMedia({ audio });
    try {
      ctx ??= makeContext();
      module ??= ctx.audioWorklet.addModule(workletUrl);
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
    return {
      // The label says how the audio is being made, so a bad transcript can be traced: the
      // context's rate (16000 = the browser resamples; else pcm.ts does) and the track's own.
      label: (track?.label || "microphone") + ` · ${ctx.sampleRate} Hz context`
        + (settings.sampleRate ? `, mic ${settings.sampleRate} Hz` : ""),
      onFrame: cb => { node.port.onmessage = e => cb(e.data as ArrayBuffer); },
      onEnded: cb => { if (track) track.onended = cb; },
      stop: () => {
        node.port.onmessage = null;
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
