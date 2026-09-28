#!/usr/bin/env python3
"""Small provider-free regression tests for the exemplar schema."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_nclex_exemplars import validate  # noqa: E402


class ExemplarLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads((HERE / "nclex_exemplars.json").read_text(encoding="utf-8"))

    def test_library_is_valid(self) -> None:
        self.assertEqual(validate(self.payload), [])

    def test_duplicate_id_is_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["item_styles"][1]["id"] = payload["item_styles"][0]["id"]
        errors = validate(payload)
        self.assertTrue(any("duplicates" in error for error in errors))

    def test_answer_must_match_an_option(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["item_styles"][0]["syntheticExample"]["correctAnswer"] = ["Not an option"]
        errors = validate(payload)
        self.assertTrue(any("must match option text" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
