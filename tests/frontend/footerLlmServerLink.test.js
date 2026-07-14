/** @jest-environment jsdom */

const domUtilsPath = '../../src/frontend/web/von_interface/static/js/domUtils.js';

describe('footer model settings button', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = `
            <button class="tab-button" data-tab="settingsTab" type="button">Settings</button>
            <iframe id="settingsFrame"></iframe>
            <div class="footer-container"><p id="modelInfoFooter"></p></div>
        `;
        localStorage.clear();
        sessionStorage.clear();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        localStorage.clear();
        sessionStorage.clear();
    });

    test('renders model footer button, omits redundant LLM segment, and opens model settings focus target', async () => {
        const settingsTabButton = document.querySelector('.tab-button[data-tab="settingsTab"]');
        const settingsFrame = document.getElementById('settingsFrame');
        const frameWindow = settingsFrame.contentWindow;
        const postMessageMock = jest.spyOn(frameWindow, 'postMessage').mockImplementation(() => { });

        const settingsClickSpy = jest.spyOn(settingsTabButton, 'click');
        global.fetch = jest.fn(async (url) => {
            const rawUrl = typeof url === 'string' ? url : (url?.url || String(url));
            const parsed = new URL(rawUrl, 'http://localhost');
            const path = parsed.pathname;
            if (path === '/api/settings/' || path === '/api/settings') {
                return {
                    ok: true,
                    json: async () => ({
                        resolved_llm: { provider: 'ollama', model: 'llama3.1:8b' }
                    })
                };
            }
            if (path === '/api/settings/llm/info') {
                return {
                    ok: true,
                    json: async () => ({
                        provider: 'ollama',
                        model: 'llama3.1:8b',
                        status: 'ready',
                        details: { host: 'http://127.0.0.1:11434' }
                    })
                };
            }
            if (path === '/api/settings/db/info') {
                return { ok: true, json: async () => ({}) };
            }
            if (path === '/von/api/auth/status' || path === '/api/auth/status') {
                return { ok: true, json: async () => ({ authenticated: true, email: 'researcher@example.test' }) };
            }
            return { ok: true, json: async () => ({}) };
        });

        const { setModelInfoFooterText } = require(domUtilsPath);
        await setModelInfoFooterText();

        const modelSegment = Array.from(document.querySelectorAll('.footer-segment'))
            .find((seg) => seg.querySelector('.footer-label-inline')?.textContent?.trim() === 'Model:');
        expect(modelSegment).toBeTruthy();
        const modelButton = modelSegment.querySelector('.concept-footer-button');
        expect(modelButton).toBeTruthy();
        expect(modelButton.textContent.trim()).toBe('llama3.1:8b');
        expect(modelButton.getAttribute('aria-label')).toContain('Model llama3.1:8b');
        expect(modelButton.getAttribute('aria-label')).toContain('Configured status Ready');
        expect(modelButton.getAttribute('aria-label')).toContain('Open language model settings');

        const llmSegment = Array.from(document.querySelectorAll('.footer-segment'))
            .find((seg) => seg.querySelector('.footer-label-inline')?.textContent?.trim() === 'LLM:');
        expect(llmSegment).toBeFalsy();

        modelButton.click();
        expect(settingsClickSpy).toHaveBeenCalledTimes(1);
        expect(postMessageMock).toHaveBeenCalledWith({ type: 'von:focus-model-settings' }, window.location.origin);
    });
});
