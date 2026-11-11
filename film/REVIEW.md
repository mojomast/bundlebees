# bundlebees — production review

## Scope

72s / 2160 frames / 1920×1080 / 30fps. HyperFrames 0.9.34, esbuild 0.25.12, Puppeteer Core 25.16.0. Source revision bf5f1a7b4ef7ed661a892ac2450186081e238e4c. Facts limited to README/docs. No live worker performance or test results claimed. Missing 571/142 figures replaced by the documented four-tool interface and explicitly labelled ≤900 schema budget.

## Completed preflight

- Asset provenance: local fonts with original OFL licenses; actual pane component/CSS captured through an isolated SDK fixture, cropped at device scale 3; original CC0 synthesized score. Original hashes verified.
- Visual review: all 15 source stills/contact sheet, full-resolution opening, tool/budget proof, pane close-up and end card. Distinct compositions cover contract fan-out, sandbox waves, curved dispatch rail, rejected gate, retry return arc, authority split and closing bee mark.
- Same renderer used for author checks and HyperFrames frame sources. 73 reverse + 73 shuffled exact-pixel matches, ten motion intervals, effect-sensitivity check, no page errors.
- 1,886 canvas text bounds checks; zero violations. Caption region y=720–1080 remained exactly flat graphite in every sampled source frame. This is custom canvas QA, not a claim that HyperFrames' DOM analyzer audited canvas text.
- Strict HyperFrames check passed at 12 selected times, including runtime, layout, motion/craft and audio checks. `out/framework-check.json` contains the original report.
- Score HIVE MIND: 120 BPM, zero-phase start, lift at 20s, exact scene-boundary accents, resolved 65–72s close. No voiceover or samples. PCM measured −15.01 LUFS / −3.01 dBTP.

## Export review

| File | Dimensions | Bytes | Picture / frames | LUFS | True peak |
|---|---|---:|---|---:|---:|
| `delivery/bundlebees-1080p.mp4` | 1920×1080 | 5,572,044 | 72.000s / 2,160 | −15.03 | −2.68 dBTP |
| `previews/bundlebees-720p.mp4` | 1280×720 | 3,104,570 | 72.000s / 2,160 | −15.05 | −2.67 dBTP |

Both files passed full audio/video decoding, H.264, frame-rate/count, dimensions, duration, audio-presence, loudness, true-peak and size checks. Stereo AAC, yuv420p, faststart. Master SHA-256: `d603b02d9d58ff440f00fba60d3d71fc78a70bebaedc472ba14eccbf345e761a`.

Actual MP4 review: 15 scene frames, 17 scene/internal boundaries at −1/0/+1 frame, ten motion intervals. All motion intervals change pixels; sampled encoded lower thirds have zero spatial color range. End-card text settles by 67s and remains readable to the end (background parallax continues; 68–71s mean pixel-channel difference 0.0526). Actual encoded contact/cut sheets and 1280×720 poster inspected.

An isolated source-only copy completed `npm ci` and `npm run build`. Its exact build fingerprint matches the rendered composition: `13b89e235612a2919aa10181a36d65330d3970cc13d3ff61efe0a9401b2d310b`.

Full browser-preview playback completed unmuted: 2,160 displayed frames, zero dropped frames, zero waiting events and no player/page errors. Seek to 46s passed. This is programmatic playback verification. Subjective audio listening has not been performed.

## Branch handoff

At the user's subsequent request, the finished master, poster, storyboard, script, verification data and editable source are packaged on `video/showcase`. All additions are under `film/`; the committed master is byte-identical to the verified render. `HANDOFF.md` supplies repository-relative instructions for the project agent on another host. Render intermediates, dependency folders, individual QA frame caches and private review state are ignored.
