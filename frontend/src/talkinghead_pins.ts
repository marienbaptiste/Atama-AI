/** What we rely on in TalkingHead, pinned (ADR-015): each entry names the call, the signature as
 *  read in talkinghead.mjs, and the date it was read. backend/constants.py carries the server-side
 *  pins (visemes, moods, gestures, the ms unit of speakAudio — V0.4); these are the ones only the
 *  page uses. avatar.ts imports from here rather than restating them, so a bump of the build below
 *  has one place to re-verify. Nothing here is assumed from the README alone. */

/** TalkingHead 1.4, loaded by URL — the build the prototype ran and V0.4 read. It imports "three"
 *  by bare name, which index.html's import map resolves to the same versions. */
export const TALKINGHEAD_VERSION = "1.4";
export const TALKINGHEAD_URL = `https://cdn.jsdelivr.net/gh/met4citizen/TalkingHead@${TALKINGHEAD_VERSION}/modules/talkinghead.mjs`;

/** How long the page waits for that module before carrying on without a face (2026-09-12): the
 *  chat and her voice must not depend on a CDN answering. */
export const TALKINGHEAD_LOAD_TIMEOUT_MS = 15_000;

/** The calls avatar.ts makes, as read on the dates given. `verified` is the file and the finding. */
export const TALKINGHEAD_PINS = {
  constructor: {
    signature: "new TalkingHead(node, { ttsEndpoint, lipsyncModules, cameraView, cameraRotateEnable, avatarMood, modelPixelRatio, avatarIdleEyeContact, avatarSpeakingEyeContact })",
    verified: "talkinghead.mjs 1.4: THROWS on a falsy ttsEndpoint (line 813) although we never call its TTS; zoom and pan are off by default (lines 146-148, OrbitControls 849-851). Read 2026-09-10 and 2026-09-11.",
  },
  showAvatar: {
    signature: "showAvatar({ url, body, avatarMood, lipsyncLang }): Promise<void>",
    verified: "sets this.avatar, which speakAudio reads — a sentence before it resolves throws and is lost. Read 2026-09-12.",
  },
  speakAudio: {
    signature: "speakAudio({ audio, words, wtimes, wdurations, visemes, vtimes, vdurations }, opt, onsubtitles)",
    verified: "times in MILLISECONDS (V0.4, 2026-09-10); `words` is required for supplied visemes to be read at all; onsubtitles fires at the first word's time, once the sentence leaves the queue (startSpeaking). Read 2026-09-11.",
  },
  isSpeaking: {
    signature: "isSpeaking: boolean",
    verified: "true from startSpeaking until the queue empties. Read 2026-09-11.",
  },
  setMood: {
    signature: "setMood(mood)",
    verified: "a closed set — neutral, happy, angry, sad, fear, disgust, love, sleep (constants.py TALKINGHEAD_MOODS); an unknown name is a silent no-op. Read 2026-09-10.",
  },
  setFixedValue: {
    signature: "setFixedValue(shape, value | null)",
    verified: "pins an ARKit blendshape until set to null; how surprised/serious/thinking ride on the neutral mood (spec §8). Read 2026-09-10.",
  },
  playGesture: {
    signature: "playGesture(name, dur?, mirror?, ms?)",
    verified: "dur in SECONDS, ms is the transition; `mirror` true is the RIGHT hand; `yes`/`no` fall through to the animated emoji (a nod, a head shake). Read 2026-09-10.",
  },
  lookAtCamera: {
    signature: "lookAtCamera(ms)",
    verified: "the call that actually points her at the student — avatarIdleEyeContact only compensates head rotation. Read 2026-09-10.",
  },
  lookAt: {
    signature: "lookAt(x, y, ms)",
    verified: "x, y in viewport pixels. Read 2026-09-10.",
  },
  setView: {
    signature: "setView(view, { cameraDistance, cameraY, cameraX })",
    verified: "computes a STATIC frame; a negative cameraDistance zooms in; a positive cameraY LIFTS her (it lowers the look-at point). Read 2026-09-10.",
  },
  poseTemplates: {
    signature: "poseTemplates: Record<name, template>; setPoseFromTemplate(template, ms?)",
    verified: "takes the TEMPLATE OBJECT, not its name — the string throws inside poseFactory, silently under a try/catch. Read 2026-09-10.",
  },
  startStop: {
    signature: "start(); stop()",
    verified: "stop() halts the whole render loop, lip-sync included. Read 2026-09-10.",
  },
} as const;
