/** @jest-environment jsdom */

const suppressTooltipsPath = '../../src/frontend/web/von_interface/static/js/suppressTooltips.js';

async function flushMutationObserver() {
    await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('dynamic native-tooltip suppression', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<span id="serverBuildInfo"></span>';
        delete window.__VON_TOOLTIP_SUPPRESS_ACTIVE__;
        delete window.__VON_RESTORE_TITLES;
        require(suppressTooltipsPath);
    });

    afterEach(() => {
        if (typeof window.__VON_RESTORE_TITLES === 'function') {
            window.__VON_RESTORE_TITLES();
        }
        delete window.__VON_TOOLTIP_SUPPRESS_ACTIVE__;
        delete window.__VON_RESTORE_TITLES;
    });

    test('keeps an auto-promoted build label in sync with title updates', async () => {
        const buildInfo = document.getElementById('serverBuildInfo');

        buildInfo.title = 'Build old | Commit 11111111';
        await flushMutationObserver();
        expect(buildInfo.getAttribute('aria-label')).toBe('Build old | Commit 11111111');
        expect(buildInfo.getAttribute('data-original-title')).toBe('Build old | Commit 11111111');
        expect(buildInfo.hasAttribute('title')).toBe(false);

        buildInfo.title = 'Build new | Commit 22222222';
        await flushMutationObserver();
        expect(buildInfo.getAttribute('aria-label')).toBe('Build new | Commit 22222222');
        expect(buildInfo.getAttribute('data-original-title')).toBe('Build new | Commit 22222222');
        expect(buildInfo.hasAttribute('title')).toBe(false);
    });

    test('does not replace an independently authored accessible label', async () => {
        const buildInfo = document.getElementById('serverBuildInfo');
        buildInfo.setAttribute('aria-label', 'Current application build');

        buildInfo.title = 'Build old | Commit 11111111';
        await flushMutationObserver();
        buildInfo.title = 'Build new | Commit 22222222';
        await flushMutationObserver();

        expect(buildInfo.getAttribute('aria-label')).toBe('Current application build');
        expect(buildInfo.getAttribute('data-original-title')).toBe('Build new | Commit 22222222');
    });
});
