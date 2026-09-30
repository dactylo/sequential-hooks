"""Encode and decode strict native hook JSON objects."""

import json
import math
from typing import cast

from sequential_hooks.hosts._contract import JsonObject, JsonValue

_MAX_JSON_INTEGER_DIGITS = 4_300


class JsonProtocolError(ValueError):
    """Report a strict hook JSON encoding or decoding failure."""


class JsonObjectExpectedError(JsonProtocolError):
    """Raised when valid protocol JSON is not an object."""


class _JsonIntegerTooLongError(ValueError):
    """Identify a JSON integer outside the bounded digit count."""


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object while rejecting duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise JsonProtocolError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _parse_integer(value: str) -> int:
    """Parse one JSON integer within the shared digit bound."""
    if len(value.removeprefix('-')) > _MAX_JSON_INTEGER_DIGITS:
        raise _JsonIntegerTooLongError
    try:
        return int(value)
    except ValueError as error:
        raise _JsonIntegerTooLongError from error


def _reject_constant(value: str) -> object:
    """Reject non-standard JSON numeric constants."""
    raise JsonProtocolError(f'non-standard JSON number: {value}')


def _to_json_value(value: object) -> JsonValue:
    """Validate and narrow a decoded JSON value."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsonProtocolError('non-finite JSON number')
        return value
    if isinstance(value, list):
        return [_to_json_value(item) for item in cast('list[object]', value)]
    if isinstance(value, dict):
        items = cast('dict[object, object]', value)
        return {str(key): _to_json_value(item) for key, item in items.items()}
    raise JsonProtocolError(f'unsupported JSON value: {type(value).__name__}')


def dump_object(value: JsonObject) -> bytes:
    """Encode one native JSON object with a trailing newline.

    Args:
        value: JSON object to encode without sorting its keys.

    Returns:
        Compact UTF-8 bytes ending in one newline.

    Raises:
        JsonProtocolError: If JSON serialization rejects a value, including a
            non-finite number or an integer beyond the interpreter's conversion
            limit.
        UnicodeEncodeError: If a string value cannot be encoded as UTF-8.
    """
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
        )
    except (TypeError, ValueError) as error:
        raise JsonProtocolError(str(error)) from error
    return f'{text}\n'.encode()


def load_object_text(text: str) -> JsonObject:
    """Decode exactly one strict JSON object from text.

    Args:
        text: Native hook protocol text. Integer literals may contain at most
            4,300 digits and must also fit the interpreter's conversion limit.

    Returns:
        Decoded object with insertion order preserved.

    Raises:
        JsonObjectExpectedError: If valid protocol JSON has a non-object
            top-level value.
        JsonProtocolError: If the text is invalid or ambiguous JSON, contains
            non-finite numbers, exceeds the integer limit, or nests beyond the
            supported depth.
    """
    try:
        decoded = cast(
            'object',
            json.loads(
                text,
                object_pairs_hook=_object_from_pairs,
                parse_constant=_reject_constant,
                parse_int=_parse_integer,
            ),
        )
        value = _to_json_value(decoded)
    except (json.JSONDecodeError, _JsonIntegerTooLongError) as error:
        raise JsonProtocolError(str(error)) from error
    except RecursionError as error:
        raise JsonProtocolError('JSON nesting exceeds the supported depth') from error
    if not isinstance(value, dict):
        raise JsonObjectExpectedError('hook input must be a JSON object')
    return value


def load_object(data: bytes) -> JsonObject:
    """Decode exactly one strict UTF-8 JSON object.

    Args:
        data: Native hook protocol bytes. Integer literals may contain at most
            4,300 digits and must also fit the interpreter's conversion limit.

    Returns:
        Decoded object with insertion order preserved.

    Raises:
        JsonObjectExpectedError: If valid protocol JSON has a non-object
            top-level value.
        JsonProtocolError: If the bytes are invalid UTF-8 or invalid or
            ambiguous JSON, contain non-finite numbers, exceed the integer
            limit, or nest beyond the supported depth.
    """
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as error:
        raise JsonProtocolError(str(error)) from error
    return load_object_text(text)
