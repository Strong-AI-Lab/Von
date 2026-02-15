import {
    CHAT_HISTORY_RECENT_LIMIT_STORAGE_KEY,
    CHAT_HISTORY_RECENT_WINDOW_DAYS_STORAGE_KEY,
    DEFAULT_CHAT_HISTORY_RECENT_LIMIT,
    DEFAULT_CHAT_HISTORY_RECENT_WINDOW_DAYS,
    clampConversationHistoryRecentLimit,
    clampConversationHistoryRecentWindowDays,
    loadConversationHistorySettings,
    selectConversationHistorySessions
} from '../utils/conversationHistoryPreferences.js';

describe('conversationHistoryPreferences', () => {
    test('loads default settings when local storage values are missing', () => {
        const settings = loadConversationHistorySettings(() => null);
        expect(settings).toEqual({
            recentLimit: DEFAULT_CHAT_HISTORY_RECENT_LIMIT,
            recentWindowDays: DEFAULT_CHAT_HISTORY_RECENT_WINDOW_DAYS
        });
    });

    test('clamps invalid conversation history settings', () => {
        expect(clampConversationHistoryRecentLimit(-5)).toBe(1);
        expect(clampConversationHistoryRecentLimit(9999)).toBe(200);
        expect(clampConversationHistoryRecentWindowDays(0)).toBe(1);
        expect(clampConversationHistoryRecentWindowDays(99999)).toBe(3650);
    });

    test('selects sessions by recency window and access order with limit', () => {
        const nowMs = Date.parse('2026-02-16T00:00:00Z');
        const sessions = [
            { session_id: 'a', last_message_at: '2026-02-15T10:00:00Z' },
            { session_id: 'b', last_message_at: '2026-02-14T10:00:00Z' },
            { session_id: 'c', last_message_at: '2026-02-06T10:00:00Z' },
            { session_id: 'd', last_message_at: '2025-12-15T10:00:00Z' }
        ];
        const accessBySession = {
            b: '2026-02-15T23:00:00Z',
            a: '2026-02-15T22:00:00Z',
            c: '2026-02-15T21:00:00Z'
        };

        const limited = selectConversationHistorySessions({
            sessions,
            accessTimestampBySessionId: accessBySession,
            recentLimit: 2,
            recentWindowDays: 30,
            showAll: false,
            nowMs
        });
        expect(limited.totalMatchingCount).toBe(3);
        expect(limited.hiddenByLimitCount).toBe(1);
        expect(limited.sessionsToRender.map((s) => s.session_id)).toEqual(['b', 'a']);

        const expanded = selectConversationHistorySessions({
            sessions,
            accessTimestampBySessionId: accessBySession,
            recentLimit: 2,
            recentWindowDays: 30,
            showAll: true,
            nowMs
        });
        expect(expanded.sessionsToRender.map((s) => s.session_id)).toEqual(['b', 'a', 'c']);
    });

    test('reads persisted settings using configured storage keys', () => {
        const data = {
            [CHAT_HISTORY_RECENT_LIMIT_STORAGE_KEY]: '15',
            [CHAT_HISTORY_RECENT_WINDOW_DAYS_STORAGE_KEY]: '7'
        };
        const settings = loadConversationHistorySettings((key) => data[key] ?? null);
        expect(settings).toEqual({
            recentLimit: 15,
            recentWindowDays: 7
        });
    });
});
