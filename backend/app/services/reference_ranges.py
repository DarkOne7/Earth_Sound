"""Documented reference values used to validate and normalize ocean currents.

Every value here carries its source. Nothing is derived from the sample data.
See ``docs/processing-methodology.md`` for the full rationale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Unit used for every processed velocity and speed value.
CANONICAL_VELOCITY_UNITS = "m s-1"

#: Absolute numeric tolerance (m s-1 for speeds, degrees for angles) used for
#: floating-point comparisons in processing and tests. A speed whose magnitude
#: does not exceed this tolerance is treated as zero (direction undefined).
NUMERIC_TOLERANCE = 1e-9

#: Version of the processing rules. Bump it whenever a rule or reference changes.
PROCESSING_VERSION = "1.0.2"


@dataclass(frozen=True)
class ReferenceRange:
    """A closed numeric range with the evidence it comes from."""

    quantity: str
    minimum: float
    maximum: float
    units: str
    source: str
    url: str

    def __post_init__(self) -> None:
        if not (math.isfinite(self.minimum) and math.isfinite(self.maximum)):
            raise ValueError(f"{self.quantity}: reference bounds must be finite")
        if not self.minimum < self.maximum:
            raise ValueError(
                f"{self.quantity}: reference minimum {self.minimum} must be below maximum {self.maximum}"
            )


#: Physically plausible bounds for each velocity component (u and v).
#: NASA CMR UMM-Var records for OSCAR v2.0 Final publish ValidRanges
#: Min -3.0 / Max 3.0 m s-1 for u (V2193278944-POCLOUD) and v (V2193278947-POCLOUD).
COMPONENT_VALID_RANGE = ReferenceRange(
    quantity="velocity component (u, v)",
    minimum=-3.0,
    maximum=3.0,
    units=CANONICAL_VELOCITY_UNITS,
    source=(
        "NASA CMR UMM-Var ValidRanges for OSCAR_L4_OC_FINAL_V2.0 variables u "
        "(V2193278944-POCLOUD) and v (V2193278947-POCLOUD), retrieved 2026-11-13"
    ),
    url=(
        "https://cmr.earthdata.nasa.gov/search/variables.umm_json?"
        "concept_id[]=V2193278944-POCLOUD&concept_id[]=V2193278947-POCLOUD"
    ),
)

#: Fill value published for u and v in the same UMM-Var records. The loader
#: masks the file's own ``_FillValue``; this documented sentinel is checked as
#: well in case a file omits the attribute.
DOCUMENTED_COMPONENT_FILL_VALUE = -999.0

#: Normalization range for current speed. The lower bound is the mathematical
#: minimum of a vector magnitude. The upper bound is NOAA's typical maximum
#: surface speed of the Gulf Stream, "about nine kilometers per hour" = 2.5 m s-1,
#: one of the fastest ocean surface currents.
SPEED_NORMALIZATION_RANGE = ReferenceRange(
    quantity="current speed",
    minimum=0.0,
    maximum=2.5,
    units=CANONICAL_VELOCITY_UNITS,
    source=(
        "Minimum: speed is a vector magnitude and cannot be negative. Maximum: NOAA "
        "National Ocean Service, 'How fast is the Gulf Stream?' — typical maximum "
        "surface speed about 9 km/h (2.5 m s-1); accessed 2026-11-13"
    ),
    url="https://oceanservice.noaa.gov/facts/gulfstreamspeed.html",
)

#: Version of the COVID-19 case-series processing rules.
CASE_PROCESSING_VERSION = "1.0.0"

#: Logarithmic normalization range for weekly reported COVID-19 deaths.
#: The lower bound is the smallest reportable non-zero count, so its log10 is 0.
#: The upper bound is the nearest power of ten at or above the global historical
#: peak. WHO's weekly epidemiological update for the week ending 2021-01-17
#: reported a then-record ~93 000 deaths, and WHO's deputy head publicly warned
#: the world was approaching "100 000 deaths a week"; the revised OWID/WHO series
#: peaks at 103 719 for the week ending 2021-01-24. 1e5 is therefore a round,
#: externally grounded ceiling rather than a value taken from the sample, and it
#: is held fixed for every location so loudness stays comparable between them.
COVID_WEEKLY_DEATHS_RANGE = ReferenceRange(
    quantity="weekly reported COVID-19 deaths",
    minimum=1.0,
    maximum=100_000.0,
    units="deaths per week",
    source=(
        "Upper bound: nearest power of ten at or above the global historical peak of weekly reported "
        "deaths. WHO Weekly epidemiological update, 19 January 2021, reported a record ~93 000 deaths "
        "for the preceding week, and WHO warned of approaching 100 000 deaths per week; the revised "
        "Our World In Data / WHO series peaks at 103 719 (week ending 2021-01-24), which clips to 1.0. "
        "Lower bound: 1 is the smallest reportable non-zero count, so log10 of the minimum is 0. "
        "Accessed 2026-11-14"
    ),
    url="https://www.who.int/publications/m/item/weekly-epidemiological-update---19-january-2021",
)

#: Logarithmic normalization range for weekly reported COVID-19 cases.
#: Cases span three more decades than deaths and are far more sensitive to
#: testing capacity and to reporting backlogs, so this series is offered but is
#: not the default. See ``docs/covid-dataset-decision.md``.
COVID_WEEKLY_CASES_RANGE = ReferenceRange(
    quantity="weekly reported COVID-19 cases",
    minimum=1.0,
    maximum=100_000_000.0,
    units="cases per week",
    source=(
        "Upper bound: nearest power of ten at or above the global historical peak of weekly reported "
        "cases in the Our World In Data / WHO series, 44 236 227 for the week ending 2022-12-25, which "
        "is itself inflated by China's reporting backlog after reopening. Lower bound: 1 is the "
        "smallest reportable non-zero count. Accessed 2026-11-14"
    ),
    url="https://github.com/owid/covid-19-data/tree/master/public/data",
)

#: Week-over-week log2 ratio that pans fully to one side. 1.0 means a doubling
#: of the weekly count pans hard right and a halving pans hard left.
PAN_LOG2_FULL_SCALE = 1.0

#: Unit spellings accepted for velocities, mapped to the factor converting them
#: to m s-1. Any other unit string is rejected rather than guessed.
VELOCITY_UNIT_FACTORS: dict[str, float] = {
    "m s-1": 1.0,
    "m s^-1": 1.0,
    "m/s": 1.0,
    "m.s-1": 1.0,
    "meter second-1": 1.0,
    "meters second-1": 1.0,
    "metre second-1": 1.0,
    "metres second-1": 1.0,
    "meters per second": 1.0,
    "cm s-1": 0.01,
    "cm/s": 0.01,
    "cm s^-1": 0.01,
}
