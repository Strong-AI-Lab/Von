import {
    selectAbbreviationForContext,
    selectBestNameForContext,
} from '../../src/frontend/web/von_interface/static/js/utils/nameSelection.js';

describe('nameSelection', () => {
    test('prefers exact language match over others', () => {
        const names = [
            { name: 'Person', language: 'en', type: 'NL' },
            { name: 'Human being', language: 'en-NZ', type: 'NL' }
        ];

        expect(selectBestNameForContext(names, 'en-NZ')).toBe('Human being');
    });

    test('falls back to base-language match when region differs', () => {
        const names = [
            { name: 'Person', language: 'en', type: 'NL' },
            { name: 'Personne', language: 'fr', type: 'NL' }
        ];

        expect(selectBestNameForContext(names, 'en-NZ')).toBe('Person');
    });

    test('prefers NL over ABBR over CODE, then shortest', () => {
        const names = [
            { name: 'European Union', language: 'en-NZ', type: 'NL' },
            { name: 'EU', language: 'en-NZ', type: 'ABBR' },
            { name: 'Mx4r...', language: 'en-NZ', type: 'CODE' }
        ];

        // Mirrors the existing tab heuristic: NL beats ABBR even if longer.
        expect(selectBestNameForContext(names, 'en-NZ')).toBe('European Union');
    });

    test('within same language/type, chooses shorter', () => {
        const names = [
            { name: 'member country', language: 'en-NZ', type: 'NL' },
            { name: 'country', language: 'en-NZ', type: 'NL' }
        ];

        expect(selectBestNameForContext(names, 'en-NZ')).toBe('country');
    });

    test('ignores technical Von ID CODE names', () => {
        const names = [
            { name: '#V#person', language: 'en-NZ', type: 'CODE' },
            { name: 'Person', language: 'en-NZ', type: 'NL' }
        ];

        expect(selectBestNameForContext(names, 'en-NZ')).toBe('Person');
    });

    test('ignores UUID/GUID CODE names (including vonGUID language)', () => {
        const names = [
            { name: '2f1c3c9b-7b9c-4d1a-9b79-0c3d1a2b3c4d', language: 'en-NZ', type: 'CODE' },
            { name: '2f1c3c9b-7b9c-4d1a-9b79-0c3d1a2b3c4d', language: 'vonGUID', type: 'CODE' },
            { name: 'Human', language: 'en-NZ', type: 'NL' }
        ];

        expect(selectBestNameForContext(names, 'en-NZ')).toBe('Human');
    });

    test('ignores ObjectId-like CODE names (24 hex) when NL exists', () => {
        const names = [
            { name: '6959bf0c1de296491ab69cb6', language: 'en-NZ', type: 'CODE' },
            { name: 'Human', language: 'en-NZ', type: 'NL' }
        ];

        expect(selectBestNameForContext(names, 'en-NZ')).toBe('Human');
    });

    test('falls back to ID-von CODE before GUID-like CODE', () => {
        const names = [
            { name: '#V#thing', language: 'en-NZ', type: 'CODE' },
            { name: '2f1c3c9b-7b9c-4d1a-9b79-0c3d1a2b3c4d', language: 'en-NZ', type: 'CODE' }
        ];

        expect(selectBestNameForContext(names, 'en-NZ')).toBe('#V#thing');
    });

    test('falls back to ID-von CODE before ObjectId-like CODE', () => {
        const names = [
            { name: '#V#thing', language: 'en-NZ', type: 'CODE' },
            { name: '6959bf0c1de296491ab69cb6', language: 'en-NZ', type: 'CODE' }
        ];

        expect(selectBestNameForContext(names, 'en-NZ')).toBe('#V#thing');
    });

    test('selects an explicitly represented abbreviation for compact identity UI', () => {
        const names = [
            { name: 'Michael Witbrock', language: 'en-NZ', type: 'NL' },
            { name: 'MJW', language: 'en-NZ', type: 'ABBR' },
            { name: 'MW', language: 'en-US', type: 'ABBR' },
        ];

        expect(selectAbbreviationForContext(names, 'en-NZ')).toBe('MJW');
    });

    test('does not invent a compact name when no abbreviation is represented', () => {
        expect(selectAbbreviationForContext([
            { name: 'University of Auckland Strong AI Lab', language: 'en-NZ', type: 'NL' },
        ], 'en-NZ')).toBeNull();
    });
});
