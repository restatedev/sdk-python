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


def test_pydantic_serdes_round_trip_aliased_fields():
    """Aliased fields survive a round trip: dump and validate use the same names."""
    from pydantic import BaseModel, ConfigDict, Field

    from restate.serde import DefaultSerde, PydanticJsonSerde, Serde

    class Call(BaseModel):
        model_config = ConfigDict(extra="allow")
        name: str
        async_: bool = Field(alias="async")

    call = Call.model_validate({"name": "get_weather", "async": True})
    serdes: list[Serde[Call]] = [DefaultSerde(Call), PydanticJsonSerde(Call)]
    for serde in serdes:
        buf = serde.serialize(call)
        assert b'"async":true' in buf
        restored = serde.deserialize(buf)
        assert restored == call
        assert restored is not None and restored.model_extra == {}


def test_openai_model_response_replay_keeps_wire_names():
    """A journaled Responses API function call replays with `async`, not `async_`."""
    import pytest

    pytest.importorskip("agents")
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall

    from restate.ext.openai.models import RestateModelResponse
    from restate.serde import DefaultSerde

    call = ResponseFunctionToolCall.model_validate(
        {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{}", "async": False}
    )
    resp = RestateModelResponse(output=[call], usage=Usage(), response_id="resp_1")

    serde = DefaultSerde(RestateModelResponse)
    replayed = serde.deserialize(serde.serialize(resp))
    assert replayed is not None
    (raw_item,) = replayed.to_input_items()
    item: dict = dict(raw_item)
    assert "async_" not in item
    assert item["async"] is False
