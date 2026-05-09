import {
    __testOnly_buildThinkingProgressPresentation,
    __testOnly_buildThinkingCardProgressViewModel,
    __testOnly_buildThinkingDiagnosticsPayload,
    __testOnly_buildDiagnosticsExportRequestPayload,
    __testOnly_buildWorkflowMonitorExportPayload,
    __testOnly_buildWorkflowMonitorLocatorPayload,
    __testOnly_loadChatHistory,
    __testOnly_refreshChatSessionTabs,
    __testOnly_refreshAvailableWorkflowDefinitions,
    __testOnly_refreshWorkflowCapabilityIndexStatus,
    __testOnly_refreshWorkflowStatusSnapshot,
    __testOnly_resetHistoryUiState,
    __testOnly_resetWorkflowDefinitionsState,
    __testOnly_resetWorkflowStatusState,
    __testOnly_formatAbsoluteTimestamp,
    __testOnly_formatThinkingEtaText,
    __testOnly_renderWorkflowDefinitionsBody,
    __testOnly_setActiveChatSession,
    __testOnly_setDisplayedHistorySession,
    __testOnly_setUnambiguousTimestampTooltip,
    __testOnly_setWorkflowShowDesigns,
    __testOnly_buildWorkflowStatusQuery,
    __testOnly_buildWorkflowStatusStreamQuery,
    __testOnly_applyWorkflowStatusUpdate,
    __testOnly_resetWorkflowCapabilityIndexState,
    __testOnly_setWorkflowCapabilityIndexPayload,
    __testOnly_setWorkflowMonitorGloballyFurled,
    __testOnly_buildLlmDebugMetadata,
    __testOnly_extractImageFilesFromClipboardEvent,
    __testOnly_convertInlineQuotedStrongSegmentsToButtons,
    __testOnly_convertQuotedInstructionBlockquotesToButtons,
    __testOnly_convertQuotedInstructionListItemsToButtons,
    __testOnly_clearLatestUnreadJumpState,
    __testOnly_convertReplyOptionsListsToButtons,
    __testOnly_deriveLlmDebugWarnings,
    __testOnly_hydrateChatConceptCartouches,
    __testOnly_jumpToLatestUnreadBoundary,
    __testOnly_shouldMaintainSharedConversationStreamForInputs,
    __testOnly_shouldRunSharedSessionBadgePollingForInputs,
    __testOnly_renderDisplayElementsIntoContainer,
    __testOnly_ensureScrollToEndButton,
    __testOnly_scrollConversationToEnd,
    __testOnly_setLatestUnreadBoundary,
    __testOnly_showNewSharedMessagesIndicator,
    __testOnly_appendMessage,
    __testOnly_updateScrollToEndButtonVisibility,
    __testOnly_reduceThinkingCardDisplayState,
    __testOnly_copyActiveThinkingDiagnostics,
    __testOnly_shouldAcceptThinkingProgressUpdate,
    __testOnly_getThinkingProgressPollFetchTimeoutMs,
    __testOnly_setThinkingCardRequests,
    __testOnly_setThinkingState,
    __testOnly_normaliseThinkingActivityHistory,
    __testOnly_renderThinkingCardBodyHTML,
    __testOnly_refreshThinkingCardProgressUi,
    __testOnly_getThinkingCardMode,
    __testOnly_bindConceptSelectionClicks,
    __testOnly_bindThinkingCardControls,
    __testOnly_updateThinkingCardMeta,
    __testOnly_persistThinkingCardBodyHeightFromDom,
    __testOnly_syncThinkingCanonicalHistoriesFromProgress,
    __testOnly_syncThinkingCanonicalStateFromTurnExecutionDiagnostics,
    __testOnly_resetChatConceptMetaCaches,
    __testOnly_createChatSession,
    __testOnly_buildConversationLlmTelemetryLocatorPayload,
    __testOnly_buildConversationTelemetryAccessPayload,
    __testOnly_buildConversationLlmTelemetryPayload,
    __testOnly_buildConversationTelemetryExportPayload,
    __testOnly_copyConversationInfoToClipboard,
    __testOnly_clearLlmDebugData,
    __testOnly_resetChatRequestState,
    __testOnly_setSessionTabsCache,
    __testOnly_setTranscriptTurns,
    setLlmDebugDataForTurn,
    formatChatTimestamp,
    sendMessage,
    switchToChatSession
} from '../chatTab.js';
import { initializePromptCartoucheOverlay } from '../components/promptCartoucheOverlay.js';

// Mock dependencies to avoid import errors
jest.mock('../apiService.js', () => {
    const actual = jest.requireActual('../apiService.js');
    return {
        ...actual,
        annotateTurn: jest.fn(),
        getUserContext: jest.fn(),
        getWindowSessionId: jest.fn(() => 'test-window-session'),
        postJson: jest.fn(),
        WINDOW_SESSION_HEADER: 'X-Window-Session-ID'
    };
});
jest.mock('../domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(),
    renderSpanSuggestions: jest.fn()
}));
jest.mock('../utils/sessionScopedStorage.js', () => ({
    getSessionScopedNamespace: jest.fn(),
    getSessionScopedOrgContext: jest.fn()
}));

