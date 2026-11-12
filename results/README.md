# Evaluation evidence

Results are partitioned by the dataset/prompt version used to produce each candidate:

- `v1/structural`: 10 structural-evaluation candidates.
- `v2/structural`, `v3/structural`, and `v3b/structural`: candidates used for the version-comparison study.
- `v3c/structural`: 30 structural-evaluation candidates in three ten-candidate chunks.
- `v3c/runtime`: the same 30 v3c candidates, arranged for input to the host-aware evaluator.

Structural files contain the contract input, reference output, model output, section-presence checks, syntax checks, endpoint/route checks, and text-similarity scores. Runtime evaluation is reported only for v3c because it is the selected release evaluated through backend replacement; the runtime evaluator creates a fresh temporary backend and runs the endpoint and whole-backend tests twice per candidate.
