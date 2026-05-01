import {
    __testOnly_applyStoredSelection,
    __testOnly_buildServerDefaultLlmPayload,
    __testOnly_buildStoredUserContextFromOption,
    __testOnly_formatServerDefaultSummary,
    __testOnly_formatRagSummaryForSettings,
    __testOnly_getPreferredRagNamespace,
    __testOnly_normaliseInternalMcpCapSettings,
    __testOnly_populateInternalMcpCapInputs,
    __testOnly_readInternalMcpCapSettingsFromForm,
    __testOnly_populateServerDefaultLlmForm,
    __testOnly_readRuntimeModelSettingFromForm,
    __testOnly_resolveActiveLlmFromSelections,
    __testOnly_resolveDisplayedProviderModels,
    __testOnly_syncInitialScopedSelections,
    __testOnly_setupInternalMcpCapAutoSave,
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

    test('prefers an explicit provider switch over the previously resolved model', () => {
        const result = __testOnly_resolveActiveLlmFromSelections(
            [
                { provider: 'openai', model: 'gpt-5.4-mini' },
                { provider: 'ollama', model: 'llama3.1:8b' },
            ],
            'openai',
            { provider: 'ollama', model: 'llama3.1:8b' },
        );

        expect(result).toEqual({ provider: 'openai', model: 'gpt-5.4-mini' });
    });

    test('server default payload falls back to the current browser chat selection when the form is blank', () => {
        document.body.innerHTML = `
            <select id="serverDefaultLlmProvider">
                <option value="" selected>Use current chat model on save</option>
            </select>
            <input id="serverDefaultLlmModel" value="" />
            <input id="serverDefaultLlmHost" value="" />
        `;

        const payload = __testOnly_buildServerDefaultLlmPayload({
            localModelPreference: {
                requestedLlm: {
                    provider: 'ollama',
                    model: 'gemma4:26b',
                    host: 'http://localhost:11434',
                },
            },
            currentResolved: null,
        });

        expect(payload).toEqual({
            provider: 'ollama',
            model: 'gemma4:26b',
            host: 'http://localhost:11434',
        });
    });

    test('server default form stays blank when there is no persisted server default', () => {
        document.body.innerHTML = `
            <select id="serverDefaultLlmProvider">
                <option value="" selected>Use current chat model on save</option>
                <option value="ollama">Ollama</option>
            </select>
            <input id="serverDefaultLlmModel" value="stale-model" />
            <input id="serverDefaultLlmHost" value="http://stale-host:11434" />
        `;

        __testOnly_populateServerDefaultLlmForm(null);

        expect(document.getElementById('serverDefaultLlmProvider').value).toBe('');
        expect(document.getElementById('serverDefaultLlmModel').value).toBe('');
        expect(document.getElementById('serverDefaultLlmHost').value).toBe('');
    });

    test('server default summary distinguishes unsaved fallback from persisted value', () => {
        expect(
            __testOnly_formatServerDefaultSummary({
                persisted: null,
                explicitFormEntry: null,
                fallback: {
                    provider: 'ollama',
                    model: 'gemma4:26b',
                    host: 'http://127.0.0.1:11434',
                },
            }),
        ).toBe(
            'Server default not saved. Save will snapshot current chat selection: ollama:gemma4:26b @ http://127.0.0.1:11434',
        );

        expect(
            __testOnly_formatServerDefaultSummary({
                persisted: {
                    provider: 'ollama',
                    model: 'gemma4:26b',
                    host: 'http://127.0.0.1:11434',
                },
            }),
        ).toBe('Server default: ollama:gemma4:26b @ http://127.0.0.1:11434');
    });

    test('runtime model form reader returns explicit payload for a configured embedder', () => {
        document.body.innerHTML = `
            <select id="ragEmbedderMode">
                <option value="inherit">Inherit</option>
                <option value="explicit" selected>Explicit</option>
            </select>
            <select id="ragEmbedderProvider">
                <option value="">Choose provider</option>
                <option value="openai" selected>OpenAI</option>
            </select>
            <input id="ragEmbedderModel" value="text-embedding-3-small" />
            <input id="ragEmbedderHost" value="" />
        `;

        expect(__testOnly_readRuntimeModelSettingFromForm('ragEmbedder')).toEqual({
            mode: 'explicit',
            provider: 'openai',
            model: 'text-embedding-3-small',
        });
    });

    test('runtime model form reader allows disabled RAG llm mode', () => {
        document.body.innerHTML = `
            <select id="ragLlmMode">
                <option value="inherit">Inherit</option>
                <option value="explicit">Explicit</option>
                <option value="disabled" selected>Disabled</option>
            </select>
            <select id="ragLlmProvider">
                <option value="">Choose provider</option>
            </select>
            <input id="ragLlmModel" value="" />
            <input id="ragLlmHost" value="" />
        `;

        expect(__testOnly_readRuntimeModelSettingFromForm('ragLlm', { allowDisabled: true })).toEqual({
            mode: 'disabled',
        });
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
                <option value="#V#university_of_auckland_strong_ai_lab" data-role="admin" selected>
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

    test('placeholder user option is treated as no selection rather than stored user context', () => {
        document.body.innerHTML = `
            <select id="currentUserSelect">
                <option value="" selected>-- No user selected --</option>
            </select>
        `;

        const option = document.querySelector('#currentUserSelect option');
        expect(__testOnly_buildStoredUserContextFromOption(option)).toBeNull();
    });

    test('internal MCP caps default and clamp to the UI/backend contract', () => {
        expect(__testOnly_normaliseInternalMcpCapSettings({})).toEqual({
            internal_mcp_max_tool_invocations: 100,
            internal_mcp_tool_batch_cap: 10,
        });

        expect(__testOnly_normaliseInternalMcpCapSettings({
            internal_mcp_max_tool_invocations: 999,
            internal_mcp_tool_batch_cap: 99,
        })).toEqual({
            internal_mcp_max_tool_invocations: 500,
            internal_mcp_tool_batch_cap: 20,
        });
    });

    test('internal MCP cap form values can persist 100 and 10', () => {
        document.body.innerHTML = `
            <input id="internalMcpMaxToolInvocations" value="100" />
            <input id="internalMcpToolBatchCap" value="10" />
        `;

        expect(__testOnly_readInternalMcpCapSettingsFromForm()).toEqual({
            internal_mcp_max_tool_invocations: 100,
            internal_mcp_tool_batch_cap: 10,
        });
    });

    test('internal MCP cap inputs are populated with canonical defaults', () => {
        document.body.innerHTML = `
            <input id="internalMcpMaxToolInvocations" value="" />
            <input id="internalMcpToolBatchCap" value="" />
        `;

        __testOnly_populateInternalMcpCapInputs({});

        expect(document.getElementById('internalMcpMaxToolInvocations').value).toBe('100');
        expect(document.getElementById('internalMcpToolBatchCap').value).toBe('10');
    });

    test('internal MCP cap inputs auto-save when changed', () => {
        document.body.innerHTML = `
            <input id="internalMcpMaxToolInvocations" value="100" />
            <input id="internalMcpToolBatchCap" value="10" />
        `;
        const saveFn = jest.fn().mockResolvedValue(true);

        __testOnly_setupInternalMcpCapAutoSave({ saveFn });
        document
            .getElementById('internalMcpMaxToolInvocations')
            .dispatchEvent(new Event('change'));

        expect(saveFn).toHaveBeenCalledTimes(1);
    });

    test('stored browser-test user is re-injected and selected when the dropdown is rebuilt without it', () => {
        document.body.innerHTML = `
            <select id="currentUserSelect">
                <option value="" selected>-- No user selected --</option>
                <option data-id="user-1" data-concept-id="#V#michael_witbrock">Michael Witbrock</option>
            </select>
        `;

        const storedUser = {
            id: null,
            concept_id: '#V#zhan_von_witbrock',
            name: 'Zhan von Witbrock',
        };

        const applied = __testOnly_applyStoredSelection('currentUserSelect', storedUser);
        const select = document.getElementById('currentUserSelect');

        expect(applied).toEqual(storedUser);
        expect(select.selectedOptions[0].dataset.conceptId).toBe('#V#zhan_von_witbrock');
        expect([...select.options].map((option) => option.textContent)).toContain('Zhan von Witbrock');
    });

    test('initial scoped selection sync preserves stored browser-test user when the visible select is still on the placeholder option', async () => {
        localStorage.setItem(
            'von_current_user',
            JSON.stringify({ concept_id: '#V#zhan_von_witbrock', name: 'Zhan von Witbrock' }),
        );
        sessionStorage.setItem(
            'von_current_user',
            JSON.stringify({ concept_id: '#V#zhan_von_witbrock', name: 'Zhan von Witbrock' }),
        );
        document.body.innerHTML = `
            <select id="currentUserSelect">
                <option value="" selected>-- No user selected --</option>
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
            namespace: '#V#zhan_von_witbrock',
        });
        const switchOrganisationFn = jest.fn().mockResolvedValue({
            namespace: '#V#zhan_von_witbrock@university_of_auckland_strong_ai_lab',
        });
        const refreshRagStatus = jest.fn();

        await __testOnly_syncInitialScopedSelections({
            setUserConcept,
            switchOrganisationFn,
            refreshRagStatus,
        });

        expect(JSON.parse(localStorage.getItem('von_current_user'))).toMatchObject({
            concept_id: '#V#zhan_von_witbrock',
            name: 'Zhan von Witbrock',
        });
        expect(setUserConcept).toHaveBeenCalledWith('#V#zhan_von_witbrock');
        expect(localStorage.getItem('current_user_namespace')).toBe(
            '#V#zhan_von_witbrock@university_of_auckland_strong_ai_lab',
        );
    });
});

