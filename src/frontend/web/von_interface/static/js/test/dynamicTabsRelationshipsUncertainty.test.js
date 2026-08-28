import {
    buildDescriptionMetadataRows,
    createRelationshipTextDisclosureCell,
    deriveRelationshipExtentQuery,
    getRelationshipConfidenceScore,
    resolveRelationshipExtentConceptKind,
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

    test('predicate columns never present predicate concepts as individuals', () => {
        expect(resolveRelationshipExtentConceptKind('individual', 'predicate')).toBe('predicate');
        expect(resolveRelationshipExtentConceptKind(undefined, 'predicate')).toBe('predicate');
        expect(resolveRelationshipExtentConceptKind('type', 'argument')).toBe('type');
        expect(resolveRelationshipExtentConceptKind(undefined, 'argument')).toBe('individual');
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

    test('long relationship literals disclose and copy their exact value', async () => {
        const longText = 'As of 2026-08-05: current PhD research spans robust knowledge representation, provenance-aware assistants, and a deliberately long final clause.';
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText }
        });

        const { cell, detailRow } = createRelationshipTextDisclosureCell(
            longText,
            'en-NZ',
            {
                roleLabel: 'Arg2',
                predicateLabel: '#V#has_research_description_as_of'
            }
        );
        document.body.append(cell, detailRow);

        expect(cell.querySelector('.relationship-text-preview').textContent).toBe(longText);
        expect(cell.querySelector('.relationship-text-preview').classList.contains('is-collapsed')).toBe(true);
        expect(detailRow.hidden).toBe(true);
        const toggle = cell.querySelector('.relationship-text-toggle');
        toggle.click();
        expect(toggle.getAttribute('aria-expanded')).toBe('true');
        expect(detailRow.hidden).toBe(false);
        expect(detailRow.querySelector('.relationship-text-full').textContent).toBe(longText);
        expect(detailRow.querySelector('strong').textContent).toContain(
            '#V#has_research_description_as_of'
        );

        cell.querySelector('.relationship-text-copy').click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(writeText).toHaveBeenCalledWith(longText);
        expect(cell.querySelector('.relationship-text-copy').textContent).toBe('Copied');

        detailRow.querySelector('.relationship-text-close').click();
        expect(detailRow.hidden).toBe(true);
        expect(toggle.getAttribute('aria-expanded')).toBe('false');
    });

    test('short relationship literals stay compact without disclosure controls', () => {
        const { cell, detailRow } = createRelationshipTextDisclosureCell('Short value');

        expect(cell.textContent).toBe('Short value');
        expect(cell.querySelector('.relationship-text-toggle')).toBeNull();
        expect(detailRow).toBeNull();
    });
});
