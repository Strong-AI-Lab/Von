import {
    fetchUserPreferences,
    hydrateStoredSelectionsFromUserPreferences,
} from '../utils/userPreferenceBootstrap.js';

describe('userPreferenceBootstrap', () => {
    beforeEach(() => {
        localStorage.clear();
        sessionStorage.clear();
    });

    test('fetchUserPreferences returns null when no concept id is provided', async () => {
        const getJsonImpl = jest.fn();

        const result = await fetchUserPreferences('', { getJsonImpl });

        expect(result).toBeNull();
        expect(getJsonImpl).not.toHaveBeenCalled();
    });

    test('hydrates missing stored org context from user preferences', async () => {
        const setStoredOrgContext = jest.fn();

        const prefs = await hydrateStoredSelectionsFromUserPreferences('#V#michael_witbrock', {
            getJsonImpl: jest.fn().mockResolvedValue({
                preferred_language: 'en-NZ',
                organisation_concept_id: '#V#university_of_auckland_strong_ai_lab',
            }),
            getStoredOrgContext: () => null,
            setStoredOrgContext,
            localStorageImpl: localStorage,
        });

        expect(prefs?.organisation_concept_id).toBe('#V#university_of_auckland_strong_ai_lab');
        expect(setStoredOrgContext).toHaveBeenCalledWith({
            id: null,
            concept_id: '#V#university_of_auckland_strong_ai_lab',
            name: null,
        });
        expect(localStorage.getItem('von_preferred_language')).toBe('en-NZ');
    });

    test('preserves an existing org selection instead of overwriting it from user preferences', async () => {
        const setStoredOrgContext = jest.fn();

        await hydrateStoredSelectionsFromUserPreferences('#V#michael_witbrock', {
            getJsonImpl: jest.fn().mockResolvedValue({
                preferred_language: 'en-NZ',
                organisation_concept_id: '#V#university_of_auckland_strong_ai_lab',
            }),
            getStoredOrgContext: () => ({
                id: null,
                concept_id: '#V#the_lu_witbrock_household',
                name: 'The Lu Witbrock Household',
            }),
            setStoredOrgContext,
            localStorageImpl: localStorage,
        });

        expect(setStoredOrgContext).not.toHaveBeenCalled();
        expect(localStorage.getItem('von_preferred_language')).toBe('en-NZ');
    });
});
