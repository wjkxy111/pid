import unittest
from unittest.mock import Mock

import serial

from pidlab.protocol import encode_message
from pidlab.transport import SerialTransport


class SerialTransportTests(unittest.TestCase):
    def receive(self, chunks):
        messages, errors = [], []
        transport = SerialTransport(messages.append, errors.append)
        transport._serial = Mock()
        transport._running.set()
        iterator = iter(chunks)

        def read(*args, **kwargs):
            try:
                return next(iterator)
            except StopIteration:
                transport._running.clear()
                return b""

        transport._serial.read_until.side_effect = read
        transport._read_loop()
        return messages, errors

    def test_timeout_and_split_utf8_preserve_frame(self):
        expected = {"type": "status", "message": "设备"}
        frame = encode_message(expected)
        split = frame.index("设".encode("utf-8")) + 1
        messages, errors = self.receive([frame[:split], b"", frame[split:]])
        self.assertEqual(messages, [expected])
        self.assertEqual(errors, [])

    def test_oversized_frame_recovers_at_next_newline(self):
        expected = {"type": "s", "v": [0, 1, 2]}
        messages, errors = self.receive([b"x" * 16384, b"tail\n", encode_message(expected)])
        self.assertEqual(messages, [expected])
        self.assertEqual(len(errors), 1)

    def test_bad_json_does_not_drop_next_frame(self):
        expected = {"type": "ack", "command": "stop", "ok": True}
        messages, errors = self.receive([b"\n", b"invalid\n", encode_message(expected)])
        self.assertEqual(messages, [expected])
        self.assertEqual(len(errors), 1)

    def test_disconnected_reader_stops_instead_of_repeating_errors(self):
        errors = []
        transport = SerialTransport(Mock(), errors.append)
        transport._serial = Mock()
        transport._serial.read_until.side_effect = serial.SerialException("disconnected")
        transport._running.set()
        transport._read_loop()
        self.assertFalse(transport._running.is_set())
        self.assertEqual(errors, ["disconnected"])
        transport._serial.read_until.assert_called_once()
