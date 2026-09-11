// Bounded diagnostics only. Client identity never supplies actor authority.
let hints = null;
let hintsRequest = null;
let clientId = null;
export function newSpeechId(root = globalThis) {
    return root.crypto?.randomUUID?.() || 'speech-' + Date.now() + '-' + Math.random().toString(36).slice(2);
}
export function refreshClientHints(root = globalThis) {
    if (!hintsRequest && root.navigator?.userAgentData?.getHighEntropyValues) {
        hintsRequest = root.navigator.userAgentData.getHighEntropyValues(['model', 'platformVersion', 'fullVersionList'])
            .then(value => { hints = value; }).catch(() => {});
    }
    return hintsRequest || Promise.resolve();
}
// Resolve optional hints while the page opens, before the first speech attempt.
void refreshClientHints();
export function getClientContext(root = globalThis) {
    if (!clientId) clientId = newSpeechId(root);
    const nav = root.navigator || {};
    const ua = hints || nav.userAgentData || {};
    void refreshClientHints(root);
    const script = root.document?.querySelector?.('script[type="module"][src*="main.js"]');
    return {
        client_id: clientId,
        user_agent: nav.userAgent || null,
        captured_at: new Date().toISOString(),
        brands: (ua.fullVersionList || ua.brands || []).slice(0, 5),
        platform: ua.platform || nav.platform || null,
        platform_version: ua.platformVersion || null,
        device_model: ua.model || null,
        mobile: typeof ua.mobile === 'boolean' ? ua.mobile : null,
        touch_points: nav.maxTouchPoints || 0,
        viewport_width: root.innerWidth || null, viewport_height: root.innerHeight || null,
        orientation: root.screen?.orientation?.type || null,
        display_mode: root.matchMedia?.('(display-mode: standalone)').matches ? 'standalone' : 'browser',
        secure_context: root.isSecureContext === true,
        visibility: root.document?.visibilityState || null,
        frontend_asset: script?.getAttribute('src') || null,
        language: nav.language || null
    };
}
export function createSpeechReporter({ fetchImpl = (...args) => fetch(...args), getContext = () => ({}), root = globalThis } = {}) {
    const attemptId = newSpeechId(root);
    const snapshot = getClientContext(root);
    const started = root.performance?.now?.() ?? Date.now();
    let sequence = 0;
    let lastState = null;
    let lastRevisionAt = -Infinity;
    return {
        attemptId,
        event(name, values = {}) {
            const context = getContext();
            const now = root.performance?.now?.() ?? Date.now();
            if (name === 'revision' && !values.final && now - lastRevisionAt < 500) return;
            if (name === 'revision') lastRevisionAt = now;
            // Report structural lifecycle/revisions, never recognition text/audio.
            const event = { name, sequence: sequence++, elapsed_ms: Math.round(now - started), ...values };
            if (name === 'state' && values.state === lastState) return;
            if (name === 'state') lastState = values.state;
            void fetchImpl('/api/speech/attempts/' + encodeURIComponent(attemptId), {
                method: 'POST', credentials: 'same-origin', keepalive: true,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ client_context: snapshot, conversation_id: context.sessionId || null, event })
            }).catch(() => {});
        }
    };
}
