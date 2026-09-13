const { FOOTER_ITEMS, FOOTER_PREFERENCES_KEY, loadFooterPreferences, mountFooterPreferences, resetFooterPreferences } = require('../../src/frontend/web/von_interface/static/js/utils/footerPreferences.js');
const { formatConversationRuntimeCostFooter } = require('../../src/frontend/web/von_interface/static/js/utils/conversationRuntimeCost.js');

beforeEach(() => { localStorage.clear(); document.body.innerHTML = '<div id="footerVisibilitySettings"></div>'; });

test('footer controls persist choices and restore them after remounting', () => {
    mountFooterPreferences();
    expect(document.querySelectorAll('input')).toHaveLength(Object.keys(FOOTER_ITEMS).length);
    expect(loadFooterPreferences()).toMatchObject({ model: true, user: true, organisation: true, build: false, runtimeCost: false });
    document.querySelector('[data-footer-preference="build"]').click();
    expect(JSON.parse(localStorage.getItem(FOOTER_PREFERENCES_KEY)).build).toBe(true);
    document.getElementById('footerVisibilitySettings').replaceChildren();
    mountFooterPreferences();
    expect(document.querySelector('[data-footer-preference="build"]').checked).toBe(true);
    expect(document.documentElement.getAttribute('data-footer-build')).toBe('true');
});

test('malformed and non-boolean saved preferences use defaults', () => {
    localStorage.setItem(FOOTER_PREFERENCES_KEY, '{');
    expect(loadFooterPreferences().model).toBe(true);
    localStorage.setItem(FOOTER_PREFERENCES_KEY, JSON.stringify({ build: 'false', model: false }));
    expect(loadFooterPreferences()).toMatchObject({ build: false, model: false });
});

test.each([[0.014889, 'US$0.01'], [0.0199, 'US$0.02'], [0.000012, '< US$0.01'], [0, 'US$0.00'], [12.3456, 'US$12.35']])('both footer costs consistently format %s', (amount, expected) => {
    const summary = { estimated_cost: { status: 'estimated', currency: 'USD', amount } };
    const result = formatConversationRuntimeCostFooter({ context: { conversation_session_id: 'fixture' }, baseline: { conversation: summary, since_restart: summary } });
    expect(result.desktopText).toBe(`Est. cost: Chat ${expected}`);
    expect(result.runtimeText).toBe(`Est. ${expected}`);
});

test('unavailable cost is not rendered as a zero', () => {
    const result = formatConversationRuntimeCostFooter({ context: { conversation_session_id: 'fixture' }, baseline: { conversation: {} } });
    expect(result.desktopText).toContain('unavailable');
    expect(result.desktopText).not.toContain('0.00');
});


test('reset restores the default controls and observes later saved changes', () => {
    mountFooterPreferences();
    document.querySelector('[data-footer-preference="build"]').click();
    resetFooterPreferences();
    expect(document.querySelector('[data-footer-preference="build"]').checked).toBe(false);
    localStorage.setItem(FOOTER_PREFERENCES_KEY, JSON.stringify({ build: true }));
    window.dispatchEvent(new StorageEvent('storage', { key: FOOTER_PREFERENCES_KEY }));
    expect(document.querySelector('[data-footer-preference="build"]').checked).toBe(true);
});
