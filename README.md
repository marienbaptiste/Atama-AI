<p align="center"><img src="docs/logo-wordmark.svg" alt="Atama-AI — a voice tutor for Japanese" width="460"></p>

# atama-AI (頭AI)

**Real-time voice Japanese tutor with a 3D avatar.** Speak Japanese to a 3D sensei that knows
exactly where you are in your WaniKani and Bunpro studies, and adapts its vocabulary and
grammar to match.

Everything runs on one laptop except Claude inference. No cloud STT, no cloud TTS, no
database, no accounts — one user, local files. **It never writes to your SRS accounts.**

> **Golden Rule.** This application **never sets or writes anything through the WaniKani or
> Bunpro API keys.** It is read-only by construction, and that is verified mechanically at
> every build and start — `make test`, `make run`, `make doctor`, backend import, `npm run
> build`, and the pre-commit hook all run a gate that fails on any write call or any
> setter-shaped name in the SRS code. The same transport refuses to send a token to any host
> but `api.wanikani.com` / `api.bunpro.jp` — origins are constants, not settings. There is no
> bypass. Details: spec §0, ADR-021, ADR-023.

> **Status: M3, the page.** M0–M2 are built (M2 declared done by the user, ADR-034) and M4's code
> is complete; you talk to the Vite + TypeScript page in `frontend/`. What is *not* done is every
> gate that needs a live session — barge-in timing, the acceptance conversation, VRAM — listed in
> [ROADMAP.md](ROADMAP.md). [ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md) remains the authoritative build
> document, and [ADR.md](ADR.md) says why the stack is pinned the way it is.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Hard requirements](#hard-requirements)
- [Getting started](#getting-started)
- [Claude login](#claude-login)
- [Settings](#settings)
- [SRS integration — read-only](#srs-integration--read-only)
- [Status bar](#status-bar)
- [The avatar](#the-avatar)
- [Emotions](#emotions)
- [The tutor prompt](#the-tutor-prompt)
- [Repo layout](#repo-layout)
- [Milestones](#milestones)
- [Design rules](#design-rules)
- [Troubleshooting](#troubleshooting)
- [Documents](#documents)

---

## What it does

You talk. The avatar listens, thinks, and talks back — in Japanese, at your level, with
lip-synced speech and a voice and face that match its mood.

The brain is **Claude Code running headless** (`claude -p`) as a single persistent subprocess
using your existing subscription auth — *not* the Anthropic API. The tutor receives a compact
**Student Profile** built at session start from your WaniKani level, recent unlocks and
leeches, plus your Bunpro JLPT progress and ghost reviews. It deliberately works your weak
items into natural conversation.

Highlights:

- **Voice in, voice out** — mic → VAD → Whisper → Claude → VOICEVOX → animated avatar.
- **Barge-in** — start talking while the avatar is speaking and it stops (< 300 ms), flushes
  its queue, and treats you as the next turn. It does *not* interrupt itself on laptop speakers.
- **Real lip-sync** — VOICEVOX mora timings are converted to Oculus visemes, not guessed from
  text.
- **Emotion, end to end** — the tutor tags sentences `[happy]`, `[thinking]`, `[surprised]` or
  `[serious]`; the tag drives the VOICEVOX voice style *and* the avatar's face, together, when
  that sentence plays.
- **Read-only SRS** — WaniKani and Bunpro are never written to. Enforced at token, client and
  tool level, not by a prompt.
- **Status bar** — you can always see whether WaniKani, Bunpro, the Bunpro MCP, Claude,
  VOICEVOX and Whisper are actually working, not just configured.
- **Settings page, not dotfiles** — tokens, voice, VAD, model and display are configured in the
  app, with a Test button per service.
- **A correction policy that respects flow** — minor errors get a natural recast, meaning-
  breaking errors get one sentence of fix and a retry. No lectures.
- **Mineable logs** — every turn lands in `logs/session-<timestamp>.jsonl` with transcript,
  reply, timings, emotion and tool calls. Future Anki fodder.

---

## Architecture

```
Browser (frontend/, Vite + TypeScript)     Python Orchestrator (backend)
┌──────────────────────────────────┐  WS   ┌──────────────────────────────┐
│ left: TalkingHead avatar         │  /ws  │ FastAPI + asyncio  (app.py)  │
│   visemes, emotion at audio start│ :8000 │  ├─ Hub: fan-out, turn epochs│
│ right: chat thread               │◄─────►│  ├─ VoiceLoop (PTT | VAD)    │
│   red grammar · word cards ·     │       │  ├─ VAD (silero)             │
│   translate · hint               │       │  ├─ STT (faster-whisper)     │
│ status bar · settings · SPACE    │       │  ├─ Brain → claude -p        │
└──────────────────────────────────┘       │  ├─ SentenceChunker (+ tags) │
  src/protocol.gen.ts is generated         │  ├─ Annotator: furigana,     │
  from backend/models.py (gate M3a)        │  │   cached explain via      │
                                           │  │   claude -p (haiku)       │
        ┌─────────────┐                    │  ├─ TTS client → VOICEVOX    │
        │ VOICEVOX    │◄──HTTP─────────────┤  ├─ SRS fetcher (WK/Bunpro)  │
        │ (Docker)    │  :50021            │  ├─ Memory + rotation        │
        └─────────────┘                    │  └─ Status registry          │
        ┌─────────────┐                    └───────────┬──────────────────┘
        │ SearxNG     │◄──HTTP :8888──┐                │ stdin/stdout
        │ (Docker)    │               │                ▼
        └─────────────┘   ┌───────────┴──┐        claude -p (persistent subprocess,
  Yahoo! JAPAN RSS ◄─GET──┤ search MCP   │◄───────stream-json in/out, MCP tools)
  (news_feeds.txt)        │ (1 tool)     │             │
                          └──────────────┘             └──► Bunpro MCP (3 read tools,
                                                            reads the SRS snapshot)

  run.cmd / up.py  : docker up → wait ready → build the page if stale → orchestrator → open page
  stop.cmd / down.py, or the page's stop button (control quit): clean shutdown
```

**Today** the page (`frontend/`, Vite + TypeScript) shows the tutor with lip-sync and her face
changing as each sentence starts, subtitles, the status bar and the settings panel; you hold
SPACE to talk, press it again to interrupt her, and hold **ALT GR** mid-sentence to throw away
what you are recording (the right-hand ALT — Chrome keeps SPACE with the left one). Nothing is
transcribed and nothing is sent: let SPACE go, press it again, start over. Your **microphone is captured by the
orchestrator** (sounddevice, with unplug recovery), not the browser, so step 1 below is not how it
works yet. **The conversation panel** (ADR-036, Settings → Display): the tutor sits on the left
and the conversation runs on the right as a chat. Grammar points she uses show in **red** — grammar only: a conjugation, an auxiliary or a
pattern, never a word she happens to like — and
the **lightbulb** lights up when she wants you to use a particular form or word — click it to see
which. Kanji carry **furigana** (Settings → Display: all, only the ones you have not reached Guru
on, or none). When you use something you have been learning **correctly**, she says so and the
word drifts up behind her in gold. Click a red point for the rule — in English or Japanese
(Settings → Display) — or the 訳 button on any of her sentences for its English; each answer
is written once and kept, so the second time is instant. **Coming next:** word cards with
readings and meanings.

| Service         | Port     | Bound to      |
|-----------------|----------|---------------|
| Orchestrator    | `:8000`  | `127.0.0.1`   |
| VOICEVOX engine | `:50021` | `127.0.0.1`   |
| Frontend (dev)  | `:5173`  | `localhost`   |

**Loopback only.** The WebSocket carries your raw microphone stream and your SRS profile;
nothing listens on the LAN. `make doctor` fails if anything does.

### One turn, end to end

1. Browser streams mic PCM (16 kHz mono, PCM16 via AudioWorklet) over the WebSocket.
2. Silero VAD detects end of speech (~600 ms silence, configurable).
3. faster-whisper transcribes with `language="ja"`.
4. The transcript is written to the Claude subprocess stdin as newline-delimited stream-JSON.
5. Claude's streamed text is cut into sentences at `。！？…\n` **as tokens arrive**; each
   sentence carries its emotion tag.
6. Each sentence hits VOICEVOX `audio_query` + `synthesis` with the emotion's voice style and
   pitch/speed/intonation → WAV + mora timings.
7. Mora timings become an Oculus viseme timeline; `{audio, visemes, vtimes, vdurations, text,
   emotion}` goes to the browser.
8. TalkingHead `speakAudio(...)` plays it with lip-sync; the face takes the emotion the moment
   the audio starts.
9. If *you* speak mid-playback → **barge-in**: stop audio, flush the TTS queue, mark remaining
   assistant text undelivered, start the next turn. The avatar's own voice through the speakers
   is filtered out by echo cancellation plus a raised, state-gated VAD threshold.

Stages are pipelined: TTS for sentence *N* overlaps generation of sentence *N+1*, and the
first sentence ships the instant it closes.

---

## Tech stack

Pinned. Substituting a piece is a conversation, not a refactor — the reasoning for each
choice is in [ADR.md](ADR.md).

**Backend** — Python 3.11+, FastAPI, uvicorn, starlette WebSockets, httpx, pydantic v2.

**Speech** — `faster-whisper` (`large-v3`, CUDA, `float16`), `silero-vad`.

**TTS** — VOICEVOX engine via the official Docker image. CPU build; it stays on CPU by design.

**Frontend** — Vite + vanilla TS, three.js,
[`@met4citizen/talkinghead`](https://github.com/met4citizen/TalkingHead) v1.7+. No React, no
state library. One page plus a settings page, a few modules.

**Brain** — the `claude` CLI (verified against 2.1.159), headless, subscription auth.

**Orchestration** — docker-compose for VOICEVOX only; everything else runs bare so Whisper
gets the host GPU.

---

## Hard requirements

Two numbers are non-negotiable. Both are instrumented and both gate milestone acceptance.

### Latency: ≤ 5.0 s voice→voice at p90

| Stage                              | Budget            |
|------------------------------------|-------------------|
| End-of-speech detect (VAD window)  | 0.50 s            |
| STT (Whisper, warm)                | 0.35 s            |
| Claude first complete sentence     | 3.60 s            |
| VOICEVOX first chunk + WS delivery | 0.40 s            |
| Playback start slack               | 0.15 s            |
| **Voice→voice total**              | **≤ 5.0 s (p90)** |

**Was 3.0 s until 2026-09-10.** Sonnet always thinks a little before answering and it cannot be
switched off; getting under 3 s meant a weaker answer, and a considered answer is worth two more
seconds (ADR-033). Claude is still the variable stage, so its allies are enforced: effort
`medium` (thinking kept down, not off), system prompt
compact (profile ≤ 600 tokens), **every built-in tool removed** (`--tools ""`), MCP tool use
rationed. `model` and `fallback model` are settings, so you can drop to a faster model if p90
drifts.

If the first sentence hasn't closed by 1.2 s, a pre-synthesized filler (うーん、そうですね…)
masks the gap — **fillers are masking, not budget compliance**; true first-content latency is
logged separately. Any turn over 5.0 s logs a warning with the full stage breakdown, and
rolling p50/p90 go into the session log.

### VRAM: 8–10 GB cap on a 16 GB RTX 4090 mobile

| Component                                  | Allocation  |
|--------------------------------------------|-------------|
| faster-whisper `large-v3` @ `float16` | **3.8 GB measured** |
| Silero VAD                                 | < 0.1 GB    |
| CUDA context + fragmentation reserve       | ~1 GB       |
| Browser / three.js (shares the GPU)        | ~1 GB       |
| **Total**                                  | **~5.6 GB** |

- VOICEVOX stays on CPU. Always.
- `make doctor` and the `--profile` overlay report `nvidia-smi` usage and warn above 10 GB.
- If VRAM gets tight, set `WHISPER_COMPUTE_TYPE=int8_float16` (2.2 GB, costs accuracy) before dropping to `medium`
  int8 via settings rather than breaching the cap.
- The remaining ~6 GB is deliberately reserved for a future photoreal (MuseTalk) experiment.
  Don't spend it.

---

## Getting started

**Target machine:** single laptop, RTX 4090 mobile (16 GB VRAM), Linux or Windows/WSL2.

### Prerequisites

- Python 3.11+
- Node 20+ (builds the avatar page; `make run` / `.\run` does it for you)
- Docker (VOICEVOX only)
- An NVIDIA GPU with CUDA available to CTranslate2
- The `claude` CLI, logged in — see [Claude login](#claude-login)
- A GLB avatar — see [The avatar](#the-avatar)
- Headphones recommended for the first session (not required — see
  [Troubleshooting](#troubleshooting))

Optional, but the whole point: a **read-only** WaniKani API token and a Bunpro API key.

### Setup

```bash
git clone <this-repo> atama-ai
cd atama-ai

python -m venv .venv          # then activate it
pip install -e ".[dev]"       # every dependency, declared in pyproject.toml
make avatar                   # fetch the default avatar (see the licence note below)

make run                      # start EVERYTHING and open the avatar
make stop                     # stop everything (containers included)
```

**On Windows there is no `make`.** Use the wrappers, which do exactly the same thing:

```powershell
.
un                          # start everything and open the avatar
.\stop                         # stop everything
```

`make run` brings up the containers, waits until VOICEVOX genuinely answers rather than assuming
it, builds the avatar page if its source is newer than the last build (the first time it also runs
`npm ci` in `frontend/`), starts the tutor with the browser avatar, and opens the page. **Hold
SPACE and talk** — press it while she is talking to interrupt her, and hold **ALT GR** while
recording to drop what you just said.
Ctrl+C stops the tutor and leaves the containers running — they are slow to start and cheap to
keep, so `make stop` is separate and deliberate.

Neither works? Both are plain modules: `python -m backend.tools.up` and
`python -m backend.tools.down`.

**The page** (`frontend/`, Vite + TypeScript, no framework — ADR-009). Working on it:

```bash
cd frontend
npm ci                  # once
npm run dev             # hot reload on http://127.0.0.1:5173, socket forwarded to a running tutor
npm test                # headless unit tests (Vitest)
npm run build           # type-check, then build into frontend/dist (served at /)
```

Its message types are **generated** from `backend/models.py`: after changing a message there, run
`python -m backend.tools.gen_protocol`. The Python tests fail until you do, and `npm run build`
fails while any server message has no handler on the page. If the launcher cannot build the page
and there is no earlier build, it stops and says why instead of opening an empty tab.

Then open `http://localhost:5173`. On first run the app opens on its **settings page**: paste
your tokens, press each **Test** button, and the conversation view unlocks once Claude tests
green. No `.env` needed.

Allow mic access, and start talking.

### The avatar (fetched, not committed)

> **The default avatar is non-commercial. If you are shipping anything commercial, replace it
> first — and note that making your own at Ready Player Me does *not* lift the restriction.**
> See [LICENSE](LICENSE) §1 for what actually does. For personal study, which is what this is
> for, it is fine as it stands.

`*.glb` is git-ignored, so a fresh clone has no face until you fetch one. Each persona names its
own file — `prompts/minami.md` declares `<!-- avatar: minami.glb -->` — so the filename is the
whole wiring.

It must be a **full-body GLB** with a Mixamo-compatible rig and **both** blendshape sets —
**ARKit (52)** and **Oculus visemes (15)**. TalkingHead needs all of them, and this is the part
that goes wrong quietly: an avatar missing them loads, renders perfectly, and simply never moves
its mouth.

Fetch the known-good default (TalkingHead's reference avatar, CC BY-NC 4.0):

```bash
python -m backend.tools.get_avatar
```

Or supply your own from [Ready Player Me](https://readyplayer.me) or
[Avaturn](https://avaturn.me), exported with both morph-target groups:

```
https://models.readyplayer.me/<YOUR_ID>.glb?morphTargets=ARKit,Oculus%20Visemes
```

**Always verify before building on it:**

```bash
python -m backend.tools.check_avatar
```

```
frontend/public/avatar.glb  (4.7 MB, 72 morph targets)
  Oculus visemes : 15/15   ok
  ARKit (sampled): 10/10   ok
Usable by TalkingHead.
```

If anything is missing it names it and tells you how to re-export. Worth trusting: it already
caught a real example avatar that was missing `viseme_sil`, the closed-mouth rest shape — that
one would have talked without ever shutting its mouth.

### Windows + WSL2

- **Inside WSL2:** the Python backend, the `claude` CLI **and its login**, `make`, Docker via
  Docker Desktop's WSL2 integration. Check `nvidia-smi` works *inside* WSL2 before anything
  else. Keep the repo on the WSL2 filesystem (`~/…`), not `/mnt/c/…` — the bridge is slow
  enough to show up in the latency budget.
- **On Windows:** only the browser. WSL2 forwards `localhost`, so `http://localhost:5173` just
  works, and because everything is bound to loopback it stays off the LAN.
- The mic is captured by the Windows browser and streamed over the WS; no WSL2 audio device is
  needed.

---

## Claude login

The tutor runs on your Claude **subscription** through the `claude` CLI — never the API. Two
ways to log in:

1. **Interactive (default).** Run `claude` once in a terminal **on the machine that runs the
   backend** (inside WSL2 on Windows — credentials live in the WSL home, not the Windows one),
   then `/login`. A browser opens, or a URL is printed to paste. Credentials are stored under
   `~/.claude/`, outside the repo, and refresh themselves. Headless `claude -p` reuses them.
2. **Long-lived token.** Run `claude setup-token` (interactive, requires a subscription), and
   paste the token into the settings page as the Claude OAuth token. Use this when the backend
   runs somewhere the interactive login is awkward.

`make doctor` proves which is active: it runs a trivial prompt in stream-json mode and asserts
`init.apiKeySource == "none"` — the only reliable "you are on subscription, not API billing"
signal. It also **hard-fails if `ANTHROPIC_API_KEY` is set in your shell**, because that
variable silently overrides subscription auth and bills the API.

Subscription rate limits are real for a chatty voice app. When the CLI reports one, the status
bar shows it and the configured fallback model carries the conversation.

---

## Settings

Configuration lives in the app's **settings page** and is stored in `settings.json` (repo
root, git-ignored, mode `0600`). There is no `.env` to edit.

**Built today:** the cog at the right end of the status bar (or `/#settings`, `/#settings/sound`) opens
a frosted panel generated from `config.py`: every key, grouped, typed, with its description.
Tabs: Account, Brain, Voice, Sound, Display, Advanced. Save writes `settings.json`. The
**microphone and output pickers list what is plugged in right now** (the list refreshes as you
plug and unplug) and apply at once; everything else applies the next time you launch. Live apply
of the rest, and the Test buttons below, are not built yet. A key also set in `.env` or an
`ATAMA_*` variable shows **set in .env** and is locked, because those override `settings.json`
and a value saved here would be silently ignored — `python -m backend.tools.migrate_env` moves
the non-secret ones out of `.env` for you (it backs `.env` up first; `--dry-run` to preview).
`HOST` is never editable from the page (ADR-017).

**Unplugging is fine.** No microphone at launch, a headset pulled out mid-lesson, a chosen
device that is missing: the app keeps running on the system default and switches back when the
device returns (spec §9). The page shows the microphone's state under the talk button.

| Group              | What's there                                                                 |
|--------------------|------------------------------------------------------------------------------|
| Account & tokens   | WaniKani token, Bunpro API key, Claude OAuth token — masked, with a **Test** button each |
| Voice              | VOICEVOX speaker/style, speed, intonation; the emotion → voice table         |
| Speech detection   | Whisper model, VAD silence window, barge-in sensitivity                      |
| Model              | Claude model, effort level, fallback model, per-turn timeout                 |
| Display            | Subtitles (JP / off), status heartbeat                                       |
| Advanced           | Ports/bind, cache & log dirs, latency/VRAM warning thresholds                |

Changes apply live where they can (voice, VAD, display). Changing the model respawns the
Claude subprocess with `--resume`, so conversation memory survives. Changing a token re-runs
the session-start fetch.

Secrets never come back to the browser: once stored, the page only ever sees `{set: true,
hint: "…abcd"}`.

**Environment variables** are an optional override layer for automation
(defaults → `settings.json` → env). [.env.example](.env.example) lists every key with its
default — it doubles as the settings inventory, and a test keeps it, `config.py` and the
settings page in sync.

---

## SRS integration — read-only

Both sources are optional and fetched in parallel at session start within a 10 s budget. The
app runs fine with zero, one, or both configured.

**This application never writes to WaniKani or Bunpro.** Not from the orchestrator, not from
the MCP server, not from Claude. Three independent layers enforce it, and a standing test
records every outgoing request in a full mocked session and asserts all are `GET`:

1. **Token scope** — create your WaniKani token with **no write permissions ticked**
   (`assignments:start`, `reviews:create`, `study_materials:*`, `user:update` all unticked).
   `make doctor` reads `/v2/user` and warns if the token can write.
2. **Client** — every SRS module is built on one HTTP client that has a `get()` method and
   nothing else. There is no write method to call.
3. **Tool surface** — the Bunpro MCP server exposes read tools only (`get_review_queue`,
   `get_ghost_reviews`, `get_grammar_progress`). A community server with write tools is
   disqualified unless they can be removed from the surface.

**The APIs are contacted only at app launch and when you press Refresh.** Nothing else calls
them — not a timer, not a conversation turn, not the MCP tools. Each sync stores a snapshot;
the status chips show `synced HH:MM`; the Bunpro MCP tools read that snapshot (and tell the
tutor how old it is) rather than calling Bunpro, so the MCP server holds no token at all.

Why do you still need tokens if Claude uses MCP? MCP is only the transport that lets Claude
*call* a tool mid-conversation; the tool still has to authenticate to WaniKani/Bunpro as you.
Tokens never enter the `claude` process itself — only the specific MCP server that needs them,
through the `env` block of its `mcp.json` entry.

- **WaniKani** (official, stable) — level, item counts by SRS stage, ~30 most recent vocab
  unlocks, ~15 leeches. Rate limit (~60 req/min) respected; cached to disk with a 1 h TTL.
- **Bunpro** (unofficial — treated as fragile) — JLPT progress and ~15 recent/ghost grammar
  points for the static profile, plus a small MCP server *written in this repo* so Claude can
  check your review queue live mid-conversation (on request or roughly every 15 minutes — not
  every turn). The credential is the **Account API Token from Bunpro → Settings → API**; the app
  never asks for your Bunpro email or password and never reads browser cookies. Bunpro has no
  official API — these endpoints can change without warning, so every response is validated and
  a change shows up as a red chip, not as wrong data. Every call is wrapped; failures log a
  warning and the session continues. Bunpro breakage never blocks startup.

The result is rendered into a **Student Profile** (≤ 600 tokens) injected into the tutor
prompt, and written to `logs/profile-<date>.json` for debugging.

---

## Status bar

A row of chips, always visible, one per dependency — driven by real signals, not by whether a
value is configured:

| Chip         | States                                                                          |
|--------------|---------------------------------------------------------------------------------|
| WaniKani     | `disabled` · `syncing` · `ok` (level, synced HH:MM) · `stale` (serving cache) · `error` |
| Bunpro       | same, for the session-start fetch                                                |
| Bunpro MCP   | `disabled` · `starting` · `connected` · `failed` · `used` (last call HH:MM, ok/error) |
| Claude       | `starting` · `ready` · `thinking` · `rate_limited` · `fallback` · `restarting` · `error` |
| VOICEVOX     | `loading` · `warm` (engine version, N styles) · `ok` (up, not preloaded) · `down` |
| STT          | `loading` · `warm` (model, VRAM MB) · `error`                                    |

`stale` is not an error — the tutor still has a profile, just an older one. Click a chip for
detail; hit **resync** to force a re-fetch past the cache TTL. `make doctor` prints the same
table, and the session log header records it, so a bad session can be diagnosed afterwards.
Error text is sanitised before it reaches a chip — never a token.

---

## The avatar

Bring your own GLB from **Ready Player Me** or **Avaturn**. It must include **ARKit and Oculus
viseme blendshapes** — RPM exports include them by default. Follow the export parameters in
the TalkingHead README (Appendix A).

Place it at `frontend/public/avatar.glb` (git-ignored).

The avatar is framed waist-up in a full-viewport canvas, auto-blinks every 2–6 s, sways
subtly, and tracks the camera with `lookAt`. While you speak it takes an attentive pose and
nods on your pauses — the あいづち a human tutor gives. While thinking, it looks up and away.

Lip-sync always goes through `speakAudio` with an explicit viseme timeline — TalkingHead's
text-based lip-sync has no Japanese module, so `speakText` is never used.

### Mora → viseme mapping

Implemented as one pure, unit-tested function with golden tests against committed
`audio_query` fixtures:

| Input                     | Viseme                        |
|---------------------------|-------------------------------|
| Vowels a / i / u / e / o  | `aa` / `I` / `U` / `E` / `O`  |
| ん (N)                     | `nn`                          |
| っ (cl), pause             | `sil`                         |
| k, g                      | `kk`                          |
| s, z, sh, j, ts           | `SS`                          |
| t, d                      | `DD`                          |
| ch                        | `CH`                          |
| n                         | `nn`                          |
| m, b, p                   | `PP`                          |
| f, h                      | `FF`                          |
| r                         | `RR`                          |
| w, y                      | skipped — the vowel dominates |

Timings honor `prePhonemeLength` and divide by `speedScale` — including the per-emotion speed.
Devoiced vowels (VOICEVOX marks them with an uppercase vowel) keep the shape, but the frontend
caps their weight at ~0.4.

---

## Emotions

The tutor may open the turn, or any later sentence, with exactly one of `[happy]` `[thinking]`
`[surprised]` `[serious]`. It's told to pick the tag for how the sentence should *sound*. The
tag is stripped before TTS and applies to that sentence and the following ones until the next
tag.

One tag drives two outputs, from one config table, applied **when that sentence's audio starts
playing** so face and voice change together:

| Tag         | Voice (VOICEVOX)                                        | Face (TalkingHead)                                 |
|-------------|---------------------------------------------------------|----------------------------------------------------|
| (none)      | base style, speed 0.90                                  | `neutral`                                          |
| `happy`     | cheerful style, speed 0.95, pitch +0.02, intonation 1.15 | `happy` mood                                       |
| `thinking`  | base, speed 0.85, intonation 0.90                       | gaze up-and-away, head tilt, brows slightly down   |
| `surprised` | base, speed 1.00, pitch +0.04, intonation 1.30          | brow-raise + eye-widen, brief                      |
| `serious`   | calm style, speed 0.85, pitch −0.03, intonation 0.85    | brows down, no sway, held gaze                     |

Style ids come from the installed speaker's real `GET /speakers` list; a missing style falls
back to the base style with the scalar overrides. Numbers are starting points, tuned by ear in
M3 and editable in settings.

---

## The tutor prompt

The system prompt lives in [`prompts/tutor.md`](prompts/tutor.md) — a versioned file, never
hardcoded in Python, so you can iterate on Sensei's personality without touching code. The
Student Profile is rendered into `{{student_profile}}` at session start.

Its hard output rules exist because the text goes straight into a voice pipeline: Japanese by
default, sentences ≤ 25 characters, 1–3 per turn, no markdown, no lists, no romaji, no furigana
notation, no parenthetical asides, no emoji. Numbers are written as they would be *spoken*, and
rare or above-level kanji is written in kana so TTS reads it correctly.

### Who is teaching you

Four tutors ship in [`prompts/`](prompts/). Each file is one person *and* the voice they are
written for — pick one with `TUTOR_PERSONA` and both follow:

| `TUTOR_PERSONA` | who | voice |
|---|---|---|
| `tanaka` (default) | たなか先生, 50, ex-engineer. Quiet, dry, explains by example, waits for you to finish. | 麒ヶ島宗麟 |
| `hayashi` | はやし先生, 28. Fast, cheerful, teaches what people actually say now. | 栗田まろん |
| `minami` | みなみ先生, 40s, linguistics. Warm, literary, loves word origins. | No.7 |
| `mori` | ゆい, 19 — **not a teacher.** A student at a 語学交換 circle who corrects only when she genuinely doesn't follow you. For practice where being corrected every sentence is the problem. | 冥鳴ひまり |

Adding your own is adding one file: write the character, put `<!-- voice: NN -->` at the top, and
set `TUTOR_PERSONA` to its name. The declaration is stripped before the file reaches the model.
`VOICEVOX_SPEAKER=-1` (the default) means "ask the persona"; set a real style id to override it
while auditioning.

Voices were not picked by browsing names — the whole `GET /speakers` catalogue was measured for
pitch stability (F0 jitter in cents), spectral roughness and synthesis cost, and a human chose from
the shortlist. たなか deliberately runs on the *least* steady voice in the catalogue: on a
fifty-year-old the unsteadiness reads as age. See [ADR-030](ADR.md) and the ROADMAP V0.3 findings
if you are tempted to "fix" it.

### What it remembers

Sensei remembers you between lessons, and none of it costs you a pause mid-conversation
([ADR-031](ADR.md), [ADR-032](ADR.md)):

- **Read once, at session start.** Notes about you and a brief on last session are loaded into the
  prompt alongside your SRS profile. There is no memory lookup during a turn — if it wasn't loaded
  at the start, Sensei doesn't know it. A tutor who says 「あれ、なんだっけ」 beats one that goes
  silent for half a second.
- **Written in the gaps.** Turn records are appended while the avatar is still speaking. The
  summary that becomes next lesson's memory is written when you next launch, so exiting stays
  instant.
- **`memory/student.md` is yours to edit.** It lives outside the repo, is never committed, and is
  plain markdown — a wrong memory recalled confidently is worse than none, and the fix is a text
  editor.
- **Long lessons don't stall.** When the conversation approaches the model's context limit, the
  session is rotated during the avatar's speaking time rather than letting the CLI compact
  mid-sentence. You should never notice it happen.

---

## Repo layout

```
atama-ai/
├─ backend/
│  ├─ app.py            # FastAPI + WS
│  ├─ claude_session.py # subprocess mgmt, stream-json, resume, env allowlist
│  ├─ chunker.py        # sentence chunking + emotion tags
│  ├─ stt.py  vad.py  tts_voicevox.py  visemes.py
│  ├─ emotions.py       # emotion → VOICEVOX style/params table
│  ├─ status.py         # service status registry → service_status messages
│  ├─ tools/readonly_gate.py  # Golden Rule gate — runs on every test/run/doctor/commit
│  ├─ srs/http.py       # GET-only client + ReadOnlyTransport
│  ├─ srs/wanikani.py  srs/bunpro.py  srs/profile.py
│  ├─ srs/bunpro_mcp.py # stdio MCP server, read tools only (if we write it)
│  ├─ config.py  constants.py (verified CLI/endpoint findings, dated)
│  ├─ models.py (pydantic WS protocol)
│  ├─ data/             # hallucination_blocklist.txt, fillers.txt
│  └─ tests/            # fixtures/ (sanitised, committed)  fixtures/private/ (ignored)
├─ frontend/            # vite, vanilla TS (ADR-009)
│  ├─ index.html  src/{main, ws, protocol.gen, avatar, speech, rig, rigpanel, mic, status, settings, ui}.ts
│  ├─ scripts/srs-grep.mjs  # prebuild half of the §0 gate
│  └─ public/avatar.glb (git-ignored; see The avatar)  cast.json, <persona>.speak.json (generated)
├─ prompts/tutor.md
├─ .cache/              # git-ignored, created at startup: mcp.json, rendered prompt,
│                       #   claude-cwd/, srs cache, fillers/
├─ logs/                # git-ignored
├─ settings.json        # git-ignored, mode 0600 — written by the settings page
├─ docker-compose.yml   # voicevox only, published on 127.0.0.1
├─ mcp.json.template  .env.example  .gitignore  Makefile
├─ README.md  ROADMAP.md  ADR.md  CLAUDE.md  ATAMA-AI_SPEC.md
```

### WebSocket protocol

Message types are defined once as shared constants and mirrored in pydantic models; a contract
test fails CI if the two sets drift.

**client → server:** `audio_chunk` (base64 PCM16), `control` (`start`, `stop`, `bargein_ack`,
`resync`), `settings` (partial update of any key, secrets included), `settings_test`
(`{service}`)

**server → client:** `state` (`listening` | `thinking` | `speaking`), `stt_final`,
`assistant_text`, `speak` (`{audio_b64, visemes[], vtimes[], vdurations[], text, emotion}`),
`emotion`, `bargein`, `srs_profile`, `service_status`, `settings` (echo, secrets as
`{set, hint}`), `timing`, `error`. `stt_partial` is reserved and never emitted.

---

## Milestones

Built strictly in order. Each ends with a runnable demo and tests. The per-subsystem test,
validation and integration plan behind these lives in [ROADMAP.md](ROADMAP.md).

| #      | Milestone                                   | Ships                                                                                                                       | Acceptance                                                                                                                                    |
|--------|---------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| **M0** | Skeleton & environment doctor               | **Read-only gate first**, then repo layout, `.gitignore`, config resolution, GET-only SRS client, verified CLI constants, `make doctor`, `make hooks` | Gate catches every violation fixture and passes a clean tree; actionable errors for every missing prerequisite; `apiKeySource == "none"`; loopback-only; every ignored path actually ignored |
| **M1** | SRS fetchers (read-only) + text brain loop  | WaniKani + Bunpro fetchers, profile renderer, persistent claude subprocess, CLI REPL with the real profile in the prompt     | Read-only recording test green; env allowlist; restart/`--resume`; chunker + emotion tests; no built-in tools in `init.tools[]`                |
| **M2** | Ears & mouth (no avatar)                    | Mic → VAD → Whisper → M1 → VOICEVOX → playback; emotion → voice live; latency + VRAM instrumentation                        | Viseme golden tests; hallucination filter; five emotions audibly distinct; VAD gating test                                                     |
| **M3** | Face                                        | Full frontend: TalkingHead, lip-sync, emotions (face + voice), listening reactions, status bar, settings drawer, barge-in    | 10 turns on headphones, barge-in < 300 ms; **10 turns on speakers, zero self-interruptions**; 4 emotions distinct; **p90 ≤ 5.0 s**; VRAM ≤ 10 GB |
| **M4** | Sensei brain                                | Prompt tuning, Bunpro MCP (read tools only), status chips on real signals, resync                                            | ≥ 3 recent unlocks used in 5 minutes; Bunpro absent → `disabled`; broken → `failed`, conversation unaffected; WK offline → `stale`             |
| **M5** | Polish                                      | Full settings page, session summary, `--profile` overlay, fresh-machine docs (Linux + WSL2), `make check-secrets`            | Clone → first conversation **without creating a `.env`**; redaction and read-only tests green                                                 |

---

## Design rules

The rules that shape this codebase. Most were expensive to learn; they are documented in
[ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md) §4, §5, §11 and §14, with rationale in [ADR.md](ADR.md).

### The SRS accounts

- **Never set or write through the WaniKani or Bunpro API keys — the Golden Rule (spec §0).**
  Read-only at token, client and tool level. No setter, no write method, no write tool behind
  any flag (ADR-021). Verified at every compilation: `make check-readonly` is the first target
  of `make test` and a prerequisite of `make run` / `make doctor`; the client self-checks at
  import; a runtime transport raises on any non-`GET`; `npm run build` greps the frontend;
  `make hooks` installs the pre-commit check.

### The Claude subprocess

- **One persistent process per session.** Never one per turn — startup latency kills the
  experience.
- **Isolated** (ADR-016): `--tools ""` (no built-in tools at all — the three Bunpro MCP tools are
  the only ones it has), `--strict-mcp-config` (only our MCP servers), an orchestrator-assigned
  `--session-id`, spawned from an empty cwd **outside the repository** (Claude Code walks up the
  tree for `CLAUDE.md`), with an **allowlisted env** (`PATH`, home, temp, `LANG`, the OAuth
  token — nothing else, so SRS tokens never enter it).
- **Waits for the MCP server's ready signal before the first turn.** Claude prints `init`
  before MCP servers connect and never announces the connection; our server does (a marker
  written on `notifications/initialized`). Sending a turn early means a tutor with no tools.
- **Assert `init.apiKeySource == "none"`.** Anything else means API billing — refuse to run.
- **Restart with `--resume <session_id>`** if the process dies; same cwd, same env.
- **Parse stdout line-by-line as JSON.** Log and skip unknown event types; never crash on one.
- **No `--bare`** (never reads OAuth), **no `--dangerously-skip-permissions`**, **no
  `--no-session-persistence`** (kills resume).
- **Per-turn timeout (default 60 s)** → SIGINT, apology line, `--resume` if needed.

### Global

- **Don't** substitute the Anthropic API/SDK for the CLI subprocess. Subscription auth is a
  hard requirement.
- **Don't** bind anything to `0.0.0.0`. Loopback only.
- **Don't** require a `.env`; **don't** send a stored secret back to the browser.
- **Don't** swap pinned stack pieces (React, cloud TTS, cloud STT, GPU VOICEVOX) without asking.
- **Don't** fabricate CLI flags, endpoint schemas, or library signatures. Verify against
  `claude --help`, VOICEVOX's live `/docs` OpenAPI, and the TalkingHead README — then pin the
  finding in a dated code comment.
- **Don't** gold-plate: no auth system, no multi-user, no database. Files and one user.
- **Do** ask a human when a verification step fails or reality contradicts the spec. Update the
  spec; do not silently diverge.

---

## Measuring latency

The hard number is **voice→voice p90 ≤ 5.0 s** (spec §10). Measure it without talking:

```bash
python -m backend.tools.latency_run                 # 20 turns with your current settings
python -m backend.tools.latency_run --effort low    # compare one setting, same script
python -m backend.tools.latency_run --model haiku --turns 10
```

It replays your recorded utterances (`logs/stt/corpus-*`) through Whisper, sends a scripted
student line to a real tutor session, and times her first complete sentence and its synthesis.
It prints p50/p90 per stage against the budget, keeps the opening turn out of the p90 (the first
turn of a session pays prompt-cache creation), and writes every turn to `logs/latency/*.jsonl`.
It uses your Claude subscription for the turns it runs, reads the study profile from the local
cache only, and writes nothing to lesson memory.

---

## Troubleshooting

**`make doctor` says the claude CLI is not authenticated.** Run `claude` **on the backend
machine** (inside WSL2 on Windows), `/login`. Or `claude setup-token` and paste it in settings.

**`make doctor` hard-fails on `ANTHROPIC_API_KEY`.** Unset it in that shell. It silently
overrides subscription auth and bills the API, and it would leak into the subprocess.

**`apiKeySource` is not `"none"`.** Same cause as above, via some other path (a profile file, a
settings file). The tutor refuses to run until it is.

**The WaniKani chip says my token can write.** Create a new personal access token with every
write permission unticked, and paste it in settings. The app will never use write scopes, but
it should not hold them either.

**The avatar keeps interrupting itself on speakers.** Echo cancellation isn't engaging (some
browsers ignore it on certain devices). Use headphones, or raise the barge-in sensitivity
factor in settings. If it happens on headphones, that's a bug — file it.

**Replies are being billed to the API.** See the two items above; also check that nothing in
your shell profile exports `ANTHROPIC_API_KEY`.

**VOICEVOX chip says `down`.** `docker compose up -d`, then check
`http://127.0.0.1:50021/docs`.

**The first turn is slow.** The Whisper model warms at startup with a 1 s dummy transcription —
if that was removed, put it back.

**Whisper transcribes silence as ご視聴ありがとうございました.** A known Japanese hallucination;
it is on the blocklist data file, alongside avg-logprob and no-speech-prob thresholds. Add new
ones to the data file, not to the code.

**Lip-sync drifts.** Check that `prePhonemeLength` is honored, that durations are divided by
`speedScale` (including the per-emotion speed), and that you are passing the timing unit
TalkingHead expects (ms vs s — pinned in code).

**The face changes before the voice does.** The emotion is being applied on message receipt
instead of at audio start. It must come from the playback-start callback.

**p90 latency creeping past 5.0 s.** Check the `--profile` overlay for which stage is blowing
its budget. If it is Claude, drop to a faster model, shrink the profile, or ration MCP tool use
further.

**VRAM above 10 GB.** Confirm VOICEVOX is still on CPU, then fall back to Whisper `medium` int8
in settings.

**`claude` dies with "The current directory is invalid." (Windows).** You're on the Microsoft
Store Python and the configured `CLAUDE_CWD` is under `%LOCALAPPDATA%`, which that build
virtualises — other processes can't see the directory. Leave `CLAUDE_CWD` empty (default
`~/.atama-ai/claude-cwd`) or point it anywhere outside both AppData and the repo.

**Bunpro MCP chip stuck on `failed`.** Bunpro's API is unofficial and changes; check the
sanitised error in the chip, then the session log. The conversation continues without it.

---

## Memory

She remembers you between lessons, without it costing anything while you talk. Everything is read
once at launch and written between turns — never while you are waiting for her to answer
(spec §6b, ADR-031).

| file | what it holds |
|------|---------------|
| `logs/sessions/<date>-<session>.jsonl` | every turn, one JSON line each — also your Anki mine |
| `~/.atama-ai/memory/last-session.md` | a sentence or two on how the last lesson went |
| `~/.atama-ai/memory/topics.jsonl` | a few subjects per lesson, so she doesn't open on the same thing twice |
| `~/.atama-ai/memory/student.md` | what she believes about you — **edit it freely** |

The first two paths are on Windows; elsewhere the state directory is `~/.local/state/atama-ai`.
None of it lives in the repository, so none of it can be committed.

Past lessons are summarised **when you next launch**, by a small cheap model (`haiku` by default),
which is why launch occasionally says `memory: catching up on 1 past session(s)`. Exiting stays
instant. If the summary fails, that lesson is simply retried next time.

**Each lesson opens with a choice.** If there is a last lesson to remember, she recalls it in a
sentence and asks whether to carry on or talk about something new. Carry on, and she picks the
thread back up with today's review items, since your SRS profile is refreshed at launch. Something
new, or your very first lesson, and she finds one fresh news item that isn't a recent topic.

**Always the newest model.** Choosing Sonnet, Opus or Haiku uses the newest version of that tier
your account can use — today claude-sonnet-5, claude-opus-5 and claude-haiku-4-5. Claude Code's
own short names lag behind (`sonnet` still meant Sonnet 4.6), so the app keeps its own list in
`backend/data/model_tiers.txt`, newest first, and checks once a week which one answers. A new
release is one line in that file. Delete `.cache/model_tiers.json` to check again straight away.

**Changing tutor is live.** Pick one in Settings → Voice (or the dropdown at the top left): the
voice and the character switch at once, in a fresh conversation, and the new tutor introduces
themselves. The avatar stays the same model for every tutor.

**Don't like the topic?** Say 「話題を変えて」 (or "change topic"), or press **↻ new topic** under
the talk button. She drops the subject, interrupting herself if she is mid-sentence, and finds
something fresh the same way.

**A wrong memory is worse than none.** If she has something wrong about you, open `student.md` and
fix it — it is plain markdown, and that is the correction mechanism. Delete the `memory` folder to
start over. Set `MEMORY_ENABLED=false` to turn it off entirely.

**New reviews mid-lesson?** Settings → Account → **Refresh now** fetches your latest WaniKani and
Bunpro progress (read-only, at most once a minute) and hands it to a fresh session that takes over
at her next answer — the lesson carries on. The status bar's service dots show the fetch.

**Long lessons.** Her memory of the current conversation has a limit — the status bar's layered
gauge shows how full it is. When it fills, Claude condenses it on its own, which is a pause of
several seconds (12 s measured for a small one). So once her context passes `CONTEXT_ROTATE_AT`
(Settings → Advanced, default `0.7` of the window) the app starts a fresh session in the
background, hands it the lesson so far, and switches over between two turns — she carries on
without greeting you again. `0` turns this off. Where Claude condenses is Claude's own policy and
nothing reports it, so the app watches for it instead: if it ever happens, the page says she is
tidying her notes, and if it came before the app rotated, the app rotates earlier from then on
(remembered in `.cache/compaction.json`; delete the file to forget).

---

## Credits and licensing

The code here is yours to read and change. Three things it depends on are not, and one of them
places an obligation on **anything the app says out loud**, not merely on this repository.

**VOICEVOX — attribution is mandatory.** The synthesised voice must be credited wherever the
audio appears. The exact credit depends on which persona is configured (`TUTOR_PERSONA`):

| persona | voice | required credit |
|---------|-------|-----------------|
| `tanaka` (default) | style 53 | `VOICEVOX:麒ヶ島宗麟` |
| `minami` | style 29 | `VOICEVOX:No.7` |
| `hayashi` | style 67 | `VOICEVOX:栗田まろん` |
| `mori` | style 14 | `VOICEVOX:冥鳴ひまり` |

Each character has its own terms; the ones above permit commercial and non-commercial use
**provided the credit is shown**. Check the individual character's page before relying on that,
and re-check when adding a persona — the terms are per character, not per engine.

**The avatar is not covered by this repo's licence.** Nothing is committed, and whichever GLB you
supply carries its own terms — the common example avatars are non-commercial only.

**The furigana dictionary.** The chat's readings come from [fugashi](https://github.com/polm/fugashi)
(MIT and BSD-3-Clause) with [unidic-lite](https://github.com/polm/unidic-lite). UniDic itself is
©2011–2013 The UniDic Consortium, released under the GPL, the LGPL **or** the BSD licence — used
here under the BSD terms. Nothing is sent anywhere: the readings are computed on your machine.

**Everything else** — TalkingHead, faster-whisper, Silero VAD — is MIT or equivalent.

---

## Documents

| File                                 | What it is                                                                       |
|--------------------------------------|----------------------------------------------------------------------------------|
| [ATAMA-AI_SPEC.md](ATAMA-AI_SPEC.md) | The authoritative build spec. Wins any disagreement with this README.             |
| [ROADMAP.md](ROADMAP.md)             | Per-subsystem test → validate → integrate plan, gates, verification spikes and their findings. |
| [ADR.md](ADR.md)                     | Architecture decision record: why each pinned choice was made, and what would reverse it. |
| [CLAUDE.md](CLAUDE.md)               | Instructions for Claude Code sessions: which document to consult for what.        |
| [.env.example](.env.example)         | The complete settings inventory; optional env-override reference.                 |
