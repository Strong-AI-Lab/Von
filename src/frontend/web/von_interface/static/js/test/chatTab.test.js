import {
    __testOnly_buildThinkingProgressPresentation,
    __testOnly_buildWorkflowMonitorExportPayload,
    __testOnly_renderWorkflowDefinitionsBody,
    __testOnly_setWorkflowShowDesigns,
    __testOnly_buildWorkflowStatusQuery,
    __testOnly_buildLlmDebugMetadata,
    __testOnly_convertInlineQuotedStrongSegmentsToButtons,
    __testOnly_convertQuotedInstructionBlockquotesToButtons,
    __testOnly_convertQuotedInstructionListItemsToButtons,
    __testOnly_convertReplyOptionsListsToButtons,
    __testOnly_deriveLlmDebugWarnings,
    __testOnly_hydrateChatConceptCartouches,
    __testOnly_renderDisplayElementsIntoContainer,
    __testOnly_resetChatConceptMetaCaches,
    formatChatTimestamp,
    sendMessage
} from '../chatTab';

// Mock dependencies to avoid import errors
jest.mock('../apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'test-window-session'),
    postJson: jest.fn(),
    WINDOW_SESSION_HEADER: 'X-Window-Session-ID'
}));
jest.mock('../domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(),
    renderSpanSuggestions: jest.fn()
}));
jest.mock('../utils/sessionScopedStorage.js', () => ({
    getSessionScopedNamespace: jest.fn(),
    getSessionScopedOrgContext: jest.fn()
}));
jest.mock('../utils/textDecorator.js', () => ({
    annotateElementText: jest.fn(),
    applyCartoucheAppearance: jest.fn(),
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn(),
    createVontologyCartouche: jest.fn((conceptId) => {
        const idRaw = String(conceptId ?? '').trim();
        const fullId = idRaw.startsWith('#V#') ? idRaw : `#V#${idRaw}`;
        const button = globalThis.document.createElement('button');
        button.type = 'button';
        button.className = 'vontology-cartouche';
        button.dataset.fullConceptId = fullId;
        button.dataset.conceptId = fullId.startsWith('#V#') ? fullId.slice(3) : fullId;
        button.textContent = fullId;
        return button;
    }),
    getCartoucheAppearanceSettings: jest.fn(() => ({
        useShortestName: false,
        showName: true,
        showId: true,
        showKind: true,
        kindAsBackground: false
    })),
    linkifyVontologyTokensInElement: jest.fn()
}));

describe('formatChatTimestamp', () => {
    // Helper to create a date relative to now
    const createDate = (daysAgo, hours = 12, minutes = 0) => {
        const date = new Date();
        date.setDate(date.getDate() - daysAgo);
        date.setHours(hours, minutes, 0, 0);
        return date;
    };

    test('returns formatted time for today', () => {
        const today = new Date();
        const isoString = today.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).toMatch(/^Today /);
    });

    test('returns formatted time for yesterday', () => {
        const yesterday = createDate(1);
        const isoString = yesterday.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).toMatch(/^Yesterday /);
    });

    test('returns day and time for within a week', () => {
        const threeDaysAgo = createDate(3);
        const isoString = threeDaysAgo.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).not.toContain('Today');
        expect(result).not.toContain('Yesterday');
        // Check for day name (Mon, Tue, etc.)
        const dayName = threeDaysAgo.toLocaleString([], { weekday: 'short' });
        expect(result).toContain(dayName);
    });

    test('returns date and time for older dates', () => {
        const tenDaysAgo = createDate(10);
        const isoString = tenDaysAgo.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).not.toContain('Today');
        expect(result).not.toContain('Yesterday');
        const month = tenDaysAgo.toLocaleString([], { month: 'short' });
        expect(result).toContain(month);
    });
});

