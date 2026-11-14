"""JSON canónico, hashes y normalización de texto (SPEC §6.1, §7.1)."""
import hashlib

import pytest

from openwitness.canonical import (ZERO_HASH, CanonicalError, canonical_json, hash_obj, normalize_text,
                                   sha256_tagged)


def test_key_order_does_not_change_bytes_or_hash():
    a = {"b": 1, "a": {"y": [1, 2], "x": "z"}}
    b = {"a": {"x": "z", "y": [1, 2]}, "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert hash_obj(a) == hash_obj(b)


def test_exact_bytes_sorted_and_without_spaces():
    assert canonical_json({"b": [1, True, None], "a": "x"}) == b'{"a":"x","b":[1,true,null]}'


def test_list_order_is_significant():
    assert hash_obj([1, 2]) != hash_obj([2, 1])


def test_utf8_without_ascii_escapes():
    out = canonical_json({"texto": "atribución ñ 日本"})
    assert out == '{"texto":"atribución ñ 日本"}'.encode("utf-8")
    assert b"\\u" not in out


@pytest.mark.parametrize("obj", [
    1.5,
    {"a": 0.1},
    {"a": [1, {"b": 2.0}]},
    {"schema": {"properties": {"n": {"minimum": 0.5}}}},
    float("nan"),
    (1, 2.5),
])
def test_floats_are_rejected(obj):
    with pytest.raises(CanonicalError):
        canonical_json(obj)


def test_float_error_names_the_path():
    with pytest.raises(CanonicalError, match=r"\$\.a\[1\]\.b"):
        canonical_json({"a": [1, {"b": 2.0}]})


def test_bool_and_big_int_are_allowed():
    assert canonical_json({"ok": True, "n": 2**70}) == b'{"n":1180591620717411303424,"ok":true}'


@pytest.mark.parametrize("obj", [{1: "a"}, {"a": b"bytes"}, {"a": {1, 2}}, {"a": object()}])
def test_non_json_types_are_rejected(obj):
    with pytest.raises(CanonicalError):
        canonical_json(obj)


def test_canonical_error_is_a_value_error():
    assert issubclass(CanonicalError, ValueError)


def test_sha256_tagged_format():
    assert sha256_tagged(b"") == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_tagged(b"abc") == "sha256:" + hashlib.sha256(b"abc").hexdigest()


def test_hash_obj_is_hash_of_canonical_bytes():
    obj = {"name": "list_issues", "description": "Lista issues", "input_schema": {"type": "object"}}
    assert hash_obj(obj) == sha256_tagged(canonical_json(obj))
    assert hash_obj(obj) != hash_obj({**obj, "description": "Lista issues."})


def test_zero_hash():
    assert ZERO_HASH == "sha256:" + "0" * 64


def test_normalize_text_nfc_newlines_and_strip():
    assert normalize_text("  hola\r\nmundo\r  ") == "hola\nmundo"
    # "é" compuesta (U+00E9) y descompuesta (e + U+0301) quedan iguales
    assert normalize_text("café") == normalize_text("café") == "café"


def test_normalize_text_is_idempotent_and_keeps_inner_spaces():
    s = normalize_text(" a  b\r\n\r\nc ")
    assert s == "a  b\n\nc"
    assert normalize_text(s) == s
