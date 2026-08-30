import unittest
import time
from core.template_engine import decode_escape_sequences, extract_template_fields, format_template

class TestTemplateEngine(unittest.TestCase):
    def test_decode_escape_sequences(self):
        self.assertEqual(decode_escape_sequences("Hello\\nWorld"), "Hello\nWorld")
        self.assertEqual(decode_escape_sequences("Col1\\tCol2"), "Col1\tCol2")
        self.assertEqual(decode_escape_sequences("Literal\\\\nTest"), "Literal\\nTest")

    def test_extract_template_fields(self):
        msg_data = {
            "sender": "N5DHO",
            "channel": 1,
            "snr": 9.5,
            "rssi": -85,
            "path": ["RPT1", "RPT2", "N5DHO"],
            "timestamp": time.time() - 2.5
        }
        fields = extract_template_fields(msg_data, timezone_str="UTC")
        self.assertEqual(fields["sender"], "N5DHO")
        self.assertEqual(fields["channel"], "1")
        self.assertEqual(fields["snr"], "9.5dB")
        self.assertEqual(fields["rssi"], "-85dBm")
        self.assertEqual(fields["hops"], "2")
        self.assertEqual(fields["hops_label"], "2 hops")
        self.assertIn("RPT1 > RPT2 > N5DHO", fields["path"])
        self.assertTrue(fields["elapsed"].endswith("s"))

    def test_format_template_substitution(self):
        fields = {
            "sender": "Alice",
            "snr": "10.0dB",
            "rssi": "-75dBm",
            "hops_label": "Direct (0 hops)"
        }
        tpl = "Ack to {sender} | SNR: {snr} | {hops_label}"
        result = format_template(tpl, fields)
        self.assertEqual(result, "Ack to Alice | SNR: 10.0dB | Direct (0 hops)")

    def test_format_template_newlines_and_unknown(self):
        fields = {"sender": "Bob"}
        tpl = "Line 1: {sender}\\nLine 2: {unknown_var}"
        result = format_template(tpl, fields)
        self.assertEqual(result, "Line 1: Bob\nLine 2: {unknown_var}")

if __name__ == "__main__":
    unittest.main()