describe('workflow status query scoping', () => {
    beforeEach(() => {
        const { getCurrentUserConceptId } = require('../domUtils.js');
        const {
            getSessionScopedNamespace,
            getSessionScopedOrgContext
        } = require('../utils/sessionScopedStorage.js');

        getCurrentUserConceptId.mockReset();
        getSessionScopedNamespace.mockReset();
        getSessionScopedOrgContext.mockReset();
    });

    test('uses namespace-only scoping when namespace is present', () => {
        const { getCurrentUserConceptId } = require('../domUtils.js');
        const {
            getSessionScopedNamespace,
            getSessionScopedOrgContext
        } = require('../utils/sessionScopedStorage.js');

        getSessionScopedNamespace.mockReturnValue('#V#michael_witbrock');
        getSessionScopedOrgContext.mockReturnValue({
            concept_id: '#V#university_of_auckland_strong_ai_lab'
        });
        getCurrentUserConceptId.mockReturnValue('#V#michael_witbrock');

        const query = __testOnly_buildWorkflowStatusQuery({ includeStatusFilter: true });
        const params = new URLSearchParams(query);

        expect(params.get('namespace')).toBe('#V#michael_witbrock');
        expect(params.get('status')).toContain('running');
        expect(params.has('org_id')).toBe(false);
        expect(params.has('user_id')).toBe(false);
    });

    test('falls back to user/org filters when namespace is unavailable', () => {
        const { getCurrentUserConceptId } = require('../domUtils.js');
        const {
            getSessionScopedNamespace,
            getSessionScopedOrgContext
        } = require('../utils/sessionScopedStorage.js');

        getSessionScopedNamespace.mockReturnValue('');
        getSessionScopedOrgContext.mockReturnValue({
            concept_id: '#V#university_of_auckland_strong_ai_lab'
        });
        getCurrentUserConceptId.mockReturnValue('#V#michael_witbrock');

        const query = __testOnly_buildWorkflowStatusQuery();
        const params = new URLSearchParams(query);

        expect(params.has('namespace')).toBe(false);
        expect(params.get('org_id')).toBe('#V#university_of_auckland_strong_ai_lab');
        expect(params.get('user_id')).toBe('#V#michael_witbrock');
    });
});

