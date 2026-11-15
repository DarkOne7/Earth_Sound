# Sonification mapping (Phase 4, Phase 4b)

**Sonification engine version:** `2.0.0` (`backend/app/services/sonification.py`, `SONIFICATION_VERSION`)
**Date:** 2026-11-14
**Scope:** turn a validated scientific time series into a reproducible, approximately 30-second stereo soundscape in which every sound parameter can be traced to a documented measurement and rule.

Code and tests:

| Layer | Code | Tests |
|---|---|---|
| Engine (domain-neutral) | `app/services/sonification.py`, `app/schemas/sonifiable.py`, `app/schemas/soundscape.py` | `tests/test_sonification.py` |
| Ocean currents domain | `app/services/current_sonification.py` | `tests/test_current_sonification.py` |
| COVID-19 domain | `app/services/case_sonification.py`, `app/services/case_processing.py` | `tests/test_case_processing.py` |

## Architecture

The engine renders a **`SonifiableSeries`**: a series whose data domain has already validated its measurements and reduced them to two audio-ready numbers per sample.

```
ProcessedCurrentSeries ──┐                            ┌── engine rules (rhythm, dB curve,
  (speed, direction)     │                            │    silence, pan law, pitch)
                         ├─► SonifiableSeries ─► engine ─► WAV + provenance
ProcessedCaseSeries ─────┘   level_normalized [0,1]   │
  (weekly counts)            pan [-1,1]               └── domain rules, supplied by the domain
                             audible: bool
```

The engine holds no knowledge of currents or case counts, so every domain-specific scientific decision stays in that domain's module and its own documentation. Both sets of rules are recorded in the provenance: `engine_mappings` and `domain_mappings`.

Adding a domain means writing an adapter that produces `level_normalized`, `pan` and `audible` per sample, plus the `MappingRule` entries justifying them. The engine's audio behaviour is unchanged by this: the OSCAR renders produced before the generalization are byte-identical to the ones produced after it (same WAV SHA-256, recorded below).

## Audio generation approach

Pure additive synthesis in NumPy, written with the standard-library `wave` module. No audio dependency was added. Rationale: the mappings are a handful of arithmetic rules, and a dependency-free renderer is easier to audit and to reproduce byte for byte.

| Property | Value | Where |
|---|---|---|
| Container / encoding | WAV (RIFF), PCM signed 16-bit little-endian | `encode_wav` |
| Channels | 2 (stereo, needed for the pan mapping) | `SonificationSettings.channels` |
| Sample rate | 44 100 Hz (`EARTH_JUKEBOX_SOUNDSCAPE_SAMPLE_RATE_HZ`) | `config.py` |
| Duration | 30.0 s exactly (`EARTH_JUKEBOX_SOUNDSCAPE_DURATION_SECONDS`) | `config.py` |
| Rounding to PCM | nearest integer (ties to even), ×32767, clipped to int16 as a last defence; no dither | `float_to_pcm16` |

The sample rate and duration are the only settings read from the environment. All other mapping constants are defaults of `SonificationSettings` and are recorded in full in every provenance file.

---

## Engine rules

Embedded in every output as `provenance.engine_mappings`.

### 1. Sample order → which pulse is which

- `F = round(duration_seconds × sample_rate_hz)` frames (1 323 000 for the defaults).
- The `n` samples are rendered in series order, one segment each, covering every frame exactly once with no overlap and no gap (tested).
- Each audible segment is shaped by a linear **attack** of 10 ms and **release** of 50 ms, each clamped to half the segment length, so each sample is heard as one pulse.
- Every segment records its own `timestamp`, `start_seconds` and `end_seconds`, so the mapping from audio time to data time is always exact and recoverable.

Nothing is resampled or interpolated by the engine.

### 2. `rate_normalized` → pulse rate (rhythm)

Segment *length* is not uniform by default. Each sample's share of the duration is proportional to

```
1 / (1 + rate_sensitivity × rate_normalized)          rate_sensitivity = 1.5
```

