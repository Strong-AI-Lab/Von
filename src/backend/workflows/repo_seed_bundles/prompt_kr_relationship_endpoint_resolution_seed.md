Resolve KR relationship endpoint references after concept materialisation. Return JSON only.

Use kr_concept_iteration_results[].result to map source_key and target_key onto verified kr_concept_id values. If a relationship spec already supplies source_id or target_id, fetch or search as needed to ensure it is an existing Vontology concept.

For dynamic predicates, require a #V# predicate concept that exists or was materialised in the concept fan-out; structural aliases such as type_of, instance_of, is_a_type_of, and is_an_instance_of are allowed.

Return decision 'assert' with resolved_relationship_specs items containing key, source_id, predicate, target_id, and rationale. If any requested relationship cannot be resolved safely, return decision 'block' and explain blocking_reason. If there are no requested relationships, return decision 'skip' with an empty resolved_relationship_specs array.

Return JSON only with keys: decision ('assert', 'skip', or 'block'), resolved_relationship_specs array, blocking_reason.