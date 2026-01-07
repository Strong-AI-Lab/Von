// Client capability reporting (browser-only)
//
// JVNAUTOSCI-954: Safely report bounded, non-authoritative client capability hints
// (primarily for speech synthesis / audio debugging) to the backend.
//
// Keep this module safe to import in test environments (no top-level browser-only refs).

import { getSpeechSynthesisVoices, isSpeechRecognitionSupported, isTextToSpeechSupported } from './speech.js';

const DEFAULT_REPORT_COOLDOWN_MS = 30_000;
const DEFAULT_DEBOUNCE_MS = 400;

function getRoot() {
    return typeof globalThis !== 'undefined' ? globalThis : null;
}

function safeIsoNow() {
    try {
        return new Date().toISOString();
    } catch (_) {
        return null;
    }
}

function safeLocalStorageGet(key) {
    try {
        return getRoot()?.localStorage?.getItem?.(key);
    } catch (_) {
        return null;
    }
}

function parseNumber(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
}

function clampNumber(value, min, max) {
    const n = parseNumber(value);
    if (n == null) {
        return null;
    }
    return Math.min(max, Math.max(min, n));
}

function summariseVoicesSample(voices, maxItems = 8) {
    const list = Array.isArray(voices) ? voices : [];
    const out = [];

    for (const voice of list.slice(0, maxItems)) {
        if (!voice) {
            continue;
        }

        const name = (typeof voice.name === 'string' && voice.name.trim()) ? voice.name.trim() : null;
        const lang = (typeof voice.lang === 'string' && voice.lang.trim()) ? voice.lang.trim() : null;
        const localService = !!voice.localService;

        out.push({ name, lang, localService });
    }

    return out;
}

function findSelectedVoiceName(voices) {
    const uri = String(safeLocalStorageGet('chatTtsVoiceUri') || '').trim();
    if (!uri) {
        return null;
    }

    const list = Array.isArray(voices) ? voices : [];
    const match = list.find((v) => v && String(v.voiceURI || '') === uri);
    if (!match) {
        return null;
    }

    const name = (typeof match.name === 'string' && match.name.trim()) ? match.name.trim() : null;
    return name;
}

function findDefaultVoiceLang(voices) {
    const list = Array.isArray(voices) ? voices : [];
    const def = list.find((v) => v && v.default);
    const lang = (def && typeof def.lang === 'string' && def.lang.trim()) ? def.lang.trim() : null;
    return lang;
}

function getMaxSpeakingSecondsSetting() {
    const raw = safeLocalStorageGet('chatTtsMaxSpeakingSeconds');
    return clampNumber(raw, 10, 600);
}

export function __testOnly_buildClientCapabilitiesPayload() {
    const root = getRoot();
    const documentEl = root?.document || null;

    const ttsSupported = isTextToSpeechSupported();
    const voices = ttsSupported ? getSpeechSynthesisVoices() : [];

    const voiceName = findSelectedVoiceName(voices);

    return {
        client_reported_timestamp: safeIsoNow(),
        speech_synthesis: {
            supported: ttsSupported,
            voices_count: Array.isArray(voices) ? voices.length : 0,
            voices_sample: summariseVoicesSample(voices, 8),
            default_voice_lang: findDefaultVoiceLang(voices),
            last_error: null,
            settings: {
                rate: clampNumber(safeLocalStorageGet('chatTtsRate'), 0.5, 2),
                pitch: clampNumber(safeLocalStorageGet('chatTtsPitch'), 0, 2),
                volume: clampNumber(safeLocalStorageGet('chatTtsVolume'), 0, 1),
                voice_name: voiceName,
                max_speaking_seconds: getMaxSpeakingSecondsSetting()
            }
        },
        audio_output: {
            document_muted: null,
            autoplay_policy_hint: null,
            user_activation: (typeof root?.navigator?.userActivation?.hasBeenActive === 'boolean')
                ? root.navigator.userActivation.hasBeenActive
                : null
        },
        other: {
            speech_recognition_supported: isSpeechRecognitionSupported(),
            visibility_state: (typeof documentEl?.visibilityState === 'string') ? documentEl.visibilityState : null
        }
    };
}

async function postCapabilities(payload) {
    const root = getRoot();
    if (!root || typeof root.fetch !== 'function') {
        return { ok: false, skipped: true, error: 'fetch_unavailable' };
    }

    try {
        const res = await root.fetch('/api/client_capabilities', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!res || !res.ok) {
            return { ok: false, status: res?.status ?? null };
        }

        return { ok: true };
    } catch (err) {
        return { ok: false, error: err && err.message ? String(err.message) : 'request_failed' };
    }
}

export function startClientCapabilitiesReporting(options = {}) {
    const root = getRoot();
    const cooldownMs = Number.isFinite(options.cooldownMs) ? Math.max(0, options.cooldownMs) : DEFAULT_REPORT_COOLDOWN_MS;
    const debounceMs = Number.isFinite(options.debounceMs) ? Math.max(0, options.debounceMs) : DEFAULT_DEBOUNCE_MS;

    if (!root) {
        return { stop: () => { } };
    }

    const state = {
        stopped: false,
        lastReportAtMs: 0,
        debounceTimerId: null,
        boundVoicesHandler: null
    };

    const scheduleReport = (reason) => {
        if (state.stopped) {
            return;
        }

        const now = Date.now();
        if (cooldownMs > 0 && state.lastReportAtMs && now - state.lastReportAtMs < cooldownMs) {
            return;
        }

        if (state.debounceTimerId) {
            try { root.clearTimeout?.(state.debounceTimerId); } catch (_) { }
        }

        state.debounceTimerId = root.setTimeout?.(async () => {
            state.debounceTimerId = null;
            if (state.stopped) {
                return;
            }

            const payload = __testOnly_buildClientCapabilitiesPayload();
            // Store the send timestamp even if POST fails, to avoid tight retry loops.
            state.lastReportAtMs = Date.now();

            const resp = await postCapabilities(payload);
            if (!resp.ok && options?.debug) {
                // eslint-disable-next-line no-console
                console.debug('[clientCapabilitiesReporter] report failed', { reason, resp });
            }
        }, debounceMs);
    };

    // Initial report.
    scheduleReport('initial');

    // Re-report when speech synthesis voices become available.
    try {
        if (root.speechSynthesis) {
            const handler = () => scheduleReport('voices_changed');
            state.boundVoicesHandler = handler;
            root.speechSynthesis.addEventListener?.('voiceschanged', handler);

            // Some browsers use the onvoiceschanged property.
            if (typeof root.speechSynthesis.onvoiceschanged !== 'function') {
                root.speechSynthesis.onvoiceschanged = handler;
            }
        }
    } catch (_) {
        // Ignore.
    }

    // Re-report when tab becomes visible (user might grant permissions / enable audio).
    try {
        const doc = root.document;
        if (doc && typeof doc.addEventListener === 'function') {
            doc.addEventListener('visibilitychange', () => {
                if (doc.visibilityState === 'visible') {
                    scheduleReport('visibilitychange');
                }
            });
        }
    } catch (_) {
        // Ignore.
    }

    return {
        stop: () => {
            state.stopped = true;
            try {
                if (state.debounceTimerId) {
                    root.clearTimeout?.(state.debounceTimerId);
                }
            } catch (_) {
                // Ignore.
            }

            try {
                if (root?.speechSynthesis && state.boundVoicesHandler) {
                    root.speechSynthesis.removeEventListener?.('voiceschanged', state.boundVoicesHandler);
                }
            } catch (_) {
                // Ignore.
            }
        }
    };
}
