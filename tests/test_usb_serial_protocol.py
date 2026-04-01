import struct

import pytest

import usb_serial


MAGIC = b"\xA5\x5A"
VERSION = 0x01
MSG_TYPE_STREAM_PPG = 0xA0


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def make_payload(num_channels: int, *, hr_enabled: bool, timestamp: int = 1234) -> bytes:
    values = [timestamp]
    values.extend(0x1000 + idx for idx in range(num_channels))
    payload = struct.pack("<" + "I" * len(values), *values)
    if hr_enabled:
        payload += struct.pack("<fI", 72.5, 1)
    return payload


def make_frame(
    payload: bytes,
    *,
    seq: int = 0x1234,
    flags: int = 0x00,
    msg_type: int = MSG_TYPE_STREAM_PPG,
    version: int = VERSION,
) -> bytes:
    header = struct.pack(
        "<BBBHH",
        version,
        msg_type,
        flags,
        seq,
        len(payload),
    )
    crc = crc16_ccitt(header + payload)
    return MAGIC + header + payload + struct.pack("<H", crc)


def parse_frames(buffer: bytes):
    log: list[str] = []
    samples, remainder, raw_payload = usb_serial._parse_frames(bytearray(buffer), log)
    return samples, bytes(remainder), raw_payload, log


@pytest.mark.parametrize(
    "num_channels,hr_enabled,expected",
    [
        (4, False, (1234, 0x1000, 0x1001, 0x1002, 0x1003)),
        (4, True, (1234, 0x1000, 0x1001, 0x1002, 0x1003, pytest.approx(72.5), 1)),
        (8, False, (1234, 0x1000, 0x1001, 0x1002, 0x1003, 0x1004, 0x1005, 0x1006, 0x1007)),
        (8, True, (1234, 0x1000, 0x1001, 0x1002, 0x1003, 0x1004, 0x1005, 0x1006, 0x1007, pytest.approx(72.5), 1)),
    ],
)
def test_parses_valid_frames_for_all_supported_payload_shapes(num_channels, hr_enabled, expected):
    frame = make_frame(make_payload(num_channels, hr_enabled=hr_enabled))

    samples, remainder, raw_payload, log = parse_frames(frame)

    assert samples == [expected]
    assert remainder == b""
    assert raw_payload == make_payload(num_channels, hr_enabled=hr_enabled)
    assert log == []


def test_rejects_bad_crc_and_recovers_on_next_frame():
    bad_frame = bytearray(make_frame(make_payload(4, hr_enabled=False)))
    bad_frame[-1] ^= 0x01
    good_frame = make_frame(make_payload(4, hr_enabled=False, timestamp=2048))

    samples, remainder, raw_payload, log = parse_frames(bytes(bad_frame) + good_frame)

    assert samples == [(2048, 0x1000, 0x1001, 0x1002, 0x1003)]
    assert raw_payload == make_payload(4, hr_enabled=False, timestamp=2048)
    assert remainder == b""
    assert any("CRC" in entry for entry in log)


def test_preserves_partial_frame_bytes_until_complete():
    frame = make_frame(make_payload(8, hr_enabled=True))
    first_chunk = frame[:11]
    second_chunk = frame[11:]

    samples1, remainder1, raw1, log1 = parse_frames(first_chunk)
    samples2, remainder2, raw2, log2 = parse_frames(remainder1 + second_chunk)

    assert samples1 == []
    assert raw1 == b""
    assert remainder1 == first_chunk
    assert log1 == []

    assert samples2 == [(1234, 0x1000, 0x1001, 0x1002, 0x1003, 0x1004, 0x1005, 0x1006, 0x1007, pytest.approx(72.5), 1)]
    assert raw2 == make_payload(8, hr_enabled=True)
    assert remainder2 == b""
    assert log2 == []


def test_discards_stray_bytes_before_magic_and_parses_frame():
    frame = make_frame(make_payload(4, hr_enabled=False, timestamp=4096))
    buffer = b"noise\x00\xff" + frame

    samples, remainder, raw_payload, log = parse_frames(buffer)

    assert samples == [(4096, 0x1000, 0x1001, 0x1002, 0x1003)]
    assert raw_payload == make_payload(4, hr_enabled=False, timestamp=4096)
    assert remainder == b""
    assert any("skipped" in entry for entry in log)
