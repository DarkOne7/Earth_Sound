# COVID-19 dataset decision and verified facts (Phase 4b)

**Date:** 2026-11-14
**Scope:** choose and verify a rapidly-changing scientific dataset to sonify alongside ocean surface currents, and record every fact the processing layer relies on.

Related: [`sonification-mapping.md`](sonification-mapping.md), [`ocean-dataset-decision.md`](ocean-dataset-decision.md).

## Why a second dataset

The Phase 4 OSCAR soundscape is audibly flat, and its own provenance shows why:

| Render | Speed range | Gain range | Pan range |
|---|---|---|---|
| point (1 °N, 209 °E) | 0.22–0.96 m s⁻¹ | **−32.8 to −22.2 dB** | −1.00 to +0.27 |
| region (0–2 °N, 208–210 °E) | 0.225–0.752 m s⁻¹ | **−32.8 to −25.2 dB** | −1.00 to −0.19 |

Three causes, all of them real rather than bugs:

1. The default bbox is an equatorial Pacific box where the South Equatorial Current flows steadily westward. It is a quiet, smooth region.
2. Phase 3 deliberately normalizes speed against a **global** 2.5 m s⁻¹ reference (NOAA's Gulf Stream maximum) so that loudness is comparable between regions. A quiet region therefore occupies the bottom tenth of the range, by design.
3. Daily ocean surface currents are an inherently smooth, slowly varying signal. There is little rapid change in them to hear.

A dataset with a genuinely large dynamic range and fast variation was needed to give a listener something to feel. Ocean currents remain supported and unchanged.

## Source

| Item | Value |
|---|---|
| Publisher | Our World In Data (OWID), compiled from the World Health Organization |
| Table | `public/data/cases_deaths/full_data.csv` |
| Access URL | `https://raw.githubusercontent.com/owid/covid-19-data/master/public/data/cases_deaths/full_data.csv` |
| Credentials | **None.** Public HTTPS, no account, no token |
| Licence | CC BY 4.0 (OWID); underlying data compiled from WHO |
| Size | 20 120 449 bytes |
| SHA-256 of the published file | `462d509b1d4d1ffde0a8cabe023da0956f2ed40c8c0f119482390306fff56eba` (retrieved 2026-11-14) |

**Access note:** OWID's own domains (`covid.ourworldindata.org`, `catalog.ourworldindata.org`) did not respond from the development environment. The `owid/covid-19-data` GitHub mirror, which is the repository OWID publishes from, did. The fetcher uses the mirror and records the URL in the retrieval record.

Citation, reproduced in every retrieval record:

> Edouard Mathieu, Hannah Ritchie, Lucas Rodés-Guirao et al. (2020) "Coronavirus Pandemic (COVID-19)". Published online at OurWorldInData.org. Underlying data: World Health Organization.

The dataset is archived: it stopped updating on 2024-08-04, which makes it a stable, reproducible sample.

## Inspected file facts

Verified by downloading and profiling the published file on 2026-11-14. Nothing below is assumed from documentation.

| Item | Value |
|---|---|
| Rows | 411 804 |
| Locations | 246 |
| Date range | 2020-01-05 to 2024-08-04 (1 674 days) |
| Columns | `date`, `location`, `new_cases`, `new_deaths`, `total_cases`, `total_deaths`, `weekly_cases`, `weekly_deaths`, `biweekly_cases`, `biweekly_deaths` |
| `World` rows | 1 674, one per day, no nulls in `new_cases` or `new_deaths` |

### The critical finding: the "daily" columns are weekly

`new_cases` and `new_deaths` are **not daily measurements.** For the `World` series:

| Year | Days | Days with `new_deaths` > 0 |
|---|---|---|
| 2020 | 362 | 52 (14 %) |
| 2021 | 365 | 52 (14 %) |
| 2022 | 365 | 52 (14 %) |
| 2023 | 365 | 53 (15 %) |
| 2024 | 217 | 31 (14 %) |

And **every single non-zero day falls on a Sunday**, in both the dense early era and the late era:

```
2020-03-01 .. 2021-12-31 : 96 non-zero days, all Sunday
2023-01-01 .. 2024-08-04 : 84 non-zero days, all Sunday
```

A worked example (`World`, `new_deaths` | `weekly_deaths`):

```
2020-04-01      0.0   22284.0
2020-04-02      0.0   22284.0
2020-04-03      0.0   22284.0
2020-04-04      0.0   22284.0
2020-04-05  36846.0   36846.0      <- Sunday carries the whole week
2020-04-06      0.0   36846.0
2020-04-07      0.0   36846.0
```

**Consequence for processing:** treating these rows as daily measurements would render 86 % of the series as genuine reports of zero deaths. They are not reports of zero; they are days with no report. The series is therefore aggregated to calendar weeks ending Sunday, and the cadence is **detected from the rows at run time** (`detect_reporting_cadence`) rather than assumed, so a location that genuinely reports daily is handled correctly and the detection is recorded in the output.

Summing over a Monday-to-Sunday week is correct under either cadence: each week contains exactly one Sunday, so for a weekly source the sum equals the posted total, and for a daily source it sums seven real days.

### Verified weekly profile (`World`)

| Metric | Weeks | Minimum | Maximum | Peak week ending | log₁₀ span |
|---|---|---|---|---|---|
| deaths | 240 | 1 | 103 719 | 2021-01-24 | 0.00 – 5.02 |
| cases | 240 | 2 | 44 236 227 | 2022-12-25 | 0.30 – 7.65 |

240 weeks over a 30-second soundscape gives 125 ms per pulse, i.e. 8 pulses per second — fast and driving, and comfortably below the engine's 20 Hz rhythm floor. Rendering the 1 674 daily rows instead would give 18 ms per segment (53 Hz), where separate pulses fuse into a buzz; the engine records that condition and the render script warns about it.

## Decisions

### Default location: `World`

- It is the Earth-scale series, which fits the project, and it avoids singling out any one country.
- It has **no nulls** in either metric, so the default render has no reporting gaps.
- Absolute and per-capita series have the same shape for the world as a whole, so per-capita normalization adds nothing to the default.

### Default metric: deaths, not cases

- Reported **cases** track testing capacity and reporting policy at least as strongly as they track infections. The case peak in the series, 44.2 million for the week ending 2022-12-25, is inflated by China's reporting backlog after reopening, and is a reporting artifact rather than an epidemiological event.
- Reported **deaths** are less sensitive to testing volume and span a narrower, more tractable five decades.
- Cases remain available (`--metric cases`) with their own cited 8-decade reference range.

### Normalization: logarithmic, against a fixed external reference

Weekly counts span five orders of magnitude. A linear mapping would leave everything except the pandemic peak inaudible, so loudness is driven by `log10(count)` against a fixed maximum of **10⁵ deaths per week**.

That ceiling is externally grounded, not taken from the sample:

- WHO's *Weekly epidemiological update* of [19 January 2021](https://www.who.int/publications/m/item/weekly-epidemiological-update---19-january-2021) reported roughly **93 000** deaths for the preceding week, a record high at the time, a 9 % increase on the week before.
- On 18 January 2021 WHO's deputy head publicly warned that the trend pointed to the world reaching **100 000 deaths a week**.
- The revised OWID/WHO series peaks at **103 719** for the week ending 2021-01-24.

10⁵ is therefore the nearest power of ten at or above the global historical peak, and is the figure WHO itself framed publicly. It is held fixed for every location, so loudness is comparable between them, and three weeks of the global series exceed it and clip to 1.0 with flag `clipped_high`.

For cases the same construction gives 10⁸ cases per week, above the 44.2 million peak.

## Validity rules

| Condition | Week status |
|---|---|
| No source rows at all in the week | `missing` (silent) |
| Any row in the week has a null value | `missing` (silent) — the total cannot be known to be complete |
| Any row in the week is negative | `invalid` (silent) — revision artifacts are not measurements |
| Otherwise | `valid`, count = sum over the week |

Weeks are never dropped: a gap keeps its place on the timeline and is rendered as silence, so the 30-second duration always maps linearly onto the date range. A **reported** total of zero is a measurement and stays audible at the gain floor, which keeps "no deaths reported" distinguishable by ear from "no report".

`reported_days` is recorded per week. A week at the edge of the series may hold fewer than seven rows and is flagged `partial_week`; for a Sunday-reporting source such a week is still complete *as reported*, because the Sunday row carries the whole total.

The null rule is conservative and matters for countries rather than the world: `United States` has 442 null days in `new_cases`, which would silence roughly 63 of its 240 weeks.

## Known limitations

- **Reported, not actual.** These are reported deaths and cases. Excess-mortality estimates put true COVID-19 deaths substantially higher, and reporting completeness varies by country and over time. The soundscape sonifies *what was reported*, which is what the dataset contains.
- **Weekly resolution.** Within-week variation is not recoverable from this table and is not rendered.
- **The pan jitters.** Week-over-week growth is noisy, so stereo position moves abruptly between adjacent pulses. That is the data, not a rendering artifact, but whether it communicates or merely distracts is a Phase 6 listening question.
- **Three clipped weeks.** The January 2021 peak exceeds the 10⁵ ceiling, so the three loudest weeks are indistinguishable from each other. Raising the ceiling to 10⁵·⁵ would separate them at the cost of a less round, less citable reference.
- **No listening validation yet.** Phase 6.
- **`weekly_deaths`/`weekly_cases` columns are unused.** They are trailing 7-day rolling sums held constant between updates, so they overlap between adjacent days. Aggregating the posted totals ourselves gives non-overlapping weeks and keeps the aggregation rule explicit and testable.
