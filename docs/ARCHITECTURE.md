# Architecture

The application separates deterministic orchestration from model judgment.

```text
React workbench
    | REST + SSE
FastAPI application
    |-- import/split/normalize
    |-- persistent task scheduler
    |-- provider adapters
    |-- prompt renderer and validators
    |-- quality gate and export builder
    |
SQLite metadata + immutable content-addressed artifacts
```

## Invariants

- Models never receive filesystem authority. Inputs are rendered into a versioned task envelope.
- A draft is not accepted state. Only validated artifacts can unlock dependent tasks.
- Run-local memory is promoted to the branch head only after final QA and a compare-and-swap check.
- Markdown and TXT files are views; versioned JSON is the canonical state.
- Adjacent chapters are serial unless the approved plan explicitly declares independent state scopes.
- Provider calls cannot be assumed exactly-once. A crash after dispatch but before persistence produces an `indeterminate` attempt.
- Retries, review cycles, calls, tokens, cost and wall time are bounded before dispatch.

## Prompt families

Runtime prompts are deliberately smaller than the archived source prompts:

- `analyzer`: chunk analysis, state extraction and memory deltas.
- `planner`: Canon synthesis, style/suspense planning and Main A decisions.
- `writer`: prose generation and issue-targeted rewriting.
- `reviewer`: parameterized independent review dimensions.
- `lead_editor`: Main B issue aggregation and revision order.

All source novel text is delimited as untrusted data so that text inside a manuscript cannot alter tool or scheduler behavior.

