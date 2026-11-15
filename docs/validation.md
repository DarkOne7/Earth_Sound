# Validation record (Phase 6)

**Date:** 2026-11-15. **Scope:** the complete data-to-audio pipeline (load → extract → process → sonify → serve) for the three MVP domains, checked against known values, injected faults, the published API contract and a bounded performance budget. Scientific limits are in [`scientific-limitations.md`](scientific-limitations.md).

**Result:** 413 automated tests pass. Validation found and fixed two API contract defects (see *Defects found*). It also found one inconsistency in the COVID source data, which is recorded below and does not affect the output.

## Reproduce

```powershell
python -m pip install -e "backend[dev]"
python -m pytest                                   # all tests; real-sample tests skip if data/raw/ is empty
python -m pytest backend/tests/test_real_samples.py -rs   # the known-value checks on the real samples
python scripts/benchmark_pipeline.py --repeat 10 --output data/metadata/benchmark.json
```

The real-sample tests need the three samples fetched as the README describes. Without them, 19 tests in `test_real_samples.py` and 4 in `test_api.py` skip, and the rest of the suite runs on synthetic files.

## Coverage against the Phase 6 tasks

| Task | Where | What is checked |
|---|---|---|
| NetCDF loading, metadata, missing values | `test_dataset_processing.py`, `test_series_extraction.py`, `test_real_samples.py` §1 | The real sample's shape (30 × 9 × 9), coordinates, units, `_FillValue`, valid range and time axis match the committed inspection record. Its SHA-256 matches the reference render's `source_sha256`. The xarray loader returns the same numbers that `netCDF4` reads directly, and masks −999 as NaN. |
| Speed and direction | `test_current_processing.py`, `test_real_samples.py` §2 | Known vectors (e.g. (1, 0) → 90°). The "toward" convention is checked against the meteorological "from" convention. For all 30 real days, speed and direction are recomputed independently from raw `u`/`v` at the selected cell. The region mean is checked as the speed of the mean vector. |
| Selection, normalization, invalid/missing | `test_series_extraction.py`, `test_current_processing.py`, `test_case_processing.py`, `test_real_samples.py` §3–4 | Inclusive window and normalization bounds; clipping flags. Fault injection on copies of the real files (below). |
| Sonification parameters, determinism | `test_sonification.py`, `test_current_sonification.py`, `test_spectrum_sonification.py`, `test_real_samples.py` §5–6 | Gain, pan and pitch are measured from decoded WAV frames of the real renders. Real renders are byte-identical across runs and equal to the committed SHA-256. |
| API contract | `test_api.py`, `test_api_contract.py` | Every row of [`api.md`](api.md): endpoint inventory (GET only), every error status and body, every audio header, ranges, validators, parameter validation, caching and CORS. |
| Performance | `scripts/benchmark_pipeline.py`, `data/metadata/benchmark.json` | Per-stage timing and peak heap (below). |
| Scientific limitations | [`scientific-limitations.md`](scientific-limitations.md) | Consolidated, with links to the per-domain records. |

## Known-value checks on the real samples

The expected values in these checks are computed **outside** the code under test. The NetCDF file is read with `netCDF4`, not the xarray loader, with axes reordered by dimension name. COVID totals are compared with OWID's own published column. Audio is measured from WAV frames, not read back from the provenance.

