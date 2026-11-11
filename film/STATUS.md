# Production checkpoint

Source: feat/v0.1.0 @ bf5f1a7b4ef7ed661a892ac2450186081e238e4c; PR #1. New project; all authored production files in film/. HyperFrames 0.9.34 exact pin (npm checked 2026-11-11).

72s / 2160 frames / 1080p30. HIVE / CONTROL design, Space Grotesk + IBM Plex Mono, original 120 BPM score, no VO. Lower third y≥720 reserved. Storyboard and script written. Product claims restricted to README/docs; 571/142 omitted because absent from those sources. Actual pane-source capture complete, clearly labelled isolated demo.

Source/preflight complete: 15 reviewed stills, 73 reverse + 73 shuffled exact-frame matches, 1,886 text bounds checks, all sampled lower thirds clear. Strict HyperFrames runtime/layout/craft/audio check passed. Actual Models-card source captured at 3× with isolated SDK fixture. Original score PCM measured −15.01 LUFS / −3.01 dBTP.

Render sh_1cc3962ed0010h7ZgqT0gF2FK1 completed. Master out/bundlebees-1080p.mp4: 5,572,044 bytes, exact 72s / 2160 frames; −15.03 LUFS / −2.68 dBTP. Preview: 3,104,570 bytes, −15.05 LUFS / −2.67 dBTP. Both passed full decode. Actual encoded stills, cut-boundary samples, motion and lower-third checks passed; poster is delivery/poster.png at 1280×720.

Clean source-only npm ci + build reproduced fingerprint 13b89e235612a2919aa10181a36d65330d3970cc13d3ff61efe0a9401b2d310b exactly. Full-preview playback sh_1cc3ca134001AFZk4A6rciDwpX passed: 2,160 frames, zero drops/waits/errors, seek46 passed. No active render or QA job. Subjective listening is unperformed.

Deliverables in delivery/: verified master and compact preview, 1280×720 poster, storyboard, screen script, review, machine-readable measurements and complete ZIP. Local review page index.html contains no private addresses. User subsequently requested a branch commit/push and a handoff for the agent on another host. Branch: video/showcase, based on the pinned source revision above; all additions under film/. See HANDOFF.md. Private review-server state remains in ignored out/private-review.json.
