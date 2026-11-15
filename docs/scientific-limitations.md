# Scientific limitations and interpretation notes

**Read this before drawing conclusions from an Earth Jukebox soundscape.** This page collects the limits that the per-domain records state in detail, so they can be found next to the MVP. The evidence behind each item is in the linked document. The validation record is [`validation.md`](validation.md).

## What a soundscape is, and is not

- **A designed encoding, not a recording.** Every sound is a deterministic, documented mapping from numbers to audio parameters. The 220 Hz carrier, the −36 dB floor, the 30 s duration, the C3–C5 Sirius pitch range and the 20 Hz rhythm floor are product and perceptual choices, not physical constants ([`sonification-mapping.md`](sonification-mapping.md)).
- **Relative, not absolute.** Loudness encodes a value relative to a cited reference range, and the perceived loudness also depends on playback level and equipment. Read the numbers from the provenance JSON, not from the sound.
- **Not validated by listening.** No listening test with people has been run. Whether stereo position and rhythm communicate the data, or only decorate it, is still open. Until such a test exists, treat pan and rhythm as secondary cues.
- **Rate-weighted timing distorts time.** By default, rapid change shortens a pulse and calm lengthens it, so audio time is **not** proportional to data time (`timing.linear_time_axis = false`). Use `timing_mode=uniform` to compare durations.
- **Silence means "no usable data", never zero.** Missing and invalid samples are digital silence. A measured zero (zero current speed, a level of 0) is quiet but audible. This is enforced and tested ([`validation.md`](validation.md#fault-injection-missing-and-invalid-data-never-sound)).

## Ocean surface currents (OSCAR v2.0 Final)

Details: [`ocean-dataset-decision.md`](ocean-dataset-decision.md), [`processing-methodology.md`](processing-methodology.md).

**Nature of the product**
- A **Level-4 analysis**, not in-situ measurement. The currents are calculated from satellite inputs (sea-surface height, ERA5 10 m wind, SST gradients) with a simplified physical model. They describe an assumed well-mixed upper 30 m, labelled `depth = "15m"` in the file.
- The **Final** tier was chosen for reproducibility. It lags real time by about a year, so this is a historical record.
- `u` and `v` are **total** currents. The geostrophic components `ug` and `vg` in the same files are not used.

**Resolution and coverage of the default sample**
- Daily averages on a 0.25° grid (about 28 km at the equator). Sub-daily and sub-grid variability, including tides, inertial oscillations and eddies smaller than the grid, is not represented.
- **One small sample:** 30 days (2020-01-01 to 2020-01-30) in a 2° × 2° box in the central equatorial Pacific (0–2° N, 152–150° W), 81 cells. It is not representative of other regions or seasons.
- Point mode takes the **nearest grid cell**, with no interpolation. Region mode reports the speed of the **mean vector**, which is smaller than the mean of the cell speeds when directions differ.

**Assumptions**
- The file's `calendar = "julian"` labels are read as Gregorian UTC dates, because `time_bounds` and the CMR extent agree with that reading. This choice is documented and will stay correct until 2100. Reading them as true Julian instants would shift every day by 13 days. ESR/PO.DAAC have not yet confirmed which reading is right.
- Speed is normalized against a **global** 0–2.5 m s⁻¹ reference (NOAA, Gulf Stream maximum), so loudness is comparable between regions. As a result, the quiet default region uses only about 10 dB of the 36 dB range. That is intended, not a fault.
- Direction uses the oceanographic **"toward"** convention, which is 180° from the meteorological "from" convention.

**Omitted variables and mappings**
- **Sea-surface temperature** is not in OSCAR v2.0 granules. It appears only as an input in the `source` attribute (verified at file level, 2026-11-14), so temperature is not sonified.
- **North–south direction is not audible.** Pan carries only the east–west component, so northward and southward flow sound the same.
- Depth structure, vertical velocity and uncertainty estimates are not available in the product, so none of them is sonified.

## COVID-19 weekly deaths and cases (Our World In Data, WHO-compiled)

Details: [`covid-dataset-decision.md`](covid-dataset-decision.md).

- **Reported, not actual.** Excess-mortality estimates put true COVID-19 deaths substantially higher. Reporting completeness varies by country and over time.
- **Weekly resolution.** The source posts each week's total on Sunday. Within-week variation cannot be recovered.
- **Source revisions.** OWID's cumulative `total_deaths` disagrees with the running sum of `new_deaths` by 0.06 % over the series, in 28 of 240 weeks. The pipeline follows `new_deaths`, and therefore OWID's own `weekly_deaths` ([`validation.md`](validation.md#source-finding-owids-cumulative-column)). Negative values, which are revision artifacts, are flagged `invalid` and silent.
- **Logarithmic loudness.** Each factor of 10 is one fifth of the loudness range against a cited ceiling of 10⁵ deaths per week. Equal loudness steps are therefore equal *ratios*, not equal counts. The three weeks above the ceiling (peak: 103,719 deaths, week ending 2021-01-24) clip and cannot be told apart.
- **Noisy pan.** Week-over-week growth jumps between adjacent weeks, so stereo position jumps too. That is the data, but it may not be a useful cue.
- **Absolute counts.** The default location is `World`, where absolute and per-capita counts have the same shape. Comparing countries would need per-capita normalization, which is not implemented.

## Sirius visible spectrum (STScI CALSPEC)

Details: [`sirius-spectrum-decision.md`](sirius-spectrum-decision.md).

- An audible encoding of a **calibrated reference composite** spectrum, not a new observation and not a natural sound of the star. Colour has no musical pitch. The 380–780 nm → C3–C5 map is a design choice.
- The scan conveys wavelength position and relative flux. It does not preserve the light's physical frequency and does not label individual spectral lines.

## Operational limits

- **Local and bounded.** The API serves only the locally fetched samples. It has no authentication or rate limiting, and its cache is per process ([`api.md`](api.md#limitations)). A 30 s render takes about 0.1–0.35 s and about 71 MB of transient memory on the reference machine ([`validation.md`](validation.md#performance)).
- **Byte identity is per platform.** Across platforms, equivalence means a difference of at most 1 LSB.
- **Validated on the default samples.** Other regions, windows, locations and the `cases` metric are covered by synthetic tests only.

## Provenance

Every soundscape's provenance JSON (`/soundscapes/{domain}` or `data/metadata/*.soundscape.json`) records the dataset and version, source file SHA-256, selection, every mapping rule with its citation, the per-segment trace and the WAV's SHA-256. Retrieval records for each sample are under `data/metadata/*.retrieval.json`.
