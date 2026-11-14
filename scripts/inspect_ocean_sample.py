"""Inspect an OSCAR NetCDF sample and write text and JSON metadata reports."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr


REPO_ROOT = Path(__file__).resolve().parents[1]


def _portable_path(path: Path) -> str:
    """Repo-relative POSIX path when possible, so committed reports carry no machine paths."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.ndarray):
        return [_plain(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return _plain(value.item())
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _missing_mask(values: np.ndarray, fill_values: list[Any]) -> np.ndarray:
    if np.issubdtype(values.dtype, np.number):
        try:
            missing = ~np.isfinite(values)
        except TypeError:
            missing = np.zeros(values.shape, dtype=bool)
    else:
        missing = np.equal(values, None)  # noqa: E711 - handle object arrays with None values
    for fill_value in fill_values:
        try:
            scalar = np.asarray(fill_value).item()
            if isinstance(scalar, float) and math.isnan(scalar):
                if np.issubdtype(values.dtype, np.number):
                    missing |= np.isnan(values)
            else:
                missing |= values == scalar
        except (TypeError, ValueError):
            continue
    return np.asarray(missing, dtype=bool)


def _time_text(value: Any) -> str:
    if isinstance(value, np.datetime64):
        return np.datetime_as_string(value, unit="s") + "Z"
    isoformat = getattr(value, "isoformat", None)
    if isoformat:
        return isoformat()
    return str(value)


def _time_summary(dataset: xr.Dataset) -> dict[str, Any] | None:
    candidates = [
        name
        for name, variable in dataset.variables.items()
        if name.lower() in {"time", "t"}
        or str(variable.attrs.get("standard_name", "")).lower() == "time"
    ]
    if not candidates:
        return None
    name = candidates[0]
    variable = dataset[name]
    try:
        decoded = xr.decode_cf(dataset, decode_times=True, mask_and_scale=False)[name].values
    except Exception:
        decoded = np.asarray(variable.values)

    flat = np.asarray(decoded).reshape(-1)
    result: dict[str, Any] = {
        "coordinate": name,
        "units": variable.attrs.get("units"),
        "calendar": variable.attrs.get("calendar"),
        "count": int(flat.size),
        "time_coverage_start": _time_text(flat[0]) if flat.size else None,
        "time_coverage_end": _time_text(flat[-1]) if flat.size else None,
        "cadence_seconds": None,
        "cadence_regular": None,
        "unique_cadence_seconds": [],
    }
    if flat.size > 1:
        try:
            seconds: np.ndarray | None = None
            if np.issubdtype(flat.dtype, np.datetime64):
                differences = np.diff(flat.astype("datetime64[ns]")).astype("timedelta64[ns]")
                seconds = np.asarray(differences / np.timedelta64(1, "s"), dtype=np.float64)
            elif np.issubdtype(flat.dtype, np.number):
                differences = np.diff(flat.astype(np.float64))
                unique = np.unique(differences)
                result["cadence_regular"] = bool(unique.size == 1)
                result["unique_cadence_values"] = [float(value) for value in unique[:20]]
                result["cadence_value_units"] = variable.attrs.get("units")
            else:
                seconds = np.asarray(
                    [(flat[index + 1] - flat[index]).total_seconds() for index in range(flat.size - 1)],
                    dtype=np.float64,
                )
            if seconds is not None:
                unique = np.unique(seconds)
                result["cadence_seconds"] = float(np.median(seconds))
                result["cadence_regular"] = bool(unique.size == 1)
                result["unique_cadence_seconds"] = [float(value) for value in unique[:20]]
        except (TypeError, ValueError, OverflowError):
            result["cadence_seconds"] = None
    return result


_AXIS_UNITS = {
    "latitude": {"degrees_north", "degree_north", "degree_n", "degrees_n"},
    "longitude": {"degrees_east", "degree_east", "degree_e", "degrees_e"},
}
_AXIS_LETTERS = {"latitude": "Y", "longitude": "X", "time": "T"}


def _coordinate_axis(variable: xr.DataArray) -> str | None:
    """CF axis ('latitude', 'longitude' or 'time') of a 1-D variable, from its attributes."""
    attrs = variable.attrs
    standard_name = str(attrs.get("standard_name", "")).strip().lower()
    units = str(attrs.get("units", "")).strip().lower().replace(" ", "_")
    axis = str(attrs.get("axis", "")).strip().upper()
    for name in ("latitude", "longitude", "time"):
        if standard_name == name or units in _AXIS_UNITS.get(name, set()) or axis == _AXIS_LETTERS[name]:
            return name
    return None


def _coordinate_variables(dataset: xr.Dataset) -> dict[str, xr.DataArray]:
    """xarray coordinates plus 1-D CF coordinate variables.

    With ``decode_cf=False`` variables attached only through a ``coordinates``
    attribute (e.g. OSCAR's ``lat(latitude)``/``lon(longitude)``) are data variables.
    """
    found = {str(name): coordinate for name, coordinate in dataset.coords.items()}
    for name, variable in dataset.data_vars.items():
        if variable.ndim == 1 and _coordinate_axis(variable):
            found[str(name)] = variable
    return found


def _decoded_numeric(variable: xr.DataArray) -> np.ndarray:
    """Values with fill/missing values removed and CF scale_factor/add_offset applied."""
    values = np.asarray(variable.values)
    if not (values.size and np.issubdtype(values.dtype, np.number)):
        return values
    values = values.astype(float)
    attrs = variable.attrs
    for key in ("_FillValue", "missing_value"):
        if key in attrs:
            for fill in np.atleast_1d(np.asarray(attrs[key], dtype=float)):
                values = np.where(values == fill, np.nan, values)
    try:
        values = values * float(attrs.get("scale_factor", 1.0)) + float(attrs.get("add_offset", 0.0))
    except (TypeError, ValueError):
        pass
    return values


def _coordinate_summary(dataset: xr.Dataset) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, coordinate in _coordinate_variables(dataset).items():
        values = _decoded_numeric(coordinate)
        item: dict[str, Any] = {
            "units": _plain(coordinate.attrs.get("units")),
            "standard_name": _plain(coordinate.attrs.get("standard_name")),
            "dtype": str(coordinate.dtype),
            "shape": list(values.shape),
            "minimum": None,
            "maximum": None,
            "resolution_degrees": None,
        }
        if values.size and np.issubdtype(values.dtype, np.number):
            finite = values[np.isfinite(values)]
            if finite.size:
                item["minimum"] = float(np.min(finite))
                item["maximum"] = float(np.max(finite))
            if values.ndim == 1 and finite.size > 1:
                differences = np.abs(np.diff(np.sort(np.unique(finite))))
                differences = differences[differences > 0]
                if differences.size:
                    item["resolution_degrees"] = float(np.median(differences))
        summary[name] = item
    return summary


def _find_coordinate(dataset: xr.Dataset, axis: str) -> str | None:
    """Coordinate for ``axis``: CF attributes first, then conventional names."""
    candidates = _coordinate_variables(dataset)
    for name, coordinate in candidates.items():
        if _coordinate_axis(coordinate) == axis:
            return name
    aliases = {"latitude": {"lat", "latitude"}, "longitude": {"lon", "longitude"}}
    for name in candidates:
        if name.lower() in aliases.get(axis, set()):
            return name
    return None


def _variable_summary(name: str, variable: xr.DataArray) -> dict[str, Any]:
    attrs = dict(variable.attrs)
    encoding = dict(variable.encoding)
    fill_values = [
        value
        for value in (attrs.get("_FillValue"), attrs.get("missing_value"), encoding.get("_FillValue"))
        if value is not None
    ]
    values = np.asarray(variable.values)
    missing = _missing_mask(values, fill_values)
    result: dict[str, Any] = {
        "name": name,
        "dtype": str(encoding.get("dtype", values.dtype)),
        "shape": list(values.shape),
        "dimensions": list(variable.dims),
        "units": _plain(attrs.get("units")),
        "long_name": _plain(attrs.get("long_name")),
        "standard_name": _plain(attrs.get("standard_name")),
        "fill_value": _plain(attrs.get("_FillValue", encoding.get("_FillValue"))),
        "missing_value": _plain(attrs.get("missing_value")),
        "scale_factor": _plain(attrs.get("scale_factor", encoding.get("scale_factor"))),
        "add_offset": _plain(attrs.get("add_offset", encoding.get("add_offset"))),
        "attributes": _plain(attrs),
        "missing_count": int(np.count_nonzero(missing)),
        "missing_percent": float(np.count_nonzero(missing) * 100.0 / values.size) if values.size else 0.0,
        "minimum": None,
        "maximum": None,
        "mean": None,
        "range_check": "not evaluated: source metadata has no valid range",
    }
    if np.issubdtype(values.dtype, np.number):
        valid_values = values[~missing]
        if valid_values.size:
            scale = attrs.get("scale_factor", 1.0)
            offset = attrs.get("add_offset", 0.0)
            try:
                physical_values = valid_values.astype(np.float64) * float(scale) + float(offset)
                result["minimum"] = float(np.min(physical_values))
                result["maximum"] = float(np.max(physical_values))
                result["mean"] = float(np.mean(physical_values))
                lower = attrs.get("valid_min")
                upper = attrs.get("valid_max")
                valid_range = attrs.get("valid_range")
                if valid_range is not None and len(valid_range) >= 2:
                    lower = valid_range[0] if lower is None else lower
                    upper = valid_range[1] if upper is None else upper
                if lower is not None or upper is not None:
                    lower_value = float(lower) * float(scale) + float(offset) if lower is not None else None
                    upper_value = float(upper) * float(scale) + float(offset) if upper is not None else None
                    outside = np.zeros(physical_values.shape, dtype=bool)
                    if lower_value is not None:
                        outside |= physical_values < lower_value
                    if upper_value is not None:
                        outside |= physical_values > upper_value
                    result["range_check"] = {
                        "source": "valid_min/valid_max or valid_range attributes",
                        "lower": lower_value,
                        "upper": upper_value,
                        "outside_count": int(np.count_nonzero(outside)),
                        "status": "flagged" if np.any(outside) else "within documented range",
                    }
            except (TypeError, ValueError, OverflowError):
                result["range_check"] = "not evaluated: variable scaling or range attributes are not numeric"
    return result


def inspect_netcdf(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"NetCDF file not found: {path}")
    with xr.open_dataset(path, decode_cf=False, mask_and_scale=False) as dataset:
        coordinates = _coordinate_summary(dataset)
        latitude_name = _find_coordinate(dataset, "latitude")
        longitude_name = _find_coordinate(dataset, "longitude")
        longitude = coordinates.get(longitude_name, {}) if longitude_name else {}
        lon_min = longitude.get("minimum")
        lon_max = longitude.get("maximum")
        if lon_min is not None and lon_max is not None:
            if lon_min >= 0 and lon_max > 180:
                longitude_convention = "0 to 360"
            elif lon_min < 0 and lon_max <= 180:
                longitude_convention = "-180 to 180"
            else:
                longitude_convention = "ambiguous from observed coordinate range"
        else:
            longitude_convention = "UNVERIFIED: longitude coordinate not identified"

        variables = {
            name: _variable_summary(name, variable)
            for name, variable in dataset.variables.items()
        }
        candidates: list[str] = []
        temperature_pattern = ("sea_surface_temperature", "sst", "temperature")
        for name, variable in dataset.data_vars.items():
            searchable = " ".join(
                [name, str(variable.attrs.get("long_name", "")), str(variable.attrs.get("standard_name", ""))]
            ).lower()
            if any(pattern in searchable for pattern in temperature_pattern):
                candidates.append(name)

        explicit_masks = [
            name
            for name in dataset.data_vars
            if any(token in name.lower() for token in ("mask", "land", "ice"))
        ]
        summary = {
            "file": _portable_path(path),
            "dimensions": {name: int(size) for name, size in dataset.sizes.items()},
            "coordinates": coordinates,
            "latitude_coordinate": latitude_name,
            "longitude_coordinate": longitude_name,
            "longitude_convention": longitude_convention,
            "grid_resolution_degrees": {
                "latitude": coordinates.get(latitude_name, {}).get("resolution_degrees") if latitude_name else None,
                "longitude": longitude.get("resolution_degrees"),
            },
            "time": _time_summary(dataset),
            "variables": variables,
            "global_attributes": _plain(dict(dataset.attrs)),
            "missing_data": {
                "representation": "per-variable NaN/non-finite and declared fill/missing sentinels",
                "explicit_mask_variables": explicit_masks,
                "interpretation": (
                    "Missing values alone do not distinguish land, sea ice, or quality-control gaps."
                    if not explicit_masks
                    else "See explicit mask-like variables; their semantics still require metadata review."
                ),
            },
            "sst": {
                "present": bool(candidates),
                "candidate_variables": candidates,
                "basis": "variable names, long_name, and standard_name in this file",
            },
        }
    return summary


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"NetCDF inspection: {report['file']}",
        "",
        "Dimensions:",
    ]
    lines.extend(f"- {name}: {size}" for name, size in report["dimensions"].items())
    lines.append("")
    lines.append(f"Longitude convention: {report['longitude_convention']}")
    lines.append(f"Grid resolution (degrees): {json.dumps(report['grid_resolution_degrees'], sort_keys=True)}")
    time = report.get("time")
    if time:
        lines.append(
            "Time: "
            f"{time.get('time_coverage_start')} to {time.get('time_coverage_end')}; "
            f"cadence={time.get('cadence_seconds')} seconds; regular={time.get('cadence_regular')}"
        )
    else:
        lines.append("Time: UNVERIFIED (no time coordinate identified)")
    lines.append("")
    lines.append("Variables:")
    for name, variable in report["variables"].items():
        lines.append(
            f"- {name}: dtype={variable['dtype']}, dims={variable['dimensions']}, "
            f"units={variable['units']!r}, fill={variable['fill_value']!r}, "
            f"missing={variable['missing_percent']:.4f}%, "
            f"min/max/mean={variable['minimum']}/{variable['maximum']}/{variable['mean']}"
        )
    lines.append("")
    lines.append(f"SST present: {'yes' if report['sst']['present'] else 'no'}")
    lines.append(f"SST candidate variables: {report['sst']['candidate_variables']}")
    lines.append(f"Missing/land/ice note: {report['missing_data']['interpretation']}")
    lines.append("")
    lines.append("Global attributes:")
    lines.append(json.dumps(report["global_attributes"], indent=2, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netcdf", type=Path, help="Path to a NetCDF sample")
    parser.add_argument("--report-dir", type=Path, default=Path("data/metadata"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = inspect_netcdf(args.netcdf)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Could not inspect NetCDF sample: {exc}", file=sys.stderr)
        return 1
    args.report_dir.mkdir(parents=True, exist_ok=True)
    base = args.netcdf.stem
    json_path = args.report_dir / f"{base}.inspection.json"
    text_path = args.report_dir / f"{base}.inspection.txt"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    text_path.write_text(render_text(report), encoding="utf-8")
    print(text_path)
    print(json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
