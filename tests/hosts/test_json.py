import pytest

from sequential_hooks.hosts import _json
from sequential_hooks.hosts._json import JsonProtocolError, dump_object, load_object


def test_dump_object_is_compact_utf8_with_one_newline() -> None:
    assert dump_object({'message': 'caf\u00e9', 'order': [2, 1]}) == (
        b'{"message":"caf\xc3\xa9","order":[2,1]}\n'
    )


@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        (b'{}', {}),
        (b'{"outer":{"unknown":[true,null,3.5]}}', {'outer': {'unknown': [True, None, 3.5]}}),
        ('{"message":"caf\u00e9"}'.encode(), {'message': 'caf\u00e9'}),
    ],
)
def test_load_object_accepts_strict_json_objects(
    data: bytes,
    expected: dict[str, object],
) -> None:
    assert load_object(data) == expected


def test_load_object_preserves_key_order() -> None:
    value = load_object(b'{"second":2,"first":1}')
    assert list(value) == ['second', 'first']


@pytest.mark.parametrize(
    'data',
    [
        b'[]',
        b'null',
        b'{"a":1,"a":2}',
        b'{"outer":{"a":1,"a":2}}',
        b'{"items":[{"a":1,"a":2}]}',
        b'{} trailing',
        b'{} {}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e999}',
        b'\xff',
    ],
)
def test_load_object_rejects_ambiguous_protocol_json(data: bytes) -> None:
    with pytest.raises(JsonProtocolError):
        load_object(data)


def test_load_object_rejects_oversized_integer() -> None:
    data = b'{"value":' + (b'1' * 4_301) + b'}'

    with pytest.raises(JsonProtocolError):
        load_object(data)


def test_load_object_text_rejects_oversized_integer() -> None:
    text = '{"value":-' + ('1' * 4_301) + '}'

    with pytest.raises(JsonProtocolError):
        _json.load_object_text(text)


def test_load_object_text_translates_recursive_nesting_failure() -> None:
    text = '{"payload-sentinel":' * 1_500 + '{}' + '}' * 1_500

    with pytest.raises(JsonProtocolError) as caught:
        _json.load_object_text(text)

    assert str(caught.value) == 'JSON nesting exceeds the supported depth'
    assert 'payload-sentinel' not in str(caught.value)


@pytest.mark.parametrize('text', ['[]', 'null', '"value"'])
def test_load_object_text_types_non_object_json(text: str) -> None:
    with pytest.raises(_json.JsonObjectExpectedError):
        _json.load_object_text(text)
