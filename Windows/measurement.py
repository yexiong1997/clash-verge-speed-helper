"""Strict worker-v1 JSON decoding and the shared composite-score formula."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import unicodedata

INT_MAX = (1 << 63) - 1
FIELDS = frozenset((
    "version", "node_name", "node_type", "head_attempts", "head_successes",
    "latency_ms", "jitter_ms", "expected_bytes", "received_bytes",
    "download_complete", "download_bytes_per_second",
))


class _Number:
    def __init__(self, text, integer):
        self.text = text
        self.integer = integer


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("invalid constant")


def _int(value):
    if not isinstance(value, _Number) or not value.integer:
        raise ValueError("not integer")
    number = int(value.text)
    if not -(1 << 63) <= number <= INT_MAX:
        raise ValueError("integer overflow")
    return number


def _nonnegative(value):
    if not isinstance(value, _Number):
        raise ValueError("not number")
    number = float(value.text)
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid number")
    if number == 0 and Decimal(value.text) != 0:
        raise ValueError("number underflow")
    return number


def _string(value):
    if not isinstance(value, str):
        raise ValueError("not string")
    value.encode("utf-8", "strict")
    return value


def composite_score(speed, latency, jitter, failure_percent):
    if not all(math.isfinite(v) for v in (speed, latency, jitter, failure_percent)):
        return None
    if speed <= 0 or latency <= 0 or jitter < 0 or not 0 <= failure_percent <= 50:
        return None
    clamp = lambda value: min(1.0, max(0.0, value))
    return (40 * min(1.0, math.log1p(speed / 1_048_576) / math.log1p(25))
            + 30 * clamp(1 - latency / 500)
            + 20 * clamp(1 - failure_percent / 100)
            + 10 * clamp(1 - jitter / 100))


@dataclass(frozen=True)
class VerifiedMeasurement:
    node_name: str
    node_type: str
    head_attempts: int
    head_successes: int
    latency_ms: float
    jitter_ms: float
    expected_bytes: int
    received_bytes: int
    download_complete: bool
    download_bytes_per_second: float

    @property
    def score(self):
        if not self.download_complete or self.received_bytes != self.expected_bytes:
            return None
        if self.head_attempts != 6 or not 3 <= self.head_successes <= 6:
            return None
        return composite_score(self.download_bytes_per_second, self.latency_ms,
                               self.jitter_ms, (6 - self.head_successes) * 100 / 6)


def decode_measurement(raw, expected_name, requested_bytes, streams):
    """Return a measurement, or None for *any* worker-v1 contract violation."""
    try:
        if type(requested_bytes) is not int or type(streams) is not int:
            return None
        if not (0 < requested_bytes <= INT_MAX and 0 < streams <= INT_MAX):
            return None
        expected = requested_bytes // streams * streams
        if expected <= 0 or expected + streams > INT_MAX:
            return None
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 1_048_576:
            return None
        if raw.endswith(b"\n"):
            raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
        if b"\n" in raw or b"\r" in raw:
            return None
        data = json.loads(raw.decode("utf-8", "strict"),
                          object_pairs_hook=_unique,
                          parse_int=lambda s: _Number(s, True),
                          parse_float=lambda s: _Number(s, False),
                          parse_constant=_invalid_constant)
        if type(data) is not dict or set(data) != FIELDS:
            return None
        if _int(data["version"]) != 1:
            return None
        name, kind = _string(data["node_name"]), _string(data["node_type"])
        if name.encode("utf-8") != expected_name.encode("utf-8"):
            return None
        if not kind or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in kind):
            return None
        attempts, successes = _int(data["head_attempts"]), _int(data["head_successes"])
        latency, jitter = _nonnegative(data["latency_ms"]), _nonnegative(data["jitter_ms"])
        size, received = _int(data["expected_bytes"]), _int(data["received_bytes"])
        complete, speed = data["download_complete"], _nonnegative(data["download_bytes_per_second"])
        if attempts != 6 or not 0 <= successes <= 6 or size != expected:
            return None
        if not 0 <= received <= expected + streams or type(complete) is not bool:
            return None
        if (complete and (received != expected or speed <= 0)) or (not complete and speed != 0):
            return None
        return VerifiedMeasurement(name, kind, attempts, successes, latency, jitter,
                                   size, received, complete, speed)
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError, InvalidOperation):
        return None
