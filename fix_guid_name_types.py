#!/usr/bin/env python3
"""
Fix GUID name types globally in text_relations collection.

GUIDs that are marked as CODE with language=en-NZ should be marked as CODE with language=vonGUID
instead, to avoid polluting search results.

This script finds all text relations where:
- name_type = "CODE"
- language = "en-NZ"
- text matches a UUID pattern (hex with dashes)

And updates them to:
- language = "vonGUID"
"""

import re
import sys
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# UUID regex pattern (8-4-4-4-12 hex digits)
UUID_PATTERN = re.compile(
    r"^[a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12}$",
    re.IGNORECASE,
)

def is_uuid(text: str) -> bool:
    """Check if text is a UUID."""
    return UUID_PATTERN.match(text) is not None

def fix_guid_name_types():
    """Fix GUID name types in text_relations."""
    try:
        # Connect to MongoDB
        client = MongoClient("mongodb://localhost:27017/")
        db = client["von_db"]
        text_relations = db["text_relations"]

        # Query for CODE names with en-NZ language that look like GUIDs
        query = {
            "name_type": "CODE",
            "language": "en-NZ"
        }

        # Fetch all matching documents to check for UUIDs
        docs = list(text_relations.find(query))
        print(f"Found {len(docs)} text relations with CODE en-NZ")

        # Filter to those that are actually UUIDs
        guid_docs = [doc for doc in docs if is_uuid(doc.get("text", ""))]
        print(f"Found {len(guid_docs)} that are UUID-like")

        if not guid_docs:
            print("No GUID names found. Nothing to do.")
            return

        # Show a few examples
        print("\nExamples of GUIDs to be fixed:")
        for doc in guid_docs[:3]:
            print(f"  Concept: {doc.get('concept_id')}, GUID: {doc.get('text')}")

        # Update all matching documents
        guid_texts = [doc["text"] for doc in guid_docs]
        result = text_relations.update_many(
            {
                "name_type": "CODE",
                "language": "en-NZ",
                "text": {"$in": guid_texts}
            },
            {
                "$set": {
                    "language": "vonGUID"
                }
            }
        )

        print(f"\nUpdate result:")
        print(f"  Matched: {result.matched_count}")
        print(f"  Modified: {result.modified_count}")

        if result.modified_count > 0:
            print(f"\n✅ Successfully migrated {result.modified_count} GUID names from en-NZ to vonGUID")
        else:
            print("\n⚠️  No documents were updated")

        client.close()
        return result.modified_count > 0

    except PyMongoError as e:
        print(f"❌ MongoDB error: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        return False

if __name__ == "__main__":
    success = fix_guid_name_types()
    sys.exit(0 if success else 1)
