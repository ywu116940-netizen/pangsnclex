#!/usr/bin/env python3
"""Offline schema and content checks for the benchmark-only NCLEX exemplar library."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


STYLES = {
    "expected_finding",
    "cue_to_condition",
    "function_mechanism",
    "patient_teaching",
    "further_teaching",
    "nursing_intervention",
    "priority_best_action",
    "nursing_problem_identification",
}
FORMATS = {"single_choice", "SATA"}
LEVELS = {"recognition", "application", "clinical_judgment"}
DENSITIES = {"minimal", "moderate", "high"}
PATTERNS = {
    "expected_finding",
    "identify_condition_from_cues",
    "function_mechanism",
    "best_nursing_action",
    "need_for_further_teaching",
    "appropriate_patient_education",
    "select_correct_interventions",
    "identify_primary_problem",
    "evaluate_outcome",
}


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list(value: Any, path: str, errors: list[str], *, min_count: int = 1) -> None:
    if not isinstance(value, list) or len(value) < min_count or not all(_nonempty_string(item) for item in value):
        errors.append(f"{path} must be a non-empty list of strings")


def _answer_values(example: dict[str, Any], path: str, errors: list[str]) -> set[str]:
    answer = example.get("correctAnswer")
    if isinstance(answer, str) and answer.strip():
        return {answer.strip()}
    if isinstance(answer, list) and answer and all(_nonempty_string(item) for item in answer):
        return {item.strip() for item in answer}
    errors.append(f"{path}.correctAnswer must be a non-empty string or string list")
    return set()


def validate(payload: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["root must be an object"]
    if payload.get("schema_version") != "nclex-exemplars-1":
        errors.append("schema_version must be nclex-exemplars-1")
    catalog = payload.get("pattern_catalog")
    if not isinstance(catalog, list):
        errors.append("pattern_catalog must be a list")
    else:
        catalog_styles: set[str] = set()
        for index, entry in enumerate(catalog):
            path = f"pattern_catalog[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{path} must be an object")
                continue
            style = entry.get("itemStyle")
            if style not in STYLES:
                errors.append(f"{path}.itemStyle has an unsupported value")
            elif style in catalog_styles:
                errors.append(f"{path}.itemStyle duplicates {style!r}")
            else:
                catalog_styles.add(style)
            for field in ("nursingTask", "decisionBoundary", "distractorStrategy", "uniquenessRationale"):
                if not _nonempty_string(entry.get(field)):
                    errors.append(f"{path}.{field} must be a non-empty string")
        if catalog_styles != STYLES:
            errors.append("pattern_catalog must contain exactly one entry for every item style")
    records = payload.get("item_styles")
    if not isinstance(records, list) or not records:
        errors.append("item_styles must be a non-empty list")
        records = []
    ids: set[str] = set()
    style_counts: dict[str, int] = {style: 0 for style in STYLES}
    for index, record in enumerate(records):
        path = f"item_styles[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{path} must be an object")
            continue
        item_id = record.get("id")
        if not _nonempty_string(item_id):
            errors.append(f"{path}.id must be a non-empty string")
        elif item_id in ids:
            errors.append(f"{path}.id duplicates {item_id!r}")
        else:
            ids.add(item_id)
        style = record.get("itemStyle")
        if style not in STYLES:
            errors.append(f"{path}.itemStyle must be one of {sorted(STYLES)}")
        else:
            style_counts[style] += 1
        if record.get("exampleKind") not in {"strong", "anti"}:
            errors.append(f"{path}.exampleKind must be strong or anti")
        if record.get("responseFormat") not in FORMATS:
            errors.append(f"{path}.responseFormat must be single_choice or SATA")
        if record.get("cognitiveLevel") not in LEVELS:
            errors.append(f"{path}.cognitiveLevel has an unsupported value")
        if record.get("scenarioDensity") not in DENSITIES:
            errors.append(f"{path}.scenarioDensity has an unsupported value")
        if record.get("stemPattern") not in PATTERNS:
            errors.append(f"{path}.stemPattern has an unsupported value")
        _string_list(record.get("designPrinciples"), f"{path}.designPrinciples", errors, min_count=3)
        _string_list(record.get("antiPatterns"), f"{path}.antiPatterns", errors, min_count=1)
        example = record.get("syntheticExample")
        if not isinstance(example, dict):
            errors.append(f"{path}.syntheticExample must be an object")
            continue
        if not _nonempty_string(example.get("stem")):
            errors.append(f"{path}.syntheticExample.stem must be a non-empty string")
        options = example.get("options")
        if not isinstance(options, list) or len(options) < 3 or len(options) > 6 or not all(_nonempty_string(item) for item in options):
            errors.append(f"{path}.syntheticExample.options must contain 3-6 non-empty strings")
            options = []
        answers = _answer_values(example, f"{path}.syntheticExample", errors)
        if record.get("responseFormat") == "single_choice" and len(answers) != 1:
            errors.append(f"{path}.syntheticExample single_choice must have exactly one answer")
        if record.get("responseFormat") == "SATA" and not isinstance(example.get("correctAnswer"), list):
            errors.append(f"{path}.syntheticExample SATA correctAnswer must be a list")
        if answers and options and not answers.issubset(set(options)):
            errors.append(f"{path}.syntheticExample.correctAnswer must match option text exactly")
    for style, count in style_counts.items():
        if count < 3:
            errors.append(f"item_styles must contain at least 3 records for {style}; found {count}")
    for style in STYLES:
        strong_count = sum(1 for record in records if isinstance(record, dict) and record.get("itemStyle") == style and record.get("exampleKind") == "strong")
        anti_count = sum(1 for record in records if isinstance(record, dict) and record.get("itemStyle") == style and record.get("exampleKind") == "anti")
        if strong_count < 2:
            errors.append(f"item_styles must contain at least 2 strong records for {style}; found {strong_count}")
        if anti_count < 1:
            errors.append(f"item_styles must contain at least 1 anti record for {style}; found {anti_count}")

    repairs = payload.get("bad_to_repaired")
    if not isinstance(repairs, list) or len(repairs) < 6:
        errors.append("bad_to_repaired must contain at least 6 records")
        repairs = []
    for index, repair in enumerate(repairs):
        path = f"bad_to_repaired[{index}]"
        if not isinstance(repair, dict):
            errors.append(f"{path} must be an object")
            continue
        repair_id = repair.get("id")
        if not _nonempty_string(repair_id):
            errors.append(f"{path}.id must be a non-empty string")
        elif repair_id in ids:
            errors.append(f"{path}.id duplicates another record")
        else:
            ids.add(repair_id)
        if not _nonempty_string(repair.get("problemType")):
            errors.append(f"{path}.problemType must be a non-empty string")
        if not _nonempty_string(repair.get("lesson")):
            errors.append(f"{path}.lesson must be a non-empty string")
        for side in ("bad", "repaired"):
            item = repair.get(side)
            if not isinstance(item, dict):
                errors.append(f"{path}.{side} must be an object")
                continue
            if not _nonempty_string(item.get("stem")):
                errors.append(f"{path}.{side}.stem must be a non-empty string")
            _string_list(item.get("options"), f"{path}.{side}.options", errors, min_count=3)
            if side == "repaired":
                answers = _answer_values(item, f"{path}.{side}", errors)
                if answers and not answers.issubset(set(item.get("options") or [])):
                    errors.append(f"{path}.repaired.correctAnswer must match option text exactly")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(Path(__file__).with_name("nclex_exemplars.json")))
    args = parser.parse_args()
    path = Path(args.path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"ERROR: could not read {path}: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON in {path}: {exc}", file=sys.stderr)
        return 2
    errors = validate(payload)
    if errors:
        print(f"Invalid exemplar library: {path}", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Valid exemplar library: {path} ({len(payload['item_styles'])} style records, {len(payload['bad_to_repaired'])} repairs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
