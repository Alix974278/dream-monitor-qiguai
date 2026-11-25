"""The user's working login/catalog/filter/manual flow must stay original."""
import ast
import hashlib
import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "UPSTREAM_BASELINE.json").read_text(encoding="utf-8"))
SOURCE_BASELINE = json.loads((ROOT / "FUNCTION_SOURCE_BASELINE.json").read_text(encoding="utf-8"))


class OriginalCompatibilityTests(unittest.TestCase):
    def test_login_pages_and_catalog_filter_crypto_sources_are_unchanged(self):
        for file, digest in BASELINE["unchanged_files"].items():
            with self.subTest(file=file):
                self.assertEqual(hashlib.sha256((ROOT / file).read_bytes()).hexdigest(), digest)

    def test_original_auth_form_encoding_and_manual_signup_functions_are_preserved(self):
        for file, functions in BASELINE["unchanged_functions"].items():
            tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
            nodes = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            for cls in tree.body:
                if isinstance(cls, ast.ClassDef):
                    nodes.update({cls.name + "." + fn.name: fn for fn in cls.body if isinstance(fn, ast.FunctionDef)})
            for name, digest in functions.items():
                with self.subTest(file=file, function=name):
                    node = nodes[name]
                    # The v2.3 ZIP's AST digests fail with its bundled 3.14.10
                    # even before modification. Exact source SHA-256 is stricter
                    # and portable; keep its original source baseline unchanged.
                    source = (ROOT / file).read_text(encoding="utf-8")
                    expected_source = SOURCE_BASELINE[file][name]
                    actual_source = hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest()
                    self.assertEqual(actual_source, expected_source)

    def test_v23_admin_scheduler_and_api_files_are_byte_identical(self):
        protected = json.loads((ROOT / "V23_PROTECTED_BASELINE.json").read_text(encoding="utf-8"))
        for file, digest in protected.items():
            with self.subTest(file=file):
                self.assertEqual(hashlib.sha256((ROOT / file).read_bytes()).hexdigest(), digest)
