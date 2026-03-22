# Relationship and Kind Classification - Authoritative Pathway

**JVNAUTOSCI-986**: Document describing the single authoritative pathway for relationship writes and kind/type classification.

## Overview

This document defines how relationships are written and how concept kind (type, predicate, individual) is derived in the Von codebase. The goal is to prevent drift between structural relationship fields and canonical predicate concepts.

## Authoritative Source of Truth

### Kind Classification

The **structural relationship fields** are the authoritative source for kind classification:

| Relationship Field | Meaning |
|-------------------|---------|
| `relationships.is_a_type_of` | Concept is a **type** (has supertypes) |
| `relationships.is_an_instance_of` | Concept is an **instance** of types |
| `relationships.has_subtype` | Inverse of is_a_type_of |
| `relationships.has_instance` | Inverse of is_an_instance_of |
| `relationships.related_to` | Generic bidirectional relation |

### Kind Derivation Rules

1. **Type**: Has non-empty `is_a_type_of` (even if also has `is_an_instance_of` for higher-order types)
2. **Predicate**: Has `is_an_instance_of` containing `#V#predicate` or a predicate subtype
3. **Individual**: Has `is_an_instance_of` but no `is_a_type_of`

See `src/backend/vontology/utils_vontology.py`:
- `is_type()` - checks for is_a_type_of
- `is_predicate()` - checks for predicate-type instance_of
- `is_pure_instance()` - checks for instance_of without type_of
- `build_pure_instance_query()` - builds the matching Mongo filter for runtime reads

### Pure-instance runtime reads

For tree/entity listing behaviour (JVNAUTOSCI-1552, aligned with earlier
instance-count work in JVNAUTOSCI-338 and JVNAUTOSCI-568), runtime "entity"
reads must use the same pure-instance rule in both in-memory and Mongo-backed
paths:

- Count or list docs only when `relationships.is_an_instance_of` is non-empty.
- Exclude docs with any non-empty `relationships.is_a_type_of`.
- Do not rely on `metadata.concept_type` for runtime classification.

### Canonical Predicate Concepts

Structural predicates have both a field name and a corresponding `#V#` concept ID:

| Field Name | Canonical Concept ID |
|------------|---------------------|
| `is_a_type_of` | `#V#is_a_type_of` |
| `has_subtype` | `#V#has_subtype` |
| `is_an_instance_of` | `#V#is_an_instance_of` |
| `has_instance` | `#V#has_instance` |
| `related_to` | `#V#related_to` |

## Write Paths

### Single Authoritative Service

All relationship writes should use `src/backend/services/relationship_write_service.py`:

```python
from src.backend.services.relationship_write_service import (
    add_relationship,           # Main entry point
    add_structural_relationship,  # For structural predicates
    add_dynamic_relationship,   # For #V# predicate concepts
    normalise_structural_predicate,
    is_structural_predicate,
    detect_kind_drift,
)
```

### Normalisation

The `normalise_structural_predicate()` function converts:
- `#V#is_a_type_of` → `is_a_type_of`
- `#V#is_an_instance_of` → `is_an_instance_of`
- `instanceOf` → `is_an_instance_of`
- etc.

Non-structural predicates (e.g., `#V#has_author`) are returned unchanged.

### Inverse Consistency

Structural predicates automatically maintain inverse relationships:
- Adding `source.is_a_type_of = target` also adds `target.has_subtype = source`
- Adding `source.is_an_instance_of = target` also adds `target.has_instance = source`

Dynamic predicates (`#V#...` concept IDs) do **not** have automatic inverses.

## Validation

### Predicate Validation

Before using a `#V#...` predicate ID (that's not a structural alias):
1. Check that a concept exists with that ID
2. Check that the concept is typed as a predicate (`is_an_instance_of` includes `#V#predicate` or similar)

Use `validate_predicate_concept()` from the write service.

### Drift Detection

Use `detect_kind_drift()` to check if a concept's computed kind matches expectations:

```python
from src.backend.services.relationship_write_service import detect_kind_drift

drift = detect_kind_drift(concept_doc, expected_kind="type")
if drift:
    logger.warning(f"Kind drift detected: {drift}")
```

## Migration Tools

- `utilities/audit_virtual_predicates.py` - Read-only audit of predicate usage
- `utilities/migrate_virtual_relationship_predicates.py` - Repair invalid relationship keys

## Testing

Consistency tests are in `tests/backend/test_structural_canonical_consistency.py`:
- Kind derivation consistency
- Structural field normalisation
- Drift detection

## Future Work

1. Add scheduled consistency check job to detect drift
2. Consider deprecating direct structural field writes in favour of service-only writes
3. Add telemetry for relationship write paths to monitor usage
