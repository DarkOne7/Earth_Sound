"""Serve in-memory WAV bytes with validators and byte ranges.

The ETag is the WAV's SHA-256, the same value the provenance records under
``output.sha256`` (``audio.sha256`` for Sirius), so a client can match the audio
to its provenance. Range support lets a browser ``<audio>`` element seek.
"""

from __future__ import annotations

import re

from fastapi import Request, Response

_SINGLE_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _byte_range(header: str, size: int) -> tuple[int, int] | None | bool:
    """``(first, last)`` inclusive; ``None`` to ignore the header; ``False`` if unsatisfiable."""
    match = _SINGLE_RANGE.match(header.strip())
    if match is None:
        return None  # malformed or multi-range: RFC 9110 lets us ignore it and send everything
    first_text, last_text = match.groups()
    if not first_text and not last_text:
        return None
    if not first_text:  # suffix range: the last N bytes
        suffix = int(last_text)
        if suffix == 0:
            return False
        return max(size - suffix, 0), size - 1
    first = int(first_text)
    last = int(last_text) if last_text else size - 1
    if first >= size or last < first:
        return False
    return first, min(last, size - 1)


def wav_response(request: Request, wav_bytes: bytes, sha256: str, filename: str) -> Response:
    etag = f'"{sha256}"'
    headers = {
        "ETag": etag,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and (if_none_match.strip() == "*" or etag in (t.strip() for t in if_none_match.split(","))):
        return Response(status_code=304, headers=headers)

    size = len(wav_bytes)
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if range_header and (if_range is None or if_range.strip() == etag):
        selected = _byte_range(range_header, size)
        if selected is False:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        if selected is not None:
            first, last = selected
            return Response(
                content=wav_bytes[first : last + 1],
                status_code=206,
                media_type="audio/wav",
                headers={**headers, "Content-Range": f"bytes {first}-{last}/{size}"},
            )
    return Response(content=wav_bytes, media_type="audio/wav", headers=headers)
