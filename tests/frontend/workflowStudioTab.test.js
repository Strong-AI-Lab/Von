jest.mock('../../src/frontend/web/von_interface/static/js/workflowStudioAccess.js', () => ({ canUseWorkflowStudio: jest.fn(() => true) }));
/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/workflowStudioPage.js', () => ({
    initialiseWorkflowStudio: jest.fn(),
}));
jest.mock('../../src/frontend/web/von_interface/static/js/chatTab.js', () => ({
    initializeWorkflowStatusPanel: jest.fn(),
}));

const { loadWorkflowStudioTab } = require('../../src/frontend/web/von_interface/static/js/workflowStudioTab.js');
const { initialiseWorkflowStudio } = require('../../src/frontend/web/von_interface/static/js/workflowStudioPage.js');
const { initializeWorkflowStatusPanel } = require('../../src/frontend/web/von_interface/static/js/chatTab.js');

describe('Workflow Studio tab loading', () => {
    let container;
    beforeEach(() => {
        jest.clearAllMocks();
        require('../../src/frontend/web/von_interface/static/js/workflowStudioAccess.js').canUseWorkflowStudio.mockReturnValue(true);
        document.body.innerHTML = '<section id="workflowStudioTab" data-src="/von/workflow-studio/content"></section>';
        container = document.getElementById('workflowStudioTab');
        global.fetch = jest.fn().mockResolvedValue({ ok: true, text: async () => '<input aria-label="Draft" value="original">' });
    });

    test('waits for activation and preserves edits across repeat or concurrent activation', async () => {
        expect(fetch).not.toHaveBeenCalled();
        await Promise.all([loadWorkflowStudioTab(container), loadWorkflowStudioTab(container)]);
        container.querySelector('input').value = 'unsaved edit';
        await loadWorkflowStudioTab(container);
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(fetch).toHaveBeenCalledWith('/von/workflow-studio/content');
        expect(initialiseWorkflowStudio).toHaveBeenCalledTimes(1);
        expect(initializeWorkflowStatusPanel).toHaveBeenCalledTimes(1);
        expect(container.querySelector('input').value).toBe('unsaved edit');
        expect(container.getAttribute('aria-busy')).toBe('false');
    });

    test('an unsuccessful first load remains retryable', async () => {
        const error = jest.spyOn(console, 'error').mockImplementation(() => {});
        fetch.mockResolvedValueOnce({ ok: false, status: 503 });
        await loadWorkflowStudioTab(container);
        expect(container.textContent).toContain('Retry');
        expect(initialiseWorkflowStudio).not.toHaveBeenCalled();
        await loadWorkflowStudioTab(container);
        expect(container.dataset.initialized).toBe('true');
        expect(initialiseWorkflowStudio).toHaveBeenCalledTimes(1);
        error.mockRestore();
    });
    test('unavailable Studio never requests content', async () => {
        require('../../src/frontend/web/von_interface/static/js/workflowStudioAccess.js').canUseWorkflowStudio.mockReturnValue(false);
        await loadWorkflowStudioTab(container);
        expect(fetch).not.toHaveBeenCalled();
    });
    test('resizing during content load does not initialise hidden Studio', async () => {
        let resolve;
        fetch.mockReturnValueOnce(new Promise(done => { resolve = done; }));
        const loading = loadWorkflowStudioTab(container);
        require('../../src/frontend/web/von_interface/static/js/workflowStudioAccess.js').canUseWorkflowStudio.mockReturnValue(false);
        resolve({ ok: true, text: async () => '<div>Studio</div>' });
        await loading;
        expect(initialiseWorkflowStudio).not.toHaveBeenCalled();
        expect(initializeWorkflowStatusPanel).not.toHaveBeenCalled();
        expect(container.dataset.initialized).toBeUndefined();
    });

});
