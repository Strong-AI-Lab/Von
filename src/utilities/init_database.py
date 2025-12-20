#!/usr/bin/env python3
"""
Von Database Initialization Utility

Initializes collections/indexes and optionally loads starter knowledge.

Usage:
    python src/utilities/init_database.py --init              # Initialize collections/indexes
    python src/utilities/init_database.py --load-starter      # Load starter ontology
    python src/utilities/init_database.py --full-setup        # Init + load starter
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.backend.db.mongo_client import get_db, USE_MOCK_DB, MONGO_URI, DATABASE_NAME


def print_banner():
    """Print welcome banner."""
    print("=" * 60)
    print("Von Database Initialization Utility")
    print("=" * 60)
    print()


def get_database_info() -> Dict[str, Any]:
    """Get current database connection info."""
    if USE_MOCK_DB:
        return {
            "type": "mock",
            "uri": "in-memory",
            "database": "mock_db",
            "connected": True,
        }

    # Redact credentials from URI
    redacted_uri = MONGO_URI
    if "@" in redacted_uri:
        prefix, rest = redacted_uri.split("://", 1)
        if "@" in rest:
            creds, hostpart = rest.split("@", 1)
            user = creds.split(":", 1)[0] if ":" in creds else creds
            redacted_uri = f"{prefix}://{user}:***@{hostpart}"

    host = (
        redacted_uri.split("@")[-1].split("/")[0]
        if "@" in redacted_uri
        else redacted_uri.split("://")[1].split("/")[0]
    )
    db_type = "local" if ("localhost" in host or "127.0.0.1" in host) else "remote"

    return {
        "type": db_type,
        "uri": redacted_uri,
        "database": DATABASE_NAME,
        "connected": False,  # Will be set by connection test
    }


def test_connection() -> bool:
    """Test database connection."""
    print("📡 Testing database connection...")

    info = get_database_info()
    print(f"   Type: {info['type'].upper()}")
    print(f"   URI: {info['uri']}")
    print(f"   Database: {info['database']}")
    print()

    try:
        db = get_db()
        if db is None:
            raise RuntimeError("Database connection unavailable")

        if USE_MOCK_DB:
            print("✅ Connected to MOCK database (in-memory)")
            print("   Note: Data will not persist after script exits")
            return True

        # Test actual connection
        db.client.admin.command("ping")
        print(f"✅ Connected to {info['type'].upper()} MongoDB successfully")
        return True

    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print()
        print("Troubleshooting:")
        if info["type"] == "local":
            print("  • Ensure MongoDB is installed and running")
            print("  • Check Windows Services for 'MongoDB' service")
            print("  • Install from: https://www.mongodb.com/try/download/community")
        else:
            print("  • Check MONGO_URI in .env file")
            print("  • Verify network access and credentials")
            print("  • Check IP whitelist for MongoDB Atlas")
        print()
        return False


def initialize_collections() -> bool:
    """Create collections and indexes if they don't exist."""
    print("📦 Initializing collections and indexes...")
    print()

    try:
        db = get_db()
        if db is None:
            raise RuntimeError("Database connection unavailable")

        # Define collections with their indexes
        collections_config = {
            "concepts": [
                {
                    "keys": [("concept_id", 1)],
                    "unique": True,
                    "name": "concept_id_unique",
                },
                {"keys": [("created_at", -1)], "name": "created_at_desc"},
                {"keys": [("names.name", 1)], "name": "names_name_idx"},
            ],
            "text_values": [
                {"keys": [("content", "text")], "name": "content_text_search"},
                {"keys": [("created_at", -1)], "name": "created_at_desc"},
            ],
            "text_relations": [
                {"keys": [("source_id", 1)], "name": "source_id_idx"},
                {"keys": [("target_id", 1)], "name": "target_id_idx"},
            ],
            "meta_relations": [
                {
                    "keys": [("relation_type", 1)],
                    "unique": True,
                    "name": "relation_type_unique",
                }
            ],
            "interactions": [
                {"keys": [("timestamp", -1)], "name": "timestamp_desc"},
                {"keys": [("user_id", 1)], "name": "user_id_idx"},
            ],
            "settings": [{"keys": [("key", 1)], "unique": True, "name": "key_unique"}],
        }

        for collection_name, indexes in collections_config.items():
            # Create collection if doesn't exist
            if collection_name not in db.list_collection_names():
                db.create_collection(collection_name)
                print(f"✅ Created collection: {collection_name}")
            else:
                print(f"ℹ️  Collection exists: {collection_name}")

            # Create indexes
            collection = db[collection_name]
            existing_indexes = {idx["name"] for idx in collection.list_indexes()}

            for index_spec in indexes:
                index_name = index_spec.get("name")
                if index_name not in existing_indexes:
                    keys = index_spec.pop("keys")
                    collection.create_index(keys, **index_spec)
                    print(f"   ✅ Created index: {index_name}")
                else:
                    print(f"   ℹ️  Index exists: {index_name}")

            print()

        print("✅ All collections and indexes initialized successfully")
        print()
        return True

    except Exception as e:
        print(f"❌ Failed to initialize collections: {e}")
        print()
        return False


