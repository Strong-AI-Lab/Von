import {
    buildDescriptionMetadataRows,
    deriveRelationshipExtentQuery,
    getRelationshipConfidenceScore,
    sortRelationshipExtentRows,
    summariseRelationshipProvenance
} from '../dynamicTabs.js';

describe('dynamicTabs uncertain relationship helpers', () => {
    test('deriveRelationshipExtentQuery returns asserted-only mode', () => {
        const query = deriveRelationshipExtentQuery('asserted_only');
        expect(query.uncertaintyMode).toBe('asserted_only');
        expect(query.includeUncertain).toBe(false);
        expect(query.uncertaintyStatuses).toEqual(['proposed']);
    });

    test('deriveRelationshipExtentQuery defaults to include uncertain mode', () => {
        const query = deriveRelationshipExtentQuery('unknown_filter');
        expect(query.uncertaintyMode).toBe('include_uncertain');
        expect(query.includeUncertain).toBe(true);
    });

    test('getRelationshipConfidenceScore clamps values into [0,1]', () => {
        expect(getRelationshipConfidenceScore({ uncertainty: { confidence_score: 1.5 } })).toBe(1);
        expect(getRelationshipConfidenceScore({ uncertainty: { confidence_score: -0.1 } })).toBe(0);
        expect(getRelationshipConfidenceScore({ uncertainty: { confidence_score: '0.42' } })).toBeCloseTo(0.42);
        expect(getRelationshipConfidenceScore({})).toBeNull();
    });

    test('summariseRelationshipProvenance builds compact provenance summary', () => {
        const summary = summariseRelationshipProvenance({
            uncertainty: {
                provenance: {
                    source: 'relation_elicitation',
                    source_interaction_id: 'int-123',
                    rejected_by: 'ui'
                }
            }
        });
        expect(summary).toContain('source: relation_elicitation');
        expect(summary).toContain('interaction: int-123');
        expect(summary).toContain('rejected by: ui');
    });

    test('sortRelationshipExtentRows sorts by confidence descending then recency', () => {
        const rows = [
            {
                relation_id: 'a',
                updated_at: '2026-02-01T10:00:00Z',
                uncertainty: { confidence_score: 0.2, updated_at_utc: '2026-02-01T10:00:00Z' }
            },
            {
                relation_id: 'b',
                updated_at: '2026-02-02T10:00:00Z',
                uncertainty: { confidence_score: 0.9, updated_at_utc: '2026-02-02T10:00:00Z' }
            },
            {
                relation_id: 'c',
                updated_at: '2026-02-03T10:00:00Z',
                uncertainty: { confidence_score: 0.9, updated_at_utc: '2026-02-03T10:00:00Z' }
            }
        ];
        const sorted = sortRelationshipExtentRows(rows, 'confidence_desc');
        expect(sorted.map((row) => row.relation_id)).toEqual(['c', 'b', 'a']);
    });

    test('buildDescriptionMetadataRows surfaces structured provenance and confidence', () => {
        const rows = buildDescriptionMetadataRows({
            context: {
                confidence_score: 0.83,
                parent_concept_id: '#V#process'
            },
            provenance: {
                source: 'relation_elicitation',
                attribution: '#V#michael_witbrock',
                timestamp: '2026-03-01T00:00:00Z'
            },
            relation_updated_at: '2026-03-01T01:00:00Z'
        });
        const labels = rows.map((row) => row.label);
        expect(labels).toEqual(expect.arrayContaining(['Source', 'Attribution', 'Parent', 'Confidence', 'Updated']));
        const confidenceRow = rows.find((row) => row.label === 'Confidence');
        expect(confidenceRow?.value).toBe('83%');
    });
});