describe('workflow monitor concept links', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="workflowStatusPanel"></div>
            <div id="workflowStatusBody"></div>
            <button id="workflowStatusRefresh"></button>
            <button id="workflowStatusToggleAvailable"></button>
            <input id="workflowStatusShowDesigns" type="checkbox" />
        `;
        __testOnly_setWorkflowShowDesigns(false);
    });

    test('renders workflow concept links and dispatches concept selection event on click', () => {
        const onSelect = jest.fn();
        document.addEventListener('von:selectConceptById', onSelect);

        __testOnly_renderWorkflowDefinitionsBody([
            {
                workflow_id: '#V#chat_assistant_workflow',
                description: 'Base chat assistant workflow.',
                initial_state: 'completed',
                source: 'built_in'
            }
        ]);

        const links = document.querySelectorAll('.workflow-status-concept-link');
        expect(links.length).toBeGreaterThan(0);

        const nameLink = Array.from(links).find((el) => el.textContent?.includes('chat assistant workflow'));
        expect(nameLink).toBeTruthy();
        expect(nameLink.dataset.conceptId).toBe('#V#chat_assistant_workflow');

        nameLink.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        expect(onSelect).toHaveBeenCalledTimes(1);
        const eventArg = onSelect.mock.calls[0][0];
        expect(eventArg.detail.conceptId).toBe('#V#chat_assistant_workflow');
        expect(eventArg.detail.createConceptTab).toBe(true);

        document.removeEventListener('von:selectConceptById', onSelect);
    });

    test('renders non-executable design artifact label with hyphenation', () => {
        __testOnly_setWorkflowShowDesigns(true);
        __testOnly_renderWorkflowDefinitionsBody([
            {
                workflow_id: '#V#chat_assistant_workflow',
                description: 'Base chat assistant workflow.',
                initial_state: 'completed',
                source: 'built_in',
                is_executable: false,
                executability_reason: 'non_executable_design_artifact',
                executability_detail: 'workflow_has_no_steps'
            }
        ]);

        const badge = document.querySelector('.workflow-status-badge');
        expect(badge).toBeTruthy();
        expect(badge.textContent).toContain('non-executable design artifact');
    });

    test('exports workflow monitor payload with namespace and rendered workflow IDs', () => {
        const { getSessionScopedNamespace } = require('../utils/sessionScopedStorage.js');
        const { getCurrentUserConceptId } = require('../domUtils.js');
        getSessionScopedNamespace.mockReturnValue('#V#michael_witbrock');
        getCurrentUserConceptId.mockReturnValue('#V#michael_witbrock');

        __testOnly_setWorkflowShowDesigns(false);
        __testOnly_renderWorkflowDefinitionsBody([
            {
                workflow_id: '#V#salient_predicate_governance_workflow',
                description: 'Salient workflow.',
                initial_state: '#V#salience_step_identify_type',
                source: 'vontology',
                is_executable: false,
                executability_reason: 'workflow_step_partially_vacuous'
            }
        ]);

        const payload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(payload.namespace_context.namespace).toBe('#V#michael_witbrock');
        expect(payload.namespace_context.user_id).toBe('#V#michael_witbrock');
        expect(payload.monitor_state.mode).toBe('available_workflows');
        expect(payload.monitor_state.show_designs).toBe(false);
        expect(payload.definitions_snapshot.rendered_workflow_ids).toContain('#V#salient_predicate_governance_workflow');
    });

    test('hides design artefacts by default in available workflows list', () => {
        __testOnly_setWorkflowShowDesigns(false);
        __testOnly_renderWorkflowDefinitionsBody([
            {
                workflow_id: '#V#chat_assistant_workflow',
                description: 'Base chat assistant workflow.',
                initial_state: 'completed',
                source: 'built_in',
                is_executable: false,
                executability_reason: 'non_executable_design_artifact'
            },
            {
                workflow_id: '#V#salient_predicate_governance_workflow',
                description: 'Salient workflow.',
                initial_state: '#V#salience_step_identify_type',
                source: 'vontology',
                is_executable: false,
                executability_reason: 'workflow_step_partially_vacuous'
            }
        ]);

        const rows = document.querySelectorAll('.workflow-status-item');
        expect(rows.length).toBe(1);
        expect(document.body.textContent).toContain('salient predicate governance workflow');
        expect(document.body.textContent).not.toContain('chat assistant workflow');
    });

    test('shows design artefacts when show-designs is enabled', () => {
        __testOnly_setWorkflowShowDesigns(true);
        __testOnly_renderWorkflowDefinitionsBody([
            {
                workflow_id: '#V#chat_assistant_workflow',
                description: 'Base chat assistant workflow.',
                initial_state: 'completed',
                source: 'built_in',
                is_executable: false,
                executability_reason: 'non_executable_design_artifact'
            },
            {
                workflow_id: '#V#salient_predicate_governance_workflow',
                description: 'Salient workflow.',
                initial_state: '#V#salience_step_identify_type',
                source: 'vontology',
                is_executable: false,
                executability_reason: 'workflow_step_partially_vacuous'
            }
        ]);

        const rows = document.querySelectorAll('.workflow-status-item');
        expect(rows.length).toBe(2);
        expect(document.body.textContent).toContain('salient predicate governance workflow');
        expect(document.body.textContent).toContain('chat assistant workflow');
    });

    test('shows episodes count and disables episodes button when count is zero', () => {
        __testOnly_setWorkflowShowDesigns(true);
        __testOnly_renderWorkflowDefinitionsBody([
            {
                workflow_id: '#V#salient_predicate_governance_workflow',
                description: 'Salient workflow.',
                initial_state: '#V#salience_step_identify_type',
                source: 'vontology',
                is_executable: false,
                executability_reason: 'workflow_step_partially_vacuous',
                episodes_count: 0
            }
        ]);

        expect(document.body.textContent).toContain('Episodes: 0');
        const episodesButton = document.querySelector('.workflow-status-episodes-btn');
        expect(episodesButton).toBeTruthy();
        expect(episodesButton.disabled).toBe(true);
    });

    test('enables episodes button and renders per-card copy button when episodes exist', () => {
        __testOnly_setWorkflowShowDesigns(true);
        __testOnly_renderWorkflowDefinitionsBody([
            {
                workflow_id: '#V#salient_predicate_governance_workflow',
                description: 'Salient workflow.',
                initial_state: '#V#salience_step_identify_type',
                source: 'vontology',
                is_executable: false,
                executability_reason: 'workflow_step_partially_vacuous',
                episodes_count: 5
            }
        ]);

        expect(document.body.textContent).toContain('Episodes: 5');
        const episodesButton = document.querySelector('.workflow-status-episodes-btn');
        expect(episodesButton).toBeTruthy();
        expect(episodesButton.disabled).toBe(false);

        const copyButton = document.querySelector('.workflow-status-copy-card-json-btn');
        expect(copyButton).toBeTruthy();
        expect(copyButton.textContent).toContain('Copy JSON');
    });

    test('shows scoped vs total episode summary when totals differ', () => {
        __testOnly_setWorkflowShowDesigns(true);
        __testOnly_renderWorkflowDefinitionsBody([
            {
                workflow_id: '#V#salient_predicate_governance_workflow',
                description: 'Salient workflow.',
                initial_state: '#V#salience_step_identify_type',
                source: 'vontology',
                is_executable: false,
                executability_reason: 'workflow_step_partially_vacuous',
                attempts: 12,
                episodes_count: 8
            }
        ]);

        expect(document.body.textContent).toContain('Episodes: 8 scoped (12 total)');
    });
});

describe('thinking liveness presentation', () => {
    test('renders active state with stage details', () => {
        const presentation = __testOnly_buildThinkingProgressPresentation({
            phase: 'tool_execute',
            phase_label: 'Executing tools',
            tool: 'search_knowledge_base',
            liveness_state: 'active',
            idle_ms: 1200
        });

        expect(presentation.livenessState).toBe('active');
        expect(presentation.livenessLabel).toBe('Active');
        expect(presentation.stageText).toContain('Executing tools');
        expect(presentation.stageText).toContain('search_knowledge_base');
        expect(presentation.lastActivityText).toContain('Last activity');
    });

    test('renders stalled state badge and text', () => {
        const presentation = __testOnly_buildThinkingProgressPresentation({
            stage: 'tool_execute',
            stage_label: 'Executing tools',
            subtask: 'mcp__atlassian__search',
            liveness_state: 'stalled',
            activity_idle_ms: 62000
        });

        expect(presentation.livenessState).toBe('stalled');
        expect(presentation.livenessLabel).toBe('Stalled');
        expect(presentation.stageText.startsWith('Stalled:')).toBe(true);
        expect(presentation.lastActivityText).toContain('Last activity');
    });
});

describe('chat abort behaviour', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="scrollableField"></div>
            <div id="loadingIndicator" aria-hidden="true"></div>
            <button id="sendButton"></button>
            <button id="abortButton" style="display:none" aria-hidden="true"></button>
            <textarea id="promptInput"></textarea>
            <input type="checkbox" id="annotationToggle" />
        `;
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('abort restores prompt text and re-enables sending', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = "since we'\n";
        promptInput.focus();
        try {
            promptInput.setSelectionRange(promptInput.value.length, promptInput.value.length);
        } catch (_) {
            // jsdom best-effort
        }

        let generateSignal = null;

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateSignal = options.signal;
                return new Promise((resolve, reject) => {
                    if (generateSignal) {
                        generateSignal.addEventListener('abort', () => {
                            const err = new Error('aborted');
                            err.name = 'AbortError';
                            reject(err);
                        });
                    }
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const sendPromise = sendMessage();

        expect(document.getElementById('sendButton').disabled).toBe(true);
        expect(document.getElementById('abortButton').getAttribute('aria-hidden')).toBe('false');

        document.getElementById('abortButton').click();

        // Let abort propagate through promise chain
        await new Promise((r) => setTimeout(r, 0));

        expect(generateSignal).not.toBeNull();
        expect(generateSignal.aborted).toBe(true);
        expect(document.getElementById('sendButton').disabled).toBe(false);
        expect(document.getElementById('abortButton').getAttribute('aria-hidden')).toBe('true');
        expect(promptInput.value).toBe("since we'\n");

        // Ensure the sendMessage promise resolves without throwing
        await expect(sendPromise).resolves.toBeUndefined();
    });
});

