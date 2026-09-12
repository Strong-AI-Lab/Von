// Decimal strings are compared before formatting. Never round to cents first.
function decimal(value) {
    if (typeof value !== 'string' && typeof value !== 'number') return null;
    const match = String(value).match(/^(\d{1,30})(?:\.(\d{1,30}))?(?:[eE]([+-]?\d{1,3}))?$/);
    if (!match) return null;
    const scale = (match[2] || '').length - Number(match[3] || 0);
    if (Math.abs(scale) > 100) return null;
    return { units: BigInt(match[1] + (match[2] || '')), scale };
}

function above(a, b) {
    const scale = Math.max(a.scale, b.scale);
    return a.units * (10n ** BigInt(scale - a.scale)) > b.units * (10n ** BigInt(scale - b.scale));
}

function amountText(amount) {
    // Preserve sub-cent differences so an alert never appears to equal its threshold.
    let digits = amount.units.toString();
    if (amount.scale <= 0) return `${digits}${'0'.repeat(-amount.scale)}.00`;
    digits = digits.padStart(amount.scale + 1, '0');
    const whole = digits.slice(0, -amount.scale);
    const fraction = digits.slice(-amount.scale).replace(/0+$/, '').padEnd(2, '0');
    return `${whole}.${fraction}`;
}

export function executionCostPresentation(cost, preferences) {
    if (!preferences || preferences.currency !== 'USD' || cost?.currency !== 'USD'
        || cost.status !== 'estimated') return null;
    const amount = decimal(cost.amount_decimal ?? cost.amount);
    const display = decimal(preferences.display_above);
    const alert = decimal(preferences.alert_above);
    if (!amount || !display || !alert || !above(amount, display)) return null;
    const high = above(amount, alert);
    return { high, text: `Estimated US$${amountText(amount)}${high ? ' · High cost' : ''}` };
}

export function renderExecutionCost(element, cost, preferences) {
    element.className = 'thinking-execution-cost';
    const presentation = executionCostPresentation(cost, preferences);
    element.textContent = presentation ? ` · ${presentation.text}` : '';
    element.title = presentation?.text || '';
    if (presentation) element.setAttribute('aria-label', presentation.text);
    else element.removeAttribute('aria-label');
    element.classList.toggle('thinking-execution-cost-high', presentation?.high === true);
    // The slot stays allocated during loading, missing telemetry and finalisation.
    element.setAttribute('aria-live', 'polite');
    element.setAttribute('aria-atomic', 'true');
}

let scope = null;
let preferences = null;
let pending = null;
let fetchedAt = 0;
let generation = 0;

export function updateExecutionCost(element, cost, context, headers) {
    const signature = JSON.stringify(context);
    if (signature !== scope) {
        generation += 1;
        scope = signature;
        preferences = null;
        pending = null;
        fetchedAt = 0;
        document.querySelectorAll('[data-execution-cost]').forEach(slot => renderExecutionCost(slot, null, null));
    }
    element.dataset.executionCost = JSON.stringify(cost || null);
    element.dataset.executionCostScope = signature;
    renderExecutionCost(element, cost, preferences);
    if (pending || Date.now() - fetchedAt < 60000 || typeof fetch !== 'function') return;
    fetchedAt = Date.now();
    const requestGeneration = generation;
    pending = fetch('/api/settings/execution_cost_display', { headers, cache: 'no-store' })
        .then(response => response.ok ? response.json() : null)
        .then(value => {
            if (generation !== requestGeneration) return;
            preferences = value;
            document.querySelectorAll('[data-execution-cost]').forEach(slot => {
                if (slot.dataset.executionCostScope === signature) {
                    renderExecutionCost(slot, JSON.parse(slot.dataset.executionCost), preferences);
                }
            });
        })
        .catch(() => { /* Unknown preferences do not invent an effective threshold. */ })
        .finally(() => { if (generation === requestGeneration) pending = null; });
}
