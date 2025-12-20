import os
import sys

import shutil
import pytest
from pathlib import Path
from backend.vontology import utils_vontology
from datetime import timezone


# --- Pytest fixture for DB cleanup ---
@pytest.fixture(autouse=True)
def clean_vontology_nodes():
    db = utils_vontology.get_db()
    if db is not None:
        db["vontology_nodes"].delete_many({})
    yield
    if db is not None:
        db["vontology_nodes"].delete_many({})


@pytest.mark.skip(
    reason="Functions scan_filesystem_to_mongodb, verify_vontology_db_vs_filesystem, and recreate_filesystem_from_mongodb have been removed as obsolete"
)
def test_scan_and_recreate(tmp_path):
    # Setup: Copy a small sample of the real vontology dir to tmp_path
    sample_dir = tmp_path / "vontology"
    sample_dir.mkdir()
    # Create a minimal tree: Thing/Example/Example_Type.md
    (sample_dir / "Thing" / "Example").mkdir(parents=True)
    md_path = sample_dir / "Thing" / "Example" / "Example_Type.md"
    md_content = """# Example\n\n**Source Concept**: cyc:Example\n**SubConcept Of**: Thing\n\n**Description**: An example concept.\n"""
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    # Test is skipped - these functions have been removed
    pytest.skip("Required functions have been removed during refactoring")


@pytest.mark.skip(
    reason="Functions scan_filesystem_to_mongodb and verify_vontology_db_vs_filesystem have been removed as obsolete"
)
def test_content_mismatch_detection(tmp_path):
    # Setup as before
    sample_dir = tmp_path / "vontology"
    sample_dir.mkdir()
    (sample_dir / "Thing" / "Example").mkdir(parents=True)
    md_path = sample_dir / "Thing" / "Example" / "Example_Type.md"
    md_content = """# Example\n\n**Source Concept**: cyc:Example\n**SubConcept Of**: Thing\n\n**Description**: An example concept.\n"""
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    # Test is skipped - these functions have been removed
    pytest.skip("Required functions have been removed during refactoring")


@pytest.mark.skip(
    reason="Functions scan_filesystem_to_mongodb and verify_vontology_db_vs_filesystem have been removed as obsolete"
)
def test_fs_only_and_db_only(tmp_path):
    # Setup as before
    sample_dir = tmp_path / "vontology"
    sample_dir.mkdir()
    (sample_dir / "Thing" / "OnlyFS").mkdir(parents=True)
    md_path = sample_dir / "Thing" / "OnlyFS" / "OnlyFS_Type.md"
    md_content = "# OnlyFS\n\n**Source Concept**: cyc:OnlyFS\n**SubConcept Of**: Thing\n\n**Description**: Only in FS.\n"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    # Test is skipped - these functions have been removed
    pytest.skip("Required functions have been removed during refactoring")


if __name__ == "__main__":
    pytest.main([__file__])