so rapid change gives short segments and a fast pulse, while calm stretches give long segments and a slow pulse. The **total duration is unchanged**; only its distribution moves. Boundaries are computed in exact rational arithmetic (`fractions.Fraction`), so the warped layout is reproducible across platforms.

| `rate_normalized` | share of a static sample's length | effect |
|---|---|---|
| 0.0 | 1.00 | slowest pulse |
| 0.5 | 0.57 | |
| 1.0 | 0.40 | fastest pulse, 2.5× the static rate |

`timing_mode = "uniform"` (or `--uniform-timing`) restores equal segments. Each domain documents what its rate of change is; a series with no rate signal is laid out uniformly either way, so the default is safe for a domain that has not defined one.

Rationale: a uniform pulse makes a long calm stretch and a sudden surge sound equally eventful. Letting the rate of change set the pulse rate means acceleration is *heard* as acceleration, which is the quantity a listener registers most immediately.

**The cost, stated plainly:** audio time is no longer proportional to elapsed data time. A listener cannot assume the halfway point of the file is the halfway point of the date range. `provenance.timing.linear_time_axis` is recorded as `false`, and each segment's own timestamp gives the exact mapping instead.

**Rhythm floor.** Above roughly **20 pulses per second** successive pulses stop being heard as separate events and fuse into a single rough or buzzing tone. Under rate weighting the pulse rate varies, so it is the *fastest* stretch that decides whether the rhythm can still be counted. `provenance.timing` records `timing_mode`, `rate_sensitivity`, `pulse_rate_hz_mean`, `pulse_rate_hz_max`, the shortest and longest segment, `linear_time_axis` and a `heard_as_rhythm` flag; `scripts/render_soundscape.py` prints a warning when that flag is false. The series is still rendered — the engine does not silently refuse — but nobody can mistake a buzz for a reported rhythm.

The default `rate_sensitivity` of 1.5 is the largest round value that keeps the reference COVID-19 render's fastest stretch (17.75 Hz) below the floor; 2.0 reaches 20.7 Hz and 3.0 reaches 26.1 Hz. This floor is also what stops the 1 674 daily COVID rows (53 Hz) from being presented as a rhythm; see the aggregation decision in [`covid-dataset-decision.md`](covid-dataset-decision.md).

The engine rules below are renumbered accordingly.

### 2. `level_normalized` → loudness

```
gain_db   = gain_floor_db × (1 − level_normalized)        gain_floor_db = −36 dB
amplitude = peak_amplitude × 10^(gain_db / 20)            peak_amplitude = 0.9
```

| `level_normalized` | gain |
|---|---|
| 0.00 | −36 dB (quietest audible level) |
| 0.25 | −27 dB |
| 0.50 | −18 dB |
| 1.00 | 0 dB |

Rationale: perceived loudness grows roughly with the logarithm of amplitude, so a mapping that is linear in decibels makes equal steps of the normalized level sound like roughly equal loudness steps. The 36 dB span is a **design choice**, not a scientific constant: wide enough to hear variation, while keeping the quietest level audible on ordinary speakers. A level of 0 stays audible so a measured minimum cannot be confused with silence (rule 3). Monotonicity is tested.

### 3. Audibility → silence

A sample the domain marked inaudible renders **digital silence** (every PCM value exactly 0) for its whole segment. The timeline keeps its place, so a gap is heard as a gap, never as a measurement. Tested against decoded PCM in both domains.

Because a level of 0 is audible and absent data is silent, "the measured minimum" and "no data" are distinguishable by ear.

### 4. `pan` → stereo position

```
angle = (pan + 1) / 2 × π/2
left  = cos(angle),  right = sin(angle)       (constant-power law, left² + right² = 1)
```

`pan = +1` is the right channel alone, `−1` the left alone, `0` is centre at −3 dB per channel. `pan = None` (unmapped or undefined) is centred. `pan_enabled = false` (or `--no-pan`) centres every segment and makes both channels identical (tested).

Rationale: stereo position is continuous and has no discontinuity, so it carries a second variable without disturbing the loudness that encodes the first.

### 5. Pitch and timbre: constant

