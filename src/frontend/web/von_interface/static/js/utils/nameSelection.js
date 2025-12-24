const DEFAULT_LANGUAGE = 'en-NZ';

export function getPreferredLanguage() {
    try {
        const stored = localStorage.getItem('von_preferred_language');
        if (typeof stored === 'string' && stored.trim()) {
            return stored.trim();
        }
    } catch (_) {
        // ignore
    }

    try {
        const docLang = document?.documentElement?.lang;
        if (typeof docLang === 'string' && docLang.trim()) {
            return docLang.trim();
        }
    } catch (_) {
        // ignore
    }

    return DEFAULT_LANGUAGE;
}

function _normaliseNameEntry(entry) {
    if (!entry || typeof entry !== 'object') {
        return null;
    }

    // Support both:
    // - raw_doc.names: {name, language, type}
    // - dynamicTabs-like: {text, language, abbrev, name, type}
    const language = (entry.language ?? entry.lang ?? '').toString().trim();
    const type = (entry.type ?? entry.name_type ?? '').toString().trim().toUpperCase();

    const candidates = [];
    const pushCandidate = (value) => {
        const text = (value ?? '').toString().trim();
        if (!text) return;
        candidates.push({ text, language, type });
    };

    pushCandidate(entry.abbrev);
    pushCandidate(entry.text);
    pushCandidate(entry.name);

    if (!candidates.length) {
        return null;
    }
    return candidates;
}

function _typeScore(type) {
    // Match existing tab logic: prefer NL, then ABBR, then CODE.
    if (type === 'NL') return 0;
    if (type === 'ABBR') return 1;
    if (type === 'CODE') return 2;
    return 3;
}

function _languageScore(language, preferredLanguage) {
    const lang = (preferredLanguage || DEFAULT_LANGUAGE).toLowerCase();
    const base = lang.split('-')[0];
    const cand = (language || '').toLowerCase();
    if (!cand) return 2;
    if (cand === lang) return 0;
    if (cand === base || cand.startsWith(base + '-')) return 1;
    return 2;
}

/**
 * Choose the best display label from a names array.
 *
 * Behaviour matches the dynamic tab label heuristic:
 * - prefer exact preferred language, then base-language matches
 * - prefer NL over ABBR over CODE
 * - prefer shorter strings
 */
export function selectBestNameForContext(names, preferredLanguage = getPreferredLanguage()) {
    if (!Array.isArray(names) || names.length === 0) {
        return null;
    }

    const expanded = [];
    for (const entry of names) {
        const candidates = _normaliseNameEntry(entry);
        if (!candidates) continue;
        for (const c of candidates) {
            expanded.push(c);
        }
    }

    if (!expanded.length) {
        return null;
    }

    // De-dupe by text, keeping the best-scoring metadata.
    const bestByText = new Map();
    for (const c of expanded) {
        const key = c.text;
        const prev = bestByText.get(key);
        if (!prev) {
            bestByText.set(key, c);
            continue;
        }

        const prevScore = [_languageScore(prev.language, preferredLanguage), _typeScore(prev.type), prev.text.length];
        const nextScore = [_languageScore(c.language, preferredLanguage), _typeScore(c.type), c.text.length];
        if (
            nextScore[0] < prevScore[0] ||
            (nextScore[0] === prevScore[0] && (nextScore[1] < prevScore[1] || (nextScore[1] === prevScore[1] && nextScore[2] < prevScore[2])))
        ) {
            bestByText.set(key, c);
        }
    }

    const unique = Array.from(bestByText.values());
    unique.sort((a, b) => {
        const aLang = _languageScore(a.language, preferredLanguage);
        const bLang = _languageScore(b.language, preferredLanguage);
        if (aLang !== bLang) return aLang - bLang;

        const aType = _typeScore(a.type);
        const bType = _typeScore(b.type);
        if (aType !== bType) return aType - bType;

        return a.text.length - b.text.length || a.text.localeCompare(b.text);
    });

    return unique[0]?.text || null;
}

export { DEFAULT_LANGUAGE };

