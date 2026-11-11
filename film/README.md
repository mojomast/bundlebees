# bundlebees — HIVE / CONTROL

An original 72-second presentation for the open-source Hermes Agent plugin by mojomast. Dark graphite, honey-amber work units, cyan verification paths, sharp geometric bees and hexagonal sandbox cells. Real product stills appear throughout: the banner, the Health pane, terminal captures of doctor/pytest/schema measurement and live orch state, including the real BB02 rejection and retry. Lower third is reserved for optional narration captions. Original 120 BPM music, no voiceover.

## Deliverables

- **Committed master:** `delivery/bundlebees-1080p.mp4` — 1920×1080, 30 fps, 72 seconds, H.264/yuv420p with AAC stereo and faststart; 5,508,989 bytes.
- `delivery/bundlebees-720p.mp4` — smaller 1280×720 review copy.
- `delivery/poster.png` — exact 1280×720 frame from the encoded master.
- `STORYBOARD.md` — timed shot table, frames, design, movement and sources.
- `SCRIPT.md` — screen script and factual source ledger.

The completed project is on branch **`video/showcase`**, with product facts from the v0.1.0 release (`57a6f12`). All additions are under `film/`. The master, compact preview, poster and Markdown documents are available directly after fetching the branch; rendering is optional. See `HANDOFF.md` for remote-host instructions. `delivery/bundlebees-delivery.zip` contains the ready-to-use delivery files and local review page.

## Reproduce

Node.js 22+, Python 3, FFmpeg/ffprobe with libx264/AAC/loudnorm, and Chromium. HyperFrames **0.9.34** was checked on npm on 2026-11-11 and is pinned exactly with a lockfile. Fonts, real stills, finished score and licenses are included. Normal rendering does not fetch media or need an account/API key.

```bash
cd film
npm ci
python3 scripts/audit.py
npm run preview
npm run render
```

HyperFrames can provision Chromium; an existing browser may be selected with `PRODUCER_HEADLESS_SHELL_PATH`. Author QA and capture scripts use `CHROME_BIN`. Rendering uses project/shared locks, two workers, low priority and two-core affinity on Linux, and caps FFmpeg threads at two. Exports are verified before atomically replacing published files.

Optional checks after building:

```bash
npm run build
npm run lint
npm run check -- --strict
npm run stills
npm run qa
python3 scripts/contact-sheet.py
```

`scripts/encoded-review.py` inspects actual master frames and creates the poster after rendering. `scripts/playback.mjs` checks the complete compact preview through a browser player.

New renders go to ignored `out/bundlebees-1080p.mp4`. After a revision, run encoded review and playback, update `REVIEW.md`, then run `python3 scripts/package.py` to refresh committed `delivery/` copies, checksums and the delivery ZIP together.

## Editing

`settings.json` defines the eleven contiguous shots; `copy.json` holds approved copy; `palette.json` defines the color roles. Each shot has one module under `scenes/`, using deterministic absolute-time drawing in `shared/`. Some deliberate line breaks and graphic labels are in scene modules. Update screen script and source ledger with editorial changes. `.build/` is disposable.

HIVE MIND is an original CC0 score made in MorningDAW 1.6.3 through the hermes-morningdaw tools. It runs 120 BPM in D minor, with the drop on bar 11 at 20.00s, mastered to −15 LUFS. `assets/audio/SCORE.md` explains how to reproduce it; factory Surge patches are not redistributed. Space Grotesk and IBM Plex Mono use their bundled original OFL licenses.

## Facts and evidence

Facts: `FACTS.md`. They are 4 tools, 571 o200k schema tokens (measured), 150 tests passed, release v0.1.0 at `57a6f12`, and catalog PR NousResearch/hermes-agent #153888.

Contract, dispatch, verify, retry, control and boundary diagrams are labelled illustrations. Shots 1, 3, 9, 10 and 11 use real stills: the banner (with its stale footer cropped off), the live orch board, terminal captures of real command output, and the Health pane screenshot. Before drawing, all on-screen text was scrubbed of tokens, hosts, IPs, private URLs and local paths.

`assets/provenance.json` records source URLs and exact hashes. No credentials, host addresses or private hostnames appear in the delivered media or documents. See `REVIEW.md` for performed checks and their limits.