describe('chat cartouche hydration retries', () => {
    beforeEach(() => {
        jest.useFakeTimers();
        __testOnly_resetChatConceptMetaCaches();
        document.body.innerHTML = `
            <div id="root">
                <button class="vontology-cartouche" data-full-concept-id="#V#literary_work">
                    <span class="vontology-cartouche-name">#V#literary_work</span>
                    <span class="vontology-cartouche-id">#V#literary_work</span>
                    <span class="vontology-cartouche-kind type">Type</span>
                </button>
            </div>
        `;
    });

    afterEach(() => {
        jest.useRealTimers();
        jest.restoreAllMocks();
        delete global.fetch;
    });

    async function flushMicrotasks() {
        await Promise.resolve();
        await Promise.resolve();
    }

    test('auto-rehydrates after initial provisional lookup', async () => {
        const fullId = '#V#literary_work';
        let nodeContentCalls = 0;

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                nodeContentCalls += 1;
                if (nodeContentCalls === 1) {
                    return Promise.resolve({ ok: false, status: 404 });
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ display_name: 'Literary Work', kind: 'type' })
                });
            }

            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/search')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ results: [{ id: fullId, kind: 'type' }] })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const root = document.getElementById('root');
        __testOnly_hydrateChatConceptCartouches(root);
        await flushMicrotasks();

        const nameEl = document.querySelector('.vontology-cartouche-name');
        expect(nameEl.textContent).toBe(fullId);

        jest.advanceTimersByTime(300);
        await flushMicrotasks();

        expect(nameEl.textContent).toBe('Literary Work');
    });
});