Carrier 220 Hz (A3) with harmonics 1–3 at relative amplitudes 1.0 / 0.5 / 0.25, divided by their sum so the waveform peak never exceeds 1 before gain. The phase is continuous over the whole file (the carrier is generated once and multiplied by the per-segment envelope), so segment boundaries do not click.

No domain maps a measurement to pitch. Pitch therefore must not vary, or it would imply a measurement that does not exist.

---

## Domain: ocean surface currents

Input: `ProcessedCurrentSeries` from Phase 3. See [`processing-methodology.md`](processing-methodology.md).

| Sound parameter | Source | Rule |
|---|---|---|
| `level_normalized` | current speed (m s⁻¹) | `= speed_normalized`, i.e. speed against the cited 0–2.5 m s⁻¹ range, used unchanged |
| `pan` | `direction_deg` (direction the current flows **toward**, clockwise from true north) | `pan = sin(radians(direction_deg))`, the east-west component |
| `rate_normalized` | change in `speed_normalized` since the previous usable sample | `min(1, |Δ speed_normalized| / 0.25)`; a 0.625 m s⁻¹ step gives the fastest pulse |
| audibility | Phase 3 status | `valid` and `undefined_direction` sound; `missing` and `invalid` are silent |

| direction | flow | pan | heard |
|---|---|---|---|
| 90° | eastward | +1 | right channel only |
| 270° | westward | −1 | left channel only |
| 0° / 180° | northward / southward | 0 | centre |
| undefined (zero speed) | — | centre | centre |

Why pan and not pitch: direction is circular. Any mapping onto a linear parameter puts a discontinuity somewhere, and at north it would make 359° and 1° sound maximally different although they are nearly the same flow. The east–west component maps continuously to left–right (continuity across north is tested).

**Omitted** (recorded as `provenance.omitted_mappings`):

- **Temperature → pitch/timbre.** Phase 2 verified at file level (2026-11-14) that OSCAR v2.0 Final granules contain only `u`, `v`, `ug`, `vg`, `lat`, `lon`, `time`. SST appears only as an *input* in the global `source` attribute.
- **North–south component of direction.** Northward and southward flow are indistinguishable. No clearly interpretable second dimension was identified for the MVP; a candidate (brightness / filter cutoff) is left for Phase 6 listening tests.

## Domain: COVID-19 reported deaths and cases

Input: `ProcessedCaseSeries`. See [`covid-dataset-decision.md`](covid-dataset-decision.md) for the source, the verified weekly cadence and the reference maximum.

| Sound parameter | Source | Rule |
|---|---|---|
| `level_normalized` | reported deaths (or cases) for one calendar week | `log10(count) / log10(maximum)`, maximum fixed at **10⁵ deaths/week** (10⁸ cases/week) |
| `pan` | week-over-week ratio | `growth_log2 = log2(count / previous)`; `pan = clamp(growth_log2 / 1.0, −1, 1)` |
| `rate_normalized` | magnitude of the week-over-week ratio | `min(1, |growth_log2| / 2.0)`; a quadrupling *or* a fall to a quarter gives the fastest pulse |
| audibility | per-week aggregation status | silent when the week has no rows, any null row, or a negative value |

### Loudness is logarithmic

| deaths that week | `level_normalized` | gain |
|---|---|---|
| 0 or 1 | 0.0 | −36 dB |
| 10 | 0.2 | −28.8 dB |
| 100 | 0.4 | −21.6 dB |
| 1 000 | 0.6 | −14.4 dB |
| 10 000 | 0.8 | −7.2 dB |
| 100 000 | 1.0 | 0 dB |
| > 100 000 (`clipped_high`) | 1.0 | 0 dB |

