# HIVE MIND — reproduction notes

Do not write a custom synthesizer or mixer. Recreate the score with MorningDAW 1.6.3
and the hermes-morningdaw tools (see the plugin skill `skills/morningdaw-song/SKILL.md`).

## Form

- 120 BPM, D minor, 4 LPB
- Intro 10 bars · Drop 16 · Break 8 · Hook 1 · Outro 1  → 36 bars = 72.00 s picture
- Drop downbeat is bar 11 = 20.00 s
- Render 2 extra bars (16 rows) of hall tail, mix_stems `seconds=74`, then trim to 72 s
  with a short close fade (0.75 s from 71.25) so the pad does not click at the cut

## generate() sketch

- title: HIVE MIND
- style: synthwave
- seed: 20261111
- Intro: pad+arp, hats, energy 0.42, chords Dm Dm Bb C Dm Gm Bb A Dm Bb
- Drop: pad+arp+bass+lead, four-on-the-floor, energy 1.0, Dm Bb F C
- Break: pad+bass+arp, hats, energy 0.55, Dm C Bb A Dm Gm Bb A
- Hook: all, four, energy 0.95, Dm
- Outro: pad+bass, hit, energy 0.4, Dm

## Instruments and effects

Query real names with `morningdaw_surge_patches` before assigning:

- Pad: `Pads/Distant.fxp` + Dragonfly Hall (`michaelwillis.dragonfly.hall`)
- Arp: `Plucks/Bell 1.fxp`
- Bass: `Basses/Sub 2.fxp`
- Master: Airwindows ToTape6 (`airwindows:ToTape6`) params `[0.5, 0.5, 0.5, 0.5, 0.7]`
- `ab_check` every effect (`changed: true`)
- Replace the default bitcrushed kit with acoustic tight pads:
  Kick Acoustic Tight 01, Snare Acoustic Tight Funk 02, HiHat Acoustic Closed Tight 01

## MorningDAW 1.6.3 render length

`--render-wav` sizes the song as (number of patterns × song.rows) and ignores each
pattern's own rows when `song.rows` stays at the default 64. Flatten the five section
patterns into one 576-row pattern, set `song.rows=576` (then 592 with the tail),
`order=[0]`, `loop.row1` to that length, so bar 11 stays at 20.00 s.

## Mastering

`mix_stems` with `qa_ok` true, then two-pass ffmpeg loudnorm `I=-15:TP=-1.5`.
Picture file is exactly 72.000 s, −15.00 LUFS integrated, true peak −4.78 dBTP.
Onset of the drop downbeat: 19.998 s (error −2 ms, within one 1/30 s frame).
