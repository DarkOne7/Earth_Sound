# Ocean dataset decision

**Research date:** 2026-11-13  
**Status:** OSCAR v2.0 Final selected for the MVP sample; actual NetCDF inspection and retrieval remain blocked pending Earthdata Login credentials.

## Decision

Use **OSCAR v2.0 Final** (`OSCAR_L4_OC_FINAL_V2.0`, DOI [10.5067/OSCAR-25F20](https://doi.org/10.5067/OSCAR-25F20)) for the first reproducible sample. The MVP needs a stable historical sequence, not near-real-time updates, and Final has the longest documented time coverage and the most mature latency tier. **Interpretation:** the product is a Level-4 analysis rather than direct in-situ measurements, based on PO.DAAC's description that the currents are calculated from satellite-sensed inputs with a simplified physical model. The product documentation describes daily averages on a global 0.25° × 0.25° grid, representing an assumed well-mixed upper 30 m.

OSCAR's published variables include current components and do not advertise a sea-surface-temperature variable. **SST decision: absent from the advertised OSCAR variables and not needed for the current MVP; file-level confirmation is `UNVERIFIED` until a NetCDF sample can be inspected.** Temperature gradients are described as an input to the current analysis, which does not mean the resulting current product contains an SST field.

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

The published variable table does not list an SST variable. Coordinate names, actual array dimensions/order, file-level attributes, fill values, encoded dtypes, and longitude convention have not yet been observed in a file and remain **`UNVERIFIED`** here.

## Access and size

- CMR collection and granule discovery is available through the public CMR Search API.
- The sample-window discovery query is reproducible at [CMR Search](https://cmr.earthdata.nasa.gov/search/granules.json?collection_concept_id=C2098858642-POCLOUD&page_size=40&sort_key=start_date&temporal=2020-01-01T00%3A00%3A00Z%2C2020-01-30T23%3A59%3A59Z&bounding_box=-152%2C0%2C-150%2C2). It returns 30 daily granules; the granules themselves cover the global grid.
- PO.DAAC documents the direct S3 data buckets as protected and says Earthdata Login is required for protected data. The dataset landing page also documents Harmony as a server-side subset service. Harmony documentation requires an Earthdata Login account for data access and supports bearer-token authentication.
- CMR's current catalog lists a full global daily OSCAR Final granule at approximately **31.7 MB**. This exceeds the fetcher's 25 MB default cap, so the fetcher must use Harmony subsetting and still enforce the cap against the response's declared size and streamed bytes. A complete granule must be refused by default.
- On 2026-11-13, this environment had no `EARTHDATA_TOKEN`/Earthdata username/password environment variables and no `%USERPROFILE%\.netrc` file. No authenticated Harmony request or data download was attempted. The human next step is to create or use an Earthdata Login account and set `EARTHDATA_TOKEN` in the local process environment.
- PO.DAAC's general data-use guidance says hosted data are openly shared and requests citation. Cite the selected dataset using the citation on its landing page. Any file-level `license` or use-constraint attributes remain **`UNVERIFIED`** until a sample is inspected.

## Reproducible sample selection

The intended default sample is:

- Collection: `OSCAR_L4_OC_FINAL_V2.0`, version `2.0`
- Time window: 2020-01-01 through 2020-01-30 UTC (30 daily granules were returned by the public CMR query on the research date)
- Variables requested: `u`, `v`
- Bounding box request in WGS84: west -152°, south 0°, east -150°, north 2°
- Retrieval: request a Harmony server-side subset, with a hard 25 MiB output cap
- Output location: Git-ignored `data/raw/`; retrieval record and inspection reports go under `data/metadata/`

The CMR granule query confirms that 30 collection granules exist in the selected time window. It does **not** confirm that this bounding box contains valid ocean measurements in each file; cell validity and the longitude transformation must be checked from the retrieved sample. Sample size after Harmony processing, service response behavior, exact coordinates and missing-data representation are **`UNVERIFIED`** until authenticated retrieval and file inspection.

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

### Open verification items

- Inspect an actual NetCDF file for dimensions, coordinate names and units, coordinate ranges, longitude convention, variable dtype/dims/attributes, fill values, scaling attributes, global attributes, grid increments, and time cadence.
- Determine whether a temperature/SST field appears in any variable or variable metadata in the inspected file.
- Verify the selected bbox contains usable current values and quantify missing values.
- Confirm Harmony's collection-specific subset support, output content type, size behavior, and single/multiple-granule response path after Earthdata authentication is available.
