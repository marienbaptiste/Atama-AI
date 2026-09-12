/** A page that joins a lesson already under way (a reload, 2026-09-12): the server replays the
 *  transcript (`history`) and says whether she has spoken (`state.spoken`), so the chat shows what
 *  was said rather than a loading bubble for an opening it already heard. Pure: the chat is
 *  whatever renders the lines, so this can be tested without a DOM. */
import type { HistoryLine, ServiceStatusMsg } from "./protocol.gen";

/** What the replay needs from the chat (src/chat.ts `Chat` has exactly these). */
export interface Transcript {
  her(line: Pick<HistoryLine, "text" | "grammar" | "readings" | "vocab">, cut?: boolean): void;
  you(text: string, accepted: boolean, reason: string, readings: HistoryLine["readings"],
      vocab: HistoryLine["vocab"]): void;
  ready(): void;
}

/** Render the lines in order. Returns true when any of them is hers — she has spoken, and the
 *  "still loading" captions are over for this lesson. */
export function applyHistory(chat: Transcript, lines: readonly HistoryLine[]): boolean {
  let spoke = false;
  for (const line of lines) {
    if (line.who === "her") {
      spoke = true;
      chat.her(line, line.cut);
    } else {
      chat.you(line.text, line.accepted, line.reason, line.readings, line.vocab);
    }
  }
  if (spoke) chat.ready();
  return spoke;
}

/** What the chat says while she is still coming up (user, 2026-09-12). The chips already carry
 *  the detail; this turns the loudest of them into one sentence, because a blank panel during a
 *  40-second launch looks broken. Null once she has spoken — server truth (`state.spoken` or a
 *  replayed line of hers), never the page's memory: a reload's fresh page has none, and the
 *  brain=ready replay would otherwise put "thinking of how to start…" back for good. */
export function loadingCaption(m: ServiceStatusMsg, spoken: boolean): string | null {
  if (spoken) return null;
  switch (m.service) {
    case "brain":
      if (m.state === "starting") return /lesson/i.test(m.detail) ? "reading back your last lesson…" : "waking your tutor…";
      return m.state === "ready" ? "she is thinking of how to start…" : null;
    case "stt": return m.state === "loading" ? "loading speech recognition…" : null;
    case "voicevox": return m.state === "loading" ? "warming her voice…" : null;
    default: return null;
  }
}
