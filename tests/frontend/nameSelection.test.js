import { selectBestNameForContext } from '../../src/frontend/web/von_interface/static/js/utils/nameSelection.js';

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
});
