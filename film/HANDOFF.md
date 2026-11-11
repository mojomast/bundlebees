# Remote-host project-agent handoff

Repository: `https://github.com/mojomast/bundlebees.git`

Branch: **`video/showcase`**

Base: `feat/v0.1.0` at `bf5f1a7b4ef7ed661a892ac2450186081e238e4c` (PR #1).

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

- **`film/delivery/bundlebees-1080p.mp4`** — primary presentation; 1920×1080, 30 fps, exactly 72 seconds / 2,160 frames, H.264/yuv420p + AAC stereo, faststart, 5,572,044 bytes.
- `film/delivery/bundlebees-720p.mp4` — compact preview, 1280×720, 3,104,570 bytes.
- **`film/delivery/poster.png`** — exact 1280×720 poster extracted from the final master.
- **`film/STORYBOARD.md`** — timing, shot design and factual sources.
- **`film/SCRIPT.md`** — screen script and source ledger.
- `film/delivery/bundlebees-delivery.zip` — ready-to-use media, Markdown documents, review page and verification data.
- `film/delivery/verification.json` — hashes and measured results; file paths are relative to `film/delivery/`.

The finished files are committed. They need neither the original host/private review server nor a rerender. All editable modules, local fonts with licenses, actual pane-source capture, original score and synthesis source are included in `film/`.

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

Facts come only from README.md and docs/ at the pinned revision. The brief supplied approximately 571 schema tokens and 142 tests, but those figures are absent from those documents. The film therefore shows **4 tools** and the documented **≤900 o200k schema-token budget**, explicitly labelled as a budget. Do not change it to a measured size or introduce a test-count claim without an updated approved source.

Diagrams are labelled illustrations. The genuine Models card comes from unmodified `desktop/plugin.js` component/CSS with an isolated SDK fixture and the documented model ID. It is labelled as an isolated demo, not a live worker run. Preserve these distinctions and the user-run verify/deploy boundary.

## Verification

Both MP4s passed full decoding, dimensions, 30fps, 2,160-frame count and 72-second duration. Master encoded audio: **−15.03 LUFS / −2.68 dBTP**. Full compact-preview playback: zero dropped frames, zero waits, no errors, seek to 46s passed. Source QA: 73 reverse + 73 shuffled exact-frame matches, ten motion intervals, 1,886 text bounds checks, sampled lower-third clearance. Clean install/build reproduced the rendered composition fingerprint exactly. Encoded scenes, cut boundaries and poster were inspected. Subjective listening was not performed.

## Optional revisions

Hyperrizz / HyperFrames **0.9.34**, exact dependency pin and lockfile. Rendering requires Node.js 22+, Python 3, FFmpeg/ffprobe and Chromium. Finished assets are bundled; NumPy is only needed to regenerate the original score.

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