describe('relation truth-state display elements', () => {
    beforeEach(() => {
        const { createVontologyCartouche } = require('../utils/textDecorator.js');
        createVontologyCartouche.mockClear();
    });

    test('renders grouped asserted and missing relation rows using compact cartouches', () => {
        const container = document.createElement('div');
        const debugData = {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_relation_truth_state',
                        element_type: 'relation_truth_state',
                        channel: 'screen',
                        order: 41,
                        intent: 'truth_state_relation_view',
                        payload: {
                            title: 'Current truth state',
                            groups: [
                                {
                                    label: 'Conference-level',
                                    status: 'asserted',
                                    assertions: [
                                        {
                                            assertion_id: 'a1',
                                            arg1: '#V#michael_witbrock',
                                            predicate: '#V#attended_event',
                                            arg2: '#V#international_ai_cooperation_and_governance_forum_2025_melbourne',
                                            is_asserted: true
                                        }
                                    ]
                                },
                                {
                                    label: 'Missing (should exist)',
                                    status: 'missing_expected',
                                    assertions: [
                                        {
                                            assertion_id: 'a2',
                                            arg1: '#V#michael_witbrock',
                                            predicate: '#V#panelist_in_event',
                                            arg2: '#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne',
                                            is_asserted: false
                                        }
                                    ]
                                }
                            ]
                        },
                        provenance: { source: 'test' }
                    }
                ]
            }
        };

        __testOnly_renderDisplayElementsIntoContainer(container, debugData);

        const section = container.querySelector('.chat-display-elements-relation-truth-state-section');
        expect(section).not.toBeNull();

        const rows = container.querySelectorAll('.chat-display-elements-relation-truth-state-assertion');
        expect(rows.length).toBe(2);
        expect(container.querySelectorAll('.chat-display-elements-relation-truth-state-assertion.not-asserted').length).toBe(1);
        expect(container.textContent).toContain('not currently asserted');

        const { createVontologyCartouche } = require('../utils/textDecorator.js');
        expect(createVontologyCartouche).toHaveBeenCalledTimes(6);
    });
});

describe('kanban display elements', () => {
    test('renders kanban columns/cards with concept and jira links', () => {
        const container = document.createElement('div');
        const debugData = {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_kanban_view',
                        element_type: 'kanban_view',
                        channel: 'screen',
                        order: 33,
                        intent: 'structured_kanban_view',
                        payload: {
                            columns: [
                                { column_id: 'pending', label: 'Pending', order: 10 },
                                { column_id: 'done', label: 'Done', order: 20 }
                            ],
                            cards: [
                                {
                                    card_id: '#V#task_alpha',
                                    title: 'Alpha task',
                                    column_id: 'pending',
                                    priority: 'high',
                                    task_links: [
                                        {
                                            link_type: 'von_task',
                                            target_id: '#V#task_alpha',
                                            label: '#V#task_alpha'
                                        },
                                        {
                                            link_type: 'jira_issue',
                                            target_id: 'JVNAUTOSCI-1181',
                                            label: 'JVNAUTOSCI-1181',
                                            href: 'https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1181'
                                        }
                                    ]
                                }
                            ]
                        },
                        provenance: { source: 'test' }
                    }
                ]
            }
        };

        __testOnly_renderDisplayElementsIntoContainer(container, debugData);

        const section = container.querySelector('.chat-display-elements-kanban-section');
        expect(section).not.toBeNull();

        const columns = container.querySelectorAll('.chat-display-elements-kanban-column');
        expect(columns.length).toBe(2);
        expect(columns[0].textContent).toContain('Pending');

        const cards = container.querySelectorAll('.chat-display-elements-kanban-card');
        expect(cards.length).toBe(1);
        expect(cards[0].textContent).toContain('Alpha task');

        const conceptButton = cards[0].querySelector('.chat-display-elements-concept-link');
        expect(conceptButton).not.toBeNull();
        expect(conceptButton.dataset.conceptId).toBe('#V#task_alpha');

        const jiraLink = cards[0].querySelector('a[href="https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1181"]');
        expect(jiraLink).not.toBeNull();
    });
});