| Check | Expected (independent) | Result |
|---|---|---|
| Nearest cell to 1.0° N, 151.0° W | lat 1.0, lon 209.0 (0–360) | match |
| Day 1 (2020-01-01), point | u = 0.081339 m s⁻¹, v = 0.294718 m s⁻¹ → speed 0.305736 m s⁻¹, toward 15.429° (NNE; "from" would give ~195°) | match to 1e-12 |
| Point, all 30 days | `hypot(u, v)`, `atan2(u, v) mod 360`, speed / 2.5 | match to 1e-12 (speed), 1e-9 (°) |
| Region, all 30 days | 81 cells per day; `hypot(mean u, mean v)` ≤ mean of cell speeds | match |
| COVID World deaths, 239 complete weeks | OWID `weekly_deaths` on each Sunday | exact match |
| COVID level | `log10(count) / 5`, clipped at 1 | match to 1e-12; peak 103,719 deaths in the week ending 2021-01-24; 3 weeks clip high |
| Gain per pulse (ocean 30, COVID 240) | `−36 (1 − level)` dB, peak `0.9 · 10^(dB/20)` | provenance to 1e-9; measured WAV peak within 1 % + 1 LSB |
| Pan per pulse | constant-power `cos`/`sin((pan + 1) π/4)`; ocean `pan = sin(direction) = u / speed` | measured per channel within 1 % + 1 LSB |
| Pitch | FFT peak at 220 Hz; harmonics at 440 and 660 Hz with relative amplitudes 0.5 and 0.25 | 220.0 Hz ± 0.1; ratios within 5 %; < 1 % at 330 Hz |
| Determinism | committed SHA-256 for ocean point, ocean region, COVID World deaths and Sirius | byte-identical across two runs and equal to the committed hash |

Measured ranges on the reference renders: ocean point speed 0.22–0.96 m s⁻¹ (gain −32.8 to −22.2 dB), ocean region 0.22–0.75 m s⁻¹. The COVID pulse rate is 8 Hz on average and 17.8 Hz at its fastest, below the 20 Hz rhythm floor.

## Fault injection: missing and invalid data never sound

The real samples contain no gaps (30/30 days and 240/240 weeks are usable), so their renders cannot show what happens to bad data. The tests therefore copy each real sample, damage chosen entries, run the full pipeline, and decode the WAV.

| Injected into a copy of | Fault | Status | Audio |
|---|---|---|---|
| OSCAR, point | `u` = −999 (day 3), `v` = −999 (day 4) | `missing` | all-zero frames |
| OSCAR, point | `u` = 3.5 (day 9), `v` = −3.0000001 (day 10) | `invalid` | all-zero frames |
| OSCAR, point | `u` = −999 and `v` = 99 on day 15 | `missing` (missing wins) | all-zero frames |
| OSCAR, point | `u` = 3.0 exactly (day 20) | `valid` (inclusive bound) | sounds |
| OSCAR, region | one cell of 81 at 7.0 m s⁻¹ (day 5) | `invalid`, nothing averaged | all-zero frames |
| OSCAR, region | all cells −999 (day 12) | `missing` | all-zero frames |
| OSCAR, region | one cell −999 (day 18) | `valid`, mean of 80 cells | sounds |
| OWID | a Sunday's `new_deaths` blank (week 21) | `missing` | all-zero frames |
| OWID | a Sunday's `new_deaths` = −5 (week 61) | `invalid` | all-zero frames |
| OWID | all seven rows of week 101 deleted | `missing`, week kept in the timeline | all-zero frames |

In every case, the set of silent segments equals the set of damaged entries exactly. Every other segment contains sound, and every silent segment is exactly zero in both channels.

## Defects found and fixed

| Defect | Contract clause | Fix |
|---|---|---|
| A malformed COVID CSV (missing columns, bad dates, duplicate rows, a missing metric column) returned `422 unprocessable_data` | [`api.md`](api.md) *Errors*: malformed sample → `500 sample_invalid` | New `CaseSampleError` (a `CaseProcessingError` subclass, so scripts are unaffected), mapped to 500 |
| Those COVID errors embedded the server's absolute file path in `detail` | *No paths leak* | Messages name the file only |
| An empty or binary COVID file raised a raw pandas/Unicode error (unstructured 500) | *Errors are JSON with `detail` and an `error` code* | Wrapped as `CaseSampleError` |

The ocean and Sirius paths already met these clauses. `test_api_contract.py` now checks all three domains.

