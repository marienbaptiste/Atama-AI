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
import { PROCESSOR } from "./pcm";

export function webOpener(): (deviceId: string) => Promise<MicSource> {
  let ctx: AudioContext | null = null;
  let module: Promise<void> | null = null;
  return async deviceId => {
    if (!navigator.mediaDevices?.getUserMedia) {
      const e = new Error("this browser has no getUserMedia (an insecure origin, or too old)");
      e.name = "NotSupportedError";
      throw e;
    }
    const audio: MediaTrackConstraints = {
      channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
    };
    const stream = await navigator.mediaDevices.getUserMedia({ audio });
    try {
      ctx ??= new AudioContext();
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
    return {
      label: track?.label || "microphone",
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