describe('chat insert prompt button behaviour', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
        `;
    });

    test('click inserts and submits by default', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');

        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = '<blockquote><p><strong>"Do the thing"</strong></p></blockquote>';
        __testOnly_convertQuotedInstructionBlockquotesToButtons(root);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        expect(String(promptInput.value)).toContain('Do the thing');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });

    test('shift-click inserts without submitting', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');

        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = '<blockquote><p><strong>"Do the thing"</strong></p></blockquote>';
        __testOnly_convertQuotedInstructionBlockquotesToButtons(root);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        expect(String(promptInput.value)).toContain('Do the thing');
        expect(sendButton.click).toHaveBeenCalledTimes(0);
    });

    test('supports blockquotes with plain quoted text (no strong)', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');

        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = '<blockquote><p>“Create the full representation as specified.”</p></blockquote>';
        __testOnly_convertQuotedInstructionBlockquotesToButtons(root);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();
        expect(btn.textContent).toBe('Create the full representation as specified.');

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        expect(String(promptInput.value)).toContain('Create the full representation as specified.');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });

    test('supports unquoted strong blockquote confirmation prompt', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');

        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML =
            '<blockquote><p><strong>Proceed with creation using verified parents and contribution‑based modelling?</strong></p></blockquote>';
        __testOnly_convertQuotedInstructionBlockquotesToButtons(root);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();
        expect(btn.textContent).toBe('Proceed with creation using verified parents and contribution‑based modelling?');

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        expect(String(promptInput.value)).toContain('Proceed with creation using verified parents and contribution‑based modelling?');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });
});

describe('chat inline insert prompt button behaviour', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
        `;
    });

    test('inline quoted strong (e.g. “Yes”) becomes an insert button', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');

        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = '<p>If you say <strong>“Yes”</strong>, I will do the thing.</p>';
        __testOnly_convertInlineQuotedStrongSegmentsToButtons(root);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();
        expect(btn.textContent).toBe('Yes');

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        expect(String(promptInput.value)).toContain('Yes');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });
});

describe('chat list-item insert prompt button behaviour', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
        `;
    });

    test('quoted bold list item becomes an insert button', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');
        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = `
            <ul>
                <li><strong>“Extract and materialise the full author list as researcher concepts.”</strong></li>
            </ul>
        `;

        __testOnly_convertQuotedInstructionListItemsToButtons(root);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();
        expect(btn.textContent).toBe('Extract and materialise the full author list as researcher concepts.');

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        expect(String(promptInput.value)).toContain('Extract and materialise the full author list as researcher concepts.');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });
});

describe('chat reply options button behaviour', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
        `;
    });

    test('list options become buttons that insert and submit by default', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');
        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = `
            <p>Please reply with one of:</p>
            <ul>
                <li><strong>“Yes, scan all emails from the last 24 hours and update the to-do list.”</strong></li>
                <li><strong>“Scan them, but show me the tasks before adding.”</strong></li>
            </ul>
        `;

        __testOnly_convertReplyOptionsListsToButtons(root);

        const list = root.querySelector('ul');
        expect(list).not.toBeNull();

        const items = Array.from(list.querySelectorAll(':scope > li'));
        expect(items.length).toBe(2);

        const buttons = Array.from(root.querySelectorAll('.chat-insert-prompt-button'));
        expect(buttons.length).toBe(2);

        buttons[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));

        expect(String(promptInput.value)).toContain('Yes, scan all emails from the last 24 hours and update the to-do list.');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });

    test('shift-click inserts without submitting', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');
        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = `
            <p>Please reply with one of:</p>
            <ul>
                <li>"Scan them, but show me the tasks before adding."</li>
            </ul>
        `;

        __testOnly_convertReplyOptionsListsToButtons(root);

        const list = root.querySelector('ul');
        expect(list).not.toBeNull();
        expect(list.querySelectorAll(':scope > li').length).toBe(1);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        expect(String(promptInput.value)).toContain('Scan them, but show me the tasks before adding.');
        expect(sendButton.click).toHaveBeenCalledTimes(0);
    });

    test('supports "tell me how you want to proceed" marker and splits multi-option items', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');
        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = `
            <p>Tell me how you want to proceed:</p>
            <ul>
                <li><strong>“Add both new tasks to today’s to-do list.”</strong></li>
                <li><strong>“Add only #2.” / “Add only #3.”</strong></li>
                <li><strong>“Add them as future / backlog items.”</strong></li>
            </ul>
        `;

        __testOnly_convertReplyOptionsListsToButtons(root);

        const list = root.querySelector('ul');
        expect(list).not.toBeNull();
        expect(list.querySelectorAll(':scope > li').length).toBe(4);

        const buttons = Array.from(root.querySelectorAll('.chat-insert-prompt-button'));
        expect(buttons.map((b) => b.textContent)).toEqual([
            "Add both new tasks to today’s to-do list.",
            'Add only #2.',
            'Add only #3.',
            'Add them as future / backlog items.'
        ]);

        buttons[2].dispatchEvent(new MouseEvent('click', { bubbles: true }));
        expect(String(promptInput.value)).toContain('Add only #3.');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });
});

