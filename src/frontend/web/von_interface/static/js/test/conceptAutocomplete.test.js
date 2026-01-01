import { buildConceptSearchUrl, getTriggerSearchText } from '../components/conceptAutocomplete.js';

describe('conceptAutocomplete trigger parsing', () => {
    test('returns null when no trigger present', () => {
        const result = getTriggerSearchText('hello world', 5);
        expect(result).toBeNull();
    });

    test('extracts search text after #V# trigger (long IDs supported)', () => {
        const text = 'Please use #V#organisational_role for this.';
        const cursorPos = text.indexOf('organisational_role') + 'organisational_role'.length;
        const result = getTriggerSearchText(text, cursorPos);
        expect(result).not.toBeNull();
        expect(result.searchText).toBe('organisational_role');
    });

    test('uses the last trigger before the cursor', () => {
        const text = '#V#person and then #V#organisational_role';
        const cursorPos = text.length;
        const result = getTriggerSearchText(text, cursorPos);
        expect(result).not.toBeNull();
        expect(result.searchText).toBe('organisational_role');
    });

    test('supports lowercase #v# trigger', () => {
        const text = 'choose #v#organisational_role now';
        const cursorPos = text.indexOf('organisational_role') + 'organisational_role'.length;
        const result = getTriggerSearchText(text, cursorPos);
        expect(result).not.toBeNull();
        expect(result.searchText).toBe('organisational_role');
    });
});

describe('conceptAutocomplete search URL building', () => {
    test('includes individuals and substring fallback (aligns with global search)', () => {
        const url = buildConceptSearchUrl('ger');
        expect(url).toContain('/vontology/api/vontology/search?');
        expect(url).toContain('q=ger');
        expect(url).toContain('include_individuals=true');
        expect(url).toContain('fallback_substring=true');
    });
});
