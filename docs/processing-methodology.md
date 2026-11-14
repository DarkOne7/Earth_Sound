# Processing methodology (Phase 3)

**Processing version:** `1.0.2` (`backend/app/services/reference_ranges.py`, `PROCESSING_VERSION`)  
**Date:** 2026-11-13  
**Scope:** turn an OSCAR v2.0 `u`/`v` sample into a validated, normalized time series of current speed and direction for the sonification layer (Phase 4).

Code: `backend/app/services/series_extraction.py`, `backend/app/services/current_processing.py`, `backend/app/services/reference_ranges.py`, `backend/app/schemas/current_series.py`.

## Inputs and evidence

Phase 2 did not commit a sample or a `data/metadata/` record because Earthdata retrieval is blocked (see [`ocean-dataset-decision.md`](ocean-dataset-decision.md)). This phase uses only facts that come from published provider metadata, and reads everything else from the file at run time:

| Item | Value used | Evidence |
|---|---|---|
| Zonal / meridional variables | `u`, `v` | PO.DAAC variable table; NASA CMR UMM-Var `V2193278944-POCLOUD` (`u`), `V2193278947-POCLOUD` (`v`) |
| Units | `m s-1` | Same UMM-Var records. Units are also **read from each file** and checked (see *Units*) |
| Valid range per component | −3.0 to 3.0 m s⁻¹ | UMM-Var `ValidRanges` for `u` and `v` |
| Fill value | −999.0 | UMM-Var `FillValues` (`SCIENCE_FILLVALUE`) |
| Dimensions | `time`, `longitude`, `latitude` | UMM-Var `Dimensions` (catalog evidence, not yet file-verified) |

UMM-Var query (public, no credentials): <https://cmr.earthdata.nasa.gov/search/variables.umm_json?concept_id[]=V2193278944-POCLOUD&concept_id[]=V2193278947-POCLOUD>, retrieved 2026-11-13.

## Extraction

- **Loading:** through the Phase 2 loader (`open_ocean_dataset`), with CF decoding, so the file's own `_FillValue` and `missing_value` become NaN.
- **Coordinates:** identified from the file's CF attributes: `standard_name` (`latitude`, `longitude`, `time`), `units` (`degrees_north`, `degrees_east`, or CF time units `<unit> since <reference>`, read from attributes or from xarray's decoded encoding) or `axis` (`Y`, `X`, `T`). If none match, the extractor falls back only to the UMM-Var dimension names (`latitude`, `longitude`, `time`). Ambiguous or missing coordinates raise `SeriesExtractionError`. Dimension order is irrelevant because selection is by name.
- **Longitude:** requests may use either −180..180 or 0..360. A request is converted to the file's convention, which is 0..360 if any longitude exceeds 180 and −180..180 otherwise.
- **Selection parameters** (lat, lon, region bounds) must be finite numbers; NaN or infinity raises `SeriesExtractionError`.
- **Point mode:** uses the nearest grid point. Longitude distance is measured around the circle. Ties go to the lowest index. A request farther than one grid step (the median coordinate spacing) from every grid point is rejected. If an axis has only one value, its spacing can't be derived, so the request must match that value within 1e-6°. The grid coordinates actually used are recorded.
- **Region mode:** inclusive bounds; `west > east` means the region crosses the antimeridian, and `east − west ≥ 360` selects every longitude (a full turn would otherwise wrap both bounds onto one meridian). For each timestamp:
  - a cell is *present* when both components are non-NaN and differ from the documented fill value;
  - no present cells gives `missing`;
  - any present cell that is infinite or outside the valid range gives `invalid`, and nothing is averaged around bad data;
  - otherwise `u` and `v` are averaged separately over the present cells, and the count is recorded as `cell_count`.
  Speed is then computed from the mean vector. That is **not** the same as the mean of the cell speeds; it is smaller when directions differ.
- **Time window:** inclusive and in UTC. A date-only bound, in ISO 8601 extended (`2020-01-30`) or basic (`20200130`) form, covers the whole day: start means 00:00:00 and end means 23:59:59.999999. Naive datetimes are treated as UTC. An empty window is an error. A series whose timestamps mix timezone-aware and naive values is rejected.
- **Ordering:** timestamps keep file order. Duplicate or out-of-order timestamps raise an error, and nothing is sorted silently. Processing re-checks the order.
- **Reproducibility:** the output records the selection parameters, the resolved UTC bounds, the dataset name and version, the file name and the file's SHA-256 (cached per path, modification time and size). The same parameters and file give byte-identical JSON (tested).
- **Defaults** (`backend/app/config.py`, overridable as `EARTH_JUKEBOX_*` environment variables) match the Phase 2 sample: 2020-01-01 to 2020-01-30, bbox W −152 / S 0 / E −150 / N 2, and point (1.0, −151.0), the bbox centre.

## Units

The `units` attribute of each component is normalized (lower-case, whitespace collapsed) and looked up in `VELOCITY_UNIT_FACTORS`. `m s-1`, `m/s`, `m s^-1`, `m.s-1` and spelled-out metres per second use factor 1. `cm s-1`, `cm/s` and `cm s^-1` use factor 0.01. A missing or unlisted unit is rejected, and units are never assumed. Extraction checks units when the file is opened and raises `SeriesExtractionError` the same way in point and region mode. Processing raises `UnitConversionError` for series built by hand. All outputs are in **m s⁻¹**, and the original units are recorded in `metadata.input_units`.

## Speed

`speed = sqrt(u² + v²)` (`numpy.hypot`), in m s⁻¹, computed only for valid samples.

## Direction convention

