import {
    __testOnly_formatRagSummaryForSettings,
    __testOnly_getPreferredRagNamespace,
    __testOnly_resolveDisplayedProviderModels,
    __testOnly_syncInitialScopedSelections,
} from '../settingsPage.js';

describe('settingsPage RAG status summary', () => {
    beforeEach(() => {
        document.body.innerHTML = '';
        localStorage.clear();
        sessionStorage.clear();
    });

    test('prefers current_user_namespace over legacy von_namespace', () => {
        localStorage.setItem('von_namespace', '#V#legacy_user');
        localStorage.setItem('current_user_namespace', '#V#user@org');

        expect(__testOnly_getPreferredRagNamespace()).toBe('#V#user@org');
    });

    test('derives namespace from stored user and organisation context when storage key is missing', () => {
        localStorage.setItem(
            'von_current_user',
            JSON.stringify({ concept_id: '#V#michael_witbrock', name: 'Michael Witbrock' }),
        );
        localStorage.setItem(
            'von_current_org',
            JSON.stringify({
                concept_id: '#V#university_of_auckland_strong_ai_lab',
                name: 'University Of Auckland Strong AI Lab',
            }),
        );

        expect(__testOnly_getPreferredRagNamespace()).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
    });

    test('repairs a stale user-only namespace when stored organisation context implies a composite scope', () => {
        localStorage.setItem('current_user_namespace', '#V#michael_witbrock');
        localStorage.setItem(
            'von_current_user',
            JSON.stringify({ concept_id: '#V#michael_witbrock', name: 'Michael Witbrock' }),
        );
        localStorage.setItem(
            'von_current_org',
            JSON.stringify({
                concept_id: '#V#university_of_auckland_strong_ai_lab',
                name: 'University Of Auckland Strong AI Lab',
            }),
        );

        expect(__testOnly_getPreferredRagNamespace()).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
        expect(localStorage.getItem('current_user_namespace')).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
        expect(sessionStorage.getItem('current_user_namespace')).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
    });

    test('formats KA + Conversation summary when chat counts available', () => {
        const ragData = {
            indexed: 10,
            pending: 0,
            failed: 0,
            skipped: 0,
            session_namespace: '#V#user@org',
            chat_history_sessions: 42,
            chat_history_messages: 1979,
            chat_history_rag_success: 1956,
            chat_history_rag_failed: 0
        };

        const { summaryText, hintText, titleText } = __testOnly_formatRagSummaryForSettings(ragData, null);
        expect(summaryText).toBe('KA 10 • Conversations 1956');
        expect(hintText).toContain('Conversations indexed=1956');
        expect(hintText).toContain('Conversations failed=0');
        expect(titleText).toContain('session_ns=#V#user@org');
        expect(titleText).toContain('Conversation sessions=42');
        expect(titleText).toContain('messages=1979');
    });

    test('formats pending summary when pending > 0', () => {
        const ragData = {
            indexed: 5,
            pending: 3,
            failed: 0,
            skipped: 0
        };

        const { summaryText } = __testOnly_formatRagSummaryForSettings(ragData, null);
        expect(summaryText).toBe('KA 5 • 3 pending');
    });

    test('prefers resolved_llm over stale enabled_llms for the active provider display', () => {
        const result = __testOnly_resolveDisplayedProviderModels({
            resolved_llm: { provider: 'openai', model: 'gpt-5.4-nano' },
            enabled_llms: [
                { provider: 'openai', model: 'gpt-5.4-mini' },
                { provider: 'ollama', model: 'llama3.1:8b' },
            ],
        });

        expect(result.currentOpenAIModel).toBe('gpt-5.4-nano');
        expect(result.currentOllamaModel).toBe('llama3.1:8b');
    });

    test('initial scoped selection sync backfills storage and composite namespace from selected user and org', async () => {
        document.body.innerHTML = `
            <select id="currentUserSelect">
                <option data-id="user-1" data-concept-id="#V#michael_witbrock" selected>Michael Witbrock</option>
            </select>
            <select id="currentOrganisationSelect">
                <option value="">Personal</option>
                <option data-id="org-1" data-concept-id="#V#university_of_auckland_strong_ai_lab" selected>
                    University Of Auckland Strong Ai Lab (admin)
                </option>
            </select>
            <div id="settingsActiveNamespaceValue"></div>
            <div id="settingsActiveNamespaceHint"></div>
        `;

        const setUserConcept = jest.fn().mockResolvedValue({
            namespace: '#V#michael_witbrock',
        });
        const switchOrganisationFn = jest.fn().mockResolvedValue({
            namespace: '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        });
        const refreshRagStatus = jest.fn();

        await __testOnly_syncInitialScopedSelections({
            setUserConcept,
            switchOrganisationFn,
            refreshRagStatus,
        });

        expect(JSON.parse(localStorage.getItem('von_current_user'))).toMatchObject({
            concept_id: '#V#michael_witbrock',
            name: 'Michael Witbrock',
        });
        expect(JSON.parse(localStorage.getItem('von_current_org'))).toMatchObject({
            concept_id: '#V#university_of_auckland_strong_ai_lab',
            name: 'University Of Auckland Strong Ai Lab',
        });
        expect(localStorage.getItem('current_user_namespace')).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
        expect(setUserConcept).toHaveBeenCalledWith('#V#michael_witbrock');
        expect(switchOrganisationFn).toHaveBeenCalledWith(
            '#V#university_of_auckland_strong_ai_lab',
            'University Of Auckland Strong Ai Lab',
        );
        expect(refreshRagStatus).toHaveBeenCalled();
    });

    test('initial scoped selection sync reads the visible Phase 2 org selector when the legacy org select is absent', async () => {
        document.body.innerHTML = `
            <select id="currentUserSelect">
                <option data-id="user-1" data-concept-id="#V#michael_witbrock" selected>Michael Witbrock</option>
            </select>
            <select id="orgSelect">
                <option value="">Personal (No Org)</option>
                <option value="#V#university_of_auckland_strong_ai_lab" data-role="admin" data-concept-id="#V#university_of_auckland_strong_ai_lab" selected>
                    University Of Auckland Strong Ai Lab (admin)
                </option>
            </select>
            <div id="settingsActiveNamespaceValue"></div>
            <div id="settingsActiveNamespaceHint"></div>
        `;

        const setUserConcept = jest.fn().mockResolvedValue({
            namespace: '#V#michael_witbrock',
        });
        const switchOrganisationFn = jest.fn().mockResolvedValue({
            namespace: '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        });
        const refreshRagStatus = jest.fn();

        await __testOnly_syncInitialScopedSelections({
            setUserConcept,
            switchOrganisationFn,
            refreshRagStatus,
        });

        expect(JSON.parse(localStorage.getItem('von_current_org'))).toMatchObject({
            concept_id: '#V#university_of_auckland_strong_ai_lab',
            name: 'University Of Auckland Strong Ai Lab',
        });
        expect(localStorage.getItem('current_user_namespace')).toBe(
            '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
        );
        expect(switchOrganisationFn).toHaveBeenCalledWith(
            '#V#university_of_auckland_strong_ai_lab',
            'University Of Auckland Strong Ai Lab',
        );
        expect(refreshRagStatus).toHaveBeenCalled();
    });
});