Each decade is exactly one fifth of the loudness range. Weekly counts span five orders of magnitude, so a linear mapping would leave everything except the peak inaudible. The reference maximum is fixed and externally cited (WHO's record ~93 000 deaths for the week ending 2021-01-17 and its public warning of approaching 100 000 a week), never derived from the sample, so loudness is comparable between locations.

### Stereo position carries the rate of change

| this week vs last | `growth_log2` | pan | heard |
|---|---|---|---|
| doubled | +1.0 | +1 | right channel only |
| ×1.41 | +0.5 | +0.5 | right of centre |
| unchanged | 0.0 | 0 | centre |
| halved | −1.0 | −1 | left channel only |
| quadrupled | +2.0 | +1 (clamped) | right channel only |
| first week, or either week zero/unusable | — | centre, flagged `undefined_growth` | centre |

Rationale: the magnitude is already carried by loudness, which on a log scale changes only slowly once the counts are large. The *rate of change* is what a listener experiences as a wave arriving or receding, so giving it its own continuous dimension makes the waves audible rather than merely present.

**Omitted:**

- **Pitch or timbre.** Candidates such as the case-fatality ratio are dominated by testing capacity and reporting practice rather than by what a listener would take them to mean.
- **Per-capita normalization.** The default location is `World`, for which absolute and per-capita series have the same shape. Per-capita columns exist in the source and are the right choice for comparing countries, which is a later phase.

---

## Reproducibility

- **No randomness.** There is no random number generator and therefore no seed. The output is a pure function of the series and `SonificationSettings`.
- **Byte-identical** WAV bytes and provenance JSON for identical input and settings on the same platform and NumPy build. Tested in both domains, and checked again by `scripts/render_soundscape.py`, which refuses to overwrite an existing WAV that differs (`--overwrite` to replace) and reports `unchanged` when it matches.
- **Equivalence criterion across platforms:** libm implementations may differ in the last bit of `sin`, which can move isolated PCM values by one least-significant bit after rounding. Two renders are considered equivalent when their provenance (excluding `output.sha256`) is identical and every PCM sample differs by at most 1 LSB. The statement is embedded in each provenance file (`determinism`).
- **Provenance** (`SoundscapeProvenance`, written as `<stem>.soundscape.json`): engine version, full settings, engine and domain mapping rules, omitted mappings, the source reference (domain, dataset, source file and SHA-256, source URL, verified cadence, selection parameters, normalization reference, sample counts), `timing`, one `SegmentTrace` per sample (timestamp, status, flags, frame range, level, gain, amplitude, pan, and the underlying `source_values`) and the output description. The format fields are read back from the written WAV header, not copied from the request, and the render fails if they disagree with the settings.

## Generated samples

Commands (from the repository root, after `python -m pip install -e backend`):

```powershell
python scripts/fetch_covid_sample.py
python scripts/render_soundscape.py covid data\raw\owid-covid-weekly\owid-covid-weekly.csv --provenance-copy data\metadata
python scripts/render_soundscape.py ocean data\raw\oscar-final-20200101-20200130\oscar-final-20200101-20200130.nc --mode point --provenance-copy data\metadata
python scripts/render_soundscape.py ocean data\raw\oscar-final-20200101-20200130\oscar-final-20200101-20200130.nc --mode region --provenance-copy data\metadata
```

All three are 30.000 s, 44.1 kHz, 2 ch, 16-bit, 5 292 044 bytes. Rendered 2026-11-14:

| Render | Segments | Pulse rate (mean / max) | Segment length | Gain range | WAV SHA-256 |
|---|---|---|---|---|---|
| COVID-19 `World` deaths | 240 weeks, 0 silent | 8.0 / 17.8 per s | 56–141 ms | **−36.0 to 0.0 dB** | `dfb691701eb25951d51c974657a55f0bfba0d2afe1d7454112ba71847d91bdd5` |
| OSCAR point | 30 days, 0 silent | 1.0 / 1.34 per s | 744–1185 ms | −32.8 to −22.2 dB | `5d0bfaf7c0f157657653f6900557b45fac07d819548be71adc8816cd3d0ab9de` |
| OSCAR region | 30 days, 0 silent | 1.0 / 1.32 per s | 758–1150 ms | −32.8 to −25.2 dB | `0ba104b77d59bc20131a846f183d625adbed9ba0b31427beac5533e1bb06221f` |

Add `--uniform-timing` to reproduce equal-length segments. Doing so gives WAV SHA-256 `26737d0d40c165a9baa264ca9460aed1e992d9a17c2d2aab5db678ed41ca8463` (point) and `fcb1bf2a51db319c0d8090d059ef0096d4934e18e74ab90ca7614cc39c9ec15c` (region), which are **unchanged from before the engine was generalized** — the evidence that the refactor itself did not alter the ocean audio. The hashes in the table above differ from those only because the rate-driven rhythm is now on by default.

The WAV files live under the Git-ignored `data/processed/`. Their provenance is committed under `data/metadata/` so the exact render can be verified by re-running the command and comparing hashes.

### What the COVID-19 render sounds like

The gain contour follows the real pandemic arc, from 1 reported death in the first week to the January 2021 peak and then a long decay:

| Week ending | Reported deaths | Gain |
|---|---|---|
| 2020-01-05 | 1 | −32.6 dB |
| 2020-03-01 | — | −16.5 dB |
| 2020-04-26 | — | −2.5 dB |
| 2021-01-24 | **103 719** (peak) | 0.0 dB, `clipped_high` |
| 2022-06-19 | — | −7.5 dB |
| 2023-07-16 | — | −14.6 dB |
| 2024-06-16 | — | −16.3 dB |

So a listener hears a near-silent opening, a 32 dB crescendo over the first few seconds, a loud sustained plateau through 2020–2022 with the waves moving the stereo image, and a long fade. Pan reaches both rails (3 weeks hard left, 7 hard right). Flags across the 240 weeks: `clipped_high` 3, `partial_week` 1, `undefined_growth` 1 (all three of the latter at the series edge).

## Known-input mapping examples

Tested in the three test modules:

| Input | Expected |
|---|---|
| `level_normalized` 0 / 0.25 / 0.5 / 1 | gain −36 / −27 / −18 / 0 dB |
| gain 0 / −20 dB | amplitude 0.9 / 0.09 |
| `pan` +1 / −1 / 0 | right only / left only / equal channels at −3 dB |
| inaudible sample | all-zero PCM for the segment |
| `level_normalized` 0 | −36 dB, non-zero PCM |
| 100 frames, 30 segments | lengths 3 or 4, sum 100 |
| 1 s at 8 kHz | envelope 0 → 1 over 80 frames, 1 → 0 over the last 400 |
| 30 samples in 1 s / in 30 s | `heard_as_rhythm` false / true |
| `rate_normalized` 0 vs 1 | the fast sample's segment is 0.40× the slow one's |
| rates 0, 0.25, 0.5, 0.75, 1 | segment lengths strictly decreasing |
| no rates supplied | layout identical to `timing_mode="uniform"` |
| ocean direction 90° / 270° / 0° / 180° | pan +1 / −1 / 0 / 0 |
| COVID weekly count 1 / 10 / 10³ / 10⁵ | level 0.0 / 0.2 / 0.6 / 1.0 |
| COVID week 100 → 200 / 200 → 100 / 100 → 100 | pan +1 / −1 / 0 |
| COVID week with a null day | silent, status `missing` |

## Known limitations

- **Ocean:** the north–south component of direction is not audible, and the default equatorial region uses only about 10 dB of the available range (see [`covid-dataset-decision.md`](covid-dataset-decision.md) for why, and the Agulhas or Gulf Stream for a louder region).
- **COVID:** weekly resolution only; the pan moves abruptly between adjacent weeks because week-over-week growth is noisy; the three weeks above 10⁵ deaths are indistinguishable from each other.
- The −36 dB floor, the 30 s duration and the 20 Hz rhythm floor are product and perceptual choices, not scientific constants. The rhythm floor is a round figure for a gradual perceptual transition, not a sharp threshold.
- A series with more samples than frames is rejected; a series dense enough to exceed the rhythm floor is rendered with a recorded warning rather than refused.
- **No listening validation has been done for either domain.** That is Phase 6, and it is the right place to decide whether the pan mappings communicate or merely decorate.
- Byte-identity is guaranteed per platform; cross-platform runs are covered by the 1-LSB equivalence criterion above.
