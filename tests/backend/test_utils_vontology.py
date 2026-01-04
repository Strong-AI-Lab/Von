"""Retired tests.

This file previously contained three tests for legacy filesystem ↔ MongoDB
Vontology migration utilities:
- scan_filesystem_to_mongodb
- verify_vontology_db_vs_filesystem
- recreate_filesystem_from_mongodb

Those utilities were removed as obsolete during refactoring, and the tests had
been permanently skipped for a long time. The test functions have been removed
to keep the backend suite clean (no perpetual skips) while preserving the
rationale in-repo.
"""
