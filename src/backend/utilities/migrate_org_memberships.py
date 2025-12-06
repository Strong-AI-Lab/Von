#!/usr/bin/env python
"""Migration script: Populate Vontology with organisation memberships from Phase 1 stub mappings.

This one-time migration script reads the hardcoded role mappings from role_resolver.py
and converts them into persistent Vontology memberOf relationships with hasRole text relations.

Usage:
    python migrate_org_memberships.py [--dry-run] [--verbose]

Options:
    --dry-run:   Show what would be migrated without making changes
    --verbose:   Print detailed output for each migration step

Note:
    This script is idempotent - running it multiple times has no adverse effects.
    It only creates relationships that don't already exist.
"""

import argparse
import sys
import os
from pathlib import Path
from typing import Dict, List, Tuple, Any

# Add src to path - handle both direct execution and module import
# Use absolute path to be safe
current_file = Path(__file__).resolve()
for potential_root in [current_file.parent] + list(current_file.parents):
    if (potential_root / "pyproject.toml").exists():
        sys.path.insert(0, str(potential_root))
        break

import logging

logger = logging.getLogger(__name__)


# Lazy imports to avoid issues before path is set
def get_stub_mappings() -> Dict[str, Dict[str, str]]:
    """Get the stub role mappings from role_resolver."""
    from src.backend.security.role_resolver import STUB_ROLE_MAPPINGS
    return STUB_ROLE_MAPPINGS


def get_create_membership_fn():
    """Get the create_organisation_membership function."""
    from src.backend.services.organisation_membership_service import (
        create_organisation_membership,
    )
    return create_organisation_membership


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the migration script."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)-8s: %(message)s"
    )


def validate_concepts_exist(user_id: str, org_id: str, dry_run: bool = False) -> Tuple[bool, str]:
    """Validate that both user and organisation concepts exist in the Vontology.

    Returns:
        Tuple of (exists, message)
    """
    from src.backend.db.repositories.concepts_repository import ConceptsRepository

    user_concept = ConceptsRepository.find_one({"concept_id": user_id})
    if not user_concept:
        return False, f"User concept '{user_id}' not found in Vontology"

    org_concept = ConceptsRepository.find_one({"concept_id": org_id})
    if not org_concept:
        return False, f"Organisation concept '{org_id}' not found in Vontology"

    return True, "Both concepts exist"


def migrate_user_memberships(
    user_id: str,
    org_roles: Dict[str, str],
    dry_run: bool = False,
    verbose: bool = False,
    create_membership_fn=None
) -> Tuple[int, int, List[str]]:
    """Migrate memberships for a single user across multiple organisations.

    Args:
        user_id: The user identifier (from stub mappings)
        org_roles: Dictionary of {org_id: role}
        dry_run: If True, only log what would be done
        verbose: If True, print detailed logs
        create_membership_fn: The create_organisation_membership function

    Returns:
        Tuple of (successful_migrations, failed_migrations, errors)
    """
    if create_membership_fn is None:
        create_membership_fn = get_create_membership_fn()

    successful = 0
    failed = 0
    errors: List[str] = []

    for org_id, role in org_roles.items():
        # Format concept IDs with #V# prefix if not already present
        user_concept_id = f"#V#{user_id}" if not user_id.startswith("#V#") else user_id
        org_concept_id = f"#V#{org_id}" if not org_id.startswith("#V#") else org_id

        # Validate concepts exist
        exists, msg = validate_concepts_exist(user_concept_id, org_concept_id, dry_run)
        if not exists:
            if verbose:
                logger.warning(f"Skipping {user_id} in {org_id}: {msg}")
            failed += 1
            errors.append(f"{user_concept_id} in {org_concept_id}: {msg}")
            continue

        if dry_run:
            logger.info(f"[DRY RUN] Would create membership: {user_concept_id} --memberOf--> {org_concept_id} (role: {role})")
            successful += 1
        else:
            try:
                result = create_membership_fn(
                    user_concept_id=user_concept_id,
                    organisation_concept_id=org_concept_id,
                    role=role
                )

                if result.get("relationship_created"):
                    logger.info(
                        f"Created membership: {user_concept_id} in {org_concept_id} (role: {role})"
                    )
                else:
                    logger.info(
                        f"Membership already exists: {user_concept_id} in {org_concept_id}"
                    )

                successful += 1

                if verbose:
                    logger.debug(f"  Relationship ID: {result.get('relationship_id')}")
                    logger.debug(f"  Role text value ID: {result.get('role_text_value_id')}")
                    logger.debug(f"  Role relation ID: {result.get('role_relation_id')}")

            except Exception as e:
                failed += 1
                error_msg = f"Failed to create membership for {user_concept_id} in {org_concept_id}: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)

    return successful, failed, errors


def run_migration(dry_run: bool = False, verbose: bool = False) -> Dict[str, Any]:
    """Execute the full migration from stub mappings to Vontology relationships.

    Args:
        dry_run: If True, only log what would be done
        verbose: If True, print detailed logs

    Returns:
        Dictionary with migration statistics
    """
    # Get functions/data after path is set
    stub_mappings = get_stub_mappings()
    create_membership = get_create_membership_fn()

    logger.info("=" * 60)
    logger.info("Organisation Membership Migration")
    logger.info("=" * 60)

    if dry_run:
        logger.warning("[DRY RUN MODE] No changes will be made to the database")

    logger.info(f"Reading stub mappings from role_resolver.py...")
    logger.info(f"Found {len(stub_mappings)} users with organisation mappings")

    total_successful = 0
    total_failed = 0
    all_errors: List[str] = []

    for user_id, org_roles in stub_mappings.items():
        logger.info(f"\nMigrating user: {user_id}")
        logger.info(f"  Organisations: {list(org_roles.keys())}")

        successful, failed, errors = migrate_user_memberships(
            user_id=user_id,
            org_roles=org_roles,
            dry_run=dry_run,
            verbose=verbose,
            create_membership_fn=create_membership
        )

        total_successful += successful
        total_failed += failed
        all_errors.extend(errors)

        if verbose:
            logger.debug(f"  User summary: {successful} successful, {failed} failed")

    logger.info("\n" + "=" * 60)
    logger.info("Migration Summary")
    logger.info("=" * 60)
    logger.info(f"Total successful migrations: {total_successful}")
    logger.info(f"Total failed migrations: {total_failed}")

    if all_errors:
        logger.warning(f"\nEncountered {len(all_errors)} error(s):")
        for error in all_errors:
            logger.warning(f"  - {error}")

    if not dry_run:
        logger.info("\n✓ Migration complete!")
        logger.info("Organisation memberships are now persistent in the Vontology.")
        logger.info("The stub role mappings in role_resolver.py can now be considered a fallback.")
    else:
        logger.info("\n✓ Dry-run complete! No changes were made.")
        logger.info("Run without --dry-run to execute the migration.")

    return {
        "total_successful": total_successful,
        "total_failed": total_failed,
        "errors": all_errors,
        "total_users": len(stub_mappings),
        "dry_run": dry_run
    }


def main() -> int:
    """Main entry point for the migration script."""
    parser = argparse.ArgumentParser(
        description="Migrate organisation memberships from stub mappings to Vontology"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without making changes"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed output for each migration step"
    )

    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    try:
        stats = run_migration(
            dry_run=args.dry_run,
            verbose=args.verbose
        )

        # Exit with non-zero status if there were any failures
        if stats["total_failed"] > 0:
            logger.error(f"\nMigration completed with {stats['total_failed']} failure(s)")
            return 1

        return 0

    except Exception as e:
        logger.error(f"Migration failed with error: {str(e)}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