def load_starter_ontology() -> bool:
    """Load starter knowledge base from JSON file."""
    print("📚 Loading starter ontology...")
    print()

    # Find the starter ontology file
    script_dir = Path(__file__).parent.parent.parent  # Von root
    ontology_path = script_dir / "sample_knowledge" / "starter_ontology.json"

    if not ontology_path.exists():
        print(f"❌ Starter ontology file not found: {ontology_path}")
        print("   Expected location: sample_knowledge/starter_ontology.json")
        print()
        return False

    try:
        # Load JSON
        with open(ontology_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        metadata = data.get("metadata", {})
        concepts = data.get("concepts", [])
        meta_relations = data.get("meta_relations", [])

        print(f"📄 Loaded: {metadata.get('description', 'Starter Ontology')}")
        print(f"   Version: {metadata.get('version', 'unknown')}")
        print(f"   Concepts: {len(concepts)}")
        print(f"   Meta Relations: {len(meta_relations)}")
        print()

        db = get_db()
        if db is None:
            raise RuntimeError("Database connection unavailable")

        # Load meta relations first
        if meta_relations:
            print("Loading meta relations...")
            meta_rel_collection = db["meta_relations"]

            for meta_rel in meta_relations:
                relation_type = meta_rel.get("relation_type")

                # Check if already exists
                existing = meta_rel_collection.find_one(
                    {"relation_type": relation_type}
                )
                if existing:
                    print(f"   ℹ️  Meta relation exists: {relation_type}")
                else:
                    meta_rel_collection.insert_one(meta_rel)
                    print(f"   ✅ Created meta relation: {relation_type}")

            print()

        # Load concepts
        if concepts:
            print("Loading concepts...")
            concepts_collection = db["concepts"]

            # First pass: Create all concepts
            concept_id_map = {}  # concept_id -> concept_id (for validation)

            for concept_data in concepts:
                concept_id = concept_data.get("concept_id")
                if not concept_id:
                    print(f"   ⚠️  Skipping concept without concept_id")
                    continue

                # Check if concept already exists
                existing = concepts_collection.find_one({"concept_id": concept_id})
                if existing:
                    print(f"   ℹ️  Concept exists: {concept_id}")
                    concept_id_map[concept_id] = concept_id
                    continue

                # Prepare concept document with proper Von schema
                now = datetime.now(timezone.utc)
                concept_doc = {
                    "concept_id": concept_id,
                    "names": concept_data.get("names", []),
                    "attributes": concept_data.get("attributes", {}),
                    "system_tags": concept_data.get("system_tags", []),
                    "user_tags": concept_data.get("user_tags", []),
                    "relationships": concept_data.get("relationships", {}),
                    "created_at": now,
                    "updated_at": now,
                }

                # Handle preserved_fields (description and notes)
                preserved = concept_data.get("preserved_fields", {})
                if preserved:
                    if "concept_data" not in concept_doc:
                        concept_doc["concept_data"] = {}
                    concept_doc["concept_data"]["preserved_fields"] = preserved

                result = concepts_collection.insert_one(concept_doc)
                concept_id_map[concept_id] = concept_id

                # Get primary name for display
                primary_name = concept_id
                if concept_doc["names"]:
                    primary_name = concept_doc["names"][0].get("name", concept_id)

                print(f"   ✅ Created concept: {primary_name} ({concept_id})")

            print(f"   Total concepts loaded: {len(concept_id_map)}")
            print()

        print("✅ Starter ontology loaded successfully!")
        print()
        print(f"Summary:")
        print(f"  • {len(concepts)} concepts loaded")
        print(f"  • {len(meta_relations)} meta relation types defined")
        print(f"  • Relationships established between concepts")
        print()
        print("You can now:")
        print("  • Start Von and explore the concepts")
        print("  • View relationships in the graph view")
        print("  • Add your own concepts and extend the knowledge base")
        print()

        return True

    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse JSON: {e}")
        print()
        return False
    except Exception as e:
        print(f"❌ Failed to load starter ontology: {e}")
        import traceback

        traceback.print_exc()
        print()
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Von Database Initialization Utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Initialize collections and indexes
  python src/utilities/init_database.py --init

  # Load starter ontology (AI concepts)
  python src/utilities/init_database.py --load-starter

  # Full setup (init + starter)
  python src/utilities/init_database.py --full-setup
        """,
    )

    parser.add_argument(
        "--init",
        action="store_true",
        help="Initialize database collections and indexes",
    )

    parser.add_argument(
        "--load-starter",
        action="store_true",
        help="Load starter ontology (requires --init or existing collections)",
    )

    parser.add_argument(
        "--full-setup",
        action="store_true",
        help="Full setup: initialize collections + load starter ontology",
    )

    args = parser.parse_args()

    # If no arguments, show help
    if not any([args.init, args.load_starter, args.full_setup]):
        parser.print_help()
        return 1

    print_banner()

    # Test connection first
    if not test_connection():
        return 1

    # Full setup mode
    if args.full_setup:
        print("Running full setup (init + load starter)...")
        print()

        if not initialize_collections():
            return 1

        if not load_starter_ontology():
            return 1

        print("=" * 60)
        print("✅ Full setup completed successfully!")
        print("=" * 60)
        return 0

    # Initialize collections
    if args.init:
        if not initialize_collections():
            return 1

    # Load starter ontology
    if args.load_starter:
        if not load_starter_ontology():
            return 1

    print("=" * 60)
    print("✅ Database initialization completed successfully!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
