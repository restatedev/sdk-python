from restate.serde import BytesSerde

def test_bytes_serde():
    s = BytesSerde()
    assert bytes(range(20)) == s.serialize(bytes(range(20)))