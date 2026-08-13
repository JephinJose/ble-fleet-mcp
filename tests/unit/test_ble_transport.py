from __future__ import annotations

import pytest

from fleet_mcp.transports.ble.transport import BleTransport, _encode


def test_encode_bytes_passthrough() -> None:
    assert _encode(b"\x01\x02") == b"\x01\x02"


def test_encode_bool() -> None:
    assert _encode(True) == b"\x01"
    assert _encode(False) == b"\x00"


def test_encode_small_int_single_byte() -> None:
    assert _encode(80) == (80).to_bytes(1, "little")


def test_encode_large_int_multi_byte() -> None:
    assert _encode(1000) == (1000).to_bytes(2, "little")


def test_encode_negative_int_rejected() -> None:
    with pytest.raises(TypeError):
        _encode(-1)


def test_encode_str_utf8() -> None:
    assert _encode("ok") == b"ok"


def test_encode_unsupported_type_rejected() -> None:
    with pytest.raises(TypeError):
        _encode(3.14)


def test_default_max_connections_is_conservative() -> None:
    assert BleTransport().max_concurrent_connections() == 4
    assert BleTransport(max_connections=7).max_concurrent_connections() == 7
