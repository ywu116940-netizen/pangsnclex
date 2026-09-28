#!/usr/bin/env python3
"""Provider-free tests for the staging-only exemplar toggle and matcher."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quiz_generation import ExemplarQuestionBlueprint, HikariQuizGenerator, SyntheticExemplarLibrary  # noqa: E402


class ExemplarRetrievalTests(unittest.TestCase):
    def _blueprint(self) -> ExemplarQuestionBlueprint:
        return ExemplarQuestionBlueprint(
            taskType="teaching",
            clinicalCues=["patient asks for instruction", "home routine is unclear"],
            decisionBoundary="Choose the teaching action that addresses the stated barrier.",
            targetAnswer="Use teach-back to verify the routine.",
            distractorStrategies=["too late", "unsupported", "different goal"],
            sourceSectionIds=["section-1"],
            itemStyle="patient_teaching",
            responseFormat="single_choice",
            cognitiveLevel="application",
            scenarioDensity="minimal",
        )

    def test_retrieval_returns_one_strong_synthetic_record(self) -> None:
        record = SyntheticExemplarLibrary.retrieve(self._blueprint())
        self.assertEqual(record["itemStyle"], "patient_teaching")
        self.assertTrue(record["id"].startswith("patient-teaching-"))
        self.assertIn("syntheticExample", record)

    def test_request_toggle_is_staging_only(self) -> None:
        old = {key: os.environ.get(key) for key in ("STUDYWELL_ENV", "STUDYWELL_USE_EXEMPLARS")}
        try:
            os.environ["STUDYWELL_ENV"] = "production"
            os.environ["STUDYWELL_USE_EXEMPLARS"] = "true"
            production = HikariQuizGenerator("test", "http://example.invalid", "test")
            self.assertFalse(production._resolve_exemplar_toggle(True))
            os.environ["STUDYWELL_ENV"] = "staging"
            staging = HikariQuizGenerator("test", "http://example.invalid", "test")
            self.assertTrue(staging._resolve_exemplar_toggle(True))
            self.assertFalse(staging._resolve_exemplar_toggle(False))
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
