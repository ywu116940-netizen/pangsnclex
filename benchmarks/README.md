# V3-C Evaluation Fixtures

`v3c_source_fixtures.json` is a stable, source-only regression set for comparing direct generation with blueprint-first generation. The source text is original evaluation material, not copied NCLEX questions.

Each case contains:

- a stable `case_id` and unique `source.sourceId`
- source text (and structured chunks where headings matter)
- intended concepts and task types
- known failure modes
- a note describing what a strong item should accomplish
- empty `direct` and `blueprint` output sections ready for recorded results

The fixture is intentionally provider-free. Populate the two output sections with paired results from the same source and settings, then run:

```bash
python3 benchmark_v3c.py \
  --fixture benchmarks/v3c_source_fixtures.json \
  --output benchmarks/v3c_report.json
```

The evaluator validates the fixture before scoring. To preserve paired comparisons, keep the same case and source IDs, generation settings, and item ordering in both output sections. Do not put real NCLEX questions or copyrighted textbook passages in this dataset.

## Trusted enrichment experiment

`v3c_trusted_knowledge.json` contains short enrichment records keyed by fixture
case. Records are accepted only from the allowlisted HTTPS domains in
`quiz_generation.py`; they are never used as final evidence. Run the experimental
path separately from the notes-only baseline:

```bash
python3 benchmarks/run_v3c_live.py \
  --fixture benchmarks/v3c_source_fixtures.json \
  --trusted-knowledge benchmarks/v3c_trusted_knowledge.json \
  --output benchmarks/v3c_enriched_outputs.json \
  --enriched-blueprint-only
```

Then compare it with the saved section-ID notes-only artifact:

```bash
python3 benchmarks/compare_v3c_enrichment.py \
  --notes benchmarks/v3c_section_id_paired.json \
  --enriched benchmarks/v3c_enriched_outputs.json \
  --output benchmarks/v3c_enrichment_report.json
```

The comparison preserves exact paired items and reports clinical-quality
dimensions, provenance violations, provider usage, tokens, latency, and regressions.

## Shadow reviewer

The reviewer is benchmark-only and cannot reject, replace, or alter questions.
It independently checks every option using the stem, rationale, and relevant note
sections. Run a one-case smoke test first:

```bash
python3 benchmarks/run_v3c_shadow_reviewer.py \
  --input benchmarks/v3c_section_id_paired.json \
  --output benchmarks/v3c_shadow_review_smoke.json \
  --limit 1
```

Then review all 15 cases:

```bash
python3 benchmarks/run_v3c_shadow_reviewer.py \
  --input benchmarks/v3c_section_id_paired.json \
  --output benchmarks/v3c_shadow_review.json
```

The report includes PASS/REVIEW/FAIL rates, independent option concerns,
benchmark-score disagreements, reviewer token usage, and latency.

## NCLEX exemplar library

`nclex_exemplars.json` is a benchmark/development-only library of original,
synthetic item-design examples. It is not a copyrighted NCLEX question bank,
does not establish medical authority, and is not loaded by production quiz
generation. Uploaded notes remain the authority for testable content when a
future retrieval experiment is connected.

The taxonomy describes the nursing task rather than the response format:

- `expected_finding`: identify a normal or expected assessment finding
- `cue_to_condition`: map a meaningful cue cluster to a supported condition or concern
- `function_mechanism`: explain what a structure or process does
- `patient_teaching`: choose useful education for a stated patient goal
- `further_teaching`: identify a patient statement that reveals a misconception
- `nursing_intervention`: select an intervention for a defined nursing goal
- `priority_best_action`: choose the first or highest-priority action
- `nursing_problem_identification`: identify the primary nursing problem supported by cues

`SATA` is a response format, not a separate style. `scenarioDensity` describes
how much context the decision needs: `minimal` uses only essential context,
`moderate` provides several decision-relevant cues, and `high` supports a
complex priority or clinical-judgment decision. Not every item should be a
long scenario.

The intended future retrieval flow is:

```text
blueprint task/style
-> retrieve 1-3 relevant exemplars
-> provide only those exemplars to the writer
```

Do not load the entire exemplar library into every generation request. Validate
the file offline without provider access:

```bash
python3 benchmarks/validate_nclex_exemplars.py
```
