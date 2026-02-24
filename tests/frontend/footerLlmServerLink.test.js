/** @jest-environment jsdom */

const domUtilsPath = '../../src/frontend/web/von_interface/static/js/domUtils.js';

describe('footer LLM settings link button', () => {
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

    test('renders LLM footer button and opens model settings focus target', async () => {
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
                        active_llm: { provider: 'ollama', model: 'llama3.1:8b' }
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
                return { ok: true, json: async () => ({ authenticated: true, email: 'm.witbrock@auckland.ac.nz' }) };
            }
            return { ok: true, json: async () => ({}) };
        });

        const { setModelInfoFooterText } = require(domUtilsPath);
        await setModelInfoFooterText();

        const llmSegment = Array.from(document.querySelectorAll('.footer-segment'))
            .find((seg) => seg.querySelector('.footer-label-inline')?.textContent?.trim() === 'LLM:');
        expect(llmSegment).toBeTruthy();
        const llmButton = llmSegment.querySelector('.concept-footer-button');
        expect(llmButton).toBeTruthy();
        expect(llmButton.textContent.trim()).toBe('Server');
        expect(llmButton.getAttribute('aria-label')).toBe('Open language model settings');

        llmButton.click();
        expect(settingsClickSpy).toHaveBeenCalledTimes(1);
        expect(postMessageMock).toHaveBeenCalledWith({ type: 'von:focus-model-settings' }, window.location.origin);
    });
});