beforeEach(() => {
    __testOnly_resetChatRequestState();
});
jest.mock('../utils/textDecorator.js', () => ({
    annotateElementText: jest.fn(),
    applyCartoucheAppearance: jest.fn(),
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn(),
    createVontologyAliasCartouche: jest.fn((conceptId, aliasText, meta = null) => {
        const idRaw = String(conceptId ?? '').trim();
        const fullId = idRaw.startsWith('#V#') ? idRaw : `#V#${idRaw}`;
        const button = globalThis.document.createElement('button');
        button.type = 'button';
        button.className = 'vontology-cartouche vontology-inline-alias-cartouche';
        button.dataset.fullConceptId = fullId;
        button.dataset.conceptId = fullId.startsWith('#V#') ? fullId.slice(3) : fullId;
        if (aliasText) button.dataset.aliasText = String(aliasText);
        button.textContent = meta?.name || fullId;
        return button;
    }),
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
    findPotentialConceptAliasMatches: jest.fn(() => []),
    normalisePotentialConceptId: jest.fn((value) => {
        let raw = String(value ?? '').trim();
        if (!raw) return '';
        if (/^[Vv]#/.test(raw)) raw = `#${raw}`;
        if (raw.startsWith('#v#')) raw = `#V#${raw.slice(3)}`;
        if (!raw.startsWith('#V#')) return '';
        raw = raw.replace(/[.,:;!?)}\]…]+$/g, '');
        return raw.startsWith('#V#') && raw.length > 3 ? raw : '';
    }),
    normalisePotentialConceptAlias: jest.fn((value) => {
        const raw = String(value ?? '').trim();
        return raw && /^[A-Za-z][A-Za-z0-9_./:–—-]*$/.test(raw) ? raw : '';
    }),
    replaceTextNodeWithVontologyAliasCartouches: jest.fn(() => []),
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

describe('clipboard image extraction for uploads', () => {
    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('normalises unnamed clipboard image files into uploadable files', () => {
        jest.spyOn(Date, 'now').mockReturnValue(1735689600000);
        const rawClipboardFile = new File(['pixels'], '', { type: 'image/png' });
        const event = {
            clipboardData: {
                items: [
                    {
                        kind: 'file',
                        type: 'image/png',
                        getAsFile: () => rawClipboardFile
                    }
                ]
            }
        };

        const files = __testOnly_extractImageFilesFromClipboardEvent(event);
        expect(files).toHaveLength(1);
        expect(files[0].type).toBe('image/png');
        expect(files[0].name).toBe('pasted-image-1735689600000-1.png');
    });

    test('ignores non-image clipboard entries', () => {
        const event = {
            clipboardData: {
                items: [
                    {
                        kind: 'string',
                        type: 'text/plain',
                        getAsFile: () => null
                    },
                    {
                        kind: 'file',
                        type: 'application/pdf',
                        getAsFile: () => new File(['pdf'], 'doc.pdf', { type: 'application/pdf' })
                    }
                ]
            }
        };

        const files = __testOnly_extractImageFilesFromClipboardEvent(event);
        expect(files).toEqual([]);
    });

    test('falls back to clipboardData.files when clipboardData.items is unavailable', () => {
        const event = {
            clipboardData: {
                files: [
                    new File(['img'], 'clipboard-face.jpg', { type: 'image/jpeg' }),
                    new File(['txt'], 'notes.txt', { type: 'text/plain' })
                ]
            }
        };

        const files = __testOnly_extractImageFilesFromClipboardEvent(event);
        expect(files).toHaveLength(1);
        expect(files[0].name).toBe('clipboard-face.jpg');
        expect(files[0].type).toBe('image/jpeg');
    });
});

describe('timestamp tooltip formatting', () => {
    test('formats absolute timestamp with explicit timezone', () => {
        const formatted = __testOnly_formatAbsoluteTimestamp('2026-02-22T10:15:30Z');
        expect(typeof formatted).toBe('string');
        expect(formatted.length).toBeGreaterThan(0);
        expect(formatted).toMatch(/GMT|UTC|[+-]\d{1,2}/i);
    });

    test('applies keep-title tooltip attributes for absolute timestamp hints', () => {
        const el = document.createElement('span');
        __testOnly_setUnambiguousTimestampTooltip(el, '2026-02-22T10:15:30Z', 'Timestamp: ');

        const title = el.getAttribute('title');
        expect(title).toBeTruthy();
        expect(title).toContain('Timestamp: ');
        expect(el.getAttribute('data-keep-title')).toBe('true');
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

    test('workflow status stream query does not filter out terminal statuses', () => {
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

        const query = __testOnly_buildWorkflowStatusStreamQuery();
        const params = new URLSearchParams(query);

        expect(params.get('namespace')).toBe('#V#michael_witbrock');
        expect(params.has('status')).toBe(false);
    });
});

describe('shared conversation stream eligibility', () => {
    test('requires active session, visible document, and shared session status', () => {
        expect(__testOnly_shouldMaintainSharedConversationStreamForInputs({
            sessionId: 'session-a',
            activeSessionId: 'session-a',
            isVisible: true,
            isSharedSession: true
        })).toBe(true);
    });

    test('rejects reconnect for non-active shared sessions', () => {
        expect(__testOnly_shouldMaintainSharedConversationStreamForInputs({
            sessionId: 'session-a',
            activeSessionId: 'session-b',
            isVisible: true,
            isSharedSession: true
        })).toBe(false);
    });

    test('rejects reconnect when tab is hidden', () => {
        expect(__testOnly_shouldMaintainSharedConversationStreamForInputs({
            sessionId: 'session-a',
            activeSessionId: 'session-a',
            isVisible: false,
            isSharedSession: true
        })).toBe(false);
    });
});

describe('shared session badge polling eligibility', () => {
    test('runs polling only when visible', () => {
        expect(__testOnly_shouldRunSharedSessionBadgePollingForInputs({
            isVisible: true
        })).toBe(true);
        expect(__testOnly_shouldRunSharedSessionBadgePollingForInputs({
            isVisible: false
        })).toBe(false);
    });
});

describe('workflow monitor concept links', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="workflowStatusPanel"></div>
            <div id="workflowStatusBody"></div>
            <button id="workflowStatusRefresh"></button>
            <button id="workflowStatusToggleAvailable"></button>
            <button id="workflowStatusFurlToggle"></button>
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

    test('exports workflow monitor MCP access with executable status-scoped instance calls', () => {
        const { getSessionScopedNamespace } = require('../utils/sessionScopedStorage.js');
        getSessionScopedNamespace.mockReturnValue('#V#michael_witbrock@university_of_auckland_strong_ai_lab');

        const payload = __testOnly_buildWorkflowMonitorLocatorPayload();

        expect(payload.mcp_access.workflow_list_definitions).toEqual(expect.objectContaining({
            tool_name: 'workflow_list_definitions',
            arguments: { limit: 200 },
            purpose: expect.stringContaining('capability_matrix')
        }));
        expect(payload.mcp_access.workflow_list_pending_instances).toEqual(expect.objectContaining({
            tool_name: 'workflow_list_instances',
            arguments: expect.objectContaining({
                namespace: '#V#michael_witbrock@university_of_auckland_strong_ai_lab',
                status: 'pending',
                limit: 200
            })
        }));
        expect(payload.mcp_access.workflow_list_running_instances.arguments.status).toBe('running');
        expect(payload.mcp_access.workflow_list_paused_instances.arguments.status).toBe('paused');
        expect(payload.mcp_access.workflow_list_instances).toBeUndefined();
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

describe('workflow monitor capability-index warning cartouche', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="workflowStatusPanel"></div>
            <div id="workflowStatusBody"></div>
            <button id="workflowStatusRefresh"></button>
            <button id="workflowStatusToggleAvailable"></button>
            <input id="workflowStatusShowDesigns" type="checkbox" />
        `;
        __testOnly_resetWorkflowDefinitionsState();
        __testOnly_resetWorkflowStatusState();
        __testOnly_resetWorkflowCapabilityIndexState();
    });

    afterEach(() => {
        __testOnly_resetWorkflowDefinitionsState();
        __testOnly_resetWorkflowStatusState();
        __testOnly_resetWorkflowCapabilityIndexState();
    });

    test('renders a warning cartouche in available-workflows mode when the capability index is not ready', () => {
        __testOnly_setWorkflowCapabilityIndexPayload({
            ready: false,
            status: 'error',
            summary: 'Workflow capability index not ready.',
            detail: 'Last build failed: OpenAI quota exhausted (insufficient_quota).',
            last_error: 'OpenAI quota exhausted (insufficient_quota)',
            startup_check: {
                checked_at_utc: '2026-04-22T01:02:03Z'
            },
            checked_at_utc: '2026-04-22T01:03:04Z',
            size: 0
        });

        __testOnly_renderWorkflowDefinitionsBody([]);

        const bodyText = document.getElementById('workflowStatusBody').textContent;
        expect(bodyText).toContain('Workflow capability index not ready');
        expect(bodyText).toContain('insufficient_quota');
        expect(bodyText).toContain('Checked:');
        expect(bodyText).toContain('Fetched:');
        const exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.capability_index.ready).toBe(false);
        expect(exportPayload.monitor_state.capability_index_error).toBeNull();
    });

    test('renders the same warning cartouche in active-workflows mode', () => {
        __testOnly_setWorkflowCapabilityIndexPayload({
            ready: false,
            status: 'building',
            summary: 'Workflow capability index still building.',
            detail: 'Workflow discovery is waiting on the authoritative capability index to finish building.',
            size: 0
        });

        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-capability-warning',
            workflow_id: '#V#turn_pipeline_monitoring_workflow',
            status: 'running',
            current_state: 'check_capability_index',
            progress: { current: 1, total: 3 }
        });

        const bodyText = document.getElementById('workflowStatusBody').textContent;
        expect(bodyText).toContain('Workflow capability index still building');
        expect(bodyText).toContain('turn pipeline monitoring workflow');
        const exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.capability_index.status).toBe('building');
    });

    test('renders automatic rebuild state without asking the user to repair the index', () => {
        __testOnly_setWorkflowCapabilityIndexPayload({
            ready: false,
            status: 'rebuilding',
            warning_level: 'warning',
            summary: 'Workflow capability index rebuilding.',
            detail: 'Von detected an incompatible persisted workflow capability index and started an automatic background rebuild.',
            size: 62,
            auto_rebuild: {
                attempt_count: 1,
                last_status: 'started',
                last_started_at_utc: '2026-04-24T09:17:45.000Z'
            }
        });

        __testOnly_renderWorkflowDefinitionsBody([]);

        const bodyText = document.getElementById('workflowStatusBody').textContent;
        expect(bodyText).toContain('Workflow capability index rebuilding');
        expect(bodyText).toContain('automatic background rebuild');
        expect(bodyText).not.toContain('requires rebuild');
        const exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.capability_index.auto_rebuild.last_status).toBe('started');
    });
});

describe('workflow monitor capability-index polling and global furl', () => {
    async function flushMicrotasks() {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
    }

    beforeEach(() => {
        jest.useFakeTimers();
        document.body.innerHTML = `
            <div id="workflowStatusPanel"></div>
            <div id="workflowStatusBody"></div>
            <button id="workflowStatusRefresh"></button>
            <button id="workflowStatusToggleAvailable"></button>
            <button id="workflowStatusFurlToggle"></button>
            <input id="workflowStatusShowDesigns" type="checkbox" />
        `;
        __testOnly_resetWorkflowDefinitionsState();
        __testOnly_resetWorkflowStatusState();
        __testOnly_resetWorkflowCapabilityIndexState();
    });

    afterEach(() => {
        __testOnly_resetWorkflowDefinitionsState();
        __testOnly_resetWorkflowStatusState();
        __testOnly_resetWorkflowCapabilityIndexState();
        delete global.fetch;
        jest.useRealTimers();
    });

    test('polls a not-ready capability index once per minute and clears the warning after readiness', async () => {
        global.fetch = jest.fn().mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => ({
                ready: true,
                status: 'ready',
                summary: 'Workflow capability index ready.',
                size: 62,
                checked_at_utc: '2026-04-24T07:19:41.000Z'
            })
        });

        __testOnly_setWorkflowCapabilityIndexPayload({
            ready: false,
            status: 'building',
            summary: 'Workflow capability index still building.',
            detail: 'Workflow discovery is waiting on the authoritative capability index to finish building.',
            size: 62,
            checked_at_utc: '2026-04-24T07:18:41.000Z'
        });
        __testOnly_renderWorkflowDefinitionsBody([]);

        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'Workflow capability index still building'
        );
        let exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.monitor_state.capability_index_poll_active).toBe(true);
        expect(exportPayload.monitor_state.capability_index_poll_interval_ms).toBe(60000);

        jest.advanceTimersByTime(60000);
        await flushMicrotasks();

        expect(global.fetch).toHaveBeenCalledTimes(1);
        expect(global.fetch.mock.calls[0][0]).toBe('/api/workflows/capability-index/status');
        expect(document.getElementById('workflowStatusBody').textContent).not.toContain(
            'Workflow capability index still building'
        );
        exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.capability_index.ready).toBe(true);
        expect(exportPayload.monitor_state.capability_index_poll_active).toBe(false);
    });

    test('renders capability-index status fetch timeouts without exposing the raw abort message', async () => {
        global.fetch = jest.fn((_url, options = {}) => new Promise((_resolve, reject) => {
            const signal = options.signal;
            if (signal && typeof signal.addEventListener === 'function') {
                signal.addEventListener('abort', () => {
                    const err = new Error('signal is aborted without reason');
                    err.name = 'AbortError';
                    reject(err);
                }, { once: true });
            }
        }));

        const refreshPromise = __testOnly_refreshWorkflowCapabilityIndexStatus();
        jest.advanceTimersByTime(8000);
        await refreshPromise;

        const bodyText = document.getElementById('workflowStatusBody').textContent;
        expect(bodyText).toContain('Capability index status unavailable');
        expect(bodyText).toContain('request timed out after 8s');
        expect(bodyText).not.toContain('signal is aborted without reason');
        const exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.monitor_state.capability_index_error).toContain('request timed out after 8s');
    });

    test('global furl hides active groups and capability-index warning without polling while furled', async () => {
        global.fetch = jest.fn().mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => ({ items: [] })
        });

        __testOnly_setWorkflowCapabilityIndexPayload({
            ready: false,
            status: 'building',
            summary: 'Workflow capability index still building.',
            detail: 'Workflow discovery is waiting on the authoritative capability index to finish building.',
            size: 62
        });
        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-furl-1',
            workflow_id: '#V#workflow_introspection_maintenance_workflow',
            status: 'running',
            current_state: 'diagnose',
            progress: { current: 1, total: 7, updated_at: '2026-03-20T07:05:19.686Z' }
        });
        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-furl-2',
            workflow_id: '#V#workflow_introspection_maintenance_workflow',
            status: 'running',
            current_state: 'diagnose',
            progress: { current: 2, total: 7, updated_at: '2026-03-20T07:06:19.686Z' }
        });

        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'Workflow capability index still building'
        );
        expect(document.querySelectorAll('.workflow-status-group')).toHaveLength(1);
        expect(document.querySelector('.workflow-status-group-body').classList.contains('is-collapsed')).toBe(true);
        let exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.monitor_state.active_live_refresh_timer_active).toBe(true);
        expect(exportPayload.monitor_state.capability_index_poll_active).toBe(true);

        __testOnly_setWorkflowMonitorGloballyFurled(true);

        const furlButton = document.getElementById('workflowStatusFurlToggle');
        expect(furlButton.getAttribute('aria-label')).toBe('Unfurl workflow monitor');
        expect(furlButton.textContent).toBe('');
        expect(furlButton.getAttribute('aria-expanded')).toBe('false');
        expect(document.getElementById('workflowStatusBody').textContent).not.toContain(
            'Workflow capability index still building'
        );
        expect(document.querySelectorAll('.workflow-status-group')).toHaveLength(0);
        exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.monitor_state.global_furled).toBe(true);
        expect(exportPayload.monitor_state.active_live_refresh_timer_active).toBe(false);
        expect(exportPayload.monitor_state.capability_index_poll_active).toBe(false);

        jest.advanceTimersByTime(60000);
        await flushMicrotasks();
        expect(global.fetch).not.toHaveBeenCalled();

        __testOnly_setWorkflowMonitorGloballyFurled(false);

        expect(furlButton.getAttribute('aria-label')).toBe('Furl workflow monitor');
        expect(furlButton.textContent).toBe('');
        expect(furlButton.getAttribute('aria-expanded')).toBe('true');
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'Workflow capability index still building'
        );
        expect(document.querySelectorAll('.workflow-status-group')).toHaveLength(1);
        expect(document.querySelector('.workflow-status-group-body').classList.contains('is-collapsed')).toBe(true);
        exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.monitor_state.global_furled).toBe(false);
        expect(exportPayload.monitor_state.active_live_refresh_timer_active).toBe(true);
        expect(exportPayload.monitor_state.capability_index_poll_active).toBe(true);

        jest.advanceTimersByTime(750);
        await flushMicrotasks();
        expect(global.fetch).toHaveBeenCalledTimes(1);
        expect(global.fetch.mock.calls[0][0]).toContain('/api/workflows/instances?');
    });
});

describe('workflow monitor definitions refresh contention handling', () => {
    async function flushMicrotasks() {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
    }

    beforeEach(() => {
        jest.useFakeTimers();
        document.body.innerHTML = `
            <div id="workflowStatusPanel"></div>
            <div id="workflowStatusBody"></div>
            <button id="workflowStatusRefresh"></button>
            <button id="workflowStatusToggleAvailable"></button>
            <input id="workflowStatusShowDesigns" type="checkbox" />
        `;
        __testOnly_resetWorkflowDefinitionsState();
        __testOnly_renderWorkflowDefinitionsBody([]);
    });

    afterEach(() => {
        jest.useRealTimers();
        delete global.fetch;
        __testOnly_resetWorkflowDefinitionsState();
    });

    test('treats refresh-in-progress response as transient and auto-retries with bounded delay', async () => {
        let fetchCount = 0;
        global.fetch = jest.fn(() => {
            fetchCount += 1;
            if (fetchCount === 1) {
                return Promise.resolve({
                    ok: false,
                    status: 503,
                    headers: {
                        get: (name) => (String(name).toLowerCase() === 'retry-after' ? '1' : null)
                    },
                    json: async () => ({
                        error: 'workflow_definitions_refresh_in_progress',
                        detail: 'Workflow definitions refresh is already running; retry shortly.',
                        retry_after_seconds: 1
                    })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                headers: { get: () => null },
                json: async () => ({
                    items: [
                        {
                            workflow_id: '#V#demo_retry_workflow',
                            description: 'Retry test workflow.',
                            initial_state: 'pending',
                            source: 'built_in'
                        }
                    ]
                })
            });
        });

        await __testOnly_refreshAvailableWorkflowDefinitions();

        expect(fetchCount).toBe(1);
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'Workflow definitions are refreshing, retrying'
        );
        const contentionExport = __testOnly_buildWorkflowMonitorExportPayload();
        expect(contentionExport.definitions_snapshot.payload.error).toBe(
            'workflow_definitions_refresh_in_progress'
        );
        expect(contentionExport.definitions_snapshot.payload.retry_after_seconds).toBe(1);
        expect(contentionExport.definitions_snapshot.payload.retry_attempt).toBe(1);

        jest.advanceTimersByTime(1100);
        await flushMicrotasks();

        expect(fetchCount).toBe(2);
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'demo retry workflow'
        );
        const successExport = __testOnly_buildWorkflowMonitorExportPayload();
        expect(successExport.monitor_state.available_error).toBeNull();
    });

    test('shows cached workflows while a stale background refresh completes', async () => {
        let fetchCount = 0;
        global.fetch = jest.fn(() => {
            fetchCount += 1;
            if (fetchCount === 1) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    headers: { get: () => null },
                    json: async () => ({
                        items: [
                            {
                                workflow_id: '#V#cached_workflow',
                                description: 'Cached workflow.',
                                initial_state: 'start',
                                source: 'built_in'
                            }
                        ],
                        cache: {
                            state: 'stale',
                            refresh_in_progress: true,
                            retry_after_seconds: 1
                        }
                    })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                headers: { get: () => null },
                json: async () => ({
                    items: [
                        {
                            workflow_id: '#V#fresh_workflow',
                            description: 'Fresh workflow.',
                            initial_state: 'ready',
                            source: 'built_in'
                        }
                    ],
                    cache: {
                        state: 'fresh',
                        refresh_in_progress: false
                    }
                })
            });
        });

        await __testOnly_refreshAvailableWorkflowDefinitions();

        expect(fetchCount).toBe(1);
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'Showing cached workflow definitions while the monitor refreshes in the background'
        );
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'cached workflow'
        );
        const staleExport = __testOnly_buildWorkflowMonitorExportPayload();
        expect(staleExport.monitor_state.available_notice).toContain('Showing cached workflow definitions');
        expect(staleExport.definitions_snapshot.payload.cache.state).toBe('stale');

        jest.advanceTimersByTime(1100);
        await flushMicrotasks();

        expect(fetchCount).toBe(2);
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'fresh workflow'
        );
        const successExport = __testOnly_buildWorkflowMonitorExportPayload();
        expect(successExport.monitor_state.available_notice).toBeNull();
    });

    test('treats timeout as transient and retries before surfacing a hard timeout', async () => {
        let fetchCount = 0;
        global.fetch = jest.fn(() => {
            fetchCount += 1;
            if (fetchCount === 1) {
                const err = new Error('timeout');
                err.name = 'AbortError';
                return Promise.reject(err);
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                headers: { get: () => null },
                json: async () => ({
                    items: [
                        {
                            workflow_id: '#V#timeout_recovery_workflow',
                            description: 'Recovered after timeout.',
                            initial_state: 'ready',
                            source: 'built_in'
                        }
                    ],
                    cache: {
                        state: 'fresh',
                        refresh_in_progress: false
                    }
                })
            });
        });

        await __testOnly_refreshAvailableWorkflowDefinitions();

        expect(fetchCount).toBe(1);
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'Workflow definitions are taking longer than expected. Retrying in'
        );
        const timeoutExport = __testOnly_buildWorkflowMonitorExportPayload();
        expect(timeoutExport.monitor_state.available_notice).toContain('Retrying');
        expect(timeoutExport.definitions_snapshot.payload.error).toBe(
            'workflow_definitions_request_timed_out'
        );

        jest.advanceTimersByTime(1300);
        await flushMicrotasks();

        expect(fetchCount).toBe(2);
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'timeout recovery workflow'
        );
        const successExport = __testOnly_buildWorkflowMonitorExportPayload();
        expect(successExport.monitor_state.available_error).toBeNull();
        expect(successExport.monitor_state.available_notice).toBeNull();
    });

    test('keeps hard error state for non-contention 5xx responses', async () => {
        global.fetch = jest.fn(() => Promise.resolve({
            ok: false,
            status: 500,
            headers: { get: () => null },
            json: async () => ({
                error: 'workflow_definitions_fetch_failed',
                detail: 'Workflow registry unavailable'
            })
        }));

        await __testOnly_refreshAvailableWorkflowDefinitions();
        jest.advanceTimersByTime(3000);
        await flushMicrotasks();

        expect(global.fetch).toHaveBeenCalledTimes(1);
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'Could not load available workflows: Workflow registry unavailable'
        );
        const exportPayload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(exportPayload.definitions_snapshot.payload.error).toBe(
            'workflow_definitions_fetch_failed'
        );
        expect(exportPayload.definitions_snapshot.payload.status).toBe(500);
    });
});

describe('workflow monitor active snapshot degradation handling', () => {
    async function flushMicrotasks() {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
    }

    beforeEach(() => {
        jest.useFakeTimers();
        document.body.innerHTML = `
            <div id="workflowStatusPanel"></div>
            <div id="workflowStatusBody"></div>
            <button id="workflowStatusRefresh"></button>
            <button id="workflowStatusToggleAvailable"></button>
            <input id="workflowStatusShowDesigns" type="checkbox" />
        `;
        __testOnly_resetWorkflowDefinitionsState();
        __testOnly_resetWorkflowStatusState();
    });

    afterEach(() => {
        jest.useRealTimers();
        delete global.fetch;
        __testOnly_resetWorkflowDefinitionsState();
        __testOnly_resetWorkflowStatusState();
    });

    test('auto-retries retryable active snapshot failures and clears the banner after a later success', async () => {
        let fetchCount = 0;
        global.fetch = jest.fn(() => {
            fetchCount += 1;
            if (fetchCount === 1) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    headers: { get: () => null },
                    json: async () => ({
                        items: [
                            {
                                instance_id: 'wf-1',
                                workflow_id: '#V#demo_retry_workflow',
                                status: 'running',
                                current_state: 'waiting_for_input',
                                progress: { current: 1, total: 2 }
                            }
                        ]
                    })
                });
            }
            if (fetchCount === 2) {
                return Promise.resolve({
                    ok: false,
                    status: 503,
                    headers: {
                        get: (name) => (String(name).toLowerCase() === 'retry-after' ? '1' : null)
                    },
                    json: async () => ({
                        items: [],
                        count: 0,
                        degraded: true,
                        retryable: true,
                        retry_after_seconds: 1,
                        error: 'Workflow monitor temporarily unavailable; please retry.',
                        detail: 'server selection timeout while reading workflow_instances'
                    })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                headers: { get: () => null },
                json: async () => ({
                    items: [
                        {
                            instance_id: 'wf-1',
                            workflow_id: '#V#demo_retry_workflow',
                            status: 'running',
                            current_state: 'resumed_after_retry',
                            progress: { current: 2, total: 2 }
                        }
                    ]
                })
            });
        });

        await __testOnly_refreshWorkflowStatusSnapshot();
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'demo retry workflow'
        );
        expect(document.getElementById('workflowStatusBody').textContent).toContain('wf-1');

        await __testOnly_refreshWorkflowStatusSnapshot();

        const bodyText = document.getElementById('workflowStatusBody').textContent;
        expect(bodyText).toContain('showing live updates while retrying');
        expect(bodyText).toContain('demo retry workflow');
        expect(fetchCount).toBe(2);
        const payload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(payload.monitor_state.active_loading).toBe(false);
        expect(payload.monitor_state.active_notice).toContain('retrying in 1.2s');
        expect(payload.active_instances_snapshot.payload.retryable).toBe(true);
        expect(payload.active_instances_snapshot.payload.retry_scheduled_in_ms).toBe(1200);

        jest.advanceTimersByTime(1300);
        await flushMicrotasks();

        expect(fetchCount).toBe(3);
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'resumed_after_retry'
        );
        expect(document.getElementById('workflowStatusBody').textContent).not.toContain(
            'snapshot unavailable'
        );
    });

    test('keeps retrying retryable active snapshot failures while the monitor stays visible', async () => {
        let fetchCount = 0;
        global.fetch = jest.fn(() => {
            fetchCount += 1;
            return Promise.resolve({
                ok: false,
                status: 503,
                headers: { get: () => null },
                json: async () => ({
                    items: [],
                    count: 0,
                    degraded: true,
                    retryable: true,
                    error: 'Workflow monitor temporarily unavailable; please retry.',
                    detail: 'read circuit open'
                })
            });
        });

        await __testOnly_refreshWorkflowStatusSnapshot();
        expect(fetchCount).toBe(1);

        for (let attempt = 0; attempt < 4; attempt += 1) {
            jest.runOnlyPendingTimers();
            await flushMicrotasks();
        }

        expect(fetchCount).toBe(5);
        const payload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(payload.monitor_state.active_notice).toContain('retrying in 8s');
        expect(payload.monitor_state.active_notice).not.toContain('Press Refresh');
        expect(payload.active_instances_snapshot.payload.retry_attempt).toBe(5);
        expect(payload.active_instances_snapshot.payload.retry_scheduled_in_ms).toBe(8000);
    });

    test('refreshes the active snapshot with an active-status filter', async () => {
        global.fetch = jest.fn().mockResolvedValue({
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: async () => ({
                items: [],
                count: 0
            })
        });

        await __testOnly_refreshWorkflowStatusSnapshot();

        expect(global.fetch).toHaveBeenCalledTimes(1);
        const requestUrl = new URL(global.fetch.mock.calls[0][0], 'http://localhost');
        expect(requestUrl.searchParams.get('status')).toBe('pending,running,paused');
    });

    test('shows visible refresh state while an active snapshot request is in flight', async () => {
        let resolveFetch;
        global.fetch = jest.fn(() => new Promise((resolve) => {
            resolveFetch = resolve;
        }));

        const refreshPromise = __testOnly_refreshWorkflowStatusSnapshot();

        expect(document.getElementById('workflowStatusRefresh').disabled).toBe(true);
        expect(document.getElementById('workflowStatusRefresh').textContent).toBe('Refreshing...');
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'Refreshing workflow monitor'
        );

        resolveFetch({
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: async () => ({ items: [] })
        });
        await refreshPromise;

        expect(document.getElementById('workflowStatusRefresh').disabled).toBe(false);
        expect(document.getElementById('workflowStatusRefresh').textContent).toBe('Refresh');
        expect(document.getElementById('workflowStatusBody').textContent).toContain(
            'No active workflows'
        );
    });

    test('renders separate instance identifiers when two active instances share the same workflow', async () => {
        global.fetch = jest.fn(() => Promise.resolve({
            ok: false,
            status: 503,
            headers: { get: () => null },
            json: async () => ({
                items: [],
                count: 0,
                degraded: true,
                retryable: true,
                error: 'Workflow monitor temporarily unavailable; please retry.',
                detail: 'read circuit open'
            })
        }));

        await __testOnly_refreshWorkflowStatusSnapshot();

        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-dup-1',
            workflow_id: '#V#jira_task_full_reconciliation_workflow',
            status: 'running',
            current_state: '#V#jira_task_full_reconciliation_refresh_recent_updates_step',
            progress: { current: 1, total: 5 }
        });
        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-dup-2',
            workflow_id: '#V#jira_task_full_reconciliation_workflow',
            status: 'running',
            current_state: '#V#jira_task_full_reconciliation_refresh_recent_updates_step',
            progress: { current: 1, total: 5 }
        });

        const bodyText = document.getElementById('workflowStatusBody').textContent;
        expect(bodyText).toContain('showing live updates');
        expect(bodyText).toContain('wf-dup-1');
        expect(bodyText).toContain('wf-dup-2');
        expect(document.querySelectorAll('.workflow-status-group').length).toBe(1);
        const groupToggle = document.querySelector('.workflow-status-group-toggle');
        expect(groupToggle).toBeTruthy();
        expect(groupToggle.getAttribute('aria-expanded')).toBe('false');
        expect(document.querySelectorAll('.workflow-status-item').length).toBe(2);
    });

    test('expands a grouped workflow section when the fold toggle is clicked', async () => {
        global.fetch = jest.fn(() => Promise.resolve({
            ok: false,
            status: 503,
            headers: { get: () => null },
            json: async () => ({
                items: [],
                count: 0,
                degraded: true,
                retryable: true,
                error: 'Workflow monitor temporarily unavailable; please retry.',
                detail: 'read circuit open'
            })
        }));

        await __testOnly_refreshWorkflowStatusSnapshot();

        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-group-1',
            workflow_id: '#V#workflow_introspection_maintenance_workflow',
            status: 'running',
            current_state: 'diagnose',
            progress: { current: 1, total: 7, updated_at: '2026-03-20T07:05:19.686Z' }
        });
        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-group-2',
            workflow_id: '#V#workflow_introspection_maintenance_workflow',
            status: 'running',
            current_state: 'diagnose',
            progress: { current: 1, total: 7, updated_at: '2026-03-20T07:06:19.686Z' }
        });

        const toggle = document.querySelector('.workflow-status-group-toggle');
        const groupBody = document.querySelector('.workflow-status-group-body');
        expect(toggle).toBeTruthy();
        expect(groupBody).toBeTruthy();
        expect(toggle.getAttribute('aria-expanded')).toBe('false');
        expect(groupBody.classList.contains('is-collapsed')).toBe(true);

        toggle.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        const expandedToggle = document.querySelector('.workflow-status-group-toggle');
        const expandedGroupBody = document.querySelector('.workflow-status-group-body');
        expect(expandedToggle.getAttribute('aria-expanded')).toBe('true');
        expect(expandedGroupBody.classList.contains('is-collapsed')).toBe(false);
    });

    test('renders workflow monitor timestamps with an explicit timezone label', async () => {
        global.fetch = jest.fn(() => Promise.resolve({
            ok: false,
            status: 503,
            headers: { get: () => null },
            json: async () => ({
                items: [],
                count: 0,
                degraded: true,
                retryable: true,
                error: 'Workflow monitor temporarily unavailable; please retry.',
                detail: 'read circuit open'
            })
        }));

        await __testOnly_refreshWorkflowStatusSnapshot();

        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-tz-1',
            workflow_id: '#V#workflow_introspection_maintenance_workflow',
            status: 'running',
            current_state: 'diagnose',
            progress: { current: 1, total: 7, updated_at: '2026-03-20T07:05:19.686Z' }
        });

        const bodyText = document.getElementById('workflowStatusBody').textContent;
        expect(bodyText).toContain('Updated:');
        expect(bodyText).toMatch(/Updated:\s+.*(?:GMT|UTC|[+-]\d{1,2})/i);
    });

    test('preserves richer snapshot fields when a thinner live status event arrives', async () => {
        global.fetch = jest.fn(() => Promise.resolve({
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: async () => ({
                items: [
                    {
                        instance_id: 'wf-merge-1',
                        workflow_id: '#V#workflow_introspection_maintenance_workflow',
                        status: 'running',
                        current_state: 'diagnose',
                        created_at: '2026-03-20T07:03:29.141Z',
                        started_at: '2026-03-20T07:03:33.986Z',
                        progress: {
                            current: 1,
                            total: 7,
                            message: 'diagnose',
                            updated_at: '2026-03-20T07:05:19.686Z'
                        },
                        max_retries: 5
                    }
                ],
                count: 1
            })
        }));

        await __testOnly_refreshWorkflowStatusSnapshot();

        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-merge-1',
            workflow_id: '#V#workflow_introspection_maintenance_workflow',
            status: 'running',
            current_state: 'ready_to_report',
            progress: {
                current: 2,
                total: 7,
                message: 'ready_to_report'
            }
        });

        const payload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(payload.active_instances_snapshot.items).toHaveLength(1);
        expect(payload.active_instances_snapshot.items[0].current_state).toBe('ready_to_report');
        expect(payload.active_instances_snapshot.items[0].created_at).toBe('2026-03-20T07:03:29.141Z');
        expect(payload.active_instances_snapshot.items[0].started_at).toBe('2026-03-20T07:03:33.986Z');
        expect(payload.active_instances_snapshot.items[0].max_retries).toBe(5);
        expect(payload.active_instances_snapshot.items[0].progress.updated_at).toBe('2026-03-20T07:05:19.686Z');
    });

    test('silently refreshes the monitor after a new live workflow event adds an active instance', async () => {
        global.fetch = jest.fn(() => Promise.resolve({
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: async () => ({
                items: [
                    {
                        instance_id: 'wf-live-1',
                        workflow_id: '#V#jira_task_full_reconciliation_workflow',
                        status: 'running',
                        current_state: 'refreshed_from_snapshot',
                        created_at: '2026-03-22T10:00:00.000Z',
                        progress: {
                            current: 1,
                            total: 5,
                            message: 'refreshed_from_snapshot',
                            updated_at: '2026-03-22T10:00:01.000Z'
                        }
                    }
                ],
                count: 1
            })
        }));

        __testOnly_applyWorkflowStatusUpdate({
            instance_id: 'wf-live-1',
            workflow_id: '#V#jira_task_full_reconciliation_workflow',
            status: 'running',
            current_state: 'from_stream',
            updated_at: '2026-03-22T10:00:00.500Z'
        });

        expect(global.fetch).not.toHaveBeenCalled();
        expect(document.getElementById('workflowStatusBody').textContent).toContain('from_stream');

        jest.advanceTimersByTime(800);
        await flushMicrotasks();

        expect(global.fetch).toHaveBeenCalledTimes(1);
        expect(document.getElementById('workflowStatusBody').textContent).toContain('refreshed_from_snapshot');
        const payload = __testOnly_buildWorkflowMonitorExportPayload();
        expect(payload.active_instances_snapshot.items[0].created_at).toBe('2026-03-22T10:00:00.000Z');
        expect(payload.active_instances_snapshot.items[0].progress.updated_at).toBe('2026-03-22T10:00:01.000Z');
    });
});

describe('loadChatHistory degraded handling', () => {
    beforeEach(() => {
        const { getUserContext, postJson } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: '#V#user',
            org_id: '#V#org',
            language: 'en-NZ',
            gmail_profile: null
        });
        postJson.mockResolvedValue({ status: 'updated' });
        document.body.innerHTML = `
            <div id="scrollableField"><div class="message-container">Kept content</div></div>
            <div id="historyBanner" class="history-banner hidden">
                <span id="historyBannerText"></span>
                <button id="loadOlderHistoryBtn" type="button"></button>
            </div>
            <div id="chat-history-length"></div>
        `;
        __testOnly_resetHistoryUiState();
        __testOnly_setActiveChatSession('session-1', 'Session 1');
    });

    afterEach(() => {
        delete global.fetch;
        __testOnly_resetHistoryUiState();
        __testOnly_setActiveChatSession(null, null);
    });

    test('preserves rendered history and surfaces a banner when the backend reports degraded history', async () => {
        __testOnly_setDisplayedHistorySession('session-1');
        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/von/api/session/context')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        user_id: '#V#user',
                        organisation_id: '#V#org',
                        namespace: '#V#user@org'
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history?')) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        history: [],
                        segments_returned: 0,
                        total_segments: 0,
                        has_more_history: false,
                        degraded: true,
                        retryable: true,
                        error: 'Chat history temporarily unavailable; please retry.',
                        detail: 'read circuit open'
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        history_length: 4,
                        session_count: 1,
                        authenticated: true
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const loaded = await __testOnly_loadChatHistory({ segments: 1 });

        expect(loaded).toBe(false);
        expect(document.getElementById('scrollableField').textContent).toContain('Kept content');
        expect(document.getElementById('historyBanner').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('historyBannerText').textContent).toContain(
            'temporarily unavailable'
        );
    });

    test('treats genuinely empty history as a successful empty load', async () => {
        __testOnly_setDisplayedHistorySession('session-1');
        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/von/api/session/context')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        user_id: '#V#user',
                        organisation_id: '#V#org',
                        namespace: '#V#user@org'
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history?')) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        history: [],
                        segments_returned: 0,
                        total_segments: 0,
                        has_more_history: false
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        history_length: 0,
                        session_count: 1,
                        authenticated: true
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const loaded = await __testOnly_loadChatHistory({ segments: 1 });

        expect(loaded).toBe(true);
        expect(document.getElementById('scrollableField').textContent).not.toContain('Kept content');
        expect(document.getElementById('historyBanner').classList.contains('hidden')).toBe(true);
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

    test('formats ETA metadata from progress payload', () => {
        expect(__testOnly_formatThinkingEtaText({ eta_ms: 4500 })).toBe('ETA ~5s');
        expect(__testOnly_formatThinkingEtaText({ eta_seconds: 2 })).toBe('ETA ~2s');
        expect(__testOnly_formatThinkingEtaText({ eta_ms: 250 })).toBe('ETA <1s');
        expect(__testOnly_formatThinkingEtaText({})).toBeNull();
    });

    test('treats terminal status as authoritative over active liveness', () => {
        const presentation = __testOnly_buildThinkingProgressPresentation({
            status: 'pending',
            orchestrator_status: 'completed',
            liveness_state: 'active',
            stage: 'response_finalising',
            idle_ms: 1800
        });

        expect(presentation.livenessState).toBe('completed');
        expect(presentation.livenessLabel).toBe('Complete');
        expect(presentation.stageText).toBe('Complete');
        expect(presentation.lastActivityText).toContain('Last activity');
    });

    test('renders follow-up-required as a distinct terminal state', () => {
        const presentation = __testOnly_buildThinkingProgressPresentation({
            status: 'pending',
            orchestrator_status: 'follow_up_required',
            liveness_state: 'active',
            stage: 'response_finalising',
            idle_ms: 900
        });

        expect(presentation.livenessState).toBe('follow_up_required');
        expect(presentation.livenessLabel).toBe('Follow-up required');
        expect(presentation.stageText).toBe('Follow-up required');
        expect(presentation.lastActivityText).toContain('Last activity');
    });

    test('surfaces workflow candidates when no direct routing match is available', () => {
        const presentation = __testOnly_buildThinkingProgressPresentation(
            {
                stage: 'workflow_discovery_complete',
                phase_label: 'Evaluating workflow applicability',
                workflow_discovery: {
                    match_count: 0,
                    candidate_count: 1,
                    candidates: [
                        {
                            concept_id: '#V#scholarly_paper_representation_workflow',
                            name: 'Scholarly paper representation workflow'
                        }
                    ]
                }
            }
        );

        expect(presentation.stageText).toContain('Evaluating workflow applicability');
        expect(presentation.stageText).toContain(
            'Candidate found: Scholarly paper representation workflow (#V#scholarly_paper_representation_workflow)'
        );
    });

    test('explains selector routing when dispatch follows a no-match discovery result', () => {
        const presentation = __testOnly_buildThinkingProgressPresentation({
            stage: 'workflow_dispatch',
            phase_label: 'Selecting workflow',
            workflow_discovery: {
                match_count: 0,
                candidate_count: 0
            },
            selected_workflow_id: '#V#tool_calling_workflow',
            selected_workflow_name: 'Tool calling workflow',
            workflow_selector_verdict: 'tool_seeking',
            workflow_selector_source: 'selector'
        });

        expect(presentation.stageText).toContain('Selecting workflow');
        expect(presentation.stageText).toContain(
            'No direct workflow match found; routed via selector to Tool calling workflow (#V#tool_calling_workflow)'
        );
        expect(presentation.stageText).toContain('Tool seeking');
    });

    test('preserves selected workflow context in terminal thinking text', () => {
        const presentation = __testOnly_buildThinkingProgressPresentation({
            status: 'completed',
            stage: 'completed',
            workflow_discovery: {
                match_count: 0,
                candidate_count: 0
            },
            selected_workflow_id: '#V#tool_calling_workflow',
            selected_workflow_name: 'Tool calling workflow',
            workflow_selector_verdict: 'tool_seeking',
            workflow_selector_source: 'selector'
        });

        expect(presentation.livenessLabel).toBe('Complete');
        expect(presentation.stageText).toContain('Complete:');
        expect(presentation.stageText).toContain(
            'No direct workflow match found; routed via selector to Tool calling workflow (#V#tool_calling_workflow)'
        );
    });

    test('prefixes the current goal when teleological progress is available', () => {
        const presentation = __testOnly_buildThinkingProgressPresentation({
            stage: 'workflow_discovery',
            phase_label: 'Searching for workflows',
            goal_label: 'Fully represent the paper #V#uploaded_file_copy_123'
        });

        expect(presentation.stageText).toContain(
            'Goal: Fully represent the paper #V#uploaded_file_copy_123'
        );
        expect(presentation.stageText).toContain('Searching for workflows');
    });
});

describe('thinking card display state reducer', () => {
    test('keeps card expanded during non-terminal progress updates', () => {
        let state = __testOnly_reduceThinkingCardDisplayState(null, { type: 'reset_for_active' });
        state = __testOnly_reduceThinkingCardDisplayState(state, {
            type: 'manual_set',
            expanded: false
        });

        state = __testOnly_reduceThinkingCardDisplayState(state, {
            type: 'progress_update',
            progress: { status: 'pending' }
        });

        expect(state.expanded).toBe(true);
        expect(state.autoFurlApplied).toBe(false);
    });

    test('auto-furls once on first terminal progress update', () => {
        let state = __testOnly_reduceThinkingCardDisplayState(null, { type: 'reset_for_active' });
        expect(state.expanded).toBe(true);
        expect(state.autoFurlApplied).toBe(false);

        state = __testOnly_reduceThinkingCardDisplayState(state, {
            type: 'progress_update',
            progress: { status: 'completed' }
        });
        expect(state.expanded).toBe(false);
        expect(state.autoFurlApplied).toBe(true);
    });

    test('manual reopen after terminal state is preserved', () => {
        let state = __testOnly_reduceThinkingCardDisplayState(null, { type: 'reset_for_active' });
        state = __testOnly_reduceThinkingCardDisplayState(state, {
            type: 'progress_update',
            progress: { status: 'error' }
        });
        expect(state.expanded).toBe(false);

        state = __testOnly_reduceThinkingCardDisplayState(state, {
            type: 'manual_set',
            expanded: true
        });
        expect(state.expanded).toBe(true);

        state = __testOnly_reduceThinkingCardDisplayState(state, {
            type: 'progress_update',
            progress: { status: 'completed' }
        });
        expect(state.expanded).toBe(true);
        expect(state.autoFurlApplied).toBe(true);
    });

    test('treats orchestrator terminal status as terminal progress update', () => {
        let state = __testOnly_reduceThinkingCardDisplayState(null, { type: 'reset_for_active' });
        state = __testOnly_reduceThinkingCardDisplayState(state, {
            type: 'progress_update',
            progress: {
                status: 'pending',
                orchestrator_status: 'completed',
                liveness_state: 'active'
            }
        });

        expect(state.expanded).toBe(false);
        expect(state.autoFurlApplied).toBe(true);
    });

    test('treats follow-up-required as a terminal progress update', () => {
        let state = __testOnly_reduceThinkingCardDisplayState(null, { type: 'reset_for_active' });
        state = __testOnly_reduceThinkingCardDisplayState(state, {
            type: 'progress_update',
            progress: {
                status: 'follow_up_required',
                liveness_state: 'active'
            }
        });

        expect(state.expanded).toBe(false);
        expect(state.autoFurlApplied).toBe(true);
    });
});

describe('thinking progress recency guards', () => {
    test('rejects stale progress updates with lower sequence numbers', () => {
        expect(__testOnly_shouldAcceptThinkingProgressUpdate(
            {
                status: 'completed',
                sequence_no: 7,
                updated_at_epoch: 1007,
                elapsed_ms: 7000
            },
            {
                status: 'follow_up_required',
                sequence_no: 6,
                updated_at_epoch: 1006,
                elapsed_ms: 6000
            }
        )).toBe(false);
    });

    test('rejects unordered waiting fallbacks once ordered progress exists', () => {
        expect(__testOnly_shouldAcceptThinkingProgressUpdate(
            {
                status: 'completed',
                sequence_no: 3,
                updated_at_epoch: 1003,
                elapsed_ms: 3000
            },
            {
                status: 'pending',
                phase_label: 'Awaiting visible progress',
                result_summary: 'Progress visibility has not caught up yet.'
            }
        )).toBe(false);
    });

    test('accepts newer progress updates with higher sequence numbers', () => {
        expect(__testOnly_shouldAcceptThinkingProgressUpdate(
            {
                status: 'follow_up_required',
                sequence_no: 6,
                updated_at_epoch: 1006,
                elapsed_ms: 6000
            },
            {
                status: 'completed',
                sequence_no: 7,
                updated_at_epoch: 1007,
                elapsed_ms: 7000
            }
        )).toBe(true);
    });
});

describe('thinking activity history normalisation', () => {
    test('captures non-tool orchestration and workflow activity rows', () => {
        const rows = __testOnly_normaliseThinkingActivityHistory([
            {
                sequence_no: 1,
                status: 'phase_transition',
                stage: 'context_build',
                stage_label: 'Building context'
            },
            {
                sequence_no: 2,
                status: 'workflow_discovery',
                stage: 'workflow_discovery'
            },
            {
                sequence_no: 3,
                status: 'workflow_discovery_complete',
                stage: 'workflow_discovery_complete'
            },
            {
                sequence_no: 4,
                status: 'orchestrator_start',
                stage: 'tool_plan'
            },
            {
                sequence_no: 5,
                status: 'llm_call_start',
                stage: 'tool_plan',
                model: 'gpt-test'
            },
            {
                sequence_no: 6,
                status: 'tool_call_start',
                stage: 'tool_execute',
                tool: 'fetch_concept'
            },
            {
                sequence_no: 7,
                status: 'tool_failed',
                stage: 'tool_execute',
                tool: 'fetch_concept',
                error: 'Timeout while fetching'
            },
            {
                sequence_no: 8,
                status: 'completed',
                stage: 'completed',
                result_summary: 'Response generated'
            }
        ]);

        expect(rows.map((row) => row.label)).toEqual(expect.arrayContaining([
            'Building context',
            'Workflow discovery',
            'Workflow dispatch',
            'Starting LLM call',
            'Tool call: fetch_concept',
            'Tool call failed: fetch_concept',
            'Turn completed'
        ]));
        const failureRow = rows.find((row) => row.label === 'Tool call failed: fetch_concept');
        expect(failureRow?.state).toBe('failure');
    });

    test('groups contiguous low-level updates', () => {
        const rows = __testOnly_normaliseThinkingActivityHistory([
            {
                sequence_no: 1,
                status: 'llm_call_chunk',
                event_kind: 'llm_call_chunk',
                stage: 'tool_plan',
                model: 'gpt-test'
            },
            {
                sequence_no: 2,
                status: 'llm_call_chunk',
                event_kind: 'llm_call_chunk',
                stage: 'tool_plan',
                model: 'gpt-test'
            },
            {
                sequence_no: 3,
                status: 'heartbeat',
                event_kind: 'heartbeat',
                stage: 'tool_plan'
            }
        ]);

        expect(rows).toHaveLength(2);
        expect(rows[0].label).toBe('Streaming LLM output');
        expect(rows[0].groupCount).toBe(2);
        expect(rows[0].isLowLevel).toBe(true);
        expect(rows[1].label).toBe('Waiting for updates');
    });

    test('treats candidate-only workflow discovery as informative rather than a miss', () => {
        const rows = __testOnly_normaliseThinkingActivityHistory([
            {
                sequence_no: 1,
                status: 'workflow_discovery_complete',
                stage: 'workflow_discovery_complete',
                workflow_match_count: 0,
                workflow_candidate_count: 1
            }
        ]);

        expect(rows).toHaveLength(1);
        expect(rows[0].label).toBe('Workflow discovery');
        expect(rows[0].detail).toContain('Relevant workflow candidate found');
        expect(rows[0].state).toBe('success');
    });

    test('links no-match discovery and dispatch activity to the selected workflow', () => {
        const rows = __testOnly_normaliseThinkingActivityHistory([
            {
                sequence_no: 1,
                status: 'workflow_discovery_complete',
                stage: 'workflow_discovery_complete',
                workflow_match_count: 0,
                workflow_candidate_count: 0,
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow',
                workflow_selector_verdict: 'tool_seeking',
                workflow_selector_source: 'selector'
            },
            {
                sequence_no: 2,
                status: 'orchestrator_start',
                stage: 'workflow_dispatch',
                workflow_match_count: 0,
                workflow_candidate_count: 0,
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow',
                workflow_selector_verdict: 'tool_seeking',
                workflow_selector_source: 'selector'
            }
        ]);

        expect(rows).toHaveLength(2);
        expect(rows[0].detail).toContain(
            'No direct workflow match found; routed via selector to Tool calling workflow (#V#tool_calling_workflow)'
        );
        expect(rows[0].detail).toContain('Tool seeking');
        expect(rows[1].detail).toContain(
            'No direct workflow match found; routed via selector to Tool calling workflow (#V#tool_calling_workflow)'
        );
        expect(rows[1].detail).toContain('Tool seeking');
    });

    test('uses teleological selector labels for workflow-dispatch LLM calls', () => {
        const rows = __testOnly_normaliseThinkingActivityHistory([
            {
                sequence_no: 1,
                status: 'llm_call_start',
                stage: 'workflow_dispatch',
                phase_label: 'Evaluating workflow candidates',
                model: 'gpt-test'
            }
        ]);

        expect(rows).toHaveLength(1);
        expect(rows[0].label).toBe('Evaluating workflow candidates');
        expect(rows[0].detail).toContain('gpt-test');
    });

    test('prefers activity history rendering and falls back to tool history', () => {
        const activityHtml = __testOnly_renderThinkingCardBodyHTML({
            activityHistory: [{
                label: 'Starting orchestrator',
                detail: '',
                state: 'pending',
                isLowLevel: false,
                groupCount: 1
            }],
            toolUseProgressHistory: [{
                tool: 'fetch_concept',
                resultSummary: 'Fetched'
            }]
        });
        expect(activityHtml).toContain('Starting orchestrator');
        expect(activityHtml).not.toContain('fetch_concept');

        const fallbackHtml = __testOnly_renderThinkingCardBodyHTML({
            toolUseProgressHistory: [{
                tool: 'fetch_concept',
                resultSummary: 'Fetched',
                success: true
            }]
        });
        expect(fallbackHtml).toContain('fetch_concept');
        expect(fallbackHtml).toContain('Fetched');
    });

    test('prefers canonical latest progress tool history over stale local tool history', () => {
        const request = {
            toolUseProgressHistory: [{
                tool: 'download_paper',
                resultSummary: 'Downloaded: 2510.06018',
                success: null
            }],
            latestProgress: {
                tool_history: [{
                    tool: 'download_paper',
                    resultSummary: 'Downloaded: 2510.06018',
                    success: true
                }],
                tool_call_count: 1,
                tool_success_count: 1,
                tool_failure_count: 0,
                tool_pending_count: 0
            },
            workflowStagePath: {
                path: [
                    { stage_id: 'tool_execute', stage_label: 'Execute tool calls' }
                ]
            }
        };

        const html = __testOnly_renderThinkingCardBodyHTML(request);
        expect(html).toContain('download_paper');
        expect(html).toContain('Downloaded: 2510.06018');
        expect(html).toContain('thinking-card-tool-status success');

        const diagnosticsPayload = __testOnly_buildThinkingDiagnosticsPayload(request);
        expect(diagnosticsPayload.tool_history).toEqual([
            expect.objectContaining({
                tool: 'download_paper',
                resultSummary: 'Downloaded: 2510.06018',
                success: true
            })
        ]);
        expect(diagnosticsPayload.stage_diagnostics).toEqual([
            expect.objectContaining({
                stage_id: 'tool_execute',
                diagnostics: expect.objectContaining({
                    tool_call_count: 1,
                    tool_success_count: 1,
                    tool_failure_count: 0,
                    tool_pending_count: 0
                })
            })
        ]);
    });

    test('syncs canonical preserved histories from progress payloads', () => {
        const phaseHistory = Array.from({ length: 45 }, (_, index) => ({
            phase: `phase_${index}`,
            phaseLabel: `Phase ${index}`,
            timestamp: index + 1
        }));
        const request = {
            activityHistory: [{ label: 'Stale row' }],
            progressEvents: [{ stage: 'stale_stage' }],
            phaseHistory: [{ phase: 'stale_phase' }],
            stageDiagnostics: [{ stage_id: 'stale_stage' }]
        };
        const progress = {
            phase_history: phaseHistory,
            progress_events: [
                { stage: 'context_build', status: 'phase_transition' },
                { stage: 'response_finalising', status: 'heartbeat' }
            ],
            activity_history: [
                { stage: 'context_build', label: 'Build context', state: 'success' },
                { stage: 'response_finalising', label: 'Finalising response', state: 'pending' }
            ],
            stage_diagnostics: [
                { stage_id: 'context_build', stage_label: 'Build context' },
                { stage_id: 'response_finalising', stage_label: 'Finalising response' }
            ]
        };

        expect(__testOnly_syncThinkingCanonicalHistoriesFromProgress(request, progress)).toBe(true);
        expect(request.phaseHistory).toEqual(progress.phase_history);
        expect(request.progressEvents).toEqual(progress.progress_events);
        expect(request.activityHistory).toEqual(progress.activity_history);
        expect(request.stageDiagnostics).toEqual(progress.stage_diagnostics);

        const diagnosticsPayload = __testOnly_buildThinkingDiagnosticsPayload({
            ...request,
            latestProgress: progress,
            clientRequestId: 'req-canonical',
            promptRaw: 'https://arxiv.org/abs/2602.20478'
        });
        expect(diagnosticsPayload.phase_history).toEqual(progress.phase_history);
        expect(diagnosticsPayload.progress_events).toEqual(progress.progress_events);
        expect(diagnosticsPayload.activity_history).toEqual(progress.activity_history);
        expect(diagnosticsPayload.stage_diagnostics).toEqual([
            expect.objectContaining({ stage_id: 'context_build' }),
            expect.objectContaining({ stage_id: 'response_finalising' })
        ]);
        expect(diagnosticsPayload.phase_history).toHaveLength(45);
        expect(diagnosticsPayload.phase_history[0]).toEqual(expect.objectContaining({ phase: 'phase_0' }));
    });

    test('syncs canonical state from turn execution diagnostics payloads', () => {
        const request = {
            latestProgress: { stage: 'stale_stage' },
            workflowStagePath: { path: [{ stage_id: 'stale_stage' }] },
            workflowDiscovery: { match_count: 0 },
            phaseHistory: [{ phase: 'stale_phase' }],
            progressEvents: [{ stage: 'stale_stage' }],
            activityHistory: [{ label: 'Stale activity' }],
            stageDiagnostics: [{ stage_id: 'stale_stage' }]
        };
        const diagnostics = {
            latest_progress: {
                stage: 'completion_gate',
                completion_gate_decision: 'follow_up_required'
            },
            workflow_stage_path: {
                path: [
                    { stage_id: 'workflow_dispatch_prepare', stage_label: 'Workflow dispatch preparation' },
                    { stage_id: 'completion_gate', stage_label: 'Completion gate' }
                ]
            },
            workflow_discovery: {
                match_count: 1,
                matches: [
                    {
                        concept_id: '#V#tool_calling_workflow',
                        name: 'Tool calling workflow'
                    }
                ]
            },
            workflow_selection: {
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow',
                selector_verdict: 'tool_seeking',
                selector_source: 'selector'
            },
            workflow_routing_diagnostics: {
                schema_version: 'workflow_routing_diagnostics.v1',
                selected_workflow_id: '#V#tool_calling_workflow',
                dispatch: {
                    dispatch_workflow_id: '#V#tool_calling_workflow',
                    dispatch_terminal_status: 'completed'
                }
            },
            phase_history: [{ phase: 'completion_gate' }],
            progress_events: [{ stage: 'completion_gate', status: 'completed' }],
            activity_history: [{ label: 'Completion gate', state: 'success' }],
            stage_diagnostics: [
                {
                    stage_id: 'completion_gate',
                    stage_label: 'Completion gate',
                    completion_gate: {
                        decision: 'follow_up_required',
                        blocking_effect_ids: ['effect_tool_execution_1']
                    }
                }
            ]
        };

        expect(__testOnly_syncThinkingCanonicalStateFromTurnExecutionDiagnostics(request, diagnostics)).toBe(true);
        expect(request.latestProgress).toEqual(diagnostics.latest_progress);
        expect(request.workflowStagePath).toEqual(diagnostics.workflow_stage_path);
        expect(request.workflowDiscovery).toEqual(diagnostics.workflow_discovery);
        expect(request.workflowSelection).toEqual(diagnostics.workflow_selection);
        expect(request.workflowRoutingDiagnostics).toEqual(diagnostics.workflow_routing_diagnostics);
        expect(request.phaseHistory).toEqual(diagnostics.phase_history);
        expect(request.progressEvents).toEqual(diagnostics.progress_events);
        expect(request.activityHistory).toEqual(diagnostics.activity_history);
        expect(request.stageDiagnostics).toEqual(diagnostics.stage_diagnostics);
    });

    test('prefers backend elapsed time and preserves canonical workflow selection and routing diagnostics', () => {
        const diagnosticsPayload = __testOnly_buildThinkingDiagnosticsPayload({
            clientRequestId: 'req-routing',
            promptRaw: 'Run the meeting invitation test',
            thinkingStartedAtMs: Date.now() - 20000,
            workflowSelection: {
                selected_workflow_id: '#V#tool_calling_workflow',
                selector_verdict: 'tool_seeking',
                selector_source: 'selector'
            },
            workflowRoutingDiagnostics: {
                schema_version: 'workflow_routing_diagnostics.v1',
                selected_workflow_id: '#V#tool_calling_workflow',
                dispatch: {
                    dispatch_workflow_id: '#V#tool_calling_workflow'
                },
                selector: {
                    prompt: { text: 'Select workflow', char_count: 15 },
                    response: { text: '#V#tool_calling_workflow', char_count: 24 },
                    fallback_used: true,
                    primary_fallback_failure_kind: 'provider_unreachable'
                }
            },
            latestProgress: {
                elapsed_ms: 85641
            }
        });

        expect(diagnosticsPayload.elapsed_ms).toBe(85641);
        expect(diagnosticsPayload.workflow_selection).toEqual(
            expect.objectContaining({
                selected_workflow_id: '#V#tool_calling_workflow',
                selector_verdict: 'tool_seeking',
                selector_source: 'selector'
            })
        );
        expect(diagnosticsPayload.workflow_routing_diagnostics).toEqual(
            expect.objectContaining({
                schema_version: 'workflow_routing_diagnostics.v1',
                selected_workflow_id: '#V#tool_calling_workflow',
                selector: expect.objectContaining({
                    fallback_used: true,
                    primary_fallback_failure_kind: 'provider_unreachable'
                })
            })
        );
    });

    test('surfaces live prepared and sent LLM request previews in stage diagnostics', () => {
        const request = {
            clientRequestId: 'req-live-llm',
            thinkingCardMode: 'debug',
            promptRaw: 'tell me about the current user',
            latestProgress: {
                status: 'llm_call_start',
                stage: 'workflow_dispatch_prepare',
                phase: 'workflow_dispatch_prepare',
                model: 'gemma4:26b',
                provider: 'ollama',
                llm_request_state: 'sent',
                llm_request_prepared_at_utc: '2026-04-15T01:02:03Z',
                llm_request_sent_at_utc: '2026-04-15T01:02:04Z',
                fallback_attempt_no: 1,
                fallback_candidate_count: 3,
                llm_request: {
                    prompt: {
                        text: 'Answer using the authenticated current user context.',
                        char_count: 49
                    },
                    context_summary: {
                        message_count: 3,
                        leading_system_message_count: 1,
                        role_counts: { system: 1, user: 1, assistant: 1 },
                        total_content_chars: 128
                    },
                    context_message_count: 3
                }
            },
            workflowStagePath: {
                path: [
                    {
                        stage_id: 'workflow_dispatch_prepare',
                        stage_label: 'Workflow dispatch preparation'
                    }
                ]
            },
            stageDiagnostics: [
                {
                    stage_id: 'workflow_dispatch_prepare',
                    stage_label: 'Workflow dispatch preparation',
                    llm_input_recorded: false,
                    llm_output_recorded: false,
                    llm_exchange_record_count: 0
                }
            ]
        };

        const diagnosticsPayload = __testOnly_buildThinkingDiagnosticsPayload(request);
        expect(diagnosticsPayload.schema_version).toBe('thinking_diagnostics_snapshot.v1');
        expect(diagnosticsPayload.stage_diagnostics).toEqual([
            expect.objectContaining({
                stage_id: 'workflow_dispatch_prepare',
                diagnostics: expect.objectContaining({
                    llm_input_recorded: true,
                    llm_exchange_record_count: 1,
                    llm_request_state: 'sent',
                    llm_selected_model: 'gemma4:26b',
                    latest_llm_exchange: expect.objectContaining({
                        entry_type: 'live_llm_request',
                        llm_request_state: 'sent',
                        selected_model: 'gemma4:26b',
                        prompt_preview: expect.objectContaining({
                            text: 'Answer using the authenticated current user context.'
                        }),
                        context_summary: expect.objectContaining({
                            message_count: 3
                        })
                    })
                })
            })
        ]);

        const html = __testOnly_renderThinkingCardBodyHTML(request);
        expect(html).toContain('Prepared prompt preview');
        expect(html).toContain('Answer using the authenticated current user context.');
        expect(html).toContain('LLM request state');
        expect(html).toContain('Sent');
    });

    test('renders expected-outcome and selector stages with context-lineage diagnostics', () => {
        const request = {
            clientRequestId: 'req-1888',
            thinkingCardMode: 'debug',
            promptRaw: 'What papers of mine do you know about?',
            latestProgress: {
                status: 'llm_call_start',
                stage: 'selector_decision',
                phase: 'selector_decision',
                model: 'gpt-5-mini',
                provider: 'openai',
                llm_request_state: 'sent',
                llm_request: {
                    prompt: {
                        text: 'Select the best workflow for this grounded authorship question.',
                        char_count: 61
                    },
                    context_summary: {
                        message_count: 4,
                        leading_system_message_count: 2,
                        role_counts: { system: 2, user: 1, assistant: 1 },
                        total_content_chars: 220
                    },
                    context_lineage: {
                        base_context_source: 'augmented_context',
                        insertion_strategy: 'after_leading_system',
                        stage_added_message_count: 1,
                        stage_added_messages: [
                            { role: 'system', preview: 'Expected answer contract for this turn:' }
                        ],
                        base_context_summary: {
                            message_count: 3,
                            role_counts: { system: 1, user: 1, assistant: 1 },
                            total_content_chars: 180
                        },
                        stage_context_summary: {
                            message_count: 4,
                            role_counts: { system: 2, user: 1, assistant: 1 },
                            total_content_chars: 220
                        }
                    },
                    context_message_count: 4
                }
            },
            workflowSelection: {
                selected_workflow_id: '#V#chat_assistant_workflow',
                selected_workflow_name: 'Chat assistant workflow',
                selector_verdict: 'direct_response',
                selector_source: 'selector',
                workflow_match_count: 0,
                workflow_candidate_count: 2
            },
            workflowRoutingDiagnostics: {
                schema_version: 'workflow_routing_diagnostics.v1',
                selected_workflow_id: '#V#chat_assistant_workflow',
                selector: {
                    prompt_id: '#V#workflow_selector_prompt',
                    requested_prompt_ids: ['#V#workflow_selector_prompt'],
                    prompt: {
                        text: 'Select the best workflow for this grounded authorship question.',
                        char_count: 61
                    },
                    candidate_list: {
                        text: '- #V#chat_assistant_workflow\n- #V#scholarly_paper_representation_workflow',
                        char_count: 78
                    },
                    response: { text: '#V#chat_assistant_workflow', char_count: 25 }
                }
            },
            workflowStagePath: {
                path: [
                    { stage_id: 'expected_outcome_inference', stage_label: 'Infer expected outcome' },
                    { stage_id: 'selector_preparation', stage_label: 'Prepare selector context' },
                    { stage_id: 'selector_decision', stage_label: 'Select workflow' }
                ]
            },
            stageDiagnostics: [
                {
                    stage_id: 'expected_outcome_inference',
                    diagnostics: {
                        stage_id: 'expected_outcome_inference',
                        stage_label: 'Infer expected outcome',
                        latest_result_summary: 'Grounded authorship answer contract captured',
                        latest_llm_exchange: {
                            entry_type: 'live_llm_request',
                            prompt_preview: {
                                text: 'Infer what would count as a grounded answer to the user.',
                                char_count: 58
                            },
                            response_preview: {
                                text: 'Only list papers that can be justified as authored by the user; prefer explicit uncertainty to unsupported inclusion.',
                                char_count: 116
                            }
                        }
                    }
                },
                {
                    stage_id: 'selector_preparation',
                    diagnostics: {
                        stage_id: 'selector_preparation',
                        stage_label: 'Prepare selector context',
                        latest_llm_exchange: {
                            entry_type: 'workflow_selector_prompt',
                            prompt_id: '#V#workflow_selector_prompt',
                            requested_prompt_ids: ['#V#workflow_selector_prompt'],
                            prompt_preview: {
                                text: 'Select the best workflow for this grounded authorship question.',
                                char_count: 61
                            },
                            candidate_list_preview: {
                                text: '- #V#chat_assistant_workflow\n- #V#scholarly_paper_representation_workflow',
                                char_count: 78
                            },
                            context_lineage: {
                                base_context_source: 'augmented_context',
                                stage_added_message_count: 1
                            }
                        }
                    }
                }
            ]
        };

        const diagnosticsPayload = __testOnly_buildThinkingDiagnosticsPayload(request);
        expect(diagnosticsPayload.stage_diagnostics).toEqual(expect.arrayContaining([
            expect.objectContaining({
                stage_id: 'selector_decision',
                diagnostics: expect.objectContaining({
                    llm_context_lineage: expect.objectContaining({
                        base_context_source: 'augmented_context',
                        stage_added_message_count: 1
                    })
                })
            })
        ]));

        const html = __testOnly_renderThinkingCardBodyHTML(request);
        expect(html).toContain('Infer expected outcome');
        expect(html).toContain('Prepare selector context');
        expect(html).toContain('Select workflow');
        expect(html).toContain('Context lineage');
        expect(html).toContain('Base context source: Augmented context');
        expect(html).toContain('Expected answer contract for this turn:');
    });

    test('separates default, expert, and debug thinking-card detail modes', () => {
        const createModeRequest = (mode) => ({
            thinkingCardMode: mode,
            clientRequestId: 'req-modes',
            promptRaw: 'Find the relevant workflow and explain the route.',
            latestProgress: {
                status: 'llm_call_start',
                phase: 'selector_decision',
                stage: 'selector_decision',
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow',
                workflow_selector_verdict: 'tool_seeking',
                workflow_selector_source: 'selector',
                workflow_match_count: 1,
                workflow_candidate_count: 2,
                model: 'gpt-5-mini',
                provider: 'openai',
                fallback_attempt_no: 2,
                fallback_candidate_count: 3,
                llm_request_state: 'sent',
                llm_request: {
                    prompt: {
                        text: 'Select a workflow using represented workflow metadata.',
                        char_count: 54
                    },
                    context_summary: {
                        message_count: 4,
                        total_content_chars: 320
                    },
                    context_lineage: {
                        base_context_source: 'augmented_context',
                        stage_added_message_count: 1
                    }
                }
            },
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_discovery', stage_label: 'Workflow discovery' },
                    { stage_id: 'selector_decision', stage_label: 'Select workflow' }
                ]
            },
            workflowDiscovery: {
                match_count: 1,
                candidate_count: 2,
                candidates: [
                    {
                        concept_id: '#V#tool_calling_workflow',
                        name: 'Tool calling workflow',
                        relevance_score: 0.92
                    }
                ],
                matches: [
                    {
                        concept_id: '#V#tool_calling_workflow',
                        name: 'Tool calling workflow'
                    }
                ]
            }
        });

        const defaultHtml = __testOnly_renderThinkingCardBodyHTML(createModeRequest('default'));
        const defaultContainer = document.createElement('div');
        defaultContainer.innerHTML = defaultHtml;
        expect(defaultHtml).toContain('thinking-card-synopsis');
        expect(defaultHtml).toContain('Objective');
        expect(defaultHtml).toContain('LLM input');
        expect(defaultContainer.textContent).toContain('Selected Tool calling workflow');
        expect(defaultHtml).not.toContain('Stage id');
        expect(defaultHtml).not.toContain('Prepared prompt preview');
        expect(defaultHtml).not.toContain('Context lineage');

        const expertHtml = __testOnly_renderThinkingCardBodyHTML(createModeRequest('expert'));
        expect(expertHtml).toContain('Selected workflow');
        expect(expertHtml).toContain('Selector verdict');
        expect(expertHtml).toContain('LLM model');
        expect(expertHtml).toContain('Fallback attempt');
        expect(expertHtml).toContain('Workflow candidates');
        expect(expertHtml).not.toContain('Prepared prompt preview');
        expect(expertHtml).not.toContain('Context lineage');
        expect(expertHtml).not.toContain('LLM provider');

        const debugHtml = __testOnly_renderThinkingCardBodyHTML(createModeRequest('debug'));
        expect(debugHtml).toContain('Stage id');
        expect(debugHtml).toContain('Prepared prompt preview');
        expect(debugHtml).toContain('Context lineage');
        expect(debugHtml).toContain('LLM provider');
    });

    test('renders LLM timing baselines only when enough observations exist', () => {
        const baseRequest = {
            thinkingCardMode: 'expert',
            clientRequestId: 'req-timing',
            latestProgress: {
                status: 'completed',
                phase: 'completed',
                stage: 'completed'
            },
            timingBreakdown: {
                llm_calls_by_stage_model: [
                    {
                        stage: 'selector_decision',
                        model: 'gpt-baseline',
                        provider: 'openai',
                        call_count: 1,
                        duration_ms: 420,
                        historical_observation_count: 5,
                        historical_mean_duration_ms: 250,
                        historical_stddev_duration_ms: 40,
                        duration_deviation_classification: 'slower_than_usual'
                    }
                ]
            }
        };

        const html = __testOnly_renderThinkingCardBodyHTML(baseRequest);
        expect(html).toContain('LLM timing');
        expect(html).toContain('gpt-baseline');
        expect(html).toContain('usual');
        expect(html).toContain('slower than usual');

        const sparseHtml = __testOnly_renderThinkingCardBodyHTML({
            ...baseRequest,
            timingBreakdown: {
                llm_calls_by_stage_model: [
                    {
                        stage: 'selector_decision',
                        model: 'gpt-baseline',
                        provider: 'openai',
                        call_count: 1,
                        duration_ms: 420,
                        historical_observation_count: 2,
                        historical_mean_duration_ms: 250
                    }
                ]
            }
        });
        expect(sparseHtml).toContain('LLM timing');
        expect(sparseHtml).toContain('gpt-baseline');
        expect(sparseHtml).not.toContain('usual');
    });

    test('builds a canonical progress view model from explicit turn state', () => {
        const viewModel = __testOnly_buildThinkingCardProgressViewModel({
            latestProgress: {
                thinking_card_view_model: {
                    objective_summary: 'Find represented papers connected to the authenticated user.',
                    object_or_target_summary: 'Represented paper and author memory.',
                    route_summary: 'Answer from Vontology profile memory.',
                    evidence_context_summary: 'Using represented papers, author links, and recent conversation context.',
                    current_activity: 'Preparing the grounded paper answer.',
                    llm_input_lifecycle: {
                        state: 'sent',
                        prompt_preview: {
                            text: 'Summarise the represented papers connected to the current user.'
                        },
                        context_message_count: 5,
                        tool_definition_count: 2,
                        model: 'gpt-5-mini'
                    },
                    wait_state: {
                        state: 'waiting',
                        label: 'Waiting for model output'
                    },
                    confirmation_requirement: 'No external action will be taken without confirmation.',
                    missing_telemetry: ['No model output has been reported yet.']
                }
            }
        });

        expect(viewModel).toEqual(expect.objectContaining({
            schema_version: 'thinking_card_progress_view_model.v1',
            source_authority: 'explicit_turn_state',
            objective_summary: 'Find represented papers connected to the authenticated user.',
            object_or_target_summary: 'Represented paper and author memory.',
            route_summary: 'Answer from Vontology profile memory.',
            confirmation_requirement: 'No external action will be taken without confirmation.',
            missing_telemetry: ['No model output has been reported yet.']
        }));
        expect(viewModel.llm_input_lifecycle).toEqual(expect.objectContaining({
            state: 'sent',
            model: 'gpt-5-mini',
            context_message_count: 5,
            tool_definition_count: 2
        }));
    });

    test('renders default paper-memory synopsis without foregrounding debug noise', () => {
        const request = {
            clientRequestId: 'req-paper-default',
            promptRaw: 'what papers of mine do you know about?',
            latestProgress: {
                status: 'llm_call_start',
                phase: 'workflow_dispatch_prepare',
                stage: 'workflow_dispatch_prepare',
                objective_summary: 'Looking for represented papers connected to the current user.',
                working_object_summary: 'Represented paper, author, and project memory.',
                evidence_context_summary: 'Using Vontology profile memory and recent conversation context.',
                confirmation_requirement: 'No external action will be taken without confirmation.',
                selected_workflow_id: '#V#scholarly_paper_representation_workflow',
                selected_workflow_name: 'Scholarly paper representation workflow',
                workflow_selector_verdict: 'rag_selected',
                workflow_selector_source: 'selector',
                model: 'gpt-5-mini',
                provider: 'openai',
                llm_request_state: 'sent',
                llm_request: {
                    prompt: {
                        text: 'Answer with represented paper records and cite uncertainty.'
                    },
                    context_summary: {
                        message_count: 4,
                        total_content_chars: 900
                    }
                }
            },
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_dispatch_prepare', stage_label: 'Workflow dispatch preparation' }
                ]
            }
        };

        const html = __testOnly_renderThinkingCardBodyHTML(request);
        const container = document.createElement('div');
        container.innerHTML = html;
        const text = container.textContent;

        expect(html).toContain('thinking-card-synopsis');
        expect(text).toContain('Looking for represented papers connected to the current user.');
        expect(text).toContain('Represented paper, author, and project memory.');
        expect(text).toContain('Scholarly paper representation workflow');
        expect(text).toContain('Using Vontology profile memory and recent conversation context.');
        expect(text).toContain('LLM input sent; waiting for model output');
        expect(text).toContain('Answer with represented paper records and cite uncertainty.');
        expect(text).toContain('No external action will be taken without confirmation.');
        expect(html).not.toContain('data-thinking-diagnostic-key');
        expect(html).not.toContain('Stage id');
        expect(html).not.toContain('LLM provider');
        expect(html).not.toContain('openai');
        expect(html).not.toContain('req-paper-default');
    });

    test('renders default conference-planning synopsis with constraints and confirmation boundary', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            promptRaw: 'help me plan what to do before the ICML deadline',
            latestProgress: {
                status: 'llm_request_prepared',
                phase: 'workflow_dispatch_prepare',
                stage: 'workflow_dispatch_prepare',
                objective_summary: 'Identify the conference-planning objective and next actions.',
                target_summary: 'ICML deadline, submission tasks, collaborators, and planning constraints.',
                route_summary: 'Draft a planning response from represented commitments and conversation context.',
                evidence_summary: 'Checking represented commitments, papers, deadline notes, and recent turns.',
                confirmation_requirement: 'Any booking, submission, or message would require confirmation first.',
                model: 'gpt-5-mini',
                llm_request_state: 'prepared',
                llm_request: {
                    prompt: {
                        text: 'Draft a conference-planning response and flag actions requiring approval.'
                    },
                    context_message_count: 6,
                    tool_count: 3
                }
            }
        });
        const container = document.createElement('div');
        container.innerHTML = html;
        const text = container.textContent;

        expect(text).toContain('Identify the conference-planning objective and next actions.');
        expect(text).toContain('ICML deadline, submission tasks, collaborators, and planning constraints.');
        expect(text).toContain('Checking represented commitments, papers, deadline notes, and recent turns.');
        expect(text).toContain('Any booking, submission, or message would require confirmation first.');
        expect(text).toContain('LLM input prepared');
        expect(text).toContain('Draft a conference-planning response and flag actions requiring approval.');
        expect(html).not.toContain('{');
        expect(html).not.toContain('workflow_dispatch_prepare');
    });

    test('shows missing live telemetry as an explicit default synopsis gap', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            promptRaw: 'tell me about the current user',
            latestProgress: {
                status: 'heartbeat',
                phase: 'workflow_dispatch_prepare',
                stage: 'workflow_dispatch_prepare',
                phase_label: 'Preparing workflow dispatch',
                liveness_state: 'waiting'
            }
        });

        const container = document.createElement('div');
        container.innerHTML = html;
        expect(container.textContent).toContain('Telemetry gap');
        expect(container.textContent).toContain('Prepared LLM input has not been reported for the current stage yet.');
        expect(container.textContent).toContain('Waiting at Preparing workflow dispatch');
    });

    test('copies progress view model in thinking and keyboard diagnostic exports', () => {
        const request = {
            clientRequestId: 'req-view-model-export',
            thinkingCardMode: 'expert',
            promptRaw: 'which route are you using?',
            latestProgress: {
                objective_summary: 'Explain the selected answer route.',
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow',
                llm_request_state: 'sent',
                llm_request: {
                    prompt: { text: 'Explain route provenance.' },
                    context_message_count: 2
                }
            }
        };

        const diagnosticsPayload = __testOnly_buildThinkingDiagnosticsPayload(request);
        expect(diagnosticsPayload.progress_view_model).toEqual(expect.objectContaining({
            schema_version: 'thinking_card_progress_view_model.v1',
            objective_summary: 'Explain the selected answer route.',
            source_authority: 'derived_from_live_telemetry'
        }));
        expect(diagnosticsPayload.progress_view_model.llm_input_lifecycle).toEqual(
            expect.objectContaining({
                state: 'sent',
                prompt_preview: expect.objectContaining({ text: 'Explain route provenance.' })
            })
        );

        __testOnly_setThinkingCardRequests(request, null);
        const exportPayload = __testOnly_buildDiagnosticsExportRequestPayload();
        expect(exportPayload.thinking_card_mode).toBe('expert');
        expect(exportPayload.thinking_card_progress_view_model).toEqual(
            expect.objectContaining({
                schema_version: 'thinking_card_progress_view_model.v1',
                objective_summary: 'Explain the selected answer route.'
            })
        );
    });

    test('renders preserved stage diagnostics when workflow stage path is absent', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            latestProgress: {
                status: 'heartbeat',
                stage: 'response_finalising'
            },
            stageDiagnostics: [
                {
                    stage_id: 'context_build',
                    label: 'Build context',
                    detail: 'Initialising request context',
                    state: 'success',
                    diagnostics: {
                        stage_id: 'context_build',
                        stage_label: 'Build context',
                        stage_event_count: 2,
                        latest_status: 'thinking'
                    }
                },
                {
                    stage_id: 'response_finalising',
                    label: 'Finalising response',
                    detail: 'Persisting conversation history',
                    state: 'pending',
                    diagnostics: {
                        stage_id: 'response_finalising',
                        stage_label: 'Finalising response',
                        stage_event_count: 1,
                        latest_status: 'heartbeat'
                    }
                }
            ]
        });

        expect(html).toContain('Build context');
        expect(html).toContain('Initialising request context');
        expect(html).toContain('Finalising response');
        expect(html).toContain('Persisting conversation history');
    });

    test('renders canonical workflow stages with selected workflow details', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_discovery', stage_label: 'Workflow discovery' },
                    { stage_id: 'workflow_dispatch', stage_label: 'Workflow dispatch' },
                    { stage_id: 'tool_plan', stage_label: 'Plan tool calls' }
                ]
            },
            workflowDiscovery: {
                match_count: 1,
                matches: [
                    {
                        concept_id: '#V#tool_calling_workflow',
                        name: 'Tool calling workflow'
                    }
                ]
            },
            latestProgress: {
                phase: 'workflow_dispatch',
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow',
                workflow_selector_verdict: 'tool_seeking',
                workflow_selector_source: 'selector'
            }
        });

        const container = document.createElement('div');
        container.innerHTML = html;
        expect(html).toContain('Workflow discovery');
        expect(container.textContent).toContain('Found Tool calling workflow');
        expect(html).toContain('Tool calling workflow (#V#tool_calling_workflow)');
        expect(html).toContain('Workflow dispatch');
        expect(container.textContent).toContain('Selected Tool calling workflow');
        expect(html).toContain('Source: Selector');
        expect(html).toContain('thinking-card-concept-link');
        expect(html).toContain('data-concept-id="#V#tool_calling_workflow"');
        expect(html).toContain('Plan tool calls');
    });

    test('renders workflow dispatch preparation with object-centred pre-dispatch detail', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_dispatch_prepare', stage_label: 'Workflow dispatch preparation' }
                ]
            },
            workflowDiscovery: {
                match_count: 1,
                matches: [
                    {
                        concept_id: '#V#tool_calling_workflow',
                        name: 'Tool calling workflow'
                    }
                ]
            },
            stageDiagnostics: [
                {
                    stage_id: 'workflow_dispatch_prepare',
                    stage_label: 'Workflow dispatch preparation',
                    selected_workflow_id: '#V#tool_calling_workflow',
                    selected_workflow_name: 'Tool calling workflow',
                    pre_dispatch: {
                        step_count: 2,
                        completed_step_count: 2,
                        failed_step_count: 0,
                        total_duration_ms: 19,
                        slowest_step_label: 'Load workflow contract',
                        slowest_step_duration_ms: 12,
                        steps: [
                            {
                                step_id: 'load_contract',
                                step_label: 'Load workflow contract',
                                result_summary: 'Loaded the launch contract from workflow metadata.',
                                status: 'completed',
                                duration_ms: 12
                            },
                            {
                                step_id: 'launchability_probe',
                                step_label: 'Evaluate launch requirements for Tool calling workflow',
                                result_summary: 'All launch requirements satisfied.',
                                status: 'completed',
                                duration_ms: 7
                            }
                        ]
                    }
                }
            ],
            latestProgress: {
                phase: 'workflow_dispatch_prepare',
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow'
            }
        });

        expect(html).toContain('Workflow dispatch preparation');
        expect(html).toContain('Preparing Tool calling workflow (#V#tool_calling_workflow) for dispatch');
        expect(html).toContain('All launch requirements satisfied.');
        expect(html).toContain('Pre-dispatch checks');
        expect(html).toContain('Load workflow contract');
        expect(html).not.toContain('No recorded LLM input/output');
    });

    test('renders tool planning against the selected workflow instead of generic mechanism text', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'tool_plan', stage_label: 'Tool-call planning' }
                ]
            },
            workflowDiscovery: {
                match_count: 1,
                matches: [
                    {
                        concept_id: '#V#tool_calling_workflow',
                        name: 'Tool calling workflow'
                    }
                ]
            },
            stageDiagnostics: [
                {
                    stage_id: 'tool_plan',
                    stage_label: 'Tool-call planning',
                    latest_workflow_task: 'fetch_concept',
                    selected_workflow_id: '#V#tool_calling_workflow',
                    selected_workflow_name: 'Tool calling workflow',
                    tool_execution: {
                        planned_count: 2,
                        started_count: 0,
                        executed_count: 0,
                        invocation_count: 0,
                        successful_invocation_count: 0,
                        failed_invocation_count: 0,
                        blocked_invocation_count: 0,
                        zero_tools_executed: false,
                        failure_codes: []
                    }
                }
            ],
            latestProgress: {
                phase: 'tool_plan',
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow'
            }
        });

        expect(html).toContain('Planning 2 tool calls for Tool calling workflow (#V#tool_calling_workflow) · fetch_concept');
        expect(html).toContain('Planned tool calls');
        expect(html).not.toContain('No recorded LLM input/output');
    });

    test('renders screen and narration backfill stages with source-aware summaries', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'screen_backfill', stage_label: 'Screen backfill' },
                    { stage_id: 'narration', stage_label: 'Narration rendering' }
                ]
            },
            stageDiagnostics: [
                {
                    stage_id: 'screen_backfill',
                    stage_label: 'Screen backfill',
                    response_transformation: {
                        status: 'fallback_success',
                        source_path: 'response_text_plus_follow_up_summary',
                        latency_ms: 44,
                        model_id: 'gpt-4.1-mini',
                        input_summary: {
                            needs_backfill: true,
                            tool_message_count: 3
                        },
                        output_summary: {
                            applied: true,
                            presenter_format: 'screen_backfill_from_response_with_operational_summary_v1'
                        }
                    }
                },
                {
                    stage_id: 'narration',
                    stage_label: 'Narration rendering',
                    response_transformation: {
                        status: 'fallback_success',
                        source_path: 'screen_text_fallback',
                        latency_ms: 18,
                        model_id: 'gpt-4.1-mini',
                        input_summary: {
                            needs_backfill: true,
                            tool_message_count: 3
                        },
                        output_summary: {
                            applied: true,
                            presenter_format: 'narration_fallback_v1'
                        }
                    }
                }
            ],
            latestProgress: {
                phase: 'narration'
            }
        });

        expect(html).toContain('Preparing the on-screen response from the drafted reply and follow-up summary');
        expect(html).toContain('Preparing spoken narration from the prepared screen response');
        expect(html).toContain('Presenter format');
        expect(html).toContain('screen_backfill_from_response_with_operational_summary_v1');
        expect(html).toContain('narration_fallback_v1');
    });

    test('renders postcondition critic and completion gate with unresolved outcome detail', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'postcondition_critic', stage_label: 'Postcondition critic' },
                    { stage_id: 'completion_gate', stage_label: 'Completion gate' }
                ]
            },
            stageDiagnostics: [
                {
                    stage_id: 'postcondition_critic',
                    stage_label: 'Postcondition critic',
                    critic_verdict: {
                        workflow_id: '#V#kb_mutation_postcondition_critic_workflow',
                        has_unresolved_checks: true,
                        unresolved_check_count: 2,
                        summary: {
                            verified_count: 1,
                            not_verified_count: 1,
                            inconclusive_count: 1,
                            error_count: 0
                        }
                    }
                },
                {
                    stage_id: 'completion_gate',
                    stage_label: 'Completion gate',
                    completion_gate: {
                        decision: 'follow_up_required',
                        decision_reason: 'required_effects_unresolved',
                        requires_follow_up: true,
                        safe_to_claim_completion: false,
                        blocking_effect_ids: ['effect_tool_execution_1'],
                        blocking_failure_codes: ['tool_execution_required_but_not_observed']
                    }
                }
            ],
            latestProgress: {
                phase: 'completion_gate'
            }
        });

        expect(html).toContain('Found 2 unresolved postcondition checks');
        expect(html).toContain('Follow-up is still required · 1 blocking effect');
        expect(html).toContain('Blocking effects');
        expect(html).toContain('effect_tool_execution_1');
        expect(html).toContain('tool_execution_required_but_not_observed');
    });

    test('explains discovery-to-dispatch routing when selector chooses a workflow after no direct match', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_discovery', stage_label: 'Workflow discovery' },
                    { stage_id: 'workflow_dispatch', stage_label: 'Workflow dispatch' },
                    { stage_id: 'tool_execute', stage_label: 'Execute tool calls' }
                ]
            },
            workflowDiscovery: {
                match_count: 0,
                candidate_count: 0,
                matches: []
            },
            latestProgress: {
                phase: 'workflow_dispatch',
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow',
                workflow_selector_verdict: 'tool_seeking',
                workflow_selector_source: 'selector'
            }
        });

        expect(html).toContain('No direct workflow match found; routed via selector to <button');
        expect(html).toContain('Tool calling workflow (#V#tool_calling_workflow)');
        expect(html).toContain('Routing rationale');
        expect(html).toContain('Dispatch outcome');
        expect(html).toContain('Tool seeking');
        expect(html).not.toContain('thinking-card-tool-status failure');
    });

    test('renders preserved earlier stage summaries even when latest progress is only finalising', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'context_build', stage_label: 'Build context' },
                    { stage_id: 'workflow_discovery', stage_label: 'Workflow discovery' },
                    { stage_id: 'response_finalising', stage_label: 'Finalising response' }
                ]
            },
            workflowDiscovery: {
                match_count: 0,
                matches: []
            },
            stageDiagnostics: [
                {
                    stage_id: 'context_build',
                    stage_label: 'Build context',
                    event_count: 1,
                    latest_status: 'thinking',
                    latest_result_summary: 'Resolving session scope and chat history.'
                },
                {
                    stage_id: 'workflow_discovery',
                    stage_label: 'Workflow discovery',
                    event_count: 1,
                    latest_status: 'thinking',
                    latest_result_summary: 'Searching applicable workflows for the turn.'
                },
                {
                    stage_id: 'response_finalising',
                    stage_label: 'Finalising response',
                    event_count: 52,
                    latest_status: 'heartbeat',
                    latest_result_summary: 'Assembling the final response payload.'
                }
            ],
            latestProgress: {
                phase: 'response_finalising',
                status: 'heartbeat',
                result_summary: 'Assembling the final response payload.'
            }
        });

        expect(html).toContain('Build context');
        expect(html).toContain('Resolving session scope and chat history.');
        expect(html).toContain('Workflow discovery');
        expect(html).toContain('No direct workflow match found');
        expect(html).toContain('Finalising response');
        expect(html).toContain('Assembling the final response payload.');
    });

    test('preserves selected custom workflow identity through finalising render stages', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                workflow_id: '#V#chat_narration_workflow',
                workflow_id_source: 'mapped_stage_consensus',
                path: [
                    { stage_id: 'workflow_dispatch_prepare', stage_label: 'Workflow dispatch preparation' },
                    { stage_id: 'screen_backfill', stage_label: 'Screen backfill' },
                    { stage_id: 'narration', stage_label: 'Narration rendering' },
                    { stage_id: 'response_finalising', stage_label: 'Finalising response' }
                ]
            },
            workflowDiscovery: {
                match_count: 1,
                matches: [
                    {
                        concept_id: '#V#arxiv_paper_representation_workflow',
                        name: 'Arxiv paper representation workflow'
                    }
                ]
            },
            workflowSelection: {
                selected_workflow_id: '#V#arxiv_paper_representation_workflow',
                selector_verdict: 'rag_selected',
                selector_source: 'selector'
            },
            workflowRoutingDiagnostics: {
                schema_version: 'workflow_routing_diagnostics.v1',
                selected_workflow_id: '#V#arxiv_paper_representation_workflow',
                dispatch: {
                    selected_execution_mode: 'custom_workflow',
                    dispatch_workflow_id: '#V#arxiv_paper_representation_workflow',
                    dispatch_terminal_status: 'completed'
                }
            },
            stageDiagnostics: [
                {
                    stage_id: 'workflow_dispatch_prepare',
                    stage_label: 'Workflow dispatch preparation',
                    latest_result_summary: 'Preparing workflow dispatch',
                    latest_subtask: 'Load workflow launch contract'
                },
                {
                    stage_id: 'screen_backfill',
                    stage_label: 'Screen backfill',
                    latest_result_summary: 'Preparing workflow dispatch',
                    response_transformation: {
                        status: 'fallback_success',
                        source_path: 'response_text',
                        output_summary: {
                            applied: true,
                            presenter_format: 'screen_backfill_from_response_with_operational_summary_v1'
                        }
                    }
                },
                {
                    stage_id: 'narration',
                    stage_label: 'Narration rendering',
                    latest_result_summary: 'Preparing workflow dispatch',
                    response_transformation: {
                        status: 'fallback_success',
                        source_path: 'screen_text_fallback',
                        output_summary: {
                            applied: true,
                            presenter_format: 'narration_fallback_v1'
                        }
                    }
                },
                {
                    stage_id: 'response_finalising',
                    stage_label: 'Finalising response',
                    latest_result_summary: 'Assembling the final response payload.'
                }
            ],
            latestProgress: {
                phase: 'response_finalising',
                status: 'heartbeat',
                result_summary: 'Assembling the final response payload.'
            }
        });

        expect(html).toContain(
            'Preparing Arxiv paper representation workflow (#V#arxiv_paper_representation_workflow) for dispatch'
        );
        expect(html).toContain(
            'Preparing the on-screen response from the drafted reply · Workflow: Arxiv paper representation workflow (#V#arxiv_paper_representation_workflow)'
        );
        expect(html).toContain(
            'Preparing spoken narration from the prepared screen response · Workflow: Arxiv paper representation workflow (#V#arxiv_paper_representation_workflow)'
        );
        expect(html).toContain('Routing rationale');
        expect(html).not.toContain('Preparing workflow dispatch</summary>');
    });

    test('dispatches concept selection from thinking card workflow links', () => {
        document.body.innerHTML = '<div id="thinkingCardDetailTest"></div>';
        const onSelect = jest.fn();
        document.addEventListener('von:selectConceptById', onSelect);

        const container = document.getElementById('thinkingCardDetailTest');
        container.innerHTML = __testOnly_renderThinkingCardBodyHTML({
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_dispatch', stage_label: 'Workflow dispatch' }
                ]
            },
            latestProgress: {
                phase: 'workflow_dispatch',
                selected_workflow_id: '#V#chat_assistant_workflow',
                selected_workflow_name: 'Chat assistant workflow'
            }
        });
        __testOnly_bindConceptSelectionClicks(container, {
            selector: '.thinking-card-concept-link',
            datasetKey: 'conceptLinkBound'
        });

        const button = container.querySelector('.thinking-card-concept-link');
        expect(button).toBeTruthy();
        expect(button.dataset.conceptId).toBe('#V#chat_assistant_workflow');

        button.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        expect(onSelect).toHaveBeenCalledTimes(1);
        const eventArg = onSelect.mock.calls[0][0];
        expect(eventArg.detail.conceptId).toBe('#V#chat_assistant_workflow');
        expect(eventArg.detail.createConceptTab).toBe(true);

        document.removeEventListener('von:selectConceptById', onSelect);
    });

    test('renders explicit no-workflow-found state as a failure row', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_discovery', stage_label: 'Workflow discovery' }
                ]
            },
            workflowDiscovery: {
                match_count: 0,
                matches: []
            },
            latestProgress: {
                phase: 'workflow_discovery_complete'
            }
        });

        expect(html).toContain('Workflow discovery');
        expect(html).toContain('No direct workflow match found');
        expect(html).toContain('thinking-card-tool-status failure');
        expect(html).toContain('data-thinking-diagnostic-key="stage::workflow_discovery"');
    });

    test('renders workflow candidates as a successful discovery row when routing is excluded', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_discovery', stage_label: 'Workflow discovery' }
                ]
            },
            workflowDiscovery: {
                match_count: 0,
                candidate_count: 1,
                candidates: [
                    {
                        concept_id: '#V#scholarly_paper_representation_workflow',
                        name: 'Scholarly paper representation workflow'
                    }
                ]
            },
            latestProgress: {
                phase: 'workflow_discovery_complete'
            }
        });

        const container = document.createElement('div');
        container.innerHTML = html;
        expect(container.textContent).toContain('Found candidate Scholarly paper representation workflow');
        expect(html).toContain('Scholarly paper representation workflow (#V#scholarly_paper_representation_workflow)');
        expect(html).toContain('thinking-card-tool-status success');
    });

    test('renders workflow discovery search trace and rejection reasons in expandable diagnostics', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_discovery', stage_label: 'Workflow discovery' }
                ]
            },
            workflowDiscovery: {
                requested_query: 'Represent this uploaded paper',
                query: 'Represent this uploaded paper\nArtefact typing context: route_hint=scholarly',
                search_sources: ['semantic', 'vontology'],
                threshold: 0.7,
                search_time_ms: 44,
                timeout_budget_seconds: 3,
                budget_exhausted: true,
                budget_exhaustion_stage: 'semantic_search',
                budget_exhaustion_detail: 'semantic_search timed out after 3.000s during workflow discovery',
                match_count: 0,
                candidate_count: 1,
                candidates: [
                    {
                        concept_id: '#V#scholarly_paper_representation_workflow',
                        name: 'Scholarly paper representation workflow',
                        match_source: 'semantic',
                        relevance_score: 0.91,
                        routing_eligible: false,
                        routing_exclusion_reason: 'graph_incomplete',
                        is_executable: false,
                        executability_reason: 'graph_incomplete',
                        executability_detail: 'Missing terminal node.'
                    }
                ]
            },
            latestProgress: {
                phase: 'workflow_discovery_complete'
            }
        });

        expect(html).toContain('Requested search');
        expect(html).toContain('Executed search');
        expect(html).toContain('Represent this uploaded paper');
        expect(html).toContain('route_hint=scholarly');
        expect(html).toContain('semantic, vontology');
        expect(html).toContain('3.000 s');
        expect(html).toContain('Budget exhausted');
        expect(html).toContain('semantic_search');
        expect(html).toContain('Budget exhaustion detail');
        expect(html).toContain('Workflow candidates');
        expect(html).toContain('Routing excluded: Graph incomplete');
        expect(html).toContain('Missing terminal node.');
    });

    test('renders fallback activity rows as expandable diagnostics entries', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            activityHistory: [{
                sequenceNo: 7,
                label: 'Tool call failed: fetch_concept',
                detail: 'Timeout while fetching',
                state: 'failure',
                status: 'tool_failed',
                stage: 'tool_execute',
                eventKind: 'tool_failed',
                groupCount: 1,
                model: 'gpt-test'
            }]
        });

        expect(html).toContain('Tool call failed: fetch_concept');
        expect(html).toContain('data-thinking-diagnostic-key="activity::7::tool_failed::tool_execute"');
        expect(html).toContain('Sequence');
        expect(html).toContain('Timeout while fetching');
    });

    test('renders workflow dispatch terminal failures as failure rows and preserves dispatch diagnostics in export', () => {
        const request = {
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_dispatch', stage_label: 'Workflow dispatch' }
                ]
            },
            latestProgress: {
                status: 'orchestrator_end',
                orchestrator_status: 'completed',
                phase: 'workflow_dispatch',
                selected_workflow_id: '#V#synthetic_workflow_regression_suite_workflow',
                selected_workflow_name: 'Synthetic workflow regression suite workflow',
                workflow_selector_verdict: 'rag_selected',
                workflow_selector_source: 'selector',
                workflow_match_count: 2,
                workflow_candidate_count: 5,
                result_summary: 'workflow_terminal:failed',
                workflow_routing_diagnostics: {
                    dispatch: {
                        dispatch_terminal_status: 'failed',
                        dispatch_terminal_failure_reason: 'metadata_validation_failed',
                        dispatch_terminal_failure_detail: 'metadata_read_context_key_missing:target_workflow_ids',
                        dispatch_terminal_unresolved_required_inputs: ['target_workflow_ids']
                    }
                }
            }
        };

        const html = __testOnly_renderThinkingCardBodyHTML(request);
        expect(html).toContain('Workflow dispatch');
        expect(html).toContain('thinking-card-tool-status failure');
        expect(html).toContain('Dispatch terminal status');
        expect(html).toContain('Metadata validation failed');
        expect(html).toContain('target_workflow_ids');

        const diagnosticsPayload = __testOnly_buildThinkingDiagnosticsPayload(request);
        expect(diagnosticsPayload.stage_diagnostics).toEqual([
            expect.objectContaining({
                stage_id: 'workflow_dispatch',
                state: 'failure',
                diagnostics: expect.objectContaining({
                    dispatch_terminal_status: 'failed',
                    dispatch_terminal_failure_reason: 'metadata_validation_failed',
                    dispatch_terminal_unresolved_required_inputs: ['target_workflow_ids']
                })
            })
        ]);
    });

    test('renders fallback latest-progress summary with selected workflow context', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            latestProgress: {
                status: 'completed',
                stage: 'completed',
                stage_label: 'Complete',
                workflow_match_count: 0,
                workflow_candidate_count: 0,
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow',
                workflow_selector_verdict: 'tool_seeking',
                workflow_selector_source: 'selector'
            }
        });

        expect(html).toContain('Complete');
        expect(html).toContain('No direct workflow match found; routed via selector to <button');
        expect(html).toContain('Tool calling workflow (#V#tool_calling_workflow)');
    });

    // JVNAUTOSCI-1441 regression: collapsible diagnostic rows must not put
    // interactive <button> elements inside <summary>, as that prevents the
    // native <details> toggle from working.
    test('collapsible diagnostic rows use plain text detail in summary instead of button HTML', () => {
        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            workflowStagePath: {
                path: [
                    { stage_id: 'workflow_discovery', stage_label: 'Workflow discovery' }
                ]
            },
            workflowDiscovery: {
                requested_query: 'Run scholarly workflow',
                query: 'Run scholarly workflow',
                match_count: 1,
                matches: [
                    {
                        concept_id: '#V#scholarly_paper_representation_workflow',
                        name: 'Scholarly paper representation workflow',
                        relevance_score: 0.95
                    }
                ]
            },
            latestProgress: {
                phase: 'workflow_discovery_complete'
            }
        });

        const container = document.createElement('div');
        container.innerHTML = html;

        const detailsRow = container.querySelector('details[data-thinking-diagnostic-key]');
        expect(detailsRow).toBeTruthy();

        const summary = detailsRow.querySelector('summary');
        expect(summary).toBeTruthy();

        // The summary must NOT contain any <button> elements — they block
        // the native <details> toggle behaviour.
        const buttonsInSummary = summary.querySelectorAll('button');
        expect(buttonsInSummary).toHaveLength(0);

        // The plain-text concept label should still appear in the summary.
        expect(summary.textContent).toContain('Scholarly paper representation workflow');
    });

    // JVNAUTOSCI-1441 regression: bindConceptSelectionClicks must not call
    // preventDefault when the click originates inside a <summary>/<details>.
    test('bindConceptSelectionClicks does not preventDefault for links inside a summary', () => {
        document.body.innerHTML = '<div id="diagnosticToggleTest"></div>';
        const container = document.getElementById('diagnosticToggleTest');
        container.innerHTML = `
            <details data-thinking-diagnostic-key="test">
                <summary>
                    <button type="button" class="concept-selection-link" data-concept-id="#V#test_concept">Test</button>
                </summary>
                <div>Diagnostic content</div>
            </details>`;

        __testOnly_bindConceptSelectionClicks(container, {
            selector: '.concept-selection-link',
            datasetKey: 'conceptLinkBound'
        });

        const button = container.querySelector('.concept-selection-link');
        const clickEvent = new MouseEvent('click', { bubbles: true, cancelable: true });
        button.dispatchEvent(clickEvent);

        // preventDefault must NOT have been called because the button lives
        // inside a <summary> — blocking it would prevent <details> toggle.
        expect(clickEvent.defaultPrevented).toBe(false);
    });
});

describe('thinking card meta telemetry', () => {
    beforeEach(() => {
        document.body.innerHTML = '<span id="thinkingCardMeta"></span><span id="thinkingCardStatusBadge"></span>';
    });

    test('hides tool counts when no tool execution is active', () => {
        const request = {
            thinkingStartedAtMs: Date.now() - 1000
        };
        __testOnly_updateThinkingCardMeta(request, {
            stage: 'workflow_dispatch',
            tool_calls_done: 0,
            tool_calls_cap: 8,
            last_activity_at_utc: '2026-03-06T22:39:48.399497Z'
        });

        expect(document.getElementById('thinkingCardMeta').textContent).not.toContain('0/8 tools');
    });

    test('shows tool counts once tool execution is active', () => {
        const request = {
            thinkingStartedAtMs: Date.now() - 1000
        };
        __testOnly_updateThinkingCardMeta(request, {
            stage: 'tool_execute',
            tool_calls_done: 1,
            tool_calls_cap: 8,
            last_activity_at_utc: '2026-03-06T22:39:48.399497Z'
        });

        expect(document.getElementById('thinkingCardMeta').textContent).toContain('1/8 tools');
    });

    test('shows model timing baseline when enough observations exist', () => {
        const request = {
            thinkingStartedAtMs: Date.now() - 1000,
            timingBreakdown: {
                llm_calls_by_stage_model: [
                    {
                        stage: 'selector_decision',
                        model: 'gpt-baseline',
                        duration_ms: 420,
                        historical_observation_count: 5,
                        historical_mean_duration_ms: 250,
                        historical_stddev_duration_ms: 40,
                        duration_deviation_classification: 'slower_than_usual'
                    }
                ]
            }
        };
        __testOnly_updateThinkingCardMeta(request, {
            stage: 'selector_decision',
            last_activity_at_utc: '2026-03-06T22:39:48.399497Z'
        });

        const text = document.getElementById('thinkingCardMeta').textContent;
        expect(text).toContain('gpt-baseline');
        expect(text).toContain('avg');
        expect(text).toContain('slower than usual');
    });

    test('suppresses model timing baseline when observations are sparse', () => {
        const request = {
            thinkingStartedAtMs: Date.now() - 1000,
            timingBreakdown: {
                llm_calls_by_stage_model: [
                    {
                        stage: 'selector_decision',
                        model: 'gpt-baseline',
                        duration_ms: 420,
                        historical_observation_count: 2,
                        historical_mean_duration_ms: 250
                    }
                ]
            }
        };
        __testOnly_updateThinkingCardMeta(request, {
            stage: 'selector_decision',
            last_activity_at_utc: '2026-03-06T22:39:48.399497Z'
        });

        const text = document.getElementById('thinkingCardMeta').textContent;
        expect(text).not.toContain('gpt-baseline');
        expect(text).not.toContain('avg');
    });
});

describe('thinking card toggle accessibility', () => {
    function seedActiveThinkingCardSession(
        sessionId = 'thinking-card-test-session',
        sessionName = 'Thinking card test session'
    ) {
        __testOnly_setSessionTabsCache([
            {
                session_id: sessionId,
                session_name: sessionName,
                message_count: 0,
                is_completed: false
            }
        ]);
        __testOnly_setActiveChatSession(sessionId, sessionName);
        __testOnly_setDisplayedHistorySession(sessionId);
    }

    beforeEach(() => {
        document.body.innerHTML = `
            <div id="scrollableField"></div>
            <div class="thinking-card-wrapper" id="thinkingCardWrapper" aria-hidden="true">
                <div class="thinking-card" role="status" aria-live="polite">
                    <div class="thinking-card-header" id="loadingIndicator" data-thinking-role="header">
                        <span class="thinking-card-phase loading-indicator-text" data-thinking-role="phase">Thinking...</span>
                        <span id="thinkingCardStatusBadge" class="thinking-card-status active" data-thinking-role="status" aria-hidden="true">Active</span>
                        <span class="thinking-card-meta" id="thinkingCardMeta" data-thinking-role="meta"></span>
                        <div class="thinking-card-mode-switch" role="group" aria-label="Thinking detail mode">
                            <button class="thinking-card-mode-button is-active" type="button" data-thinking-role="mode" data-thinking-mode="default" aria-hidden="true" aria-pressed="true">Default</button>
                            <button class="thinking-card-mode-button" type="button" data-thinking-role="mode" data-thinking-mode="expert" aria-hidden="true" aria-pressed="false">Expert</button>
                            <button class="thinking-card-mode-button" type="button" data-thinking-role="mode" data-thinking-mode="debug" aria-hidden="true" aria-pressed="false">Debug</button>
                        </div>
                        <button id="thinkingCardToggleButton" type="button" data-thinking-role="toggle" aria-hidden="true" aria-expanded="true" aria-controls="loadingIndicatorDetail" aria-label="Collapse thinking details" title="Collapse thinking details">
                            <span class="thinking-card-toggle-icon" aria-hidden="true">⌄</span>
                            <span class="visually-hidden">Toggle thinking details</span>
                        </button>
                        <button type="button" class="thinking-card-size-button" data-thinking-role="size-decrease" aria-hidden="true">−</button>
                        <button type="button" class="thinking-card-size-button" data-thinking-role="size-increase" aria-hidden="true">+</button>
                        <button id="retryThinkingButton" type="button" data-thinking-role="retry" aria-hidden="true">Retry</button>
                        <button id="copyThinkingDiagnosticsButton" type="button" data-thinking-role="copy" aria-hidden="true">Copy diagnostic snapshot</button>
                        <button id="abortButton" type="button" data-thinking-role="abort" aria-hidden="true"></button>
                    </div>
                    <div class="thinking-card-body" id="loadingIndicatorDetail" data-thinking-role="detail" aria-live="polite"></div>
                </div>
            </div>
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
            <input type="checkbox" id="annotationToggle" />
        `;
        seedActiveThinkingCardSession();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
        window.localStorage.removeItem('von:thinkingCardMode');
    });

    function getRetainedThinkingCardElements() {
        const wrapper = document.querySelector('.thinking-card-inline-slot .thinking-card-wrapper');
        if (!wrapper) {
            return null;
        }
        return {
            wrapper,
            toggleButton: wrapper.querySelector('[data-thinking-role="toggle"]'),
            detail: wrapper.querySelector('[data-thinking-role="detail"]'),
            copyButton: wrapper.querySelector('[data-thinking-role="copy"]'),
            sizeIncreaseButton: wrapper.querySelector('[data-thinking-role="size-increase"]'),
            sizeDecreaseButton: wrapper.querySelector('[data-thinking-role="size-decrease"]')
        };
    }

    test('mode switch rerenders the active card without changing the request path', () => {
        const request = {
            thinkingStartedAtMs: Date.now() - 1000,
            latestProgress: {
                status: 'llm_call_start',
                phase: 'selector_decision',
                stage: 'selector_decision',
                selected_workflow_id: '#V#tool_calling_workflow',
                selected_workflow_name: 'Tool calling workflow',
                workflow_selector_verdict: 'tool_seeking',
                workflow_selector_source: 'selector',
                model: 'gpt-5-mini',
                provider: 'openai',
                llm_request_state: 'sent',
                llm_request: {
                    prompt: {
                        text: 'Select a workflow using represented workflow metadata.',
                        char_count: 54
                    }
                }
            },
            workflowStagePath: {
                path: [
                    { stage_id: 'selector_decision', stage_label: 'Select workflow' }
                ]
            }
        };

        __testOnly_setThinkingCardRequests(request, null);
        __testOnly_refreshThinkingCardProgressUi(request);
        __testOnly_bindThinkingCardControls();

        const detail = document.getElementById('loadingIndicatorDetail');
        const defaultButton = document.querySelector('[data-thinking-mode="default"]');
        const expertButton = document.querySelector('[data-thinking-mode="expert"]');
        const debugButton = document.querySelector('[data-thinking-mode="debug"]');

        expect(defaultButton.getAttribute('aria-pressed')).toBe('true');
        expect(detail.textContent).toContain('Selected Tool calling workflow');
        expect(detail.innerHTML).not.toContain('Prepared prompt preview');

        expertButton.click();
        expect(request.thinkingCardMode).toBe('expert');
        expect(expertButton.getAttribute('aria-pressed')).toBe('true');
        expect(detail.innerHTML).toContain('Selected workflow');
        expect(detail.innerHTML).toContain('LLM model');
        expect(detail.innerHTML).not.toContain('Prepared prompt preview');

        debugButton.click();
        expect(request.thinkingCardMode).toBe('debug');
        expect(debugButton.getAttribute('aria-pressed')).toBe('true');
        expect(detail.innerHTML).toContain('Prepared prompt preview');
        expect(detail.innerHTML).toContain('LLM provider');
    });

    test('persists the selected thinking-card mode for new cards', () => {
        const firstRequest = {
            thinkingStartedAtMs: Date.now() - 1000,
            latestProgress: {
                status: 'pending',
                phase: 'workflow_dispatch',
                phase_label: 'Preparing workflow dispatch'
            }
        };
        const nextRequest = {
            thinkingStartedAtMs: Date.now(),
            latestProgress: {
                status: 'pending',
                phase: 'selector_decision',
                phase_label: 'Selecting workflow'
            }
        };

        __testOnly_setThinkingCardRequests(firstRequest, null);
        __testOnly_refreshThinkingCardProgressUi(firstRequest);
        __testOnly_bindThinkingCardControls();

        document.querySelector('[data-thinking-mode="expert"]').click();

        expect(firstRequest.thinkingCardMode).toBe('expert');
        expect(window.localStorage.getItem('von:thinkingCardMode')).toBe('expert');
        expect(__testOnly_getThinkingCardMode(nextRequest)).toBe('expert');

        __testOnly_setThinkingCardRequests(nextRequest, null);
        __testOnly_refreshThinkingCardProgressUi(nextRequest);

        expect(document.querySelector('[data-thinking-mode="expert"]').getAttribute('aria-pressed')).toBe('true');
        expect(document.getElementById('thinkingCardWrapper').classList.contains('thinking-card-mode-expert')).toBe(true);
    });

    test('marks active expert cards as free-height and disables manual size controls', () => {
        const request = {
            thinkingCardMode: 'expert',
            thinkingStartedAtMs: Date.now() - 1000,
            latestProgress: {
                status: 'pending',
                phase: 'tool_execute',
                phase_label: 'Executing tools'
            },
            activityHistory: [{
                sequenceNo: 1,
                label: 'Tool call: search_knowledge_base',
                detail: 'Searching indexed knowledge',
                state: 'pending',
                status: 'tool_call_start',
                stage: 'tool_execute',
                eventKind: 'tool_call_start',
                groupCount: 1
            }]
        };

        __testOnly_setThinkingCardRequests(request, null);
        __testOnly_setThinkingState(true, request);
        __testOnly_refreshThinkingCardProgressUi(request);

        const wrapper = document.getElementById('thinkingCardWrapper');
        const detail = document.getElementById('loadingIndicatorDetail');
        const sizeDecreaseButton = document.querySelector('[data-thinking-role="size-decrease"]');
        const sizeIncreaseButton = document.querySelector('[data-thinking-role="size-increase"]');

        expect(wrapper.classList.contains('is-active-turn')).toBe(true);
        expect(wrapper.classList.contains('thinking-card-mode-expert')).toBe(true);
        expect(detail.style.height).toBe('');
        expect(sizeDecreaseButton.disabled).toBe(true);
        expect(sizeIncreaseButton.disabled).toBe(true);
    });

    test('keeps card expanded while active and disables collapse toggle', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        document.getElementById('promptInput').value = 'test prompt';

        let generateSignal = null;
        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: true,
                    status: 202,
                    json: async () => ({})
                });
            }

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
        const toggleButton = document.getElementById('thinkingCardToggleButton');
        const wrapper = document.getElementById('thinkingCardWrapper');
        const detail = document.getElementById('loadingIndicatorDetail');

        expect(toggleButton.getAttribute('aria-hidden')).toBe('false');
        expect(toggleButton.getAttribute('aria-expanded')).toBe('true');
        expect(toggleButton.getAttribute('aria-label')).toBe('Collapse thinking details');
        expect(toggleButton.getAttribute('title')).toBe('Collapse thinking details');
        expect(toggleButton.disabled).toBe(true);

        toggleButton.click();

        expect(toggleButton.getAttribute('aria-expanded')).toBe('true');
        expect(toggleButton.getAttribute('aria-label')).toBe('Collapse thinking details');
        expect(toggleButton.getAttribute('title')).toBe('Collapse thinking details');
        expect(wrapper.classList.contains('is-collapsed')).toBe(false);
        expect(detail.getAttribute('aria-hidden')).toBe('false');

        document.getElementById('abortButton').click();
        await new Promise((r) => setTimeout(r, 0));

        expect(generateSignal).not.toBeNull();
        expect(generateSignal.aborted).toBe(true);
        expect(toggleButton.getAttribute('aria-hidden')).toBe('true');

        await expect(sendPromise).resolves.toBeUndefined();
    });

    test('renders structured pending progress payloads instead of generic waiting placeholder', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        document.getElementById('promptInput').value = 'https://arxiv.org/abs/2602.20478';

        let generateSignal = null;
        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: true,
                    status: 202,
                    json: async () => ({
                        status: 'pending',
                        phase: 'context_build',
                        phase_label: 'Initialising request',
                        liveness_state: 'waiting',
                        result_summary: 'Resolving session scope, namespace, and chat-history context before workflow/tool selection.',
                        subtask: 'request setup'
                    })
                });
            }

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
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        const indicatorText = document.querySelector('.loading-indicator-text');
        expect(indicatorText.textContent).toContain('Initialising request');
        expect(indicatorText.textContent).toContain('request setup');
        expect(document.getElementById('loadingIndicatorDetail').innerHTML).toContain('Resolving session scope, namespace, and chat-history context');

        document.getElementById('abortButton').click();
        await new Promise((r) => setTimeout(r, 0));
        await expect(sendPromise).resolves.toBeUndefined();
    });

    test('progress poll timeout falls back to retryable waiting state instead of freezing the card', async () => {
        jest.useFakeTimers();
        try {
            const { getUserContext } = require('../apiService.js');
            getUserContext.mockReturnValue({
                user_id: 'user',
                org_id: 'org',
                language: 'en-NZ',
                gmail_profile: null
            });

            document.getElementById('promptInput').value = 'slow progress prompt';

            let generateSignal = null;
            global.fetch = jest.fn((url, options = {}) => {
                if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                    return Promise.resolve({
                        ok: true,
                        json: async () => ({ show_tool_use_during_thinking: true })
                    });
                }

                if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                    return new Promise((resolve, reject) => {
                        options.signal?.addEventListener('abort', () => {
                            const err = new Error('progress timeout');
                            err.name = 'AbortError';
                            reject(err);
                        });
                    });
                }

                if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                    return Promise.resolve({
                        ok: true,
                        json: async () => ({ history_length: 0, authenticated: true })
                    });
                }

                if (typeof url === 'string' && url.startsWith('/von/generate')) {
                    generateSignal = options.signal;
                    return new Promise((resolve, reject) => {
                        generateSignal?.addEventListener('abort', () => {
                            const err = new Error('aborted');
                            err.name = 'AbortError';
                            reject(err);
                        });
                    });
                }

                return Promise.resolve({ ok: true, json: async () => ({}) });
            });

            const sendPromise = sendMessage();
            await Promise.resolve();
            const progressPollTimeoutMs = __testOnly_getThinkingProgressPollFetchTimeoutMs();

            const indicatorText = document.querySelector('.loading-indicator-text');
            expect(indicatorText.textContent).toContain('Preparing response');
            expect(indicatorText.textContent).toContain('request setup');
            expect(document.getElementById('loadingIndicatorDetail').innerHTML)
                .toContain('No live progress has been received yet.');

            jest.advanceTimersByTime(2_100);
            await Promise.resolve();
            await Promise.resolve();

            expect(indicatorText.textContent).toContain('Preparing response');
            expect(indicatorText.textContent).not.toContain('Retrying live progress');
            expect(document.getElementById('loadingIndicatorDetail').innerHTML)
                .toContain('No live progress has been received yet.');
            expect(document.getElementById('loadingIndicatorDetail').innerHTML)
                .not.toContain('timed out after');

            jest.advanceTimersByTime(progressPollTimeoutMs);
            await Promise.resolve();
            await Promise.resolve();

            expect(indicatorText.textContent).toContain('Still preparing response');
            expect(document.getElementById('loadingIndicatorDetail').innerHTML)
                .toContain('remain delayed');
            expect(document.getElementById('loadingIndicatorDetail').innerHTML)
                .not.toContain('timed out after');

            jest.advanceTimersByTime(progressPollTimeoutMs);
            await Promise.resolve();
            await Promise.resolve();

            expect(document.getElementById('loadingIndicatorDetail').innerHTML)
                .toContain('remain delayed');

            document.getElementById('abortButton').click();
            await Promise.resolve();
            await expect(sendPromise).resolves.toBeUndefined();
        } finally {
            jest.useRealTimers();
        }
    });

    test('allows unfurl after terminal progress even while request is still active', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        document.getElementById('promptInput').value = 'test prompt';

        let generateSignal = null;
        let progressPollCount = 0;
        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                progressPollCount += 1;
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'pending',
                        orchestrator_status: 'completed',
                        liveness_state: 'active',
                        phase: 'response_finalising',
                        phase_label: 'Finalising response',
                        tool: 'response_finalising',
                        result_summary: 'Preparing response payload'
                    })
                });
            }

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
        const toggleButton = document.getElementById('thinkingCardToggleButton');
        const wrapper = document.getElementById('thinkingCardWrapper');
        const detail = document.getElementById('loadingIndicatorDetail');

        expect(toggleButton.disabled).toBe(true);

        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        expect(progressPollCount).toBeGreaterThan(0);
        expect(toggleButton.disabled).toBe(false);
        expect(toggleButton.getAttribute('aria-expanded')).toBe('false');
        expect(wrapper.classList.contains('is-collapsed')).toBe(true);
        expect(detail.getAttribute('aria-hidden')).toBe('true');
        expect(document.getElementById('thinkingCardStatusBadge').textContent).toBe('Complete');
        expect(document.getElementById('thinkingCardStatusBadge').classList.contains('completed')).toBe(true);

        toggleButton.click();

        expect(toggleButton.getAttribute('aria-expanded')).toBe('true');
        expect(wrapper.classList.contains('is-expanded')).toBe(true);

        document.getElementById('abortButton').click();
        await new Promise((r) => setTimeout(r, 0));

        expect(generateSignal).not.toBeNull();
        expect(generateSignal.aborted).toBe(true);
        await expect(sendPromise).resolves.toBeUndefined();
    });

    test('retains finished run history inline per turn and allows expanding afterwards', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });

        document.getElementById('promptInput').value = 'test prompt';

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'completed',
                        phase: 'tool_execute',
                        phase_label: 'Executing tools',
                        tool: 'search_knowledge_base',
                        result_summary: 'Completed'
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Done',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await expect(sendMessage()).resolves.toBeUndefined();

        const activeWrapper = document.getElementById('thinkingCardWrapper');
        const retained = getRetainedThinkingCardElements();

        expect(activeWrapper.getAttribute('aria-hidden')).toBe('true');
        expect(retained).not.toBeNull();
        expect(retained.toggleButton.getAttribute('aria-expanded')).toBe('false');
        expect(retained.toggleButton.getAttribute('aria-label')).toBe('Expand thinking details');
        expect(retained.toggleButton.getAttribute('title')).toBe('Expand thinking details');
        expect(retained.detail.getAttribute('aria-hidden')).toBe('true');
        expect(retained.detail.innerHTML).toContain('search_knowledge_base');
        expect(retained.copyButton.getAttribute('aria-hidden')).toBe('false');

        retained.copyButton.click();
        await Promise.resolve();

        expect(writeText).toHaveBeenCalledTimes(1);
        expect(JSON.parse(writeText.mock.calls[0][0])).toEqual(expect.objectContaining({
            schema_version: 'thinking_diagnostics_snapshot.v1',
            mcp_access: expect.objectContaining({
                turn_execution_get_live_progress: expect.objectContaining({
                    tool_name: 'turn_execution_get_live_progress'
                })
            })
        }));

        retained.toggleButton.click();

        expect(retained.toggleButton.getAttribute('aria-expanded')).toBe('true');
        expect(retained.toggleButton.getAttribute('aria-label')).toBe('Collapse thinking details');
        expect(retained.toggleButton.getAttribute('title')).toBe('Collapse thinking details');
        expect(retained.detail.getAttribute('aria-hidden')).toBe('false');
    });

    test('retains a finished card inline even when live progress never becomes visible', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        document.getElementById('promptInput').value = 'fast response prompt';

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return new Promise((_resolve, reject) => {
                    options.signal?.addEventListener('abort', () => {
                        const err = new Error('aborted');
                        err.name = 'AbortError';
                        reject(err);
                    });
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Done',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await expect(sendMessage()).resolves.toBeUndefined();

        const activeWrapper = document.getElementById('thinkingCardWrapper');
        const retained = getRetainedThinkingCardElements();
        const indicatorText = retained.wrapper.querySelector('.loading-indicator-text');

        expect(activeWrapper.getAttribute('aria-hidden')).toBe('true');
        expect(retained).not.toBeNull();
        expect(indicatorText.textContent).toBe('Complete');
        expect(retained.detail.innerHTML).toContain('Turn completed');
        expect(retained.detail.innerHTML).toContain('Response generated');
    });

    test('retains a completed card inline when the last live progress remains non-terminal', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        __testOnly_setActiveChatSession('session-stale-progress-complete', 'Stale Progress Complete');
        document.getElementById('promptInput').value = 'stale pending progress';

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'thinking',
                        phase: 'plain_response',
                        stage: 'plain_response',
                        phase_label: 'Answering directly',
                        result_summary: 'Drafting a direct response',
                        liveness_state: 'active'
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Done',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await expect(sendMessage()).resolves.toBeUndefined();

        const retained = getRetainedThinkingCardElements();
        const indicatorText = retained.wrapper.querySelector('.loading-indicator-text');

        expect(retained).not.toBeNull();
        expect(indicatorText.textContent).toBe('Complete');
        expect(retained.detail.innerHTML).toContain('Turn completed');
        expect(retained.detail.innerHTML).toContain('Response generated');
        expect(retained.detail.innerHTML).not.toContain('Active');
    });

    test('retains a failed card inline when the last live progress remains non-terminal', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        __testOnly_setActiveChatSession('session-stale-progress-failed', 'Stale Progress Failed');
        document.getElementById('promptInput').value = 'stale pending failure progress';

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'thinking',
                        phase: 'plain_response',
                        stage: 'plain_response',
                        phase_label: 'Answering directly',
                        result_summary: 'Drafting a direct response',
                        liveness_state: 'active'
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: false,
                    json: async () => ({
                        error: 'authoritative workflow failed'
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await expect(sendMessage()).resolves.toBeUndefined();

        const retained = getRetainedThinkingCardElements();
        const indicatorText = retained.wrapper.querySelector('.loading-indicator-text');

        expect(retained).not.toBeNull();
        expect(indicatorText.textContent).toBe('Failed');
        expect(retained.detail.innerHTML).toContain('Turn failed');
        expect(retained.detail.innerHTML).toContain('authoritative workflow failed');
        expect(retained.detail.innerHTML).not.toContain('Active');
    });

    test('keeps one retained thinking card per completed turn instead of replacing the previous turn', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'completed',
                        phase: 'tool_execute',
                        phase_label: 'Executing tools',
                        tool: 'search_knowledge_base',
                        result_summary: 'Completed'
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Done',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        document.getElementById('promptInput').value = 'first prompt';
        await expect(sendMessage()).resolves.toBeUndefined();

        document.getElementById('promptInput').value = 'second prompt';
        await expect(sendMessage()).resolves.toBeUndefined();

        const retainedCards = Array.from(document.querySelectorAll('.thinking-card-inline-slot .thinking-card-wrapper'));
        expect(retainedCards).toHaveLength(2);
        retainedCards.forEach((card) => {
            const toggleButton = card.querySelector('[data-thinking-role="toggle"]');
            expect(toggleButton.getAttribute('aria-expanded')).toBe('false');
        });
    });

    test('supports keyboard resizing on retained thinking cards within bounds', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'completed',
                        phase: 'tool_execute',
                        phase_label: 'Executing tools',
                        tool: 'search_knowledge_base',
                        result_summary: 'Completed'
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Done',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        document.getElementById('promptInput').value = 'resize prompt';
        await expect(sendMessage()).resolves.toBeUndefined();

        const retained = getRetainedThinkingCardElements();
        retained.toggleButton.click();

        expect(retained.detail.style.height).toBe('120px');
        expect(retained.sizeDecreaseButton.disabled).toBe(true);
        expect(retained.sizeIncreaseButton.disabled).toBe(false);

        retained.sizeIncreaseButton.click();
        expect(retained.detail.style.height).toBe('200px');
        expect(retained.sizeDecreaseButton.disabled).toBe(false);

        retained.sizeIncreaseButton.click();
        retained.sizeIncreaseButton.click();
        retained.sizeIncreaseButton.click();
        retained.sizeIncreaseButton.click();
        expect(retained.detail.style.height).toBe('520px');
        expect(retained.sizeIncreaseButton.disabled).toBe(true);

        retained.sizeDecreaseButton.click();
        retained.sizeDecreaseButton.click();
        retained.sizeDecreaseButton.click();
        retained.sizeDecreaseButton.click();
        retained.sizeDecreaseButton.click();
        expect(retained.detail.style.height).toBe('120px');
        expect(retained.sizeDecreaseButton.disabled).toBe(true);
    });
});

describe('active thinking card manual resize persistence', () => {
    let originalResizeObserver;

    beforeEach(() => {
        originalResizeObserver = global.ResizeObserver;
        global.ResizeObserver = undefined;
        __testOnly_setThinkingCardRequests(null, null);
        document.body.innerHTML = `
            <div id="scrollableField"></div>
            <div class="thinking-card-wrapper" id="thinkingCardWrapper" aria-hidden="false">
                <div class="thinking-card" role="status" aria-live="polite">
                    <div class="thinking-card-header" id="loadingIndicator" data-thinking-role="header">
                        <span class="thinking-card-phase loading-indicator-text" data-thinking-role="phase">Thinking...</span>
                        <span id="thinkingCardStatusBadge" class="thinking-card-status active" data-thinking-role="status" aria-hidden="true">Active</span>
                        <span class="thinking-card-meta" id="thinkingCardMeta" data-thinking-role="meta"></span>
                        <button id="thinkingCardToggleButton" type="button" data-thinking-role="toggle" aria-hidden="false" aria-expanded="true" aria-controls="loadingIndicatorDetail" aria-label="Collapse thinking details" title="Collapse thinking details">
                            <span class="thinking-card-toggle-icon" aria-hidden="true">⌄</span>
                            <span class="visually-hidden">Toggle thinking details</span>
                        </button>
                        <button type="button" class="thinking-card-size-button" data-thinking-role="size-decrease" aria-hidden="false">−</button>
                        <button type="button" class="thinking-card-size-button" data-thinking-role="size-increase" aria-hidden="false">+</button>
                        <button id="retryThinkingButton" type="button" data-thinking-role="retry" aria-hidden="true">Retry</button>
                        <button id="copyThinkingDiagnosticsButton" type="button" data-thinking-role="copy" aria-hidden="false">Copy diagnostic snapshot</button>
                        <button id="abortButton" type="button" data-thinking-role="abort" aria-hidden="false"></button>
                    </div>
                    <div class="thinking-card-body" id="loadingIndicatorDetail" data-thinking-role="detail" aria-live="polite"></div>
                </div>
            </div>
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
            <input type="checkbox" id="annotationToggle" />
        `;
    });

    afterEach(() => {
        global.ResizeObserver = originalResizeObserver;
        __testOnly_setThinkingCardRequests(null, null);
        jest.restoreAllMocks();
    });

    function createActiveThinkingRequest() {
        return {
            latestProgress: {
                status: 'pending',
                phase: 'tool_execute',
                phase_label: 'Executing tools'
            },
            activityHistory: [{
                sequenceNo: 1,
                label: 'Tool call: search_knowledge_base',
                detail: 'Searching indexed knowledge',
                state: 'pending',
                status: 'tool_call_start',
                stage: 'tool_execute',
                eventKind: 'tool_call_start',
                groupCount: 1
            }]
        };
    }

    test('keeps a manually dragged active thinking-card height across live refreshes', () => {
        const request = createActiveThinkingRequest();
        __testOnly_setThinkingCardRequests(request, null);

        __testOnly_refreshThinkingCardProgressUi(request);
        __testOnly_bindThinkingCardControls();

        const wrapper = document.getElementById('thinkingCardWrapper');
        const detail = document.getElementById('loadingIndicatorDetail');
        expect(wrapper.classList.contains('has-tools')).toBe(true);

        detail.style.height = '360px';
        detail.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        expect(request.thinkingCardBodyHeightPx).toBe(360);

        request.activityHistory = [
            ...request.activityHistory,
            {
                sequenceNo: 2,
                label: 'Tool call completed: search_knowledge_base',
                detail: 'Found 4 relevant results',
                state: 'success',
                status: 'tool_invoked',
                stage: 'tool_execute',
                eventKind: 'tool_invoked',
                groupCount: 1
            }
        ];

        __testOnly_refreshThinkingCardProgressUi(request);

        expect(request.thinkingCardBodyHeightPx).toBe(360);
        expect(detail.style.height).toBe('360px');
    });

    test('clamps manually dragged active thinking-card heights to explicit bounds', () => {
        const request = createActiveThinkingRequest();
        __testOnly_setThinkingCardRequests(request, null);

        __testOnly_refreshThinkingCardProgressUi(request);

        const detail = document.getElementById('loadingIndicatorDetail');

        detail.style.height = '80px';
        expect(__testOnly_persistThinkingCardBodyHeightFromDom(request)).toBe(120);
        expect(request.thinkingCardBodyHeightPx).toBe(120);
        expect(detail.style.height).toBe('120px');

        detail.style.height = '800px';
        expect(__testOnly_persistThinkingCardBodyHeightFromDom(request)).toBe(520);
        expect(request.thinkingCardBodyHeightPx).toBe(520);
        expect(detail.style.height).toBe('520px');
    });
});

describe('copy diagnostics button visibility on preserved finished card', () => {
    beforeEach(() => {
        jest.useFakeTimers();
        __testOnly_setThinkingCardRequests(null, null);
        document.body.innerHTML = `
            <div id="scrollableField"></div>
            <div class="thinking-card-wrapper" id="thinkingCardWrapper" aria-hidden="true">
                <div class="thinking-card" role="status" aria-live="polite">
                    <div class="thinking-card-header" id="loadingIndicator" data-thinking-role="header">
                        <span class="thinking-card-phase loading-indicator-text" data-thinking-role="phase">Thinking...</span>
                        <span id="thinkingCardStatusBadge" class="thinking-card-status active" data-thinking-role="status" aria-hidden="true">Active</span>
                        <span class="thinking-card-meta" id="thinkingCardMeta" data-thinking-role="meta"></span>
                        <button id="thinkingCardToggleButton" type="button" data-thinking-role="toggle" aria-hidden="true" aria-expanded="true" aria-controls="loadingIndicatorDetail" aria-label="Collapse thinking details" title="Collapse thinking details">
                            <span class="thinking-card-toggle-icon" aria-hidden="true">⌄</span>
                            <span class="visually-hidden">Toggle thinking details</span>
                        </button>
                        <button type="button" class="thinking-card-size-button" data-thinking-role="size-decrease" aria-hidden="true">−</button>
                        <button type="button" class="thinking-card-size-button" data-thinking-role="size-increase" aria-hidden="true">+</button>
                        <button id="retryThinkingButton" type="button" data-thinking-role="retry" aria-hidden="true">Retry</button>
                        <button id="copyThinkingDiagnosticsButton" type="button" data-thinking-role="copy" aria-hidden="true">Copy diagnostic snapshot</button>
                        <button id="abortButton" type="button" data-thinking-role="abort" aria-hidden="true"></button>
                    </div>
                    <div class="thinking-card-body" id="loadingIndicatorDetail" data-thinking-role="detail" aria-live="polite"></div>
                </div>
            </div>
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
            <input type="checkbox" id="annotationToggle" />
        `;
    });

    afterEach(() => {
        jest.useRealTimers();
        jest.restoreAllMocks();
        __testOnly_setThinkingCardRequests(null, null);
    });

    test('copy diagnostics button remains visible on preserved finished card (JVNAUTOSCI-1431)', () => {
        const copyBtn = document.getElementById('copyThinkingDiagnosticsButton');
        const toggleBtn = document.getElementById('thinkingCardToggleButton');

        // While thinking — both visible
        __testOnly_setThinkingState(true, {});
        expect(copyBtn.getAttribute('aria-hidden')).toBe('false');
        expect(toggleBtn.getAttribute('aria-hidden')).toBe('false');

        // Finished with preserveFinishedCard — both should stay visible
        __testOnly_setThinkingState(false, {}, { preserveFinishedCard: true });
        expect(copyBtn.getAttribute('aria-hidden')).toBe('false');
        expect(toggleBtn.getAttribute('aria-hidden')).toBe('false');

        // Finished without preserveFinishedCard — both should hide
        __testOnly_setThinkingState(false, {});
        expect(copyBtn.getAttribute('aria-hidden')).toBe('true');
        expect(toggleBtn.getAttribute('aria-hidden')).toBe('true');
    });

    test('copy diagnostics button resets text only when fully dismissed', () => {
        const copyBtn = document.getElementById('copyThinkingDiagnosticsButton');

        // Simulate a successful copy feedback state
        copyBtn.textContent = '✓ Copied';
        copyBtn.classList.add('copy-json-copied');
        copyBtn.setAttribute('title', 'Copied to clipboard');
        copyBtn.setAttribute('aria-label', 'Copied to clipboard');

        // Preserved finished card should keep the feedback text
        __testOnly_setThinkingState(false, {}, { preserveFinishedCard: true });
        expect(copyBtn.textContent).toBe('✓ Copied');
        expect(copyBtn.classList.contains('copy-json-copied')).toBe(true);

        // Full dismissal should reset
        __testOnly_setThinkingState(false, {});
        expect(copyBtn.textContent).toBe('Copy diagnostic snapshot');
        expect(copyBtn.getAttribute('title')).toBe('Copy diagnostic snapshot');
        expect(copyBtn.getAttribute('aria-label')).toBe('Copy diagnostic snapshot');
        expect(copyBtn.classList.contains('copy-json-copied')).toBe(false);
    });

    test('copy diagnostics button uses shared copied-state feedback after success (JVNAUTOSCI-1458)', async () => {
        const copyBtn = document.getElementById('copyThinkingDiagnosticsButton');
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });

        const copied = await __testOnly_copyActiveThinkingDiagnostics(copyBtn, {
            clientRequestId: 'request-1458',
            thinkingCardMode: 'debug',
            promptRaw: 'diagnose this',
            thinkingStartedAtMs: Date.now() - 250,
            latestProgress: null,
            activityHistory: [],
            progressEvents: [],
            phaseHistory: [],
            workflowDiscovery: null,
            workflowStagePath: null
        });

        expect(copied).toBe(true);
        const copiedPayload = JSON.parse(writeText.mock.calls[0][0]);
        expect(copiedPayload.schema_version).toBe('thinking_diagnostics_snapshot.v1');
        expect(copiedPayload.thinking_card_mode).toBe('debug');
        expect(copiedPayload.mcp_access).toEqual(expect.objectContaining({
            turn_execution_get_live_progress: expect.objectContaining({
                tool_name: 'turn_execution_get_live_progress'
            })
        }));
        expect(copyBtn.textContent).toBe('✓ Copied');
        expect(copyBtn.classList.contains('copy-json-copied')).toBe(true);
        expect(copyBtn.classList.contains('success-feedback')).toBe(false);
        expect(copyBtn.getAttribute('title')).toBe('Copied to clipboard');
        expect(copyBtn.getAttribute('aria-label')).toBe('Copied to clipboard');

        jest.advanceTimersByTime(2300);
        expect(copyBtn.textContent).toBe('Copy diagnostic snapshot');
        expect(copyBtn.classList.contains('copy-json-copied')).toBe(false);
    });

    test('copy diagnostics button uses the preserved finished card when no active request exists', async () => {
        const copyBtn = document.getElementById('copyThinkingDiagnosticsButton');
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });

        const finishedRequest = {
            clientRequestId: 'request-preserved',
            promptRaw: 'diagnose this',
            thinkingStartedAtMs: Date.now() - 250,
            latestProgress: {
                status: 'completed',
                phase_label: 'Turn completed',
                result_summary: 'Response generated',
                sequence_no: 3,
                updated_at_epoch: 1003,
                elapsed_ms: 250
            },
            activityHistory: [],
            progressEvents: [],
            phaseHistory: [],
            workflowDiscovery: null,
            workflowStagePath: null,
            stageDiagnostics: []
        };

        __testOnly_setThinkingCardRequests(null, finishedRequest);
        __testOnly_setThinkingState(false, finishedRequest, { preserveFinishedCard: true });

        const copied = await __testOnly_copyActiveThinkingDiagnostics(copyBtn);

        expect(copied).toBe(true);
        expect(writeText).toHaveBeenCalledTimes(1);
        expect(JSON.parse(writeText.mock.calls[0][0])).toEqual(expect.objectContaining({
            request_id: 'request-preserved'
        }));
    });

    test('diagnostics export snapshot uses the preserved finished card when no active request exists', () => {
        const finishedRequest = {
            clientRequestId: 'request-export',
            thinkingCardMode: 'expert',
            promptRaw: 'diagnose this',
            thinkingStartedAtMs: Date.now() - 250,
            latestProgress: {
                status: 'completed',
                phase_label: 'Turn completed',
                result_summary: 'Response generated',
                sequence_no: 4,
                updated_at_epoch: 1004,
                elapsed_ms: 250
            },
            activityHistory: [],
            progressEvents: [],
            phaseHistory: [],
            workflowDiscovery: null,
            workflowStagePath: null,
            stageDiagnostics: []
        };

        __testOnly_setThinkingCardRequests(null, finishedRequest);

        const payload = __testOnly_buildDiagnosticsExportRequestPayload();

        expect(payload.capture_scope).toBe('current_diagnostic_snapshot');
        expect(payload.thinking_card_mode).toBe('expert');
        expect(payload.diagnostics.active_thinking).toEqual(expect.objectContaining({
            request_id: 'request-export',
            thinking_card_mode: 'expert'
        }));
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
        __testOnly_setSessionTabsCache([
            {
                session_id: 'chat-abort-test-session',
                session_name: 'Chat abort test session',
                message_count: 0,
                is_completed: false
            }
        ]);
        __testOnly_setActiveChatSession('chat-abort-test-session', 'Chat abort test session');
        __testOnly_setDisplayedHistorySession('chat-abort-test-session');
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

        expect(document.getElementById('sendButton').disabled).toBe(false);
        expect(document.getElementById('sendButton').textContent).toBe('Queue Prompt');
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

describe('chat session composer state', () => {
    function renderSessionComposerDom() {
        document.body.innerHTML = `
            <div id="chatTab" class="tab-content active"></div>
            <div id="chatSessionTabs"></div>
            <div id="chatSessionCount"></div>
            <div id="chatSessionMetadata"></div>
            <div id="workflowStatusPanel"></div>
            <div id="workflowStatusBody"></div>
            <div id="historyBanner" class="hidden"></div>
            <span id="historyBannerText"></span>
            <button id="loadOlderHistoryBtn" type="button"></button>
            <div id="scrollableField"></div>
            <div class="thinking-card-wrapper" id="thinkingCardWrapper" aria-hidden="true">
                <div class="thinking-card">
                    <div id="loadingIndicator" data-thinking-role="header"></div>
                    <div id="loadingIndicatorDetail" data-thinking-role="detail"></div>
                </div>
            </div>
            <button id="sendButton" type="button"></button>
            <button id="resetButton" type="button"></button>
            <button id="abortButton" type="button" aria-hidden="true"></button>
            <textarea id="promptInput" class="prompt-input"></textarea>
        `;
    }

    beforeEach(() => {
        renderSessionComposerDom();
        __testOnly_setThinkingCardRequests(null, null);
        __testOnly_setActiveChatSession(null, null);
        __testOnly_setSessionTabsCache([]);

        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReset();
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });
    });

    afterEach(() => {
        __testOnly_setThinkingCardRequests(null, null);
        __testOnly_setActiveChatSession(null, null);
        __testOnly_setSessionTabsCache([]);
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('switching chat sessions keeps the originating request running in the background', async () => {
        const promptInput = document.getElementById('promptInput');
        initializePromptCartoucheOverlay(promptInput);

        const overlayContent = promptInput.parentElement.querySelector('.prompt-input-overlay-content');
        expect(overlayContent.textContent).toBe('');

        const abortController = new AbortController();
        const abortSpy = jest.spyOn(abortController, 'abort');

        __testOnly_setActiveChatSession('session-1', 'Current');
        __testOnly_setSessionTabsCache([
            { session_id: 'session-1', session_name: 'Current', message_count: 1, last_message_at: '2026-04-06T06:00:00Z' },
            { session_id: 'session-2', session_name: 'Target', message_count: 0, last_message_at: '2026-04-06T06:05:00Z' }
        ]);
        __testOnly_setThinkingCardRequests({
            abortController,
            sessionId: 'session-1',
            promptRaw: 'https://arxiv.org/abs/2411.04983',
            selectionStart: 31,
            selectionEnd: 31,
            clientRequestId: 'request-session-1',
            activityHistory: [],
            progressEvents: [],
            phaseHistory: [],
            stageDiagnostics: [],
            latestProgress: null,
            workflowStagePath: null,
            thinkingCardDisplayState: null
        }, null);

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/von/api/session/set_chat_session')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ session_id: 'session-2', session_name: 'Target' })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/session/chat_session_links')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ session_links: {} })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history?')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        history: [],
                        segments_returned: 1,
                        total_segments: 0,
                        total_messages: 0
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({ sessions: [] }) });
        });

        await expect(switchToChatSession('session-2')).resolves.toEqual(
            expect.objectContaining({ ok: true })
        );

        const backgroundTab = document.querySelector('.chat-session-tab[data-session-id="session-1"]');
        expect(backgroundTab).not.toBeNull();
        expect(backgroundTab.classList.contains('has-background-request')).toBe(true);
        expect(abortSpy).not.toHaveBeenCalled();
        expect(document.getElementById('sendButton').textContent).toBe('Queue Prompt');
        expect(promptInput.value).toBe('');
        expect(overlayContent.textContent).toBe('');
    });

    test('queued prompts stay pinned to their originating session after switching away again', async () => {
        const promptInput = document.getElementById('promptInput');
        initializePromptCartoucheOverlay(promptInput);

        __testOnly_setActiveChatSession('session-1', 'Current');
        __testOnly_setSessionTabsCache([
            { session_id: 'session-1', session_name: 'Current', message_count: 1, last_message_at: '2026-04-06T06:00:00Z' },
            { session_id: 'session-2', session_name: 'Target', message_count: 0, last_message_at: '2026-04-06T06:05:00Z' }
        ]);

        let resolveFirstGenerate;
        const firstGeneratePromise = new Promise((resolve) => {
            resolveFirstGenerate = resolve;
        });
        const generateBodies = [];

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/session/set_chat_session')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        session_id: body.session_id,
                        session_name: body.session_id === 'session-2' ? 'Target' : 'Current'
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/session/chat_session_links')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ session_links: {} })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history?')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        history: [],
                        segments_returned: 1,
                        total_segments: 0,
                        total_messages: 0
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, session_count: 2, authenticated: true })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'completed',
                        phase: 'tool_execute',
                        phase_label: 'Executing tools',
                        tool: 'search_knowledge_base',
                        result_summary: 'Completed'
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateBodies.push(JSON.parse(options.body || '{}'));
                if (generateBodies.length === 1) {
                    return firstGeneratePromise;
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Queued session response',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        promptInput.value = 'request for session 1';
        const firstSendPromise = sendMessage();
        await Promise.resolve();
        await Promise.resolve();

        await expect(switchToChatSession('session-2')).resolves.toEqual(
            expect.objectContaining({ ok: true })
        );

        promptInput.value = 'queued for session 2';
        await expect(sendMessage()).resolves.toBeUndefined();

        const queueLabels = Array.from(document.querySelectorAll('.chat-task-queue-item-label'))
            .map((node) => node.textContent || '');
        expect(queueLabels).toContain('Next up • Target');

        await expect(switchToChatSession('session-1')).resolves.toEqual(
            expect.objectContaining({ ok: true })
        );

        resolveFirstGenerate({
            ok: true,
            json: async () => ({
                response: 'First session response',
                llm_debug: { model: 'gpt-5.2' }
            })
        });

        await firstSendPromise;
        await Promise.resolve();
        await Promise.resolve();
        await new Promise((resolve) => setTimeout(resolve, 0));
        await Promise.resolve();
        await Promise.resolve();

        expect(generateBodies).toHaveLength(2);
        expect(generateBodies[0].conversation_session_id).toBe('session-1');
        expect(generateBodies[1].conversation_session_id).toBe('session-2');
        expect(document.getElementById('scrollableField').textContent).not.toContain('queued for session 2');
    });

    test('refresh adopts the most recent session when startup state has no active conversation', async () => {
        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/von/history/sessions')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        active_session_id: null,
                        sessions: [
                            {
                                session_id: 'session-recent',
                                session_name: 'Most recent',
                                message_count: 4,
                                last_message_at: '2026-04-13T05:00:00Z'
                            },
                            {
                                session_id: 'session-older',
                                session_name: 'Older',
                                message_count: 2,
                                last_message_at: '2026-04-10T05:00:00Z'
                            }
                        ]
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/session/chat_session_links')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ session_links: {} })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await expect(__testOnly_refreshChatSessionTabs()).resolves.toBeUndefined();

        const activeTab = document.querySelector(
            '.chat-session-tab[data-session-id="session-recent"]'
        );
        expect(activeTab).not.toBeNull();
        expect(activeTab.classList.contains('is-active')).toBe(true);
    });

    test('first send without an active conversation creates a real session before generate', async () => {
        const promptInput = document.getElementById('promptInput');
        initializePromptCartoucheOverlay(promptInput);
        promptInput.value = 'First prompt in a fresh window';

        const createBodies = [];
        const generateBodies = [];

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/api/settings/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ show_tool_use_during_thinking: true })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/sessions')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        active_session_id: null,
                        sessions: []
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/session/create_chat_session')) {
                createBodies.push(JSON.parse(options.body || '{}'));
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        session_id: 'session-created-first-send',
                        session_name: 'Chat 2026-04-13 18:30',
                        history: []
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/session/chat_session_links')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ session_links: {} })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'completed',
                        phase: 'tool_execute',
                        phase_label: 'Executing tools',
                        result_summary: 'Completed'
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        history_length: 1,
                        session_count: 1,
                        authenticated: true
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateBodies.push(JSON.parse(options.body || '{}'));
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Created session response',
                        session_id: 'session-created-first-send',
                        conversation_session_id: 'session-created-first-send',
                        conversation_session_name: 'Chat 2026-04-13 18:30',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await expect(sendMessage()).resolves.toBeUndefined();

        expect(createBodies).toHaveLength(1);
        expect(generateBodies).toHaveLength(1);
        expect(generateBodies[0].conversation_session_id).toBe(
            'session-created-first-send'
        );
    });

    test('creating a new chat session clears the composer overlay as well as the textarea value', async () => {
        const promptInput = document.getElementById('promptInput');
        initializePromptCartoucheOverlay(promptInput);

        promptInput.value = 'Draft text to clear';
        promptInput.dispatchEvent(new Event('input', { bubbles: true }));

        const overlayContent = promptInput.parentElement.querySelector('.prompt-input-overlay-content');
        expect(overlayContent.textContent).toContain('Draft text to clear');

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/von/api/session/create_chat_session')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        session_id: 'session-new',
                        session_name: 'New chat',
                        history: []
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/session/chat_session_links')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ session_links: {} })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({ sessions: [] }) });
        });

        await expect(__testOnly_createChatSession('New chat')).resolves.toEqual(
            expect.objectContaining({ session_id: 'session-new' })
        );

        expect(promptInput.value).toBe('');
        expect(overlayContent.textContent).toBe('');
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

describe('chat message layout hooks', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="chatTab">
                <div class="content-wrapper">
                    <div id="scrollableField"></div>
                </div>
            </div>
        `;
        __testOnly_clearLlmDebugData();
    });

    test('renders assistant turns with chat layout classes for wide wrapping', () => {
        __testOnly_appendMessage(
            'Von',
            'A recommendation with a very long title and a long explanation body.',
            'turn-assistant-layout'
        );

        const messageContainer = document.querySelector('.message-container.assistant-turn');
        expect(messageContainer).toBeTruthy();
        expect(messageContainer.querySelector('.message-body')).toBeTruthy();
        expect(messageContainer.querySelector('.message-header')).toBeTruthy();
        expect(messageContainer.querySelector('.chat-message-controls')).toBeTruthy();
        expect(messageContainer.querySelector('.chat-message-text')).toBeTruthy();
    });

    test('renders user turns with the shared message header/text layout hooks', () => {
        __testOnly_appendMessage(
            'User',
            'A user prompt containing a long inline concept reference.',
            'turn-user-layout'
        );

        const messageContainer = document.querySelector('.message-container.user-turn');
        expect(messageContainer).toBeTruthy();
        expect(messageContainer.querySelector('.message-header')).toBeTruthy();
        expect(messageContainer.querySelector('.chat-message-text')).toBeTruthy();
    });
});

describe('relation truth-state display elements', () => {
    beforeEach(() => {
        const { createVontologyCartouche, normalisePotentialConceptId } = require('../utils/textDecorator.js');
        createVontologyCartouche.mockClear();
        normalisePotentialConceptId.mockClear();
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

    test('derives relation rows from triple-like text when structured elements are absent', () => {
        const container = document.createElement('div');
        container.dataset.originalText = [
            '- #V#michael_witbrock -- #V#panelist_in_event --> #V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne',
            'This line is not a triple.'
        ].join('\n');

        __testOnly_renderDisplayElementsIntoContainer(container, {});

        const section = container.querySelector('.chat-display-elements-relation-truth-state-section');
        expect(section).not.toBeNull();
        expect(section.textContent).toContain('Relation triples (parsed from text)');
        expect(section.textContent).toContain('derived from text');

        const rows = container.querySelectorAll('.chat-display-elements-relation-truth-state-assertion');
        expect(rows.length).toBe(1);

        const root = container.querySelector('.chat-display-elements');
        expect(root.dataset.relationTruthStateSource).toBe('derived_from_text');
        expect(root.dataset.relationTripleParseCount).toBe('1');

        const { createVontologyCartouche } = require('../utils/textDecorator.js');
        expect(createVontologyCartouche).toHaveBeenCalledTimes(3);
    });

    test('prefers structured relation elements over text-derived fallback triples', () => {
        const container = document.createElement('div');
        container.dataset.originalText = '#V#a -- #V#p --> #V#b';
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
                                    label: 'Structured',
                                    status: 'asserted',
                                    assertions: [
                                        {
                                            assertion_id: 'a1',
                                            arg1: '#V#left',
                                            predicate: '#V#relates_to',
                                            arg2: '#V#right',
                                            is_asserted: true
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ]
            }
        };

        __testOnly_renderDisplayElementsIntoContainer(container, debugData);

        const section = container.querySelector('.chat-display-elements-relation-truth-state-section');
        expect(section).not.toBeNull();
        expect(section.textContent).toContain('Current truth state');
        expect(section.textContent).not.toContain('derived from text');

        const root = container.querySelector('.chat-display-elements');
        expect(root.dataset.relationTruthStateSource).toBe('structured');
        expect(root.dataset.relationTripleParseCount).toBe('0');

        const { createVontologyCartouche } = require('../utils/textDecorator.js');
        expect(createVontologyCartouche).toHaveBeenCalledTimes(3);
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
                            title: 'Task board',
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
        expect(section.textContent).toContain('Task board');

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

describe('calendar display elements', () => {
    test('renders calendar day cards with date labels and task links', () => {
        const container = document.createElement('div');
        const debugData = {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_calendar_view',
                        element_type: 'calendar_view',
                        channel: 'screen',
                        order: 38,
                        intent: 'structured_calendar_view',
                        payload: {
                            title: 'Upcoming schedule',
                            default_granularity: 'month',
                            focus_date: '2026-02-17',
                            items: [
                                {
                                    item_id: '#V#task_alpha',
                                    title: 'Alpha task',
                                    start_at: '2026-02-17',
                                    all_day: true,
                                    status: 'pending',
                                    task_links: [
                                        {
                                            link_type: 'von_task',
                                            target_id: '#V#task_alpha',
                                            label: '#V#task_alpha'
                                        },
                                        {
                                            link_type: 'jira_issue',
                                            target_id: 'JVNAUTOSCI-1177',
                                            label: 'JVNAUTOSCI-1177',
                                            href: 'https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1177'
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

        const section = container.querySelector('.chat-display-elements-calendar-section');
        expect(section).not.toBeNull();
        expect(section.textContent).toContain('Upcoming schedule');
        expect(section.textContent).toContain('Alpha task');

        const dayCards = container.querySelectorAll('.chat-display-elements-calendar-day');
        expect(dayCards.length).toBe(1);
        expect(dayCards[0].textContent).toContain('2026');

        const conceptButton = section.querySelector('.chat-display-elements-concept-link');
        expect(conceptButton).not.toBeNull();
        expect(conceptButton.dataset.conceptId).toBe('#V#task_alpha');

        const jiraLink = section.querySelector('a[href="https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1177"]');
        expect(jiraLink).not.toBeNull();
    });
});

describe('chart display elements', () => {
    test('renders structured chart view with svg and readable legend', () => {
        const container = document.createElement('div');
        const debugData = {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_chart_view',
                        element_type: 'chart_view',
                        channel: 'screen',
                        order: 37,
                        intent: 'structured_chart_view',
                        payload: {
                            title: 'Task trend',
                            chart_type: 'line',
                            x_axis: 'Week',
                            y_axis: 'Count',
                            legend: true,
                            series: [
                                {
                                    series_id: 'open_tasks',
                                    label: 'Open tasks',
                                    points: [
                                        { x: '2026-02-17', y: 8 },
                                        { x: '2026-02-24', y: 6 }
                                    ]
                                },
                                {
                                    series_id: 'closed_tasks',
                                    label: 'Closed tasks',
                                    points: [
                                        { x: '2026-02-17', y: 2 },
                                        { x: '2026-02-24', y: 4 }
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

        const section = container.querySelector('.chat-display-elements-chart-section');
        expect(section).not.toBeNull();
        expect(section.textContent).toContain('Task trend');
        expect(section.textContent).toContain('Open tasks');
        expect(section.textContent).toContain('Closed tasks');

        const svg = section.querySelector('.chat-display-elements-chart-svg');
        expect(svg).not.toBeNull();
        expect(svg.getAttribute('viewBox')).toContain('0 0');

        const legendItems = section.querySelectorAll('.chat-display-elements-chart-legend-item');
        expect(legendItems.length).toBe(2);

        const fallbackRows = section.querySelectorAll('.chat-display-elements-chart-fallback-list li');
        expect(fallbackRows.length).toBe(2);
    });
});

describe('location display elements', () => {
    test('renders location map markers with address fallback and task links', () => {
        const container = document.createElement('div');
        const debugData = {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_location_view',
                        element_type: 'location_view',
                        channel: 'screen',
                        order: 39,
                        intent: 'structured_location_view',
                        payload: {
                            title: 'Meeting locations',
                            points: [
                                {
                                    point_id: '#V#task_alpha',
                                    label: 'AUT city campus',
                                    latitude: -36.8509,
                                    longitude: 174.7676,
                                    address: '55 Wellesley Street East, Auckland',
                                    task_links: [
                                        {
                                            link_type: 'von_task',
                                            target_id: '#V#task_alpha',
                                            label: '#V#task_alpha'
                                        }
                                    ]
                                },
                                {
                                    point_id: '#V#task_beta',
                                    label: 'Remote participant',
                                    address: 'Wellington, New Zealand'
                                }
                            ]
                        },
                        provenance: { source: 'test' }
                    }
                ]
            }
        };

        __testOnly_renderDisplayElementsIntoContainer(container, debugData);

        const section = container.querySelector('.chat-display-elements-location-section');
        expect(section).not.toBeNull();
        expect(section.textContent).toContain('Meeting locations');
        expect(section.textContent).toContain('AUT city campus');
        expect(section.textContent).toContain('Wellington, New Zealand');

        const markers = container.querySelectorAll('.chat-display-elements-location-map-marker');
        expect(markers.length).toBe(1);

        const mapLinks = section.querySelectorAll('.chat-display-elements-location-map-link');
        expect(mapLinks.length).toBeGreaterThanOrEqual(1);
        expect(mapLinks[0].href).toContain('openstreetmap.org');

        const conceptButton = section.querySelector('.chat-display-elements-concept-link');
        expect(conceptButton).not.toBeNull();
        expect(conceptButton.dataset.conceptId).toBe('#V#task_alpha');
    });
});

describe('document display elements', () => {
    test('renders collapsible document cards with section excerpts and citations', () => {
        const container = document.createElement('div');
        const debugData = {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_document_view',
                        element_type: 'document_view',
                        channel: 'screen',
                        order: 40,
                        intent: 'structured_document_view',
                        payload: {
                            title: 'Knowledge snippets',
                            documents: [
                                {
                                    document_id: 'doc_alpha',
                                    title: 'Alpha document',
                                    source_uri: 'https://example.com/doc-alpha',
                                    source_label: 'search_knowledge_base',
                                    updated_at: '2026-02-17T10:00:00Z',
                                    sections: [
                                        {
                                            section_id: 's1',
                                            heading: 'Summary',
                                            excerpt: 'Alpha excerpt ... [truncated]',
                                            excerpt_truncated: true,
                                            excerpt_original_char_count: 1200,
                                            citation: 'https://example.com/doc-alpha#summary',
                                            task_links: [
                                                {
                                                    link_type: 'von_task',
                                                    target_id: '#V#task_alpha',
                                                    label: '#V#task_alpha'
                                                }
                                            ]
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

        const section = container.querySelector('.chat-display-elements-document-section');
        expect(section).not.toBeNull();
        expect(section.textContent).toContain('Knowledge snippets');
        expect(section.textContent).toContain('Alpha document');
        expect(section.textContent).toContain('Excerpt truncated');

        const details = section.querySelectorAll('.chat-display-elements-document-card');
        expect(details.length).toBe(1);
        expect(details[0].open).toBe(true);

        const citationLink = section.querySelector('a[href="https://example.com/doc-alpha#summary"]');
        expect(citationLink).not.toBeNull();

        const conceptButton = section.querySelector('.chat-display-elements-concept-link');
        expect(conceptButton).not.toBeNull();
        expect(conceptButton.dataset.conceptId).toBe('#V#task_alpha');
    });
});

describe('relation graph display elements', () => {
    beforeEach(() => {
        const { createVontologyCartouche } = require('../utils/textDecorator.js');
        createVontologyCartouche.mockClear();
    });

    test('renders relation graph nodes with clickable cartouches and predicate edge labels', () => {
        const container = document.createElement('div');
        const debugData = {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_relation_graph_view',
                        element_type: 'relation_graph_view',
                        channel: 'screen',
                        order: 43,
                        intent: 'relation_graph_view',
                        payload: {
                            title: 'Predicate relation graph',
                            nodes: [
                                {
                                    node_id: '#V#michael_witbrock',
                                    label: 'Michael Witbrock',
                                    node_kind: 'individual'
                                },
                                {
                                    node_id: '#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne',
                                    label: 'Panel 4',
                                    node_kind: 'individual'
                                }
                            ],
                            edges: [
                                {
                                    edge_id: 'edge_1',
                                    source: '#V#michael_witbrock',
                                    target: '#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne',
                                    predicate: '#V#panelist_in_event',
                                    direction: 'directed'
                                }
                            ],
                            focus_node_id: '#V#michael_witbrock'
                        },
                        provenance: { source: 'test' }
                    }
                ]
            }
        };

        __testOnly_renderDisplayElementsIntoContainer(container, debugData);

        const section = container.querySelector('.chat-display-elements-relation-graph-section');
        expect(section).not.toBeNull();
        expect(section.textContent).toContain('Predicate relation graph');

        const nodes = container.querySelectorAll('.chat-display-elements-relation-graph-node');
        expect(nodes.length).toBe(2);
        expect(container.querySelectorAll('.chat-display-elements-relation-graph-edge-label').length).toBe(1);
        expect(container.textContent).toContain('#V#panelist_in_event');

        const { createVontologyCartouche } = require('../utils/textDecorator.js');
        expect(createVontologyCartouche).toHaveBeenCalledTimes(2);
    });
});

describe('hierarchy display elements', () => {
    beforeEach(() => {
        const { createVontologyCartouche } = require('../utils/textDecorator.js');
        createVontologyCartouche.mockClear();
    });

    test('renders hierarchy with focus neighbourhood panels for parents, siblings and children', () => {
        const container = document.createElement('div');
        const debugData = {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_hierarchy_view',
                        element_type: 'hierarchy_view',
                        channel: 'screen',
                        order: 42,
                        intent: 'hierarchy_view',
                        payload: {
                            title: 'Meeting hierarchy',
                            nodes: [
                                { node_id: '#V#meeting', label: 'Meeting', node_kind: 'type' },
                                { node_id: '#V#reading_group_meeting', label: 'Reading group meeting', node_kind: 'type' },
                                { node_id: '#V#naoi_reading_group_meeting', label: 'NAOI reading group meeting', node_kind: 'type' },
                                { node_id: '#V#sail_reading_group_meeting', label: 'SAIL reading group meeting', node_kind: 'type' },
                                { node_id: '#V#other_reading_group_meeting', label: 'Other reading group meeting', node_kind: 'type' }
                            ],
                            edges: [
                                {
                                    edge_id: 'hierarchy_edge_1',
                                    parent_node_id: '#V#meeting',
                                    child_node_id: '#V#reading_group_meeting',
                                    predicate: '#V#is_a_type_of',
                                    branch_kind: 'type_hierarchy'
                                },
                                {
                                    edge_id: 'hierarchy_edge_2',
                                    parent_node_id: '#V#reading_group_meeting',
                                    child_node_id: '#V#naoi_reading_group_meeting',
                                    predicate: '#V#is_a_type_of',
                                    branch_kind: 'type_hierarchy'
                                },
                                {
                                    edge_id: 'hierarchy_edge_3',
                                    parent_node_id: '#V#reading_group_meeting',
                                    child_node_id: '#V#other_reading_group_meeting',
                                    predicate: '#V#is_a_type_of',
                                    branch_kind: 'type_hierarchy'
                                },
                                {
                                    edge_id: 'hierarchy_edge_4',
                                    parent_node_id: '#V#naoi_reading_group_meeting',
                                    child_node_id: '#V#sail_reading_group_meeting',
                                    predicate: '#V#is_a_type_of',
                                    branch_kind: 'type_hierarchy'
                                }
                            ],
                            focus_node_id: '#V#naoi_reading_group_meeting',
                            root_node_ids: ['#V#meeting'],
                            expansion: {
                                show_parents: true,
                                show_children: true,
                                show_siblings: true,
                                max_depth: 4
                            }
                        },
                        provenance: { source: 'test' }
                    }
                ]
            }
        };

        __testOnly_renderDisplayElementsIntoContainer(container, debugData);

        const section = container.querySelector('.chat-display-elements-hierarchy-section');
        expect(section).not.toBeNull();
        expect(section.textContent).toContain('Meeting hierarchy');
        expect(section.textContent).toContain('Parents (1)');
        expect(section.textContent).toContain('Siblings (1)');
        expect(section.textContent).toContain('Children (1)');
        expect(section.textContent).toContain('Hierarchy tree');

        const { createVontologyCartouche } = require('../utils/textDecorator.js');
        expect(createVontologyCartouche).toHaveBeenCalled();
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
            'Configured tool-invocation cap (2) was reached; later tool-shaped output was not executed.'
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

    test('includes calendar render plan provenance counts when present', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                attempted: true,
                success: true,
                reason: 'resolved',
                selected_renderer_ids: ['#V#renderer_calendar'],
                selected_renderer_types: ['calendar'],
                screen_element_families: ['calendar_view'],
                screen_calendar_elements: [
                    {
                        element_id: 'screen_calendar_view',
                        provenance: {
                            source_tools: ['task_list', 'workflow_list_instances'],
                            record_family: 'calendar_items'
                        }
                    }
                ]
            }
        });

        expect(metadata.render_plan).toMatchObject({
            selected_renderer_types: ['calendar'],
            screen_element_families: ['calendar_view'],
            screen_calendar_element_count: 1,
            source_tools: ['task_list', 'workflow_list_instances'],
            record_families: ['calendar_items']
        });
    });

    test('includes location render plan provenance counts when present', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                attempted: true,
                success: true,
                reason: 'resolved',
                selected_renderer_ids: ['#V#renderer_location'],
                selected_renderer_types: ['location'],
                screen_element_families: ['location_view'],
                screen_location_elements: [
                    {
                        element_id: 'screen_location_view',
                        provenance: {
                            source_tools: ['task_list'],
                            record_family: 'location_points'
                        }
                    }
                ]
            }
        });

        expect(metadata.render_plan).toMatchObject({
            selected_renderer_types: ['location'],
            screen_element_families: ['location_view'],
            screen_location_element_count: 1,
            source_tools: ['task_list'],
            record_families: ['location_points']
        });
    });

    test('includes chart render plan provenance counts when present', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                attempted: true,
                success: true,
                reason: 'resolved',
                selected_renderer_ids: ['#V#renderer_chart'],
                selected_renderer_types: ['chart'],
                screen_element_families: ['chart_view'],
                screen_chart_elements: [
                    {
                        element_id: 'screen_chart_view',
                        provenance: {
                            source_tools: ['task_list'],
                            record_family: 'chart_series'
                        }
                    }
                ]
            }
        });

        expect(metadata.render_plan).toMatchObject({
            selected_renderer_types: ['chart'],
            screen_element_families: ['chart_view'],
            screen_chart_element_count: 1,
            source_tools: ['task_list'],
            record_families: ['chart_series']
        });
    });

    test('includes document render plan provenance counts when present', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                attempted: true,
                success: true,
                reason: 'resolved',
                selected_renderer_ids: ['#V#renderer_document'],
                selected_renderer_types: ['document'],
                screen_element_families: ['document_view'],
                screen_document_elements: [
                    {
                        element_id: 'screen_document_view',
                        provenance: {
                            source_tools: ['search_knowledge_base'],
                            record_family: 'documents'
                        }
                    }
                ]
            }
        });

        expect(metadata.render_plan).toMatchObject({
            selected_renderer_types: ['document'],
            screen_element_families: ['document_view'],
            screen_document_element_count: 1,
            source_tools: ['search_knowledge_base'],
            record_families: ['documents']
        });
    });

    test('includes relation graph render plan provenance counts when present', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                attempted: true,
                success: true,
                reason: 'resolved',
                selected_renderer_ids: ['#V#renderer_relation_graph'],
                selected_renderer_types: ['relation_graph'],
                screen_element_families: ['relation_graph_view'],
                screen_relation_graph_elements: [
                    {
                        element_id: 'screen_relation_graph_view',
                        provenance: {
                            source_tools: ['get_predicate_extent'],
                            record_family: 'relation_graph'
                        }
                    }
                ]
            }
        });

        expect(metadata.render_plan).toMatchObject({
            selected_renderer_types: ['relation_graph'],
            screen_element_families: ['relation_graph_view'],
            screen_relation_graph_element_count: 1,
            source_tools: ['get_predicate_extent'],
            record_families: ['relation_graph']
        });
    });

    test('includes hierarchy render plan provenance counts when present', () => {
        const metadata = __testOnly_buildLlmDebugMetadata({
            model: 'gpt-test',
            messages: [],
            render_plan: {
                enabled: true,
                attempted: true,
                success: true,
                reason: 'resolved',
                selected_renderer_ids: ['#V#renderer_hierarchy'],
                selected_renderer_types: ['hierarchy'],
                screen_element_families: ['hierarchy_view'],
                screen_hierarchy_elements: [
                    {
                        element_id: 'screen_hierarchy_view',
                        provenance: {
                            source_tools: ['fetch_concept'],
                            record_family: 'hierarchy'
                        }
                    }
                ]
            }
        });

        expect(metadata.render_plan).toMatchObject({
            selected_renderer_types: ['hierarchy'],
            screen_element_families: ['hierarchy_view'],
            screen_hierarchy_element_count: 1,
            source_tools: ['fetch_concept'],
            record_families: ['hierarchy']
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

describe('latest unread jump affordance', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="chatTab">
                <div class="content-wrapper">
                    <div id="scrollableField"></div>
                </div>
            </div>
        `;
        __testOnly_clearLatestUnreadJumpState();
    });

    test('stays hidden when no unread boundary exists', () => {
        __testOnly_showNewSharedMessagesIndicator();
        const indicator = document.getElementById('newSharedMessagesIndicator');
        expect(indicator).toBeTruthy();
        expect(indicator.style.display).toBe('none');
        expect(indicator.disabled).toBe(true);
    });

    test('jumps to the latest unread boundary when clicked', () => {
        const scrollableField = document.getElementById('scrollableField');
        const message = document.createElement('div');
        message.className = 'chat-message assistant-message';
        scrollableField.appendChild(message);

        const boundary = __testOnly_setLatestUnreadBoundary(message);
        boundary.scrollIntoView = jest.fn();
        boundary.focus = jest.fn();

        __testOnly_showNewSharedMessagesIndicator();
        const indicator = document.getElementById('newSharedMessagesIndicator');
        expect(indicator.style.display).toBe('block');
        expect(indicator.disabled).toBe(false);

        indicator.click();

        expect(__testOnly_jumpToLatestUnreadBoundary()).toBe(true);
        expect(boundary.scrollIntoView).toHaveBeenCalled();
        expect(boundary.focus).toHaveBeenCalled();
    });
});

describe('scroll to latest message affordance', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="chatTab">
                <div class="content-wrapper">
                    <div id="scrollableField"></div>
                </div>
            </div>
        `;
    });

    test('shows floating control only when conversation is away from the end', () => {
        const scrollableField = document.getElementById('scrollableField');
        Object.defineProperty(scrollableField, 'clientHeight', { value: 200, configurable: true });
        Object.defineProperty(scrollableField, 'scrollHeight', { value: 600, configurable: true });

        scrollableField.scrollTop = 20;
        __testOnly_updateScrollToEndButtonVisibility(scrollableField);
        const button = __testOnly_ensureScrollToEndButton(scrollableField);
        expect(button).toBeTruthy();
        expect(button.classList.contains('visible')).toBe(true);
        expect(button.getAttribute('aria-hidden')).toBe('false');

        scrollableField.scrollTop = 410;
        __testOnly_updateScrollToEndButtonVisibility(scrollableField);
        expect(button.classList.contains('visible')).toBe(false);
        expect(button.getAttribute('aria-hidden')).toBe('true');
    });

    test('clicking floating control scrolls to the latest message', () => {
        jest.useFakeTimers();

        const scrollableField = document.getElementById('scrollableField');
        Object.defineProperty(scrollableField, 'clientHeight', { value: 180, configurable: true });
        Object.defineProperty(scrollableField, 'scrollHeight', { value: 500, configurable: true });
        scrollableField.scrollTop = 0;
        scrollableField.scrollTo = jest.fn(({ top }) => {
            scrollableField.scrollTop = top;
        });

        const button = __testOnly_ensureScrollToEndButton(scrollableField);
        __testOnly_updateScrollToEndButtonVisibility(scrollableField);
        expect(button.classList.contains('visible')).toBe(true);

        button.click();

        expect(scrollableField.scrollTo).toHaveBeenCalledWith({
            top: 500,
            behavior: 'smooth'
        });

        jest.runOnlyPendingTimers();
        expect(__testOnly_scrollConversationToEnd(scrollableField, { smooth: false })).toBe(true);

        jest.useRealTimers();
    });
});

describe('conversation LLM telemetry clipboard export', () => {
    beforeEach(() => {
        const { getCurrentUserConceptId } = require('../domUtils.js');
        const {
            getSessionScopedNamespace,
            getSessionScopedOrgContext
        } = require('../utils/sessionScopedStorage.js');

        document.body.innerHTML = '';
        __testOnly_clearLlmDebugData();
        __testOnly_setTranscriptTurns([]);
        __testOnly_setActiveChatSession('session-1684', 'Compact telemetry test session');

        getCurrentUserConceptId.mockReset();
        getSessionScopedNamespace.mockReset();
        getSessionScopedOrgContext.mockReset();

        getCurrentUserConceptId.mockReturnValue('#V#michael_witbrock');
        getSessionScopedNamespace.mockReturnValue('#V#michael_witbrock');
        getSessionScopedOrgContext.mockReturnValue({
            concept_id: '#V#university_of_auckland_strong_ai_lab'
        });
    });

    afterEach(() => {
        __testOnly_clearLlmDebugData();
        __testOnly_setTranscriptTurns([]);
        __testOnly_setActiveChatSession(null, null);
        delete global.fetch;
        document.body.innerHTML = '';
    });

    test('builds a compact locator payload with retrieval handles instead of debug blobs', () => {
        const firstTimestampMs = 1743760800000;
        const secondTimestampMs = firstTimestampMs + 60000;

        __testOnly_setTranscriptTurns([
            { sender: 'user', message: 'One' },
            { sender: 'assistant', message: 'Two' },
            { sender: 'assistant', message: 'Three' }
        ]);

        setLlmDebugDataForTurn('assistant-1743760800000', {
            timestamp: firstTimestampMs,
            request_id: 'req-1684-a',
            history_location: {
                session_id: 'session-1684',
                history_index: 4
            },
            model: 'gpt-5'
        });
        setLlmDebugDataForTurn('assistant-1743760860000', {
            timestamp: secondTimestampMs,
            turn_execution_diagnostics: {
                request_id: 'req-1684-b'
            },
            response: {
                content: 'hello'
            }
        });

        const payload = __testOnly_buildConversationLlmTelemetryLocatorPayload();

        expect(payload).toBeTruthy();
        expect(payload.schema_version).toBe('conversation_llm_telemetry_locator.v1');
        expect(payload.session_id).toBe('session-1684');
        expect(payload.session_name).toBe('Compact telemetry test session');
        expect(payload.namespace_context).toEqual({
            namespace: '#V#michael_witbrock',
            user_id: '#V#michael_witbrock',
            org_id: '#V#university_of_auckland_strong_ai_lab'
        });
        expect(payload.metadata).toMatchObject({
            total_turns: 2,
            llm_debug_turn_count: 2,
            transcript_turn_count: 3,
            assistant_transcript_turn_count: 2,
            has_partial_telemetry: false,
            missing_turn_telemetry_count: 0,
            turns_with_history_location_count: 1,
            turns_with_request_id_count: 2,
            turns_with_unavailable_locator_fields_count: 0,
            ordering: 'timestamp_then_turn_id'
        });
        expect(payload.turns).toHaveLength(2);
        expect(payload.turns[0]).toMatchObject({
            sequence: 1,
            turn_id: 'assistant-1743760800000',
            timestamp_utc: new Date(firstTimestampMs).toISOString(),
            history_location: {
                session_id: 'session-1684',
                history_index: 4
            },
            request_id: 'req-1684-a'
        });
        expect(payload.turns[0].mcp_access).toEqual(expect.objectContaining({
            chat_history_get_debug_entry: expect.any(Object),
            turn_execution_get_diagnostics: expect.any(Object)
        }));
        expect(payload.turns[1]).toMatchObject({
            sequence: 2,
            turn_id: 'assistant-1743760860000',
            timestamp_utc: new Date(secondTimestampMs).toISOString(),
            history_location: null,
            request_id: 'req-1684-b'
        });
        expect(payload.turns[0]).not.toHaveProperty('debug_data');
        expect(payload.turns[1]).not.toHaveProperty('debug_data');
    });

    test('counts partial locator coverage against assistant turns rather than the full transcript', () => {
        const timestampMs = 1743760800000;

        __testOnly_setTranscriptTurns([
            { sender: 'user', message: 'One' },
            { sender: 'assistant', message: 'Two' },
            { sender: 'assistant', message: 'Three' }
        ]);

        setLlmDebugDataForTurn('assistant-1743760800000', {
            timestamp: timestampMs,
            request_id: 'req-1684-partial',
            history_location: {
                session_id: 'session-1684',
                history_index: 6
            }
        });

        const payload = __testOnly_buildConversationLlmTelemetryLocatorPayload();

        expect(payload.metadata).toMatchObject({
            transcript_turn_count: 3,
            assistant_transcript_turn_count: 2,
            has_partial_telemetry: true,
            missing_turn_telemetry_count: 1,
            turns_with_unavailable_locator_fields_count: 0
        });
    });

    test('does not fabricate locator timestamps from client turn ids and marks unresolved handles explicitly', () => {
        __testOnly_setTranscriptTurns([
            { sender: 'assistant', message: 'Placeholder only' }
        ]);

        setLlmDebugDataForTurn('history-assistant-1', {
            history_location: {
                session_id: 'session-1684',
                history_index: 11
            }
        });

        const payload = __testOnly_buildConversationLlmTelemetryLocatorPayload();

        expect(payload.metadata).toMatchObject({
            transcript_turn_count: 1,
            assistant_transcript_turn_count: 1,
            has_partial_telemetry: true,
            missing_turn_telemetry_count: 0,
            turns_with_unavailable_locator_fields_count: 1
        });
        expect(payload.turns[0]).toMatchObject({
            sequence: 1,
            turn_id: 'history-assistant-1',
            timestamp_utc: null,
            history_location: {
                session_id: 'session-1684',
                history_index: 11
            },
            request_id: null,
            unavailable_locator_fields: ['timestamp_utc', 'request_id']
        });
    });

    test('wraps file-save export in a conversation info envelope', () => {
        const timestampMs = 1743760800000;
        jest.useFakeTimers().setSystemTime(Date.parse('2026-04-04T19:03:45.963Z'));

        try {
            __testOnly_setActiveChatSession('session-1684', 'Session 1684');
            setLlmDebugDataForTurn('assistant-1743760800000', {
                timestamp: timestampMs,
                request_id: 'req-1684-full',
                history_location: {
                    session_id: 'session-1684',
                    history_index: 7
                },
                model: 'gpt-5',
                response: {
                    content: 'full telemetry'
                }
            });

            const fullPayload = __testOnly_buildConversationLlmTelemetryPayload();
            const exportPayload = __testOnly_buildConversationTelemetryExportPayload();
            const locatorPayload = __testOnly_buildConversationLlmTelemetryLocatorPayload();

            expect(exportPayload.schema_version).toBe('conversation_info_export.v1');
            expect(exportPayload.session_id).toBe('session-1684');
            expect(exportPayload.session_name).toBe('Session 1684');
            expect(exportPayload.conversation_locator).toEqual(locatorPayload);
            expect(exportPayload.detailed_turn_telemetry).toEqual(fullPayload);
            expect(exportPayload.transcript_snapshot).toBeTruthy();
            expect(fullPayload.schema_version).toBe('conversation_llm_telemetry.v1');
            expect(fullPayload.turns[0].debug_data).toBeTruthy();
            expect(fullPayload.turns[0].debug_data.model).toBe('gpt-5');
            expect(locatorPayload.turns[0].debug_data).toBeUndefined();
            expect(locatorPayload.turns[0].request_id).toBe('req-1684-full');
        } finally {
            jest.useRealTimers();
        }
    });

    test('copies an access envelope without hydrating turn-level locator state', async () => {
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });
        global.fetch = jest.fn((url) => {
            const parsed = new URL(url, 'http://localhost');
            if (parsed.pathname === '/von/history/telemetry_locator') {
                return Promise.resolve({
                    ok: false,
                    status: 404,
                    json: async () => ({
                        error: 'not_available'
                    })
                });
            }
            throw new Error(`Unexpected fetch: ${parsed.pathname}`);
        });

        __testOnly_setTranscriptTurns([
            { sender: 'assistant', message: 'Hydrate me' }
        ]);
        __testOnly_setActiveChatSession('session-1684', 'Session 1684');
        setLlmDebugDataForTurn('history-assistant-12', {
            history_location: {
                session_id: 'session-1684',
                history_index: 12
            }
        });

        const copied = await __testOnly_copyConversationInfoToClipboard();
        const copiedPayload = JSON.parse(writeText.mock.calls[0][0]);

        expect(copied).toBe(true);
        expect(global.fetch).toHaveBeenCalledTimes(1);
        expect(copiedPayload.metadata).toMatchObject({
            assistant_transcript_turn_count: 1,
            has_partial_telemetry: true,
            missing_turn_telemetry_count: 0,
            turns_with_unavailable_locator_fields_count: 1,
            authoritative_locator_available: false,
            access_payload_source: 'local_context_summary'
        });
        expect(copiedPayload.turns).toBeUndefined();
    });

    test('copies a conversation telemetry access envelope to the clipboard instead of the full telemetry blob', async () => {
        const timestampMs = 1743760800000;
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });
        global.fetch = jest.fn().mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => ({
                schema_version: 'conversation_llm_telemetry_locator.v1',
                generated_at_utc: '2026-04-04T19:03:45.963Z',
                session_id: 'session-1684',
                session_name: 'Session 1684',
                namespace_context: {
                    namespace: '#V#michael_witbrock',
                    user_id: '#V#michael_witbrock',
                    org_id: '#V#university_of_auckland_strong_ai_lab'
                },
                metadata: {
                    total_turns: 1,
                    llm_debug_turn_count: 1,
                    transcript_turn_count: 0,
                    assistant_transcript_turn_count: 0,
                    has_partial_telemetry: false,
                    missing_turn_telemetry_count: 0,
                    turns_with_history_location_count: 1,
                    turns_with_request_id_count: 1,
                    turns_with_unavailable_locator_fields_count: 0,
                    ordering: 'history_index'
                },
                turns: [
                    {
                        turn_id: 'assistant-1743760800000',
                        request_id: 'req-1684-copy',
                        history_location: {
                            session_id: 'session-1684',
                            history_index: 9
                        }
                    }
                ]
            })
        });

        setLlmDebugDataForTurn('assistant-1743760800000', {
            timestamp: timestampMs,
            request_id: 'req-1684-copy',
            history_location: {
                session_id: 'session-1684',
                history_index: 9
            },
            model: 'gpt-5',
            tool_invocations: [
                { tool_name: 'turn_execution_get_diagnostics' }
            ]
        });

        __testOnly_setActiveChatSession('session-1684', 'Session 1684');

        const copied = await __testOnly_copyConversationInfoToClipboard();
        const copiedPayload = JSON.parse(writeText.mock.calls[0][0]);
        const accessPayload = __testOnly_buildConversationTelemetryAccessPayload({
            locatorPayload: {
                schema_version: 'conversation_llm_telemetry_locator.v1',
                generated_at_utc: '2026-04-04T19:03:45.963Z',
                session_id: 'session-1684',
                session_name: 'Session 1684',
                namespace_context: {
                    namespace: '#V#michael_witbrock',
                    user_id: '#V#michael_witbrock',
                    org_id: '#V#university_of_auckland_strong_ai_lab'
                },
                metadata: {
                    total_turns: 1,
                    llm_debug_turn_count: 1,
                    transcript_turn_count: 0,
                    assistant_transcript_turn_count: 0,
                    has_partial_telemetry: false,
                    missing_turn_telemetry_count: 0,
                    turns_with_history_location_count: 1,
                    turns_with_request_id_count: 1,
                    turns_with_unavailable_locator_fields_count: 0
                }
            }
        });
        const fullPayload = __testOnly_buildConversationTelemetryExportPayload();

        expect(copied).toBe(true);
        expect(writeText).toHaveBeenCalledTimes(1);
        expect(copiedPayload.schema_version).toBe('conversation_telemetry_access.v1');
        expect(copiedPayload.metadata.authoritative_locator_available).toBe(true);
        expect(copiedPayload.metadata.access_payload_source).toBe('authoritative_locator');
        expect(copiedPayload.turns).toBeUndefined();
        expect(copiedPayload.agent_instructions.steps).toHaveLength(3);
        expect(copiedPayload).toEqual(expect.objectContaining({
            session_id: accessPayload.session_id,
            session_name: accessPayload.session_name,
            namespace_context: accessPayload.namespace_context,
            mcp_access: accessPayload.mcp_access,
            agent_instructions: accessPayload.agent_instructions
        }));
        expect(fullPayload.conversation_locator.turns[0].debug_data).toBeUndefined();
        expect(fullPayload.detailed_turn_telemetry.turns[0].debug_data).toBeTruthy();
        expect(fullPayload.detailed_turn_telemetry.turns[0].debug_data.tool_invocations).toBeTruthy();
    });

    test('fails gracefully when no conversation info is available', async () => {
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });

        __testOnly_setActiveChatSession(null, null);
        __testOnly_setTranscriptTurns([]);
        __testOnly_clearLlmDebugData();
        const payload = __testOnly_buildConversationLlmTelemetryLocatorPayload();
        const copied = await __testOnly_copyConversationInfoToClipboard();

        expect(payload).toBeNull();
        expect(copied).toBe(false);
        expect(writeText).not.toHaveBeenCalled();
    });
});
