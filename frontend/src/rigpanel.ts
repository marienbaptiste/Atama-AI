/** The rig panel: hand controls for judging her face and body by eye (ROADMAP 9 and 17 validate
 *  by eye). Collapsed inside the activity card — it is for tuning, and in the way in a lesson. */
import type { Avatar } from "./avatar";
import type { SpeakMsg } from "./protocol.gen";
import { GESTURES, MOODS, POSES, RIG } from "./rig";
import { $, esc, log } from "./ui";

type Sample = Omit<SpeakMsg, "type" | "turn" | "emotion">;

function buttons(host: HTMLElement, names: readonly string[], fn: (name: string) => void): void {
  host.innerHTML = "";
  for (const name of names) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = name;
    b.onclick = () => { fn(name); b.blur(); };
    host.appendChild(b);
  }
}

export function mountRigPanel(avatar: Avatar): void {
  buttons($("rig-moods"), MOODS, m => { avatar.head.setMood(m); log(`mood <b>${m}</b>`); });
  buttons($("rig-gestures"), [...GESTURES, "stop"], g => {
    if (g === "stop") { avatar.head.stopGesture?.(500); log("gesture cleared"); return; }
    avatar.head.playGesture(g, avatar.holdS, avatar.varyHands, 800);
    log(`gesture <b>${g}</b> · ${avatar.holdS}s`);
  });
  buttons($("rig-poses"), POSES, p => { if (avatar.setPose(p, 1200)) log(`posture <b>${p}</b>`); });
  buttons($("rig-reactions"), ["yes", "no"], e => {
    avatar.head.playGesture(e, 2, false, 400);
    log(`reaction <b>${e}</b> — ${e === "yes" ? "nod (あいづち)" : "shake"}`);
  });

  const slider = (id: keyof Avatar["framing"]) => {
    const input = $<HTMLInputElement>("rig-" + id);
    const out = $("rig-" + id + "-v");
    input.value = String(avatar.framing[id]);
    out.textContent = input.value;
    input.oninput = () => { out.textContent = input.value; avatar.setFraming({ [id]: Number(input.value) }); };
  };
  (["zoom", "high", "offx"] as const).forEach(slider);

  const hold = $<HTMLInputElement>("rig-hold");
  hold.oninput = () => { avatar.holdS = Number(hold.value); $("rig-hold-v").textContent = hold.value + "s"; };
  const hands = $<HTMLInputElement>("rig-hands");
  hands.onchange = () => { avatar.varyHands = hands.checked; };

  $("rig-freeze").onclick = () => {
    avatar.freeze(!avatar.frozen);
    $("rig-freeze").classList.toggle("on", avatar.frozen);
    log(avatar.frozen ? "motion frozen — this halts the whole render loop, so lip-sync stops too" : "motion resumed");
  };
  $("rig-reset").onclick = () => { avatar.reset(); log("reset to neutral"); };
  $("rig-recentre").onclick = () => { avatar.reframe(); log("camera recentred on the upper body"); };
}

/** The persona's sample sentences, one per emotion (backend.tools.make_preview →
 *  <id>.speak.json, the exact shape of a `speak` message). Played outside any turn. */
export async function loadSamples(id: string, avatar: Avatar): Promise<void> {
  const host = $("rig-say");
  host.innerHTML = "";
  let samples: Record<string, Sample>;
  try {
    samples = await (await fetch(`${id}.speak.json`)).json();
  } catch {
    host.innerHTML = `<i>no samples for ${esc(id)} — run <code>make_preview</code></i>`;
    return;
  }
  buttons(host, Object.keys(RIG).filter(tag => samples[tag]), tag => {
    void avatar.player.play({ ...samples[tag], type: "speak", emotion: tag, turn: 0 }, true);
  });
}
