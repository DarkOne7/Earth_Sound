"""Small Xarray loader for verified ocean-data NetCDF samples."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import xarray as xr


class DatasetLoadError(RuntimeError):
    """Raised when a dataset is missing, unreadable, or lacks requested variables."""


@dataclass(frozen=True)
class VariableMetadata:
    name: str
    units: str | None
    dimensions: tuple[str, ...]
    dtype: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class DatasetMetadata:
    path: Path
    dimensions: dict[str, int]
    variables: dict[str, VariableMetadata]
    global_attributes: dict[str, Any]


def open_ocean_dataset(
    path: str | Path,
    *,
    required_variables: Iterable[str] = (),
) -> tuple[xr.Dataset, DatasetMetadata]:
    """Open a NetCDF file with Xarray's fill-value decoding enabled.

    The caller owns the returned Xarray dataset and must close it when finished.
    Variable names and units are read from the file, not assumed by this loader.
    """
    source = Path(path)
    if not source.is_file():
        raise DatasetLoadError(f"Dataset file does not exist: {source}")
    try:
        dataset = xr.open_dataset(source, decode_cf=True, mask_and_scale=True)
    except (OSError, ValueError) as exc:
        raise DatasetLoadError(f"Could not open NetCDF dataset {source}: {exc}") from exc

    required = tuple(required_variables)
    missing = [name for name in required if name not in dataset.variables]
    if missing:
        dataset.close()
        raise DatasetLoadError(
            f"Dataset {source} is missing required variable(s): {', '.join(missing)}. "
            f"Available variables: {', '.join(dataset.variables)}"
        )

    variables = {
        name: VariableMetadata(
            name=name,
            units=variable.attrs.get("units"),
            dimensions=tuple(variable.dims),
            dtype=str(variable.encoding.get("dtype", variable.dtype)),
            attributes=dict(variable.attrs),
        )
        for name, variable in dataset.variables.items()
    }
    metadata = DatasetMetadata(
        path=source.resolve(),
        dimensions={name: int(size) for name, size in dataset.sizes.items()},
        variables=variables,
        global_attributes=dict(dataset.attrs),
    )
    return dataset, metadata
