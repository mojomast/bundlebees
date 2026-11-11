# bundlebees — production review

## Scope

72s / 2160 frames / 1920×1080 / 30fps. HyperFrames 0.9.34, esbuild 0.25.12, Puppeteer Core 25.16.0, Node 22. Product facts are the measured v0.1.0 figures: 4 tools, 571 o200k schema tokens, 150 tests, released at `57a6f12cdcfa789c5aba5f22f3f46efd2b8b3d5c`, catalog PR NousResearch/hermes-agent #153888 (open). See `FACTS.md`. This replaces the earlier "≤900 budget" stand-in.

## Revision 2: real stills and new score

Real images now on screen:

- `assets/images/banner.png`: the real docs/assets/banner.png from v0.1.0. It is cropped to the top 1280×556 because the repo banner's footer strip still reads "142 tests", which is out of date. Used in shots 1 and 11.
- `assets/images/orch-state.png`: the board is drawn from live launcher state for today's compose-v2 and bundlebees waves (CV01–04, BB01–04), including the BB02 rejection (`att_8d589c81592c66a7`, child_env allowlist mismatch after BB01) and retry (`att_238d7f7d39f31891`, merged `da2aff1`). Scrubbed of tokens, hosts, IPs, private URLs and local paths. Used in shot 3.
- `assets/images/term-schemas.png`, `term-doctor.png`, `term-pytest.png`: terminal stills rendered from real command output on the v0.1.0 tree (`tools=4 chars=2617 o200k=571 cl100k=561`, `registrations: 4 tool(s), 0 hook(s)`, `150 passed`). Local paths and the Hermes update preamble were removed. Used in shot 9.
- `assets/images/pane-health.png`: the real docs/screenshots/pane-health.png, labelled ACTUAL PANE · HEALTH. Used in shot 10.
- Shot 6 keeps the labelled retry illustration and adds the real BB02 attempt record as a caption.

Score: HIVE MIND, an original piece made in MorningDAW 1.6.3 with the hermes-morningdaw tools. 120 BPM, D minor. Intro 10 bars, Drop 16, Break 8, Hook 1, Outro 1 (36 bars = 72s), with the drop on bar 11 at 20.00s. Instruments are Surge XT Pads/Distant with Dragonfly Hall, Plucks/Bell 1 and Basses/Sub 2, an acoustic tight drum kit, and ToTape6 on the master. `ab_check` showed every effect changed (ToTape6 −1.5 dB, Hall +3.6 dB). `mix_stems` passed `qa_ok` with zero clicks on every stem and the mix (74s with the hall tail). Two-pass loudnorm then brought the 72s picture file to **−15.00 LUFS, −4.78 dBTP, LRA 3.3**. An onset check put the drop downbeat at **19.998s** (−2 ms, within one 1/30s frame) in both the PCM master and the encoded 1080p audio. See `assets/audio/SCORE.md`. The old NumPy `scripts/music.py` was removed.

## Preflight

- `scripts/audit.py`: exact timeline, measured 4/571/150 facts, asset hashes from provenance.json, no private addresses or credential signatures in screen copy.
- `hyperframes lint`: 0 errors, 0 warnings.
- 15 source stills: lower third clear, 125 typography checks, zero violations.
- Seek QA: 73 reverse and 73 shuffled exact-pixel matches, ten motion intervals, effect sensitivity, 1,743 canvas text-bounds checks, zero violations. Caption region y=720–1080 stayed flat graphite in every sampled frame.

## Export review

| File | Dimensions | Bytes | Picture / frames | LUFS | True peak |
|---|---|---:|---|---:|---:|
| `delivery/bundlebees-1080p.mp4` | 1920×1080 | 5,508,989 | 72.000s / 2,160 | −15.13 | −3.33 dBTP |
| `delivery/bundlebees-720p.mp4` | 1280×720 | 3,039,141 | 72.000s / 2,160 | −15.23 | −3.27 dBTP |

Both files passed full decode plus checks for H.264, frame rate and count, dimensions, duration, audio presence, loudness, true peak and size. Master SHA-256: `e4530749288d1b0fa4d1c8288b234c83a498abd232d480f2a7632ed806fb2cf1`.

Encoded review covered 15 scene frames, 17 boundaries at −1/0/+1 frame and ten motion intervals, all of which changed pixels. The encoded lower third has zero spatial range. The contact sheet and the 1280×720 poster (frame 2070) were checked by eye.

A separate source-only copy ran `npm ci` and `npm run build` and reproduced the rendered fingerprint `92160fbed1683e1609cb5efede8e05f61c41111ec72f5bb0e4deb94a6687a825`.

Programmatic playback of the full 720p preview completed unmuted: 2,160 frames, zero dropped frames, zero waiting events, no errors, and the seek to 46s passed. Nobody has listened to it by ear yet.
