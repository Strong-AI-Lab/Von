import {
    buildNamespaceScopedStorageKey,
    getSessionScopedNamespace,
    syncNamespaceFromLocalStorage,
} from '../utils/sessionScopedStorage.js';

describe('sessionScopedStorage namespace repair', () => {
    beforeEach(() => {
        localStorage.clear();
        sessionStorage.clear();
    });

    test('repairs a stale current namespace when local user and organisation storage imply a composite namespace', () => {
        localStorage.setItem(
            'von_current_user',
            JSON.stringify({ concept_id: '#V#michael_witbrock' }),
        );
        localStorage.setItem(
            'von_current_org',
            JSON.stringify({ concept_id: '#V#university_of_auckland_strong_ai_lab' }),
        );
        localStorage.setItem('current_user_namespace', '#V#michael_witbrock');

        expect(getSessionScopedNamespace()).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
        expect(localStorage.getItem('current_user_namespace')).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
        expect(sessionStorage.getItem('current_user_namespace')).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
    });

    test('syncNamespaceFromLocalStorage repairs an already-populated session namespace instead of trusting the stale value', () => {
        localStorage.setItem(
            'von_current_user',
            JSON.stringify({ concept_id: '#V#michael_witbrock' }),
        );
        sessionStorage.setItem(
            'von_current_org',
            JSON.stringify({ concept_id: '#V#university_of_auckland_strong_ai_lab' }),
        );
        sessionStorage.setItem('current_user_namespace', '#V#michael_witbrock');
        localStorage.setItem('current_user_namespace', '#V#michael_witbrock');

        syncNamespaceFromLocalStorage();

        expect(sessionStorage.getItem('current_user_namespace')).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
        expect(localStorage.getItem('current_user_namespace')).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
    });

    test('buildNamespaceScopedStorageKey uses the repaired canonical namespace', () => {
        localStorage.setItem(
            'von_current_user',
            JSON.stringify({ concept_id: '#V#michael_witbrock' }),
        );
        localStorage.setItem(
            'von_current_org',
            JSON.stringify({ concept_id: '#V#university_of_auckland_strong_ai_lab' }),
        );
        localStorage.setItem('current_user_namespace', '#V#michael_witbrock');

        expect(buildNamespaceScopedStorageKey('von:openConceptTabs')).toBe(
            'von:openConceptTabs:%23V%23michael_witbrock%40university_of_auckland_strong_ai_lab',
        );
    });
});
