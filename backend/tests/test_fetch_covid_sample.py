"""Tests for the COVID-19 sample fetcher: filtering, limits and provenance."""

from __future__ import annotations

import csv
import hashlib
import io
import json

import pytest

import scripts.fetch_covid_sample as fetcher
from scripts.fetch_covid_sample import (
    FetchError,
    download,
    filter_locations,
    parse_args,
    write_subset,
)

HEADER = "date,location,new_cases,new_deaths,total_cases,total_deaths\n"
BODY = (
    "2020-01-05,World,1,1,1,1\n"
    "2020-01-05,Italy,0,0,0,0\n"
    "2020-01-12,World,10,2,11,3\n"
    "2020-01-12,Italy,5,1,5,1\n"
)
RAW = (HEADER + BODY).encode("utf-8")


class FakeResponse:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._stream = io.BytesIO(payload)
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def fake_urlopen(monkeypatch):
    def install(payload: bytes, status: int = 200):
        monkeypatch.setattr(
            fetcher.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse(payload, status)
        )

    return install


# --- argument parsing -------------------------------------------------------


def test_defaults_target_the_world_series():
    args = parse_args([])
    assert args.locations is None  # resolved to DEFAULT_LOCATIONS in main
    assert fetcher.DEFAULT_LOCATIONS == ("World",)
    assert args.sample_name == "owid-covid-weekly"
    assert args.url.startswith("https://raw.githubusercontent.com/owid/covid-19-data/")


def test_location_flag_is_repeatable():
    assert parse_args(["--location", "Italy", "--location", "World"]).locations == ["Italy", "World"]


# --- download ---------------------------------------------------------------


def test_download_returns_bytes_and_the_stream_hash(fake_urlopen):
    fake_urlopen(RAW)
    raw, digest = download("https://example.test/data.csv", 1 << 20)
    assert raw == RAW
    assert digest == hashlib.sha256(RAW).hexdigest()


def test_download_enforces_the_size_limit(fake_urlopen):
    fake_urlopen(b"x" * 4096)
    with pytest.raises(FetchError, match="exceeds the"):
        download("https://example.test/data.csv", 1024)


def test_download_rejects_an_empty_response(fake_urlopen):
    fake_urlopen(b"")
    with pytest.raises(FetchError, match="empty response"):
        download("https://example.test/data.csv", 1 << 20)


def test_download_rejects_a_non_ok_status(fake_urlopen):
    fake_urlopen(RAW, status=404)
    with pytest.raises(FetchError, match="HTTP 404"):
        download("https://example.test/data.csv", 1 << 20)


# --- filtering --------------------------------------------------------------


def test_filter_keeps_only_the_requested_locations():
    header, rows, counts = filter_locations(RAW, ("World",))
    assert header[:4] == ["date", "location", "new_cases", "new_deaths"]
    assert counts == {"World": 2}
    assert [row[1] for row in rows] == ["World", "World"]


def test_filter_keeps_several_locations():
    _, rows, counts = filter_locations(RAW, ("World", "Italy"))
    assert counts == {"World": 2, "Italy": 2} and len(rows) == 4


def test_filter_rejects_a_location_with_no_rows():
    with pytest.raises(FetchError, match="Atlantis"):
        filter_locations(RAW, ("Atlantis",))


def test_filter_rejects_a_file_missing_required_columns():
    with pytest.raises(FetchError, match="missing required column"):
        filter_locations(b"date,location\n2020-01-05,World\n", ("World",))


def test_filter_rejects_a_file_with_no_header():
    with pytest.raises(FetchError, match="no header"):
        filter_locations(b"", ("World",))


def test_filter_tolerates_a_utf8_bom():
    _, _, counts = filter_locations(b"\xef\xbb\xbf" + RAW, ("World",))
    assert counts == {"World": 2}


# --- writing ----------------------------------------------------------------


def test_write_subset_round_trips_through_csv(tmp_path):
    header, rows, _ = filter_locations(RAW, ("World",))
    path = tmp_path / "nested" / "subset.csv"
    write_subset(path, header, rows)
    with path.open(encoding="utf-8", newline="") as handle:
        read_back = list(csv.reader(handle))
    assert read_back[0] == header
    assert len(read_back) == 3
    assert read_back[1][1] == "World"


# --- end to end -------------------------------------------------------------


def test_main_writes_the_subset_and_a_retrieval_record(fake_urlopen, tmp_path, monkeypatch):
    fake_urlopen(RAW)
    monkeypatch.setattr(fetcher, "REPO_ROOT", tmp_path)
    assert fetcher.main([]) == 0

    subset = tmp_path / "data" / "raw" / "owid-covid-weekly" / "owid-covid-weekly.csv"
    record_path = tmp_path / "data" / "metadata" / "owid-covid-weekly.retrieval.json"
    assert subset.is_file() and record_path.is_file()

    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["upstream_sha256"] == hashlib.sha256(RAW).hexdigest()
    assert record["upstream_bytes"] == len(RAW)
    assert record["subset_sha256"] == hashlib.sha256(subset.read_bytes()).hexdigest()
    assert record["subset_rows"] == 2 and record["rows_per_location"] == {"World": 2}
    assert record["date_min"] == "2020-01-05" and record["date_max"] == "2020-01-12"
    assert "CC BY 4.0" in record["license"] and "World Health Organization" in record["citation"]


def test_main_leaves_an_existing_subset_alone_unless_overwrite(fake_urlopen, tmp_path, monkeypatch, capsys):
    fake_urlopen(RAW)
    monkeypatch.setattr(fetcher, "REPO_ROOT", tmp_path)
    subset = tmp_path / "data" / "raw" / "owid-covid-weekly" / "owid-covid-weekly.csv"
    subset.parent.mkdir(parents=True)
    subset.write_text("sentinel", encoding="utf-8")

    assert fetcher.main([]) == 0
    assert subset.read_text(encoding="utf-8") == "sentinel"
    assert "already exists" in capsys.readouterr().out

    fake_urlopen(RAW)
    assert fetcher.main(["--overwrite"]) == 0
    assert subset.read_text(encoding="utf-8") != "sentinel"


def test_main_reports_a_fetch_failure(fake_urlopen, tmp_path, monkeypatch, capsys):
    fake_urlopen(b"")
    monkeypatch.setattr(fetcher, "REPO_ROOT", tmp_path)
    assert fetcher.main([]) == 1
    assert "error:" in capsys.readouterr().err
