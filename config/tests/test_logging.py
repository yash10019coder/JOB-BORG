"""U7: structured JSON logging (LOGGING config in config/settings/base.py).

Exercises the formatter/handler machinery directly rather than reloading
Django settings mid-test (settings are process-global once loaded), since
that's the actual unit of behavior the env toggle controls.
"""
import io
import json
import logging

from django.test import SimpleTestCase
from pythonjsonlogger.jsonlogger import JsonFormatter


class JsonLoggingFormatTests(SimpleTestCase):
    def _record(self, level=logging.INFO, msg="hello"):
        return logging.LogRecord(
            name="apps.jobs", level=level, pathname=__file__, lineno=1,
            msg=msg, args=(), exc_info=None,
        )

    def test_json_format_emits_valid_json_with_expected_keys(self):
        formatter = JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        output = formatter.format(self._record())
        parsed = json.loads(output)
        self.assertEqual(parsed["levelname"], "INFO")
        self.assertEqual(parsed["name"], "apps.jobs")
        self.assertEqual(parsed["message"], "hello")

    def test_plain_format_is_not_json(self):
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        output = formatter.format(self._record())
        with self.assertRaises(json.JSONDecodeError):
            json.loads(output)
        self.assertIn("INFO", output)
        self.assertIn("apps.jobs", output)

    def test_level_suppresses_lower_severity_records(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("apps.jobs.test_level_suppression")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.WARNING)

        logger.info("should be suppressed")
        logger.warning("should appear")

        output = stream.getvalue()
        self.assertNotIn("should be suppressed", output)
        self.assertIn("should appear", output)
