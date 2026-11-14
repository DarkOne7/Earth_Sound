# Sirius visible-spectrum sonification

**Date:** 2026-11-14  
**Scope:** add an auditable stellar spectrum as a third sound source alongside the project's ocean and COVID time series.

## Project status before this addition

The project already had a deterministic 30-second WAV renderer for validated time series. Ocean current speed or weekly reported deaths controlled loudness; direction or week-over-week growth controlled stereo position; pulse timing followed the sample order and, where available, the rate of change. That engine deliberately kept pitch constant because its supported datasets had no defensible pitch measurement. The Sirius work is a wavelength scan with its own pitch mapping, so it lives in `app/services/spectrum_sonification.py` rather than changing the shared time-series engine.

## Spectrum source and selection

| Item | Value |
|---|---|
| Provider | Space Telescope Science Institute (STScI), CALSPEC |
| File | `sirius_stis_005.fits` |
| Download | <https://ssb.stsci.edu/cdbs/calspec/sirius_stis_005.fits> |
| CALSPEC catalogue | <https://www.stsci.edu/hst/instrumentation/reference-data-for-calibration-and-tools/astronomical-catalogs/calspec> |
| Spectrum extension | FITS `SCI` binary table, wavelength / flux columns |
| Target | Sirius (`SIRIUS`) |
| Flux-scale reference in file header | Bohlin et al. 2020, *AJ*, 160, 21 |
| File pedigree in header | `INFLIGHT 1997 to 2022` |
| Raw FITS SHA-256 | `1349da7b8b59ad035aefea8d7948f552b41b3897d07e5ad82ca162a53af97271` |
| Selected range | 380 to 780 nm (visible light) |

STScI describes CALSPEC Sirius as a composite with IUE data in the shorter ultraviolet and a Kurucz model beyond 1 micrometre. This render uses only the 380–780 nm visible interval. The FITS header identifies the selected spectrum's visible measurements as STIS data; the far-UV and infrared model extensions do not enter the render.

Only rows flagged `DATAQUAL=1` with finite positive flux are used. The interval is averaged into 400 consecutive 1 nm bins. This gives one pulse per nanometre, in increasing wavelength order, over exactly 30 seconds (75 ms per bin, about 13.33 pulses per second). Missing or invalid bins cause an error instead of being invented or silently filled.

## What each pitch means

Each bin's **pitch represents its wavelength interval**. The wavelength is linearly transposed to equal-tempered notes from C3 to C5:

```
MIDI note = 48 + 24 * (wavelength_nm - 380) / 400
frequency_hz = 440 * 2 ** ((MIDI note - 69) / 12)
```

The mapping covers two octaves across 400 nm: one semitone is 16.667 nm, and each 1 nm bin moves by 0.06 semitone. These are audible synthesized pitches, **not** the physical frequency of the light. The light itself is far above the human hearing range. Lower wavelengths begin at C3; higher wavelengths rise in pitch toward C5.

| Note anchor | Wavelength | Colour region |
|---|---:|---|
| C3 | 380.0 nm | violet edge |
| C#3 | 396.7 nm | violet |
| D3 | 413.3 nm | violet / blue |
| D#3 | 430.0 nm | blue |
| E3 | 446.7 nm | blue |
| F3 | 463.3 nm | blue / cyan |
| F#3 | 480.0 nm | cyan |
| G3 | 496.7 nm | cyan / green |
| G#3 | 513.3 nm | green |
| A3 | 530.0 nm | green |
| A#3 | 546.7 nm | green / yellow |
| B3 | 563.3 nm | yellow-green |
| C4 | 580.0 nm | yellow |
| C#4 | 596.7 nm | yellow / orange |
| D4 | 613.3 nm | orange |
| D#4 | 630.0 nm | orange / red |
| E4 | 646.7 nm | red |
| F4 | 663.3 nm | red |
| F#4 | 680.0 nm | red |
| G4 | 696.7 nm | deep red |
| G#4 | 713.3 nm | deep red |
| A4 | 730.0 nm | deep red |
| A#4 | 746.7 nm | deep red |
| B4 | 763.3 nm | deep red |
| C5 | 780.0 nm | red edge |

Pitch is the spectral position. **Loudness is flux:** each bin's mean calibrated flux is logarithmically normalized between the minimum and maximum measured visible flux in this selection, then mapped from -36 dB to 0 dB. A brighter part of the spectrum sounds louder; a flux dip, including an absorption feature if it is wide enough to survive 1 nm averaging, sounds quieter. Loudness cannot identify an element by itself, and narrow absorption lines are smoothed by the binning.

The `C4`-to-`C5` labels are pitch anchors; the 400 individual bin pitches lie at 1 nm intervals between them and are listed with flux and gain in the provenance JSON. Timing is scan position, not stellar time. Stereo is centered dual mono, and the instrument is a pure sine tone.

## Outputs and reproduction

Download the public spectrum and render it from the repository root:

```powershell
Invoke-WebRequest -Uri "https://ssb.stsci.edu/cdbs/calspec/sirius_stis_005.fits" -OutFile "data/raw/sirius_stis_005.fits"
python scripts/render_sirius_spectrum.py data/raw/sirius_stis_005.fits
```

Outputs:

- `data/processed/sirius-visible-spectrum.wav` (Git-ignored; 30 seconds, 44.1 kHz, stereo, 16-bit PCM)
- `data/metadata/sirius-visible-spectrum.soundscape.json` (committed render provenance and all 400 per-bin mappings)

Reference WAV SHA-256: `7bad63fa6433fc37c55edf6790d11343eacd2f0bcf71b789c2ec0389388d241b`.

## References

- [STScI CALSPEC catalogue](https://www.stsci.edu/hst/instrumentation/reference-data-for-calibration-and-tools/astronomical-catalogs/calspec) — explains CALSPEC's role and the composite construction of Sirius reference spectra.
- [CALSPEC public file index](https://ssb.stsci.edu/cdbs/calspec/) and [the Sirius FITS file](https://ssb.stsci.edu/cdbs/calspec/sirius_stis_005.fits) — exact input spectrum. Its own header was read for column names, units, pedigree, and provenance.
- [Bohlin, R. C. (2014), “HST CALSPEC Flux Standards: Sirius (and Vega)”](https://arxiv.org/abs/1403.6861) — published absolute-flux calibration work; the downloaded file itself names the Bohlin et al. 2020 flux scale.
- [STScI ETC spectral distribution reference](https://etc.stsci.edu/etcstatic/users_guide/1_ref_2_spectral_distribution.html) — identifies Sirius as an A1V CALSPEC standard.

## Limitations

- This is an intentionally designed audible encoding, not a natural recording of Sirius and not a claim that colour has a musical pitch.
- The sound communicates spectral wavelength position and relative flux. It does not preserve the light's physical frequency, and it does not label individual chemical transitions.
- The selected spectrum is a calibrated reference composite; its FITS pedigree and flux-scale reference are kept in the render provenance. This is not a new observation taken for this project.
- The wavelength-to-pitch endpoints and 36 dB loudness span are design choices. They make the scan audible and repeatable; they are not physical constants.
