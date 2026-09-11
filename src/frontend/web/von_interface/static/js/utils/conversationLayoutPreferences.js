// Browser navigation preferences deliberately survive sign-in and organisation changes.
export const CONVERSATION_LAYOUT_KEY = 'von:chatSessionTabsLayout';
export const CONVERSATION_TRAY_WIDTH_KEY = 'von:conversationTrayWidth';
export const CONVERSATION_TRAY_COLLAPSED_KEY = 'von:conversationTrayCollapsed';
export const CONVERSATION_TRAY_HOVER_KEY = 'von:conversationTrayExpandOnHover';
export const CONVERSATION_LAYOUT_KEYS = [
    CONVERSATION_LAYOUT_KEY, CONVERSATION_TRAY_WIDTH_KEY,
    CONVERSATION_TRAY_COLLAPSED_KEY, CONVERSATION_TRAY_HOVER_KEY
];
export const CONVERSATION_NARROW_QUERY = '(max-width: 800px), (hover: none) and (pointer: coarse)';
export const CONVERSATION_TRAY_MIN_WIDTH = 220;
export const CONVERSATION_TRAY_MAX_WIDTH = 400;

export function normaliseConversationLayout(value) {
    return value === 'horizontal' ? 'horizontal' : 'vertical';
}

export function clampConversationTrayWidth(value) {
    const number = value == null || value === '' ? NaN : Number(value);
    return Number.isFinite(number)
        ? Math.max(CONVERSATION_TRAY_MIN_WIDTH, Math.min(CONVERSATION_TRAY_MAX_WIDTH, Math.round(number)))
        : 260;
}

const sessionFallback = new Map();

export function loadConversationLayoutPreferences(reader = (key) =>
    sessionFallback.has(key) ? sessionFallback.get(key) : localStorage.getItem(key)) {
    const read = (key) => { try { return reader(key); } catch (_) { return null; } };
    return {
        layout: normaliseConversationLayout(read(CONVERSATION_LAYOUT_KEY)),
        width: clampConversationTrayWidth(read(CONVERSATION_TRAY_WIDTH_KEY)),
        collapsed: read(CONVERSATION_TRAY_COLLAPSED_KEY) === 'true',
        expandOnHover: read(CONVERSATION_TRAY_HOVER_KEY) !== 'false'
    };
}

export function saveConversationLayoutPreference(key, value) {
    try {
        localStorage.setItem(key, String(value));
        sessionFallback.delete(key);
    } catch (_) {
        sessionFallback.set(key, String(value));
    }
    window.dispatchEvent(new CustomEvent('von-preferences-changed', { detail: { key, value } }));
}
