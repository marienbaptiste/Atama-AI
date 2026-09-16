/** The audio thread's half of capture (ADR-040): every 128-sample quantum from the microphone
 *  goes through Pcm16k, and each complete 16 kHz frame is posted to the page as a transferable
 *  buffer (capture_web.ts forwards it to the socket). Loaded with `?worker&url`, so Vite bundles
 *  pcm.ts into it. The processor writes no output: its one output stays silent, and exists only
 *  so the node can be connected to the destination, which keeps some engines processing it. */
import { Pcm16k, PROCESSOR } from "./pcm";

//: The AudioWorkletGlobalScope's globals, which lib.dom does not declare.
declare const sampleRate: number;
declare class AudioWorkletProcessor { readonly port: MessagePort; }
declare function registerProcessor(name: string, ctor: new () => AudioWorkletProcessor): void;

class Capture16k extends AudioWorkletProcessor {
  private readonly pcm = new Pcm16k(sampleRate);

  constructor() {
    super();
    // A marker from the page comes back through the same port, BEHIND every frame posted before
    // it: that is how the page orders the talk key's release after the audio it captured.
    this.port.onmessage = e => this.port.postMessage(e.data);
  }

  process(inputs: Float32Array[][]): boolean {
    const channel = inputs[0]?.[0];
    if (channel) {
      for (const frame of this.pcm.push(channel)) this.port.postMessage(frame.buffer, [frame.buffer]);
    }
    return true;                           // keep running until the node is disconnected
  }
}

registerProcessor(PROCESSOR, Capture16k);