describe('LLM debug warnings (presenter channel health)', () => {
    test('flags missing spoken channel when screen exists', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            presenter_channels: {
                screen: 'On-screen content',
                spoken: null,
                format: 'tagged_blocks_v1'
            }
        });

        expect(warnings).toContain(
            'Presenter output missing spoken channel; text-to-speech will fall back to screen text.'
        );
    });

    test('flags missing screen channel when spoken exists', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            presenter_channels: {
                screen: null,
                spoken: 'Talk track',
                format: 'tagged_blocks_v1'
            }
        });

        expect(warnings).toContain(
            'Presenter output missing screen channel; display will fall back to spoken text.'
        );
    });

    test('uses display elements contract when presenter channels are missing', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_text',
                        element_type: 'text_block',
                        channel: 'screen',
                        intent: 'primary_response',
                        payload: { text: 'On-screen content' }
                    },
                    {
                        element_id: 'spoken_text',
                        element_type: 'text_block',
                        channel: 'spoken',
                        intent: 'narration',
                        payload: { text: 'Talk track' }
                    }
                ]
            }
        });

        expect(warnings).not.toContain(
            'Presenter output missing spoken channel; text-to-speech will fall back to screen text.'
        );
        expect(warnings).not.toContain(
            'Presenter output missing screen channel; display will fall back to spoken text.'
        );
    });

    test('flags failed spoken backfill attempt', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            presenter_channels: {
                screen: 'On-screen content',
                spoken: '',
                format: 'narration_fallback_v1'
            },
            spoken_backfill_second_pass_attempted: true,
            spoken_backfill_second_pass_reason: 'missing_spoken'
        });

        expect(warnings).toContain(
            'Spoken backfill attempted but spoken channel is still missing (missing_spoken).'
        );
    });

    test('flags when response claims more operations than executed tools', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            response: 'Done — 9 links added.',
            tool_invocations: [
                { method: 'add_relationship', arguments: { i: 0 } },
                { method: 'add_relationship', arguments: { i: 1 } },
                { method: 'add_relationship', arguments: { i: 2 } },
                { method: 'add_relationship', arguments: { i: 3 } }
            ]
        });

        expect(warnings).toContain(
            'Response claims 9 operations, but only 4 tool invocations were recorded.'
        );
    });

    test('surfaces tool-call parse errors from tool invocations', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            response: 'Tool call was not executed due to an MCP serialisation error.',
            tool_invocations: [
                {
                    method: '__tool_call_parse_error__',
                    error: 'Tool call was not executed: multiple JSON values were emitted in one response.'
                }
            ]
        });

        expect(warnings).toContain(
            'Tool call was not executed: multiple JSON values were emitted in one response.'
        );
    });

    test('flags when max tool invocation cap is hit but response still looks tool-shaped', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            response: '{"action":"call_tool","tool":"test","payload":{}}',
            tool_invocations: [{ method: 'a' }, { method: 'b' }],
            internal_mcp: {
                execution_caps: {
                    max_tool_invocations: 2,
                    tool_batch_cap: 1
                }
            }
        });

        expect(warnings).toContain(
            'Reached max tool invocation limit (2); additional tool calls were not executed.'
        );
    });
});

