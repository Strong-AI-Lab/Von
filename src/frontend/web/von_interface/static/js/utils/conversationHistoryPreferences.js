export const CHAT_HISTORY_RECENT_LIMIT_STORAGE_KEY = 'chatHistoryRecentLimit';
export const CHAT_HISTORY_RECENT_WINDOW_DAYS_STORAGE_KEY = 'chatHistoryRecentWindowDays';

export const DEFAULT_CHAT_HISTORY_RECENT_LIMIT = 20;
export const DEFAULT_CHAT_HISTORY_RECENT_WINDOW_DAYS = 30;

export const MIN_CHAT_HISTORY_RECENT_LIMIT = 1;
export const MAX_CHAT_HISTORY_RECENT_LIMIT = 200;

export const MIN_CHAT_HISTORY_RECENT_WINDOW_DAYS = 1;
export const MAX_CHAT_HISTORY_RECENT_WINDOW_DAYS = 3650;

const DAY_MS = 24 * 60 * 60 * 1000;

function parseFiniteInteger(value) {
    if (value === null || value === undefined) {
        return null;
    }
    if (typeof value === 'string' && !value.trim()) {
        return null;
    }
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) {
        return null;
    }
    return Math.trunc(parsed);
}

export function clampConversationHistoryRecentLimit(value) {
    const parsed = parseFiniteInteger(value);
    if (parsed === null) {
        return DEFAULT_CHAT_HISTORY_RECENT_LIMIT;
    }
    return Math.min(Math.max(parsed, MIN_CHAT_HISTORY_RECENT_LIMIT), MAX_CHAT_HISTORY_RECENT_LIMIT);
}

export function clampConversationHistoryRecentWindowDays(value) {
    const parsed = parseFiniteInteger(value);
    if (parsed === null) {
        return DEFAULT_CHAT_HISTORY_RECENT_WINDOW_DAYS;
    }
    return Math.min(Math.max(parsed, MIN_CHAT_HISTORY_RECENT_WINDOW_DAYS), MAX_CHAT_HISTORY_RECENT_WINDOW_DAYS);
}

export function parseIsoTimestampMs(value) {
    if (typeof value !== 'string' || !value.trim()) {
        return null;
    }
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
}

export function getSessionOccurredAtMs(session) {
    if (!session || typeof session !== 'object') {
        return null;
    }
    return parseIsoTimestampMs(session.last_message_at)
        ?? parseIsoTimestampMs(session.created_at)
        ?? parseIsoTimestampMs(session.shared_accepted_at);
}

export function loadConversationHistorySettings(readStorage = null) {
    const reader = (typeof readStorage === 'function')
        ? readStorage
        : (key) => {
            try {
                if (typeof localStorage === 'undefined') {
                    return null;
                }
                return localStorage.getItem(key);
            } catch (_) {
                return null;
            }
        };

    return {
        recentLimit: clampConversationHistoryRecentLimit(
            reader(CHAT_HISTORY_RECENT_LIMIT_STORAGE_KEY)
        ),
        recentWindowDays: clampConversationHistoryRecentWindowDays(
            reader(CHAT_HISTORY_RECENT_WINDOW_DAYS_STORAGE_KEY)
        )
    };
}

function getAccessTimestampMs(session, accessTimestampBySessionId = {}) {
    const sessionId = (typeof session?.session_id === 'string') ? session.session_id.trim() : '';
    const sessionAccessTs = sessionId ? accessTimestampBySessionId[sessionId] : null;
    return parseIsoTimestampMs(sessionAccessTs)
        ?? parseIsoTimestampMs(session?.last_accessed_at)
        ?? getSessionOccurredAtMs(session)
        ?? 0;
}

export function selectConversationHistorySessions({
    sessions = [],
    accessTimestampBySessionId = {},
    recentLimit = DEFAULT_CHAT_HISTORY_RECENT_LIMIT,
    recentWindowDays = DEFAULT_CHAT_HISTORY_RECENT_WINDOW_DAYS,
    showAll = false,
    nowMs = Date.now()
} = {}) {
    const safeLimit = clampConversationHistoryRecentLimit(recentLimit);
    const safeWindowDays = clampConversationHistoryRecentWindowDays(recentWindowDays);
    const cutoffMs = nowMs - (safeWindowDays * DAY_MS);

    let olderThanWindowCount = 0;
    const sessionsInWindow = sessions.filter((session) => {
        const occurredAtMs = getSessionOccurredAtMs(session);
        if (occurredAtMs !== null && occurredAtMs < cutoffMs) {
            olderThanWindowCount += 1;
            return false;
        }
        return occurredAtMs !== null;
    });

    const sorted = sessionsInWindow.slice().sort((a, b) => (
        getAccessTimestampMs(b, accessTimestampBySessionId)
        - getAccessTimestampMs(a, accessTimestampBySessionId)
    ));

    const limited = showAll ? sorted : sorted.slice(0, safeLimit);
    const hiddenByLimitCount = Math.max(0, sorted.length - safeLimit);

    return {
        sessionsToRender: limited,
        totalMatchingCount: sorted.length,
        hiddenByLimitCount,
        olderThanWindowCount,
        recentLimit: safeLimit,
        recentWindowDays: safeWindowDays
    };
}
