from __future__ import annotations

import array
import math
import sys
from collections.abc import Sequence


class VectorError(ValueError):
    pass


def validate_vector(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    if not values:
        raise VectorError("embedding vector must not be empty")
    if any(not math.isfinite(value) for value in values):
        raise VectorError("embedding vector must contain only finite values")
    return values


def normalize_vector(vector: Sequence[float]) -> list[float]:
    values = validate_vector(vector)
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        raise VectorError("embedding vector must not have zero norm")
    return [value / norm for value in values]


def encode_vector(vector: Sequence[float], *, dimensions: int | None = None) -> bytes:
    values = normalize_vector(vector)
    if dimensions is not None and len(values) != dimensions:
        raise VectorError(f"expected {dimensions} dimensions, got {len(values)}")
    encoded = array.array("f", values)
    if sys.byteorder != "little":
        encoded.byteswap()
    return encoded.tobytes()


def decode_vector(blob: bytes, *, dimensions: int) -> list[float]:
    if dimensions <= 0:
        raise VectorError("dimensions must be positive")
    expected = dimensions * 4
    if len(blob) != expected:
        raise VectorError(f"expected {expected} vector bytes, got {len(blob)}")
    values = array.array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    decoded = [float(value) for value in values]
    validate_vector(decoded)
    return decoded


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    left_values = validate_vector(left)
    right_values = validate_vector(right)
    if len(left_values) != len(right_values):
        raise VectorError(
            f"dimension mismatch: {len(left_values)} != {len(right_values)}"
        )
    return sum(a * b for a, b in zip(left_values, right_values, strict=True))