describe('LLM debug popup metadata (internal MCP caps + usage)', () => {
    test('includes internal MCP caps and usage against them', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            tool_invocations: [{}, {}, {}],
            internal_mcp: {
                execution_caps: {
                    max_tool_invocations: 5,
                    tool_batch_cap: 2
                },
                tool_use_progress: {
                    enabled: true,
                    request_id: 'req-123'
                }
            }
        });

        expect(metadata.internal_mcp).toBeTruthy();
        expect(metadata.internal_mcp.execution_caps).toEqual({
            max_tool_invocations: 5,
            tool_batch_cap: 2
        });

        expect(metadata.internal_mcp.usage_against_caps).toEqual({
            tool_invocations_done: 3,
            tool_invocations_cap: 5,
            tool_invocations_remaining: 2,
            tool_invocations_exceeded: false,
            tool_batch_cap: 2,
            estimated_batches: 2,
            estimated_last_batch_size: 1
        });

        expect(metadata.internal_mcp.tool_use_progress).toEqual({
            enabled: true,
            request_id: 'req-123'
        });
    });

    test('includes render plan provenance summary when present', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                attempted: true,
                success: true,
                reason: 'resolved',
                render_mode: 'spoken+screen',
                should_narrate: true,
                selection_rationale: 'Selected table and workflow views for task-focused output.',
                request_payload_object_kind: 'concept',
                request_payload_selected_concept_id: '#V#task_123',
                selected_renderer_ids: ['#V#renderer_a', '#V#renderer_b'],
                selected_renderer_types: ['table', 'workflow'],
                selected_modalities: ['screen', 'narrated_audio'],
                screen_element_mapping_mode: 'selected_renderer_types',
                screen_element_reason_codes: ['renderer_screen_elements:selected_renderer_types'],
                screen_element_families: ['table', 'workflow'],
                screen_table_record_sets: [
                    {
                        element_id: 'screen_task_table',
                        provenance: {
                            source_tool: 'task_list',
                            record_family: 'tasks'
                        }
                    }
                ],
                screen_workflow_elements: [
                    {
                        element_id: 'screen_workflow_view',
                        provenance: {
                            source_tools: ['workflow_list_instances', 'workflow_get_instance'],
                            record_family: 'workflow_instances'
                        }
                    }
                ]
            }
        });

        expect(metadata.render_plan).toMatchObject({
            enabled: true,
            attempted: true,
            success: true,
            reason: 'resolved',
            selection_rationale: 'Selected table and workflow views for task-focused output.',
            render_mode: 'spoken+screen',
            should_narrate: true,
            request_payload_object_kind: 'concept',
            request_payload_selected_concept_id: '#V#task_123',
            selected_renderer_count: 2,
            selected_renderer_ids: ['#V#renderer_a', '#V#renderer_b'],
            selected_renderer_types: ['table', 'workflow'],
            selected_modalities: ['screen', 'narrated_audio'],
            screen_element_mapping_mode: 'selected_renderer_types',
            screen_element_reason_codes: ['renderer_screen_elements:selected_renderer_types'],
            screen_element_families: ['table', 'workflow'],
            source_tools: ['task_list', 'workflow_list_instances', 'workflow_get_instance'],
            record_families: ['tasks', 'workflow_instances'],
            screen_table_record_set_count: 1,
            screen_workflow_element_count: 1
        });
    });

    test('includes kanban render plan provenance counts when present', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                attempted: true,
                success: true,
                reason: 'resolved',
                selected_renderer_ids: ['#V#renderer_kanban'],
                selected_renderer_types: ['kanban'],
                screen_element_families: ['kanban_view'],
                screen_kanban_elements: [
                    {
                        element_id: 'screen_kanban_view',
                        provenance: {
                            source_tools: ['task_list', 'workflow_list_instances'],
                            record_family: 'tasks_and_workflows'
                        }
                    }
                ]
            }
        });

        expect(metadata.render_plan).toMatchObject({
            selected_renderer_types: ['kanban'],
            screen_element_families: ['kanban_view'],
            screen_kanban_element_count: 1,
            source_tools: ['task_list', 'workflow_list_instances'],
            record_families: ['tasks_and_workflows']
        });
    });

    test('falls back to legacy selected_renderer_definition_ids for historical payloads', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                reason: 'resolved',
                selected_renderer_definition_ids: ['#V#legacy_renderer']
            }
        });

        expect(metadata.render_plan.selected_renderer_ids).toEqual(['#V#legacy_renderer']);
        expect(metadata.render_plan.selected_renderer_count).toBe(1);
    });

    test('surfaces fallback diagnostics when resolver fails', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                attempted: true,
                success: false,
                reason: 'resolver_unsuccessful',
                resolver_error_code: 'no_renderer_match',
                resolver_suggestions: ['Add a renderer for transient microtheory objects.']
            }
        });

        expect(metadata.render_plan).toMatchObject({
            attempted: true,
            success: false,
            reason: 'resolver_unsuccessful',
            resolver_error_code: 'no_renderer_match',
            resolver_suggestions: ['Add a renderer for transient microtheory objects.']
        });
    });

    test('summarises turn diagnostics events for non-LLM failures', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'diagnostic',
            messages: [],
            turn_diagnostics: {
                events: [
                    { type: 'file_upload_failure', detail: 'background upload task failed' }
                ]
            }
        });

        expect(metadata.turn_diagnostics).toEqual({
            event_count: 1,
            categories: ['file_upload_failure'],
            latest_event_type: 'file_upload_failure'
        });
    });
});
