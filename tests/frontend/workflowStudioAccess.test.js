/** @jest-environment jsdom */
const { setWorkflowStudioAccess, canUseWorkflowStudio, STUDIO_MOBILE_QUERY } = require('../../src/frontend/web/von_interface/static/js/workflowStudioAccess.js');
const { activateTab, setupTabNavigation } = require('../../src/frontend/web/von_interface/static/js/tabNavigation.js');
jest.mock('../../src/frontend/web/von_interface/static/js/workflowStudioTab.js', () => ({ loadWorkflowStudioTab: jest.fn() }));

describe('Studio admission and responsive navigation', () => {
    let media;
    beforeEach(() => {
        media = { matches: false, addEventListener: jest.fn() };
        window.matchMedia = jest.fn(() => media);
        setWorkflowStudioAccess({ authenticated: true, workflow_studio_access: true });
        document.body.innerHTML = `
            <button class="tab-button" data-tab="chatTab">Conversations</button>
            <button class="tab-button" data-tab="workflowStudioTab" hidden>Studio</button>
            <button class="tab-button" data-tab="settingsTab">Settings</button>
            <section class="tab-content" id="chatTab"><textarea id="draft">Keep this draft</textarea></section>
            <section class="tab-content" id="workflowStudioTab"></section>
            <section class="tab-content" id="settingsTab"></section>`;
    });
    test.each([
        [false, false], [false, true], [true, true]
    ])('denies hidden clicks and saved selection: admin=%s mobile=%s', (admin, mobile) => {
        setWorkflowStudioAccess({ authenticated: true, workflow_studio_access: admin });
        media.matches = mobile;
        setupTabNavigation();
        localStorage.setItem('role_in_org', 'admin');
        activateTab('workflowStudioTab');
        document.querySelector('[data-tab="workflowStudioTab"]').click();
        expect(document.body.dataset.activeTab).toBe('chatTab');
        expect(location.hash).toBe('#chatTab');
        expect(document.querySelector('[data-tab="workflowStudioTab"]').hidden).toBe(true);
        expect(canUseWorkflowStudio()).toBe(false);
    });
    test('desktop Admin opens Studio; resize preserves drafts and stops active Studio', () => {
        setupTabNavigation();
        activateTab('workflowStudioTab');
        expect(document.body.dataset.activeTab).toBe('workflowStudioTab');
        expect(document.querySelector('[data-tab="workflowStudioTab"]').hidden).toBe(false);
        const changed = jest.fn();
        document.addEventListener('von:tab-activated', changed);
        media.matches = true;
        media.addEventListener.mock.calls[0][1]();
        expect(document.body.dataset.activeTab).toBe('chatTab');
        expect(document.getElementById('draft').value).toBe('Keep this draft');
        expect(changed).toHaveBeenCalled();
        expect(window.matchMedia).toHaveBeenCalledWith(STUDIO_MOBILE_QUERY);
        media.matches = false;
        media.addEventListener.mock.calls[0][1]();
        expect(document.body.dataset.activeTab).toBe('chatTab');
        activateTab('settingsTab');
        expect(document.body.dataset.activeTab).toBe('settingsTab');
        document.removeEventListener('von:tab-activated', changed);
    });
    test.each(['authStatusChanged', 'von:studio-access-changed'])('loss of capability closes Studio via %s', eventName => {
        setupTabNavigation();
        activateTab('workflowStudioTab');
        document.dispatchEvent(new CustomEvent(eventName, { detail: { authenticated: false } }));
        expect(canUseWorkflowStudio()).toBe(false);
        expect(document.body.dataset.activeTab).toBe('chatTab');
    });
});
