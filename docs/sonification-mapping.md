# Sonification mapping (Phase 4)

**Sonification version:** `1.0.0` (`backend/app/services/sonification.py`, `SONIFICATION_VERSION`)  
**Date:** 2026-11-14  
**Scope:** turn a `ProcessedCurrentSeries` (Phase 3) into a reproducible, approximately 30-second stereo soundscape in which every sound parameter can be traced to a documented measurement and rule.

Code: `backend/app/services/sonification.py`, `backend/app/schemas/soundscape.py`, `scripts/render_soundscape.py`. Tests: `backend/tests/test_sonification.py`.

## Input

The engine consumes only the Phase 3 output (`ProcessedCurrentSeries`): per-sample `status`, `speed`, `speed_normalized`, `direction_deg` and `flags`, plus the series metadata. It never reads the NetCDF file and never re-derives a scale from the data. Everything about units, validity, the 0–2.5 m s⁻¹ normalization range and the direction convention is inherited from [`processing-methodology.md`](processing-methodology.md).

## Audio generation approach

Pure additive synthesis in NumPy, written with the standard-library `wave` module. No audio dependency was added. Rationale: the mapping is a handful of arithmetic rules, and a dependency-free renderer is easier to audit and to reproduce byte for byte.

| Property | Value | Where |
|---|---|---|
| Container / encoding | WAV (RIFF), PCM signed 16-bit little-endian | `encode_wav` |
| Channels | 2 (stereo, needed for the pan mapping) | `SonificationSettings.channels` |
| Sample rate | 44 100 Hz (`EARTH_JUKEBOX_SOUNDSCAPE_SAMPLE_RATE_HZ`) | `config.py` |
| Duration | 30.0 s exactly (`EARTH_JUKEBOX_SOUNDSCAPE_DURATION_SECONDS`) | `config.py` |
| Rounding to PCM | nearest integer (ties to even), ×32767, clipped to int16 as a last defence; no dither | `float_to_pcm16` |

The sample rate and duration are the only settings read from the environment. All other mapping constants are defaults of `SonificationSettings` and are recorded in full in every provenance file.

## Mapping rules

The same rules are embedded in the output as `provenance.mappings` (`MAPPINGS` in the code), so the audio file always travels with its own explanation.

### 1. Time axis → rhythm

- `F = round(duration_seconds × sample_rate_hz)` frames (1 323 000 for the defaults).
- The `n` samples of the series, in series order, get segments `[round(i·F/n), round((i+1)·F/n))`. Segments differ by at most one frame and cover every frame exactly once (tested). With the 30-day OSCAR sample each day is exactly 1.000 s.
- Each audible segment is shaped by a linear **attack** of 10 ms and **release** of 50 ms, each clamped to half the segment length, so each sample is heard as one pulse. The rhythm is therefore the measured time axis itself; nothing is resampled or interpolated.

### 2. Speed → loudness

Source: `speed_normalized ∈ [0, 1]`, i.e. speed against the documented 0–2.5 m s⁻¹ range (NOAA Gulf Stream maximum, see Phase 3).

```
gain_db   = gain_floor_db × (1 − speed_normalized)        gain_floor_db = −36 dB
amplitude = peak_amplitude × 10^(gain_db / 20)            peak_amplitude = 0.9
```

| speed (m s⁻¹) | speed_normalized | gain | meaning |
|---|---|---|---|
| 0.0 | 0.00 | −36 dB | quietest audible level (zero speed, direction undefined) |
| 0.625 | 0.25 | −27 dB | |
| 1.25 | 0.50 | −18 dB | |
| 2.5 | 1.00 | 0 dB | reference maximum |
| > 2.5 (`clipped_high`) | 1.00 | 0 dB | already clipped by Phase 3; the flag is copied into the segment trace |

Rationale: perceived loudness grows roughly with the logarithm of amplitude, so a mapping that is linear in decibels makes equal steps of normalized speed sound like roughly equal loudness steps. The 36 dB span is a **design choice** (not a scientific constant): it is wide enough to hear the daily variation of a quiet region, while keeping the quietest level audible on ordinary speakers. Zero speed must remain audible so that it cannot be confused with silence (rule 3). Monotonicity is tested.

For the default sample (equatorial Pacific, January 2020) the point series spans 0.22–0.96 m s⁻¹, which renders as −32.8 to −22.2 dB. A quiet region uses only part of the range on purpose: loudness stays comparable across regions because the reference is global, not per sample.

### 3. Status → silence

Samples with status `missing` or `invalid` render **digital silence** (every PCM value exactly 0) for their whole segment. The timeline keeps its place, so a gap is heard as a gap, never as a measurement. Tested against the decoded PCM.

Because `undefined_direction` samples are audible at −36 dB and gaps are silent, "no data" and "no current" are distinguishable by ear.

### 4. Direction → stereo position

Source: `direction_deg`, the direction the current flows **toward**, degrees clockwise from true north (Phase 3 convention).

```
pan   = sin(radians(direction_deg))         ∈ [−1, 1]
angle = (pan + 1) / 2 × π/2
left  = cos(angle),  right = sin(angle)     (constant-power law, left² + right² = 1)
```

| direction | flow | pan | heard |
|---|---|---|---|
| 90° | eastward | +1 | right channel only |
| 270° | westward | −1 | left channel only |
| 0° / 180° | northward / southward | 0 | centre (both channels, −3 dB each) |
| undefined (zero speed) | — | 0 | centre |

Why this mapping and not pitch: direction is circular. Any mapping onto a linear parameter (pitch, filter cutoff) puts a discontinuity somewhere, and at north it would make 359° and 1° sound maximally different although they are nearly the same flow. The east–west component maps continuously to left–right (the pan near north is continuous; tested), and it is the component most relevant to the equatorial currents in the default sample.

