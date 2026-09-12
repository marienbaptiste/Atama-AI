// A reloaded page sat on "she is thinking of how to start…" for an opening it had already heard
// (user, 2026-09-12): the server replays the lesson, and the caption follows server truth.
import { describe, expect, it } from "vitest";
import { applyHistory, loadingCaption, type Transcript } from "./history";
import type { HistoryLine, ServiceStatusMsg } from "./protocol.gen";

function recorder() {
  const seen: string[] = [];
  const chat: Transcript = {
    her: (line, cut) => { seen.push(`her:${line.text}${cut ? "(cut)" : ""}:${line.grammar.map(g => g.point).join(",")}`); },
    you: (text, accepted, reason) => { seen.push(`you:${text}:${accepted}:${reason}`); },
    ready: () => { seen.push("ready"); },
  };
  return { seen, chat };
}

const her = (text: string, extra: Partial<HistoryLine> = {}): HistoryLine => ({
  who: "her", text, emotion: "", turn: 1, grammar: [], target: "", used: "", used_kind: "",
  readings: [], vocab: [], cut: false, accepted: true, reason: "", ...extra,
});
const you = (text: string, extra: Partial<HistoryLine> = {}): HistoryLine => ({ ...her(text), who: "you", ...extra });

const brainReady: ServiceStatusMsg = { type: "service_status", service: "brain", state: "ready", detail: "claude-cli", last_error: "" };

describe("applyHistory", () => {
  it("renders her lines with their marks and yours as heard, then clears the loading bubble", () => {
    const { seen, chat } = recorder();
    const lines = [her("雨が降ったら。", { grammar: [{ start: 2, end: 6, point: "〜たら", level: "beginner" }] }),
                   you("はい", { accepted: false, reason: "blocklist" }),
                   her("そうですね。", { cut: true })];
    expect(applyHistory(chat, lines)).toBe(true);
    expect(seen).toEqual(["her:雨が降ったら。:〜たら", "you:はい:false:blocklist", "her:そうですね。(cut):", "ready"]);
  });

  it("reports nothing spoken when only your lines are there, and leaves the bubble alone", () => {
    const { seen, chat } = recorder();
    expect(applyHistory(chat, [you("はい")])).toBe(false);
    expect(seen).toEqual(["you:はい:true:"]);
    expect(applyHistory(chat, [])).toBe(false);
  });
});

describe("loadingCaption", () => {
  it("captions what is still loading before she has spoken", () => {
    expect(loadingCaption({ ...brainReady, state: "starting", detail: "" }, false)).toBe("waking your tutor…");
    expect(loadingCaption({ ...brainReady, state: "starting", detail: "last lesson" }, false)).toBe("reading back your last lesson…");
    expect(loadingCaption(brainReady, false)).toBe("she is thinking of how to start…");
    expect(loadingCaption({ ...brainReady, service: "stt", state: "loading" }, false)).toBe("loading speech recognition…");
    expect(loadingCaption({ ...brainReady, service: "voicevox", state: "loading" }, false)).toBe("warming her voice…");
    expect(loadingCaption({ ...brainReady, service: "voicevox", state: "ok" }, false)).toBeNull();
  });

  it("never re-adds the caption once she has spoken this lesson — a replayed brain=ready included", () => {
    const { chat } = recorder();
    const spoke = applyHistory(chat, [her("こんにちは。")]);
    expect(loadingCaption(brainReady, spoke)).toBeNull();
    expect(loadingCaption({ ...brainReady, state: "starting", detail: "" }, true)).toBeNull();
  });
});
