Resolve KR relationship endpoint references after concept materialisation. Return JSON only.

Use verified kr_concept_iteration_results[].result to map source_key and target_key directly onto kr_concept_id values. Do not look those verified IDs up again solely to prove existence.

When kr_materialisation_guard_passed is true, fixed source_id or target_id values admitted by kr_materialisation_guard.fixed_authorised_concept_ids have already passed the deterministic authority boundary. Map them directly and do not look them up again solely to prove existence.

For an unguarded exact #V# source_id, target_id, or predicate ID whose existence is not already established by verified materialisation results, use concept_exists for the bounded existence and access check. Use fetch_concept only when endpoint or predicate metadata or kind must actually be verified; pass the exact concept_id, limit 1, and request no incoming structural relations, text relations, or concept previews.

Do not use broad lexical search in this stage. An endpoint or predicate supplied only as an unresolved label is a planning defect: block it rather than guessing. Never substitute or re-resolve an exact ID after concept_exists or fetch_concept times out, fails, or returns inaccessible or absent. Block and report that exact-ID lookup failure instead.

For dynamic predicates, require a #V# predicate concept that exists or was materialised in the concept fan-out; structural aliases such as type_of, instance_of, is_a_type_of, and is_an_instance_of are allowed.

Return decision 'assert' with resolved_relationship_specs items containing key, source_id, predicate, target_id, and rationale. If any requested relationship cannot be resolved safely, return decision 'block' and explain blocking_reason. If there are no requested relationships, return decision 'skip' with an empty resolved_relationship_specs array.

Return JSON only with keys: decision ('assert', 'skip', or 'block'), resolved_relationship_specs array, blocking_reason.
