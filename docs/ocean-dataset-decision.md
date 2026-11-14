# Ocean dataset decision

**Research date:** 2026-11-13  
**Status:** OSCAR v2.0 Final selected. The default sample was retrieved through PO.DAAC Cloud OPeNDAP and inspected on 2026-11-14 (see *Inspected file facts*).

## Decision

Use **OSCAR v2.0 Final** (`OSCAR_L4_OC_FINAL_V2.0`, DOI [10.5067/OSCAR-25F20](https://doi.org/10.5067/OSCAR-25F20)) for the first reproducible sample. The MVP needs a stable historical sequence, not near-real-time updates, and Final has the longest documented time coverage and the most mature latency tier. **Interpretation:** the product is a Level-4 analysis rather than direct in-situ measurements, based on PO.DAAC's description that the currents are calculated from satellite-sensed inputs with a simplified physical model. The product documentation describes daily averages on a global 0.25° × 0.25° grid, representing an assumed well-mixed upper 30 m.

OSCAR's published variables include current components and do not advertise a sea-surface-temperature variable. **SST decision: absent and not needed for the MVP. Confirmed at file level on 2026-11-14:** the inspected granules contain only `u`, `v`, `ug`, `vg`, `lat`, `lon` and `time`, and the inspector found no temperature candidates. SST appears only in the `source` attribute, as an *input* (CMC 0.1° SST V3.0). Temperature gradients are described as an input to the current analysis, which does not mean the resulting current product contains an SST field.

## Product comparison

The current date ranges below are the collection metadata and most recent public CMR granules observed on the research date. These are catalog snapshots and should be checked again when a sample is fetched.

| Product | Collection / DOI | Collection time range | Latest public CMR granule observed | Update latency | MVP trade-off |
|---|---|---|---|---|---|
| Final | `OSCAR_L4_OC_FINAL_V2.0` / [10.5067/OSCAR-25F20](https://doi.org/10.5067/OSCAR-25F20) | 1993-01-01 to 2026-01-17 | 2026-01-16 | About 1 year | Longest historical record and completed quality tier; chosen for reproducibility. |
| Interim | `OSCAR_L4_OC_INTERIM_V2.0` / [10.5067/OSCAR-25I20](https://doi.org/10.5067/OSCAR-25I20) | 2020-01-01 to present | 2026-11-08 | About 1 month | More recent than Final, with a shorter record and provisional latency tier. |
| Near Real-Time | `OSCAR_L4_OC_NRT_V2.0` / [10.5067/OSCAR-25N20](https://doi.org/10.5067/OSCAR-25N20) | 2021-01-01 to present | 2026-11-11 | About 2 days | Most current, but recency is unnecessary for a fixed demonstration sample. |

All three are published by PO.DAAC; Earth & Space Research (ESR) is the product generator. PO.DAAC describes the files as NetCDF-4 and the products as daily, global, 0.25° × 0.25° grids. The published variables are:

| Variable | Published long name | Published unit |
|---|---|---|
| `u` | Zonal total surface current | m s⁻¹ |
| `ug` | Zonal geostrophic surface current | m s⁻¹ |
| `v` | Meridional total surface current | m s⁻¹ |
| `vg` | Meridional geostrophic surface current | m s⁻¹ |

The published variable table does not list an SST variable.

### Inspected file facts (2026-11-14)

Observed in the granule DMR (`oscar_currents_final_20200101`) and in the retrieved sample; see `data/metadata/oscar-final-20200101-20200130.inspection.*`.

| Item | Observed |
|---|---|
| Dimensions | `time` (1 per granule), `longitude` (1440), `latitude` (719) |
| Coordinate variables | `lat(latitude)`: `standard_name=latitude`, `degrees_north`, −89.75…89.75. `lon(longitude)`: `standard_name=longitude`, `degrees_east`, **0…359.75**. They are **not** named like their dimensions. |
| Data variables | `u`, `v`, `ug`, `vg`: `float64`, dims `(time, longitude, latitude)`, `units = m s-1`, `_FillValue = -999.`, `valid_min = -3.0`, `valid_max = 3.0`, `coordinates = "lon lat"`, `depth = "15m"` |
| Time | `float64`, `days since 1990-1-1`, `calendar = "julian"`, one value per day at the day start (e.g. 10957.0 → 2020-01-01). `time_bounds = "2020-01-01 00:00:00 to 2020-01-01 23:59:59"` |
| Grid | 0.25° × 0.25° (computed from the coordinates) |
| Global attributes | `Conventions = CF-1.8 …, ACDD-1.3`; `product_version = v2.0`; `acknowledgment = "OSCAR products are supported by NASA and may be freely distributed."` No separate `license` attribute. |
| Default bbox | 9 × 9 cells (lat 0.0–2.0, lon 208.0–210.0), **0 % missing** for `u` and `v` over all 30 days |

## Access and size

- CMR collection and granule discovery is available through the public CMR Search API.
- The sample-window discovery query is reproducible at [CMR Search](https://cmr.earthdata.nasa.gov/search/granules.json?collection_concept_id=C2098858642-POCLOUD&page_size=40&sort_key=start_date&temporal=2020-01-01T00%3A00%3A00Z%2C2020-01-30T23%3A59%3A59Z&bounding_box=-152%2C0%2C-150%2C2). It returns 30 daily granules; the granules themselves cover the global grid.
- PO.DAAC documents the direct S3 data buckets as protected and says Earthdata Login is required for protected data. The dataset landing page also documents Harmony as a server-side subset service. Harmony documentation requires an Earthdata Login account for data access and supports bearer-token authentication.
- CMR's current catalog lists a full global daily OSCAR Final granule at approximately **31.7 MB**. This exceeds the fetcher's 25 MB default cap, so the fetcher subsets on the server and still enforces the cap against declared and streamed bytes. A complete granule is refused by default.
- **Access path used: PO.DAAC Cloud OPeNDAP (Hyrax, DAP4).** Each CMR granule record links to `https://opendap.earthdata.nasa.gov/collections/C2098858642-POCLOUD/granules/<granule>`, which accepts the Earthdata bearer token. The fetcher:
  1. reads the granule DMR to identify the CF coordinate variables;
  2. downloads only `lat`/`lon` and converts the bbox to index ranges using the real values (rows 359–367 → lat 0.0–2.0; columns 832–840 → lon 208.0–210.0);
  3. requests `/u[0:0][832:840][359:367];/v[…];/lat[…];/lon[…];/time` for each day, about 44 KB per granule;
  4. combines the days along time after checking that `lat`/`lon` are identical in every file.
  The combined file was checked value-for-value against the raw daily files, and against points fetched independently from OPeNDAP (day 15 at 1.0°/209.0°, day 27 at 1.5°/208.5°).
- **Harmony capabilities, verified 2026-11-14** from the public endpoint <https://harmony.earthdata.nasa.gov/capabilities?collectionId=C2098858642-POCLOUD>: the service is `sds/HOSS-geographic`. It supports bbox, temporal, variable, dimension and shape subsetting. Concatenation is **not** supported, and the only output format is NETCDF-4 with MIME type `application/netcdf`. The rangeset route is `/{collection}/ogc-api-coverages/1.0.0/collections/{variables}/coverage/rangeset` ([Harmony OpenAPI schema](https://github.com/nasa/harmony/blob/main/services/harmony/app/schemas/ogc-api-coverages/1.0.0/ogc-api-coverages-v1.0.0.yml)). An earlier fetcher version left out the `collections/` segment and received HTTP 404 after authentication. Because each granule is subset separately, the fetcher combines the daily outputs locally along time.
- **Harmony cannot spatially subset OSCAR v2.0 (2026-11-14).** Both request forms failed on every work item:
  - bbox (`lat()`/`lon()`), job `fb97df4f-46d3-4164-b3d6-3bc718a70898`: `Projected variable "/u" does not have an associated "grid_mapping" metadata attribute`;
  - named dimension (`latitude(…)`/`longitude(…)`), job `70ec899e-8ddc-405e-89c9-f6f6a639fc87`: `"latitude" is not a dimension for any of the requested variables`.
  The cause is the file layout. HOSS ([source](https://github.com/nasa/harmony-opendap-subsetter/blob/main/hoss/dimension_utilities.py)) needs a coordinate variable named like each dimension, but OSCAR's dimensions `latitude`/`longitude` are described by variables `lat`/`lon`. This needs a NASA-side configuration change, so the fetcher defaults to OPeNDAP and keeps `--access harmony` for unsubsetted requests or other collections. Details of the first failure: A `lat()`/`lon()` request failed on every work item (job `fb97df4f-46d3-4164-b3d6-3bc718a70898`) with: `nasa/harmony-opendap-subsetter:1.3.0: Projected variable "/u" does not have an associated "grid_mapping" metadata attribute` (and the same for `/v`). Without same-named coordinate variables HOSS treats `u`/`v` as projected and requires CF `grid_mapping`, which OSCAR does not provide.
- Credentials: the fetcher reads `EARTHDATA_TOKEN` from the process environment (or a local `.netrc`). The token is never stored in the repository or written to records.
- PO.DAAC's general data-use guidance says hosted data are openly shared and requests citation. Cite the selected dataset using the citation on its landing page. The inspected files have no `license` attribute. Their `acknowledgment` attribute states that OSCAR products "may be freely distributed".

## Reproducible sample selection

The intended default sample is:

- Collection: `OSCAR_L4_OC_FINAL_V2.0`, version `2.0`
- Time window: 2020-01-01 through 2020-01-30 UTC (30 daily granules were returned by the public CMR query on the research date)
- Variables requested: `u`, `v`
- Bounding box request in WGS84: west -152°, south 0°, east -150°, north 2°
- Retrieval: PO.DAAC Cloud OPeNDAP DAP4 index subset of each daily granule, combined locally, with a hard 25 MiB cap on downloaded bytes
- Output location: Git-ignored `data/raw/`; retrieval record and inspection reports go under `data/metadata/`

The retrieved sample has 30 daily granules of 9 × 9 cells each, with no missing `u`/`v` values (combined file about 78 KB). Missing data (land, sea ice, no analysis) is stored as `_FillValue = -999` and decoded to NaN. Live checks found 67 % missing cells in a bbox over Hawaii (land) and 100 % missing near the North Pole, both of which Phase 3 flags rather than fills.

The inspection tool flags values only against `valid_min`, `valid_max`, or `valid_range` attributes found in the file. It does not introduce an undocumented physical threshold.

If credentials are unavailable, the sample is not fabricated or downloaded through another route. Use the fetch script after setting the token; alternatively, commit only this decision and metadata until access is available.

## Provenance and citations

Provider: NASA Physical Oceanography Distributed Active Archive Center (PO.DAAC), Jet Propulsion Laboratory. Product generator: Earth & Space Research (ESR). Collection: OSCAR Level 4, version 2.0, Final latency. Selected DOI: <https://doi.org/10.5067/OSCAR-25F20>.

Suggested dataset citation: ESR; Dohan, Kathleen. 2022. *Ocean Surface Current Analyses Real-time (OSCAR) Surface Currents - Final 0.25 Degree (Version 2.0).* Ver. 2.0. PO.DAAC, CA, USA. Dataset accessed 2026-11-13 at <https://doi.org/10.5067/OSCAR-25F20>.

### Authoritative references

- [PO.DAAC OSCAR v2.0 Final landing page](https://podaac.jpl.nasa.gov/dataset/OSCAR_L4_OC_FINAL_V2.0)
- [PO.DAAC OSCAR v2.0 Interim landing page](https://podaac.jpl.nasa.gov/dataset/OSCAR_L4_OC_INTERIM_V2.0)
- [PO.DAAC OSCAR v2.0 Near Real-Time landing page](https://podaac.jpl.nasa.gov/dataset/OSCAR_L4_OC_NRT_V2.0)
- [PO.DAAC OSCAR v2.0 release announcement](https://podaac.jpl.nasa.gov/announcements/2022-01-31-Earth-and-Space-Research-OSCAR-L4-V2.0-Dataset-Release)
- [NASA CMR Search API documentation](https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html)
- [NASA Harmony documentation](https://harmony.earthdata.nasa.gov/docs)
- [PO.DAAC Data Use and Citation Guideline](https://podaac.jpl.nasa.gov/CitingPODAAC)

### Related visual resource

NASA SVS [Global Sea Surface Currents and Temperature](https://svs.gsfc.nasa.gov/3912) is an animation rendered from ECCO2 model output (March 2007 – March 2008). It is imagery, not a measurement dataset, so it is not used as processing input. It is a candidate visual companion for the frontend. ECCO2 is a possible future source of SST if temperature is added.

### Open verification items

- Resolved on 2026-11-14: file structure, coordinates, longitude convention, fill values, valid range, time encoding, SST absence, and missing values in the default bbox (see *Inspected file facts*).
- Open: the `calendar = "julian"` label conflicts with `time_bounds` and the CMR temporal extent, which both give Gregorian dates. Phase 3 reads the labels as Gregorian (see `processing-methodology.md`). Confirming this with ESR/PO.DAAC would remove the remaining doubt.
- Open: Harmony spatial subsetting for OSCAR depends on a NASA-side HOSS configuration change.
