// Shared name normalization and duplicate detection utilities
// Emits a CustomEvent 'concept-name-normalized' with details { original, normalized, changed, notes }
// Optionally posts a lightweight metric if window.postJsonMetric is defined or fetch available

export function normalizeConceptName(input) {
    let name = input || '';
    const original = name;
    const notes = [];

    if (/ {2,}/.test(name)) { name = name.replace(/ {2,}/g, ' '); notes.push('collapsed spaces'); }

    // Date conversions
    name = name.replace(/\b(\d{4})\/(\d{1,2})\/(\d{1,2})\b/g, (m, y, mo, d) => { notes.push('converted date slashes'); return `${y}-${mo.padStart(2, '0')}-${d.padStart(2, '0')}`; });
    name = name.replace(/\b(\d{1,2})\/(\d{1,2})\/(\d{4})\b/g, (m, d, mo, y) => { notes.push('converted date slashes'); return `${y}-${mo.padStart(2, '0')}-${d.padStart(2, '0')}`; });

    if (/[\\/]|\.\./.test(name)) { name = name.replace(/\\/g, '_').replace(/\//g, '_').replace(/\.\./g, '_'); notes.push('replaced illegal chars'); }
    if (/__+/.test(name)) { name = name.replace(/__+/g, '_'); notes.push('collapsed underscores'); }
    const trimmed = name.replace(/^_+|_+$/g, '');
    if (trimmed !== name) { name = trimmed; notes.push('trimmed edge underscores'); }

    const changed = name !== original;
    const detail = { original, normalized: name, changed, notes: notes.join(', ') || 'normalized' };
    try { document.dispatchEvent(new CustomEvent('concept-name-normalized', { detail })); } catch (_) { }
    return detail;
}

export function checkDuplicateName(normalizedName, selectorList, attr = 'data-name') {
    try {
        const entries = document.querySelectorAll(selectorList);
        const existing = Array.from(entries).map(e => (e.getAttribute(attr) || e.textContent || '').trim().toLowerCase()).filter(Boolean);
        return existing.includes((normalizedName || '').toLowerCase());
    } catch (_) { return false; }
}

export async function recordNameNormalizationMetric(kind) {
    // POST to backend metric endpoint (non-blocking best effort)
    try {
        await fetch('/vontology/api/vontology/record_name_normalization', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind }) });
    } catch (_) { /* ignore */ }
}
