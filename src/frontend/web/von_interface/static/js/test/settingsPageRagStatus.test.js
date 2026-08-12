import {
    __testOnly_applyStoredSelection,
    __testOnly_buildPersistedLlmSelections,
    __testOnly_buildServerDefaultLlmPayload,
    __testOnly_buildStoredUserContextFromOption,
    __testOnly_formatGmailOAuthStoredStatus,
    __testOnly_formatServerDefaultSummary,
    __testOnly_formatRagSummaryForSettings,
    __testOnly_getPreferredRagNamespace,
    __testOnly_normaliseGmailOutboundRateLimitSettings,
    __testOnly_normaliseInternalMcpCapSettings,
    __testOnly_populateGmailOutboundRateLimitInputs,
    __testOnly_populateInternalMcpCapInputs,
    __testOnly_readGmailOutboundRateLimitSettingsFromForm,
    __testOnly_readInternalMcpCapSettingsFromForm,
    __testOnly_populateServerDefaultLlmForm,
    __testOnly_readRuntimeModelSettingFromForm,
    __testOnly_resolveActiveLlmFromSelections,
    __testOnly_resolveDisplayedProviderModels,
    __testOnly_resolvePersistedActiveLlm,
    __testOnly_renderRuntimeModelSummaries,
    __testOnly_setModelScopeState,
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

    test('formats Gmail OAuth status as stored-token state, not live access success', () => {
        expect(__testOnly_formatGmailOAuthStoredStatus({
            has_tokens: true,
            authorised_email: 'agent@example.test',
            expires_at: '2026-05-17T21:00:00+00:00',
        })).toBe(
            'stored tokens for agent@example.test (expires 2026-05-17T21:00:00+00:00); live access untested',
        );
        expect(__testOnly_formatGmailOAuthStoredStatus({ has_tokens: false })).toBe('not authorised');
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

    test('does not implicitly enable an inactive configured OpenAI model', () => {
        document.body.innerHTML = `
            <select id="openaiModelSelect">
                <option value="gpt-5.4-mini" selected>gpt-5.4-mini</option>
            </select>
            <select id="globalModelSelect">
                <option value="http://127.0.0.1:11434:gemma4:31b" data-host-url="http://127.0.0.1:11434" data-model-name="gemma4:31b" selected>gemma4:31b</option>
            </select>
        `;

        const result = __testOnly_buildPersistedLlmSelections({
            localModelPreference: {
                activeSource: 'ollama',
                requestedLlm: {
                    provider: 'ollama',
                    model: 'gemma4:31b',
                    host: 'http://127.0.0.1:11434',
                },
            },
        });

        expect(result).toEqual([
            { provider: 'ollama', model: 'gemma4:31b', host: 'http://127.0.0.1:11434' },
        ]);
    });

    test('preserves only explicit workflow alternatives behind the primary', () => {
        const result = __testOnly_buildPersistedLlmSelections({
            primary: {
                provider: 'ollama',
                model: 'gemma4:31b',
                host: 'http://127.0.0.1:11434',
            },
            enabledAlternatives: [
                {
                    provider: 'openai',
                    model: 'gpt-5.4-mini',
                    model_parameters: { reasoning_effort: 'low' },
                },
                { provider: 'gemini', model: 'gemini-2.5-pro' },
            ],
            localModelPreference: { requestedLlm: null },
        });

        expect(result).toEqual([
            { provider: 'ollama', model: 'gemma4:31b', host: 'http://127.0.0.1:11434' },
            {
                provider: 'openai',
                model: 'gpt-5.4-mini',
                model_parameters: { reasoning_effort: 'low' },
            },
            { provider: 'gemini', model: 'gemini-2.5-pro' },
        ]);
    });

    test('persists the active LLM from the current local Ollama selection', () => {
        const result = __testOnly_resolvePersistedActiveLlm(
            [
                { provider: 'openai', model: 'gpt-5.4-mini' },
                { provider: 'ollama', model: 'gemma4:31b', host: 'http://127.0.0.1:11434' },
            ],
            {
                localModelPreference: {
                    requestedLlm: {
                        provider: 'ollama',
                        model: 'gemma4:31b',
                        host: 'http://127.0.0.1:11434',
                    },
                },
                currentResolved: { provider: 'openai', model: 'gpt-5.4-mini' },
            },
        );

        expect(result).toEqual({
            provider: 'ollama',
            model: 'gemma4:31b',
            host: 'http://127.0.0.1:11434',
        });
    });

    test('server default payload retains the persisted shared default when the form is blank', () => {
        document.body.innerHTML = `
            <select id="serverDefaultLlmProvider">
                <option value="" selected>Keep saved shared default</option>
            </select>
            <input id="serverDefaultLlmModel" value="" />
            <input id="serverDefaultLlmHost" value="" />
        `;

        const payload = __testOnly_buildServerDefaultLlmPayload({
            persisted: {
                provider: 'ollama',
                model: 'qwen3:8b',
                host: 'http://localhost:11434',
            },
        });

        expect(payload).toEqual({
            provider: 'ollama',
            model: 'qwen3:8b',
            host: 'http://localhost:11434',
        });
    });

    test('server default Save requires an explicit value when no shared default exists', () => {
        document.body.innerHTML = `
            <select id="serverDefaultLlmProvider"><option value="" selected>Keep saved shared default</option></select>
            <input id="serverDefaultLlmModel" value="" />
            <input id="serverDefaultLlmHost" value="" />
        `;

        expect(() => __testOnly_buildServerDefaultLlmPayload({
            strictFromUi: true,
            persisted: null,
        })).toThrow('Set an explicit provider and model');
    });

    test('server default form stays blank when there is no persisted server default', () => {
        document.body.innerHTML = `
            <select id="serverDefaultLlmProvider">
                <option value="" selected>Keep saved shared default</option>
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

    test('server default summary requires an explicit shared value when none is persisted', () => {
        expect(
            __testOnly_formatServerDefaultSummary({
                persisted: null,
                explicitFormEntry: null,
            }),
        ).toBe(
            'Server default not saved. Enter an explicit provider and model.',
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

    test('runtime status repaint keeps the persisted shared server default label', () => {
        document.body.innerHTML = `
            <select id="serverDefaultLlmProvider">
                <option value="ollama" selected>Ollama</option>
            </select>
            <input id="serverDefaultLlmModel" value="qwen3:8b" />
            <input id="serverDefaultLlmHost" value="http://127.0.0.1:11434" />
            <div id="serverDefaultLlmSummary"></div>
        `;
        __testOnly_setModelScopeState({
            serverDefaultLlm: {
                provider: 'ollama',
                model: 'qwen3:8b',
                host: 'http://127.0.0.1:11434',
            },
        });

        __testOnly_renderRuntimeModelSummaries();

        expect(document.getElementById('serverDefaultLlmSummary').textContent).toBe(
            'Server default: ollama:qwen3:8b @ http://127.0.0.1:11434',
        );
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

    test('Gmail outbound limits default disabled and clamp to the backend contract', () => {
        expect(__testOnly_normaliseGmailOutboundRateLimitSettings({})).toEqual({
            schema_version: 'gmail_outbound_rate_limits.v1',
            enabled: false,
            max_messages_per_10_minutes: 5,
            max_messages_per_day: 25,
            max_recipients_per_message: 10,
            max_recipient_deliveries_per_day: 50,
        });

        expect(__testOnly_normaliseGmailOutboundRateLimitSettings({
            enabled: true,
            max_messages_per_10_minutes: 999,
            max_messages_per_day: 9999,
            max_recipients_per_message: 100,
            max_recipient_deliveries_per_day: 4,
        })).toEqual({
            schema_version: 'gmail_outbound_rate_limits.v1',
            enabled: true,
            max_messages_per_10_minutes: 100,
            max_messages_per_day: 1000,
            max_recipients_per_message: 4,
            max_recipient_deliveries_per_day: 4,
        });
    });

    test('Gmail outbound limit form counts messages and recipient deliveries separately', () => {
        document.body.innerHTML = `
            <input type="checkbox" id="gmailOutboundEnabled" checked />
            <input id="gmailOutboundMaxMessagesPer10Minutes" value="4" />
            <input id="gmailOutboundMaxMessagesPerDay" value="20" />
            <input id="gmailOutboundMaxRecipientsPerMessage" value="8" />
            <input id="gmailOutboundMaxRecipientDeliveriesPerDay" value="40" />
        `;

        expect(__testOnly_readGmailOutboundRateLimitSettingsFromForm()).toEqual({
            schema_version: 'gmail_outbound_rate_limits.v1',
            enabled: true,
            max_messages_per_10_minutes: 4,
            max_messages_per_day: 20,
            max_recipients_per_message: 8,
            max_recipient_deliveries_per_day: 40,
        });
    });

    test('Gmail outbound limit inputs show the effective admin policy', () => {
        document.body.innerHTML = `
            <input type="checkbox" id="gmailOutboundEnabled" />
            <input id="gmailOutboundMaxMessagesPer10Minutes" />
            <input id="gmailOutboundMaxMessagesPerDay" />
            <input id="gmailOutboundMaxRecipientsPerMessage" />
            <input id="gmailOutboundMaxRecipientDeliveriesPerDay" />
            <div id="gmailOutboundRateLimitStatus"></div>
        `;

        __testOnly_populateGmailOutboundRateLimitInputs({
            gmail_outbound_rate_limits: {
                enabled: true,
                max_messages_per_10_minutes: 3,
                max_messages_per_day: 15,
                max_recipients_per_message: 5,
                max_recipient_deliveries_per_day: 30,
            },
        });

        expect(document.getElementById('gmailOutboundEnabled').checked).toBe(true);
        expect(document.getElementById('gmailOutboundMaxMessagesPer10Minutes').value).toBe('3');
        expect(document.getElementById('gmailOutboundMaxMessagesPerDay').value).toBe('15');
        expect(document.getElementById('gmailOutboundMaxRecipientsPerMessage').value).toBe('5');
        expect(document.getElementById('gmailOutboundMaxRecipientDeliveriesPerDay').value).toBe('30');
        expect(document.getElementById('gmailOutboundRateLimitStatus').textContent).toContain('enabled');
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
