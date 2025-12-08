from restate.serde import BytesSerde


def test_bytes_serde():
    s = BytesSerde()
    assert bytes(range(20)) == s.serialize(bytes(range(20)))


def extract_core_type_optional():
    from restate.types import extract_core_type

    from typing import Optional, Union

    kind, tpe = extract_core_type(Optional[int])
    assert kind == "optional"
    assert tpe is int

    kind, tpe = extract_core_type(Union[int, None])
    assert kind == "optional"
    assert tpe is int

    kind, tpe = extract_core_type(str | None)
    assert kind == "optional"
    assert tpe is str

    kind, tpe = extract_core_type(None | str)
    assert kind == "optional"
    assert tpe is str

    for t in [int, str, bytes, dict, list, None]:
        kind, tpe = extract_core_type(t)
        assert kind == "simple"
        assert tpe is t
