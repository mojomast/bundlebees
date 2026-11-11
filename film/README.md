# bundlebees — HIVE / CONTROL

An original 72-second presentation for the open-source Hermes Agent plugin by mojomast. Dark graphite, honey-amber work units, cyan verification paths, sharp geometric bees and hexagonal sandbox cells. A rejected attempt visibly returns, revises and retries. Lower third is reserved for optional narration captions. Original 120 BPM music, no voiceover.

## Deliverables

- **Committed master:** `delivery/bundlebees-1080p.mp4` — 1920×1080, 30 fps, 72 seconds, H.264/yuv420p with AAC stereo and faststart; 5,572,044 bytes.
- `previews/bundlebees-720p.mp4` — smaller 1280×720 review copy.
- `delivery/poster.png` — exact 1280×720 frame from the encoded master.
- `STORYBOARD.md` — timed shot table, frames, design, movement and sources.
- `SCRIPT.md` — screen script and factual source ledger.

The completed project is on branch **`video/showcase`**, based on the pinned `feat/v0.1.0` revision below. All additions are under `film/`. The master, compact preview, poster and Markdown documents are available directly after fetching the branch; rendering is optional. See `HANDOFF.md` for remote-host instructions. `delivery/bundlebees-delivery.zip` contains the ready-to-use delivery files and local review page.

## Reproduce

Node.js 22+, Python 3, FFmpeg/ffprobe with libx264/AAC/loudnorm, and Chromium. HyperFrames **0.9.34** was checked on npm on 2026-11-11 and is pinned exactly with a lockfile. Fonts, image capture, finished score and licenses are included. Normal rendering does not fetch media or need an account/API key.

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

Original music source is `scripts/music.py` (NumPy required only to regenerate audio). HIVE MIND is original CC0 synthesis; phase zero at frame zero, lift at 20s, a resolved close at 65s and decay within the 72s picture. Space Grotesk and IBM Plex Mono use their bundled original OFL licenses.

## Facts and evidence

Source: `https://github.com/mojomast/bundlebees`, PR #1, branch `feat/v0.1.0`, commit `bf5f1a7b4ef7ed661a892ac2450186081e238e4c`. Claims come only from README.md and docs/. The four tools and **≤900 o200k schema-token budget** are documented there. The brief's 571-token and 142-test results are absent from those files, so they are not asserted. A budget is labelled as a budget, not a measured result.

All swarm/retry graphics are labelled illustrations. The Models card is the actual unmodified `desktop/plugin.js` component/CSS rendered through an isolated React SDK fixture; its model ID comes from docs/RESEARCH.md. It is cropped at device scale 3 for readable original pixels. It is labelled as an isolated demo, not a live launcher session. Recapturing with `npm run capture` requires the pinned repository's `desktop/plugin.js`; ordinary builds use the bundled PNG.

`assets/provenance.json` records source URLs and exact hashes. No credentials, host addresses or private hostnames appear in the delivered media or documents. See `REVIEW.md` for performed checks and their limits.
