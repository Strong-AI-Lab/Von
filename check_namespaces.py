#!/usr/bin/env python3
"""Quick diagnostic script to check namespace distribution in RAG sessions."""

from src.backend.db.connection_manager import get_db

db = get_db()
if db is None:
    print("ERROR: Database not available")
    exit(1)

coll = db['interaction_sessions']

# Total counts
total = coll.count_documents({})
indexed = coll.count_documents({'indexing_status': 'indexed'})

print(f"=== RAG Session Namespace Analysis ===\n")
print(f"Total sessions: {total}")
print(f"Indexed sessions: {indexed}\n")

# Breakdown by namespace (all sessions)
print("All sessions by namespace:")
pipeline = [
    {"$group": {"_id": "$namespace", "count": {"$sum": 1}}},
    {"$sort": {"count": -1}}
]
for item in coll.aggregate(pipeline):
    ns = item['_id'] or '(no namespace)'
    count = item['count']
    print(f"  {ns}: {count}")

# Breakdown by namespace (indexed only)
print("\nIndexed sessions by namespace:")
pipeline = [
    {"$match": {"indexing_status": "indexed"}},
    {"$group": {"_id": "$namespace", "count": {"$sum": 1}}},
    {"$sort": {"count": -1}}
]
for item in coll.aggregate(pipeline):
    ns = item['_id'] or '(no namespace)'
    count = item['count']
    print(f"  {ns}: {count}")

# Sample session IDs per namespace (indexed only)
print("\nSample session IDs per namespace (indexed, first 3):")
for ns_item in coll.aggregate([
    {"$match": {"indexing_status": "indexed"}},
    {"$group": {"_id": "$namespace", "count": {"$sum": 1}}},
    {"$sort": {"count": -1}}
]):
    ns = ns_item['_id']
    print(f"\n  Namespace: {ns or '(none)'}")
    sample_sessions = list(coll.find(
        {"indexing_status": "indexed", "namespace": ns},
        {"_id": 1, "created_at": 1}
    ).limit(3))
    for sess in sample_sessions:
        print(f"    - {sess['_id']} (created: {sess.get('created_at', 'unknown')})")
