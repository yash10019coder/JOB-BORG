"""Storage selection and deadline regressions for submission file uploads."""
import gzip
import io
import os
import tempfile
from unittest.mock import Mock, patch

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase
from storages.backends.s3 import S3Storage

from apps.auto_apply.greenhouse_form.exceptions import GreenhouseFormError
from apps.auto_apply.tasks import _cleanup_temp_files, _materialize_file_answers


class FileAnswerTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.storage = FileSystemStorage(location=directory.name)
        self.storage_patch = patch("apps.auto_apply.tasks.storages", {"default": self.storage})
        self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        self.clock = patch("apps.auto_apply.tasks.time.monotonic", return_value=100.0).start()
        self.addCleanup(patch.stopall)
        self.temp_files = []
        self.addCleanup(_cleanup_temp_files, self.temp_files)

    def materialize(self, value):
        answers = {"Resume": value}
        _materialize_file_answers(
            {"Resume": {"field_type": "file"}}, answers, self.temp_files,
            deadline_monotonic=110.0,
        )
        return answers["Resume"]

    def test_relative_local_file_cannot_shadow_storage_key(self):
        key = self.storage.save("resume.pdf", ContentFile(b"stored resume"))
        with patch("apps.auto_apply.tasks.os.path.isfile", return_value=True) as isfile:
            result = self.materialize(key)
        isfile.assert_not_called()
        with open(result, "rb") as copied:
            self.assertEqual(copied.read(), b"stored resume")
        self.assertTrue(result.endswith(".pdf"))

    def test_existing_legacy_absolute_path_is_preserved(self):
        with tempfile.NamedTemporaryFile() as legacy:
            with patch.object(self.storage, "exists") as exists:
                self.assertEqual(self.materialize(legacy.name), legacy.name)
        exists.assert_not_called()
        self.assertEqual(self.temp_files, [])

    def test_missing_storage_key_keeps_fallback(self):
        self.assertEqual(self.materialize("missing.pdf"), "missing.pdf")
        self.assertEqual(self.temp_files, [])

    def test_multichunk_copy(self):
        content = b"x" * (64 * 1024 * 2 + 3)
        key = self.storage.save("resume.pdf", ContentFile(content))
        result = self.materialize(key)
        with open(result, "rb") as copied:
            self.assertEqual(copied.read(), content)

    def test_expired_deadline_does_not_open_storage(self):
        self.clock.return_value = 110.0
        with patch.object(self.storage, "exists") as exists:
            with self.assertRaises(GreenhouseFormError):
                self.materialize("resume.pdf")
        exists.assert_not_called()

    def test_deadline_checked_after_a_slow_read_and_partial_file_is_cleanable(self):
        source = io.BytesIO(b"x" * (64 * 1024 * 3))
        original_read = source.read
        reads = []

        def slow_read(size):
            reads.append(size)
            if len(reads) == 2:
                self.clock.return_value = 111.0
            return original_read(size)

        source.read = slow_read
        with patch.object(self.storage, "exists", return_value=True), patch.object(
            self.storage, "open", return_value=source,
        ):
            with self.assertRaises(GreenhouseFormError):
                self.materialize("resume.pdf")
        self.assertEqual(reads, [64 * 1024, 64 * 1024])
        self.assertTrue(source.closed)
        partial = self.temp_files[0]
        self.assertEqual(os.path.getsize(partial), 64 * 1024)
        _cleanup_temp_files(self.temp_files)
        self.assertFalse(os.path.exists(partial))

    def test_expiry_after_eof_does_not_accept_upload(self):
        source = io.BytesIO()

        def late_eof(size):
            self.clock.return_value = 110.0
            return b""

        source.read = late_eof
        with patch.object(self.storage, "exists", return_value=True), patch.object(
            self.storage, "open", return_value=source,
        ):
            with self.assertRaises(GreenhouseFormError):
                self.materialize("resume.pdf")

    def test_supported_read_timeout_tracks_remaining_budget(self):
        source = io.BytesIO(b"resume")
        source.set_socket_timeout = Mock()
        original_read = source.read

        def advancing_read(size):
            self.clock.return_value += 1.0
            return original_read(size)

        source.read = advancing_read
        with patch.object(self.storage, "exists", return_value=True), patch.object(
            self.storage, "open", return_value=source,
        ):
            self.materialize("resume.pdf")
        self.assertEqual(
            [call.args[0] for call in source.set_socket_timeout.call_args_list], [10.0, 9.0],
        )

    def test_s3_streams_with_isolated_timeouts_and_preserves_download_options(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                self.check_s3_stream(compressed)

    def check_s3_stream(self, compressed):
        storage = S3Storage(
            bucket_name="test-bucket", location="uploads", gzip=compressed,
            access_key="testing", secret_key="testing", region_name="us-east-1",
            object_parameters={"RequestPayer": "requester", "CacheControl": "private"},
        )
        original_config = storage.client_config
        resource = boto3.Session(
            aws_access_key_id="testing", aws_secret_access_key="testing",
        ).resource("s3", region_name="us-east-1")
        session = Mock()
        session.resource.return_value = resource
        content = b"resume" * 20000
        payload = gzip.compress(content) if compressed else content
        body = StreamingBody(io.BytesIO(payload), len(payload))
        body.set_socket_timeout = Mock()
        response = {"Body": body}
        if compressed:
            response["ContentEncoding"] = "gzip"
        params = {"Bucket": "test-bucket", "Key": "uploads/resume.pdf", "RequestPayer": "requester"}
        with Stubber(resource.meta.client) as stubber:
            stubber.add_response("head_object", {}, params)
            stubber.add_response("head_object", {}, params)
            stubber.add_response("get_object", response, params)
            with patch("apps.auto_apply.tasks.storages", {"default": storage}), patch.object(
                S3Storage, "_create_session", return_value=session,
            ):
                result = self.materialize("resume.pdf")
            stubber.assert_no_pending_responses()
        with open(result, "rb") as copied:
            self.assertEqual(copied.read(), content)
        config = session.resource.call_args.kwargs["config"]
        self.assertEqual(config.read_timeout, 10.0)
        self.assertEqual(config.connect_timeout, 10.0)
        self.assertEqual(config.retries["total_max_attempts"], 1)
        self.assertIs(storage.client_config, original_config)
        self.assertIsNone(storage._bucket)
        body.set_socket_timeout.assert_called_with(10.0)
        self.assertTrue(body._raw_stream.closed)