- **Oceanographic "toward" convention:** the direction the current **flows toward**.
- **Reference:** degrees **clockwise from true north**. 0° = northward, 90° = eastward, 180° = southward, 270° = westward.
- **Formula:** `direction = atan2(u, v)` in degrees, then `mod 360`.
- **Range:** `[0, 360)`. A result that rounds to exactly 360 is wrapped to 0.
- **Zero speed:** when `speed <= 1e-9` m s⁻¹ the direction is undefined. The sample gets status `undefined_direction`, and its direction and normalized direction are `null`. Its speed (0) and normalized speed remain usable.

Note: the meteorological wind convention ("comes from") differs by 180°. A test fails if the meaning is swapped.

## Validity rules and status values

All rules are applied in one place (`component_status_masks`), which processing and region aggregation both use. Each component is classified first, then the two classifications are combined. The −999.0 sentinel is a file value, so it is compared before unit conversion. A value that only equals −999 after conversion (for example −99900 cm s⁻¹) is out of range, so it is `invalid`, not `missing`.

| Condition | Component status |
|---|---|
| NaN (masked `_FillValue` / `missing_value`, land, ice or grid gap), or a **raw (unconverted)** value of exactly −999.0 | missing |
| ±infinity | invalid |
| Outside [−3.0, 3.0] m s⁻¹ (bounds inclusive) | invalid |
| Otherwise | valid |

Sample status:

| Status | Meaning |
|---|---|
| `missing` | Either component is missing. Missing takes precedence over invalid, so a sample is never half-valid. |
| `invalid` | No component is missing, but at least one is invalid, or region aggregation found invalid cells. |
| `undefined_direction` | Valid components, speed ≤ tolerance. |
| `valid` | Valid components and a defined direction. |

Only `valid` and `undefined_direction` samples carry numbers. For `missing` and `invalid` samples, `u`, `v`, speed, direction and both normalized values are `null`. Nothing is zero-filled, interpolated or forward-filled. All samples stay in the series, so the timeline is unchanged.

## Minimum-samples rule

`usable = valid + undefined_direction`. If `usable < minimum_usable_samples` (default **10**, read from `Settings.minimum_usable_samples` / `EARTH_JUKEBOX_MINIMUM_USABLE_SAMPLES` when the caller passes no explicit value), processing raises `InsufficientDataError`. The fully flagged series is attached as `.series` for diagnostics. Nothing is padded.

The value 10 is a **product design choice, not a scientific constant**. It is one third of the default 30-day window, which leaves roughly 3 seconds of a 30-second soundscape per usable day. Phase 4 may revise it.

## Normalization

### Speed → [0, 1]

`speed_normalized = (speed − min) / (max − min)`, where:

| Bound | Value | Source |
|---|---|---|
| min | 0.0 m s⁻¹ | Speed is a vector magnitude and cannot be negative. |
| max | 2.5 m s⁻¹ | NOAA National Ocean Service, ["How fast is the Gulf Stream?"](https://oceanservice.noaa.gov/facts/gulfstreamspeed.html): the maximum speed is "typically about nine kilometers per hour" (9 km/h = 2.5 m s⁻¹). The Gulf Stream is among the fastest ocean surface currents. Accessed 2026-11-13. |

- Exactly `min` gives 0.0 and exactly `max` gives 1.0; both are tested.
- Above `max` gives 1.0 with flag `clipped_high`, and the status stays `valid`. This can happen because two in-range components can give a speed up to √18 ≈ 4.24 m s⁻¹.
- Below `min` gives 0.0 with flag `clipped_low`. This cannot occur for real speeds but is defined for completeness.
- The scale is **never** derived from the sample, and the same input always gives the same output.

The full reference, with source text and URL, is embedded in every output (`metadata.speed_normalization`).

### Direction → [0, 1)

`direction_normalized = direction_deg / 360`, giving `[0, 1)`. 360° wraps to 0, so north is 0.0, east 0.25, south 0.5 and west 0.75. The scale is circular: 0.99 and 0.0 are adjacent directions.

## Tolerances

`NUMERIC_TOLERANCE = 1e-9` (m s⁻¹ for speeds, degrees for angles). It is used as the zero-speed threshold and as the absolute tolerance in tests. It is far below the precision of OSCAR's analysed currents. Trigonometric known-value tests use 1e-6°.

## Output representation

`ProcessedCurrentSeries` is a Pydantic model and is JSON-serializable (`model_dump_json()`) for the Phase 5 API. It contains:

- **`metadata`:** dataset and version, source file and SHA-256, variable names, input and output units, selection parameters, direction convention, normalization references with citations, component valid range, tolerance, minimum-samples rule and processing version.
- **`samples[]`:** `timestamp`, `u`, `v`, `speed`, `direction_deg`, `speed_normalized`, `direction_normalized`, `status`, `flags` and `cell_count` (region mode only).
- **`summary`:** `total`, `valid`, `undefined_direction`, `missing`, `invalid`, `clipped_low`, `clipped_high` and `usable`.

## Known limitations and `UNVERIFIED` items

- **No real OSCAR file has been processed yet.** All tests use synthetic NetCDF files. Coordinate attributes, the longitude convention, the `units` spelling, the presence of `_FillValue` and the time encoding in real files remain **`UNVERIFIED`** until Phase 2's Earthdata blocker is resolved. The code reads these from the file and fails clearly when they don't match.
- The ±3 m s⁻¹ valid range and the −999 fill value come from catalog metadata (UMM-Var), not from an inspected file.
- The 2.5 m s⁻¹ maximum describes the strongest western boundary currents. A quiet equatorial region will use only a small part of [0, 1]; that is intended, so loudness stays comparable across regions.
- OSCAR currents represent an assumed well-mixed upper 30 m and are a Level-4 analysis, not in-situ measurements.
- Region mode reports the speed of the mean vector, not the mean speed.
- Time cadence is not resampled; the series follows the file's time axis.
