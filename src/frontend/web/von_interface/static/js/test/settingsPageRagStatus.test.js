import {
    __testOnly_formatRagSummaryForSettings,
    __testOnly_getPreferredRagNamespace
} from '../settingsPage.js';

describe('settingsPage RAG status summary', () => {
    beforeEach(() => {
        localStorage.clear();
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
});

