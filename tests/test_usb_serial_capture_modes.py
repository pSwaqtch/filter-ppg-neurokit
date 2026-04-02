from __future__ import annotations

import unittest
from unittest import mock

import ui.serial_tab as serial_tab
import usb_serial


class FakeSerial:
    def __init__(self, reads: list[bytes] | None = None, *, lines: list[str] | None = None) -> None:
        self.reads = list(reads or [])
        self.lines = list(lines or [])
        self.writes: list[bytes] = []
        self.closed = False

    def reset_input_buffer(self) -> None:
        return None

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        return None

    def read(self, _n: int) -> bytes:
        if self.reads:
            return self.reads.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


class CaptureModeTests(unittest.TestCase):
    def test_select_capture_window_returns_only_recent_samples(self) -> None:
        buf = [
            (0, 1, 2, 3, 4),
            (1000, 1, 2, 3, 4),
            (6000, 1, 2, 3, 4),
            (9000, 1, 2, 3, 4),
            (12000, 1, 2, 3, 4),
        ]

        recent = serial_tab._select_capture_window(buf, 5)

        self.assertEqual(recent, [(9000, 1, 2, 3, 4), (12000, 1, 2, 3, 4)])

    def test_select_capture_window_keeps_full_buffer_when_window_is_empty(self) -> None:
        buf = [(0, 1, 2, 3, 4), (1000, 1, 2, 3, 4)]

        self.assertEqual(serial_tab._select_capture_window(buf, None), buf)

    def test_build_ppg_stream_command_switches_between_binary_and_text(self) -> None:
        self.assertEqual(
            usb_serial.build_ppg_stream_command("slotab", 250, hr_channel="sBch3", binary=True),
            "adpd ppg slotab stream-bin 250 hr on sBch3",
        )
        self.assertEqual(
            usb_serial.build_ppg_stream_command("slota", 100, hr_channel=None, binary=False),
            "adpd ppg slota stream 100",
        )

    def test_stream_text_live_dual_port_uses_control_shell_for_text_stream(self) -> None:
        ctrl = FakeSerial()
        stream = FakeSerial()
        lines = ["0,1,2,3,4", "1,5,6,7,8", ""]

        def fake_open(port: str, _baud: int, timeout: float = 0.0):
            return ctrl if port == "/dev/control" else stream

        def fake_read_line(_ser, timeout_s: float = 0.0):
            return lines.pop(0) if lines else ""

        with mock.patch.object(usb_serial, "_open", side_effect=fake_open), \
             mock.patch.object(usb_serial, "_read_line", side_effect=fake_read_line), \
             mock.patch.object(usb_serial, "SERIAL_AVAILABLE", True):
            chunks = list(
                usb_serial.stream_text_live_dual_port(
                    "/dev/control",
                    "/dev/stream",
                    115200,
                    2,
                    stream_timeout_s=0.01,
                    slot="slota",
                )
            )

        self.assertEqual(ctrl.writes[0], b"adpd ppg slota stream 2\r\n")
        self.assertEqual(chunks[0], ([], b"", [">> [/dev/control] adpd ppg slota stream 2"], False))
        self.assertEqual(chunks[1], (["0,1,2,3,4"], b"0,1,2,3,4\n", [], False))
        self.assertEqual(chunks[2], (["1,5,6,7,8"], b"1,5,6,7,8\n", [], True))
        self.assertTrue(ctrl.closed)
        self.assertFalse(stream.closed)

    def test_build_ppg_stream_command_supports_text_hr_mode(self) -> None:
        self.assertEqual(
            usb_serial.build_ppg_stream_command("slota", 75, hr_channel="sAch3", binary=False),
            "adpd ppg slota stream 75 hr on sAch3",
        )


if __name__ == "__main__":
    unittest.main()
