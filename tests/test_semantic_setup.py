"""Security installer behavior at the wheel and setup boundaries."""
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile
from scripts import semantic_setup


class SemanticSecuritySetupTests(unittest.TestCase):
    def fixture_wheel(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("fastembed/__init__.py", "MODEL_API = 'unchanged'\n")
            archive.writestr("fastembed-0.8.0.dist-info/METADATA", (
                'Name: fastembed\nVersion: 0.8.0\n'
                'Requires-Dist: pillow (>=11.0.0,<13.0) ; python_version == "3.13"\n'))
            archive.writestr("fastembed-0.8.0.dist-info/RECORD", "")
        return output.getvalue()

    def test_unverified_source_fails_before_a_wheel_is_built(self):
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            semantic_setup.security_wheel(b"untrusted replacement archive")

    def test_patch_preserves_executable_code_and_is_reproducible(self):
        source = self.fixture_wheel()
        with mock.patch.object(semantic_setup, "FASTEMBED_WHEEL_SHA256", hashlib.sha256(source).hexdigest()):
            first = semantic_setup.security_wheel(source)
            second = semantic_setup.security_wheel(source)
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as wheel:
            self.assertEqual(wheel.read("fastembed/__init__.py"), b"MODEL_API = 'unchanged'\n")
            self.assertIn(b"pillow (>=13.0.0,<14.0)", wheel.read("fastembed-0.8.0.dist-info/METADATA"))

    def test_cached_setup_repairs_a_replaced_patch_without_network(self):
        source = self.fixture_wheel()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            cache = runtime.parent / "security-patches" / "pillow13"
            cache.mkdir(parents=True)
            (cache / "fastembed-0.8.0.upstream.whl").write_bytes(source)
            wheel = cache / "fastembed-0.8.0-py3-none-any.whl"
            wheel.write_bytes(b"replaced by old package")
            with mock.patch.object(semantic_setup, "RUNTIME", runtime), mock.patch.object(
                semantic_setup, "FASTEMBED_WHEEL_SHA256", hashlib.sha256(source).hexdigest()
            ), mock.patch.object(semantic_setup, "urlopen") as network:
                packages, receipt = semantic_setup.install_packages((3, 13))
                network.assert_not_called()
            self.assertIn("pillow==13.0.0", packages)
            self.assertEqual(receipt["wheel_sha256"], hashlib.sha256(wheel.read_bytes()).hexdigest())
            self.assertTrue(zipfile.is_zipfile(wheel))

    def test_cached_source_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            cache = runtime.parent / "security-patches" / "pillow13"
            cache.mkdir(parents=True)
            (cache / "fastembed-0.8.0.upstream.whl").write_bytes(b"corrupted cache")
            with mock.patch.object(semantic_setup, "RUNTIME", runtime), self.assertRaisesRegex(ValueError, "checksum"):
                semantic_setup.install_packages((3, 13))

    def test_oversized_cached_wheel_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            cache = runtime.parent / "security-patches" / "pillow13"
            cache.mkdir(parents=True)
            with (cache / "fastembed-0.8.0.upstream.whl").open("wb") as handle:
                handle.truncate(2 * 1024 * 1024)
            with mock.patch.object(semantic_setup, "RUNTIME", runtime), self.assertRaisesRegex(ValueError, "1 MiB"):
                semantic_setup.install_packages((3, 13))

    def test_existing_runtime_minor_controls_patch_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin/python3").touch()
            (runtime / semantic_setup.OWNER).touch()
            with mock.patch.object(semantic_setup, "RUNTIME", runtime), mock.patch.object(
                semantic_setup, "interpreter_version", side_effect=[(3, 12), (3, 13)]
            ), mock.patch.object(semantic_setup, "install_packages", return_value=(["reviewed-fastembed.whl", "onnxruntime==1.23.2", "pillow==13.0.0"], {"patch": "checked"})) as selection, mock.patch.object(
                semantic_setup.subprocess, "run"
            ):
                receipt = semantic_setup.setup("requested-python312", model=False)
            selection.assert_called_once_with((3, 13))
            self.assertEqual(receipt["security_patch"]["patch"], "checked")
