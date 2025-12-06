# AI Notes

## Current Status
- **Date**: 2025-12-04
- **Recent Activity**:
  - Added quote and space validation to concept name generation (JVNAUTOSCI-760)
  - Created reusable `validate_concept_name_for_id()` helper function in utils_vontology.py
  - Updated linkification regex to remove spaces from allowed character set (consistent with validation)
  - Concept IDs now disallow: quotes (reserved for text boundaries), spaces (use underscores/hyphens)
- **Immediate Focus**: Awaiting user verification of validation changes.
- **Reminders**:
  - **Spelling**: Always use New Zealand English (e.g., "behaviour", "colour", "optimise")

## Todo
- [ ] (Add new tasks here)
