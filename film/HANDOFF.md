# Remote-host project-agent handoff

Repository: `https://github.com/mojomast/bundlebees.git`

Branch: **`video/showcase`**

Base: `feat/v0.1.0` at `bf5f1a7b4ef7ed661a892ac2450186081e238e4c`. Product facts: v0.1.0 at `57a6f12cdcfa789c5aba5f22f3f46efd2b8b3d5c` (see `FACTS.md`). Revision 2 adds real stills and a MorningDAW score.

The user requested this finished presentation be committed and pushed for the agent managing the project on another host. All additions are under `film/`.

## Fetch on your host

From an existing clone:

```bash
git fetch origin video/showcase
git log -1 --oneline origin/video/showcase
git diff --stat bf5f1a7b4ef7ed661a892ac2450186081e238e4c origin/video/showcase
```

Or use a fresh review clone:

```bash
git clone --branch video/showcase --single-branch \
  https://github.com/mojomast/bundlebees.git bundlebees-video
cd bundlebees-video
```

Read `film/README.md`, `film/REVIEW.md`, `film/STORYBOARD.md` and `film/SCRIPT.md`. Review and integrate the `film/` deliverables using the project's existing branch workflow, preserving any ongoing work.

## Finished artifacts

- **`film/delivery/bundlebees-1080p.mp4`** — primary presentation; 1920×1080, 30 fps, exactly 72 seconds / 2,160 frames, H.264/yuv420p + AAC stereo, faststart, 5,508,989 bytes.
- `film/delivery/bundlebees-720p.mp4` — compact preview, 1280×720, 3,039,141 bytes.
- **`film/delivery/poster.png`** — exact 1280×720 poster extracted from the final master.
- **`film/STORYBOARD.md`** — timing, shot design and factual sources.
- **`film/SCRIPT.md`** — screen script and source ledger.
- `film/delivery/bundlebees-delivery.zip` — ready-to-use media, Markdown documents, review page and verification data.
- `film/delivery/verification.json` — hashes and measured results; file paths are relative to `film/delivery/`.

The finished files are committed. They need neither the original host/private review server nor a rerender. All editable modules, local fonts with licenses, real stills, the original score and its reproduction notes (`assets/audio/SCORE.md`) are included in `film/`.

Verify committed delivery files from the repository root:

```bash
python3 - <<'PY'
import hashlib, json
from pathlib import Path
root = Path('film/delivery')
for item in json.loads((root / 'verification.json').read_text())['files']:
    p = root / item['file']
    assert p.stat().st_size == item['bytes'], p
    assert hashlib.sha256(p.read_bytes()).hexdigest() == item['sha256'], p
    print('OK', p)
PY
```

## Editorial contract

HIVE / CONTROL: graphite dark mode, honey amber and electric cyan, geometric bees and hexagonal sandbox cells, Space Grotesk + IBM Plex Mono. The film shows contracts, two dispatch waves, attempts, user-run verification, rejection and retry, feature-branch integration and explicit user control. Lower third y=720–1080 stays clear. Original 120 BPM instrumental lifts at 20s; no voiceover. Final tagline: **Lead the swarm. Keep control.** Closing URL: `github.com/mojomast/bundlebees`.

Facts come from `FACTS.md`: 4 tools, 571 o200k schema tokens (measured), 150 tests, v0.1.0 at `57a6f12`, catalog PR #153888. Only change these if a newer release changes the measurements.

Diagrams are labelled illustrations. Real stills (banner, orch board, terminal captures, Health pane) are scrubbed of private data and recorded in `assets/provenance.json`. Keep the user-run verify/deploy boundary.

## Verification

Both MP4s passed full decoding plus dimension, 30fps, 2,160-frame and 72-second checks. Encoded master audio measures **−15.13 LUFS / −3.33 dBTP**. The score PCM is −15.00 LUFS / −4.78 dBTP, and the drop onset is at 19.998s. Preview playback had zero dropped frames. A clean source build reproduced the fingerprint. Details are in `REVIEW.md`. Nobody has listened to it by ear yet.

## Optional revisions

HyperFrames **0.9.34**, exact dependency pin and lockfile. Rendering requires Node.js 22+, Python 3, FFmpeg/ffprobe and Chromium. Finished assets are bundled. To regenerate the score you need MorningDAW 1.6.3 and the hermes-morningdaw tools (see `assets/audio/SCORE.md`).

```bash
cd film
npm ci
python3 scripts/audit.py
npm run render
python3 scripts/encoded-review.py
node scripts/playback.mjs
# Update REVIEW.md, then refresh delivery files and checksums:
python3 scripts/package.py
```

Set `PRODUCER_HEADLESS_SHELL_PATH` for an existing rendering browser and `CHROME_BIN` for QA/playback. New renders go to ignored `film/out/`. Keep the approved facts, legibility, caption-safe lower third, no-voiceover treatment and 1080p30 output requirements.

Report the integration branch/commit and final artifact paths or links to the owner. The current request covers branch delivery and this handoff; publication to social platforms is a separate owner action.
