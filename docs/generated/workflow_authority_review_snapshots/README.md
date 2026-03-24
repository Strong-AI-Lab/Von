# Workflow Authority Review Snapshots

These files are generated review artefacts derived from authoritative Vontology workflow state.

They are non-authoritative by design:

- edit Vontology-backed workflow and template artefacts, not these files;
- use these snapshots for inspection, review, and diffing only;
- if a generated snapshot and Vontology disagree, Vontology is correct.

Default commands:

```text
pdm run python scripts/workflow_authority_review_snapshot.py export
```

```text
pdm run python scripts/workflow_authority_review_snapshot.py diff
```

The generated `*.generated.json` files in this directory are intentionally not committed by default. Re-run `export` whenever you want a fresh local review snapshot, then use `diff` to compare the current authoritative KB state against the last generated local snapshot.