## Source finding: OWID's cumulative column

OWID's `total_deaths` is not exactly the running sum of `new_deaths` in the 2026-11-14 snapshot. In 28 of 240 weeks, the week-on-week difference of `total_deaths` differs from that week's summed `new_deaths`. The largest single-week difference is 3,432 (week ending 2023-04-02). The total absolute difference is 4,042 deaths out of 7,060,988 (0.06 %), and the net difference is 3,856: the sum of `new_deaths` is 7,060,988, against a final `total_deaths` of 7,057,132.

The pipeline follows `new_deaths`, the column OWID's own `weekly_deaths` is built from, and matches `weekly_deaths` exactly. The difference is a revision inside the source, not a processing error. `test_owid_cumulative_column_disagrees_with_its_daily_column_by_under_0_1_percent` keeps it bounded, so a refetched snapshot with a larger inconsistency will be noticed.

## Performance

**Method.** `scripts/benchmark_pipeline.py` times each stage separately on the real samples, with default settings (30 s, 44.1 kHz stereo). It does one untimed warm-up run, then 10 timed runs (`time.perf_counter`). Peak Python heap allocation is measured on a separate run with `tracemalloc`, so the measurement does not slow the timed runs. API rows go through FastAPI's `TestClient`. *Cold* clears the render cache before each request; *warm* serves from it. Raw results are in [`data/metadata/benchmark.json`](../data/metadata/benchmark.json).

**Environment.** Windows 11 (10.0.26300), Intel 12-thread CPU (Family 6 Model 151), CPython 3.15.0, NumPy 2.5.4, pandas 3.1.1, xarray 2026.9.0. Measured 2026-11-15.

| Stage | Median (s) | Max (s) | Peak heap (MB) |
|---|---:|---:|---:|
| Ocean point: extract (NetCDF → u/v) | 0.205 | 0.226 | 0.1 |
| Ocean point: process | 0.0001 | 0.0002 | 0.0 |
| Ocean point: render (30 pulses) | 0.102 | 0.110 | 71.1 |
| Ocean region: extract | 0.202 | 0.227 | 0.3 |
| Ocean region: render | 0.101 | 0.121 | 71.1 |
| COVID World: process (CSV → 240 weeks) | 0.034 | 0.044 | 0.8 |
| COVID World: render (240 pulses) | 0.169 | 0.223 | 71.4 |
| Sirius: parse + render (400 bins) | 0.103 | 0.116 | 70.8 |
| API ocean audio, cold / warm | 0.325 / 0.006 | 0.357 / 0.009 | 71.2 / 0.1 |
| API COVID audio, cold / warm | 0.212 / 0.007 | 0.242 / 0.008 | 71.8 / 0.1 |
| API Sirius audio, cold / warm | 0.103 / 0.003 | 0.132 / 0.006 | 70.8 / 0.1 |

**Reading.** Every cold end-to-end request takes under 0.4 s on this machine, which agrees with the 0.1–1 s range stated in [`api.md`](api.md). Rendering uses about 71 MB of transient heap for a 30 s file: float64 stereo plus carrier buffers before 16-bit encoding. Memory scales linearly with `duration_seconds`, so the 120 s API cap bounds one render at roughly 285 MB. These are single-machine figures for the bounded MVP samples, not a load test. Throughput under concurrent requests was not measured.

## Not validated here

- **Listening.** No listening test with people has been run. Whether the pan and rhythm mappings communicate anything, rather than merely decorate, is still open (see [`scientific-limitations.md`](scientific-limitations.md)).
- **Cross-platform byte identity.** Renders are byte-identical on one platform. Other platforms are covered only by the 1-LSB equivalence criterion in [`sonification-mapping.md`](sonification-mapping.md).
- **Other samples.** Validation used the documented default samples. Other regions, windows, locations and the `cases` metric are covered by synthetic tests only.