**Limitation (documented, by design):** the north–south component is **not rendered**. Northward and southward flow are indistinguishable. A second cue for `cos(direction)` was considered (brightness or filter cutoff) but no mapping was found that is clearly interpretable without training, so it is left for Phase 6 listening tests. The mapping can be disabled with `direction_mapping = "none"` (or `--no-direction`), which centres every segment and makes both channels identical (tested).

### 5. Pitch and timbre: constant

Carrier 220 Hz (A3) with harmonics 1–3 at relative amplitudes 1.0 / 0.5 / 0.25, divided by their sum so the waveform peak never exceeds 1 before gain. The phase is continuous over the whole file (the carrier is generated once and multiplied by the per-segment envelope), so segment boundaries do not click.

Nothing is mapped to pitch. Pitch therefore must not vary, or it would suggest a measurement that does not exist.

### Omitted mappings

Recorded in the output as `provenance.omitted_mappings`:

- **Temperature → pitch/timbre: omitted.** Phase 2 verified at file level (2026-11-14) that OSCAR v2.0 Final granules contain only `u`, `v`, `ug`, `vg`, `lat`, `lon`, `time`. SST appears only as an *input* in the global `source` attribute. See [`ocean-dataset-decision.md`](ocean-dataset-decision.md).
- **North–south direction component: omitted** (see rule 4).

## Reproducibility

- **No randomness.** There is no random number generator and therefore no seed. The output is a pure function of the processed series and `SonificationSettings`.
- **Byte-identical** WAV bytes and provenance JSON for identical input and settings on the same platform and NumPy build. Tested, and checked again by `scripts/render_soundscape.py`, which refuses to overwrite an existing WAV that differs (`--overwrite` to replace) and reports `unchanged` when it matches.
- **Equivalence criterion across platforms:** libm implementations may differ in the last bit of `sin`, which can move isolated PCM values by one least-significant bit after rounding. Two renders are considered equivalent when their provenance (excluding `output.sha256`) is identical and every PCM sample differs by at most 1 LSB. The statement is embedded in each provenance file (`determinism`).
- **Provenance** (`SoundscapeProvenance`, written as `<sample>.<mode>.soundscape.json`): sonification version, full settings, mapping rules, omitted mappings, the source series reference (dataset, version, source file and its SHA-256, selection parameters, processing version, normalization reference, direction convention, sample counts), one `SegmentTrace` per sample (timestamp, status, flags, frame range, gain, amplitude, direction, pan) and the output description (format, frames, duration, byte count, SHA-256). The format fields are read back from the written WAV header, not copied from the request, and the render fails if they disagree with the settings.

## Generated sample

Command (from the repository root, after `python -m pip install -e backend`):

```powershell
python scripts/render_soundscape.py data\raw\oscar-final-20200101-20200130\oscar-final-20200101-20200130.nc --mode point --provenance-copy data\metadata
python scripts/render_soundscape.py data\raw\oscar-final-20200101-20200130\oscar-final-20200101-20200130.nc --mode region --provenance-copy data\metadata
```

Rendered on 2026-11-14 from the Phase 2 sample (source file SHA-256 `920209900b74…`, recorded in full in the provenance):

| Mode | Output | Duration | Format | Bytes | WAV SHA-256 | Segments |
|---|---|---|---|---|---|---|
| point (1.0 °N, 209.0 °E) | `data/processed/oscar-final-20200101-20200130.point.wav` | 30.000 s | 44.1 kHz, 2 ch, 16-bit | 5 292 044 | `26737d0d40c165a9baa264ca9460aed1e992d9a17c2d2aab5db678ed41ca8463` | 30 audible, 0 silent |
| region (0–2 °N, 208–210 °E mean) | `data/processed/oscar-final-20200101-20200130.region.wav` | 30.000 s | 44.1 kHz, 2 ch, 16-bit | 5 292 044 | `fcb1bf2a51db319c0d8090d059ef0096d4934e18e74ab90ca7614cc39c9ec15c` | 30 audible, 0 silent |

The WAV files live under the Git-ignored `data/processed/`. Their provenance is committed under `data/metadata/` so the exact render can be verified by re-running the command and comparing hashes. Point mode spans −32.8 to −22.2 dB with pans from −1.00 (westward days) to +0.27; region mode is westward throughout (pan −1.00 to −0.19), which matches the mean westward flow of the South Equatorial Current in that box.

## Known-input mapping examples

Tested in `backend/tests/test_sonification.py`:

| Input | Expected |
|---|---|
| speed_normalized 0 / 0.25 / 0.5 / 1 | gain −36 / −27 / −18 / 0 dB |
| gain 0 / −20 dB | amplitude 0.9 / 0.09 |
| direction 90° / 270° / 0° / 180° | pan +1 (right only) / −1 (left only) / 0 / 0 |
| status missing or invalid | all-zero PCM for the segment |
| speed 0, status undefined_direction | −36 dB, centred, non-zero PCM |
| 100 frames, 30 segments | lengths 3 or 4, sum 100 |
| 1 s at 8 kHz | envelope 0 → 1 over 80 frames, 1 → 0 over the last 400 |

## Known limitations

- The north–south component of direction is not audible (rule 4).
- The loudness floor (−36 dB) and the 30 s duration are product choices, not scientific constants. The 30 s default gives 1 s per day for the 30-day sample; a longer series shortens each pulse proportionally, and a series with more samples than frames is rejected.
- Phase 3's `minimum_usable_samples` (10) still governs whether a series is rendered at all; Phase 4 does not revise it.
- No listening validation has been done yet; that is Phase 6.
- Byte-identity is guaranteed per platform; cross-platform runs are covered by the 1-LSB equivalence criterion above.
