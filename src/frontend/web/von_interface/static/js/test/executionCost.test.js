import { executionCostPresentation, renderExecutionCost, updateExecutionCost } from '../components/executionCost.js';

const preferences = { currency: 'USD', display_above: '0.01', alert_above: '0.10' };
const cost = amount => ({ status: 'estimated', currency: 'USD', amount_decimal: amount });

test.each([
    ['0', null], ['0.01', null], ['0.010000000000000001', false],
    ['0.10', false], ['0.100000000000000001', true], ['1.1e-1', true]
])('exact boundary %s', (amount, high) => {
    const result = executionCostPresentation(cost(amount), preferences);
    if (high === null) expect(result).toBeNull();
    else expect(result.high).toBe(high);
});

test('loading, missing, partial and other currency costs stay blank; legacy numeric estimates work', () => {
    for (const value of [null, {}, { ...cost('0.2'), status: 'partial' }, { ...cost('0.2'), currency: 'NZD' }]) {
        expect(executionCostPresentation(value, preferences)).toBeNull();
    }
    expect(executionCostPresentation(cost('0.2'), null)).toBeNull();
    expect(executionCostPresentation({ status: 'estimated', currency: 'USD', amount: 0.11 }, preferences).high).toBe(true);
});

test('delayed finalisation uses the same reserved slot and signals high cost visibly and accessibly', () => {
    const slot = document.createElement('span');
    renderExecutionCost(slot, null, preferences);
    expect(slot.textContent).toBe('');
    expect(slot.className).toBe('thinking-execution-cost');
    renderExecutionCost(slot, cost('0.100001'), preferences);
    expect(slot.textContent).toContain('Estimated US$0.100001 · High cost');
    expect(slot.getAttribute('aria-label')).toContain('High cost');
    expect(slot.classList.contains('thinking-execution-cost-high')).toBe(true);
    renderExecutionCost(slot, cost('0.1'), preferences);
    expect(slot.textContent).not.toContain('High cost');
    expect(slot.classList.contains('thinking-execution-cost-high')).toBe(false);
});

test('resolved settings update mounted historical slots and ignore a stale scope response', async () => {
    const pending = [];
    global.fetch = jest.fn(() => new Promise(resolve => pending.push(resolve)));
    document.body.innerHTML = '<span id="cost"></span>';
    const slot = document.getElementById('cost');
    updateExecutionCost(slot, cost('0.11'), { user: 'old' }, {});
    updateExecutionCost(slot, cost('0.11'), { user: 'new' }, {});
    pending[1]({ ok: true, json: async () => preferences });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(slot.textContent).toContain('High cost');
    pending[0]({ ok: true, json: async () => ({ ...preferences, display_above: '1' }) });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(slot.textContent).toContain('High cost');
    delete global.fetch;
});
