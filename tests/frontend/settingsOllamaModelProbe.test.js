/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    postJson: jest.fn(),
    getWindowSessionId: jest.fn(() => 'test-window-session'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session',
}));

describe('settingsPage Ollama model probe', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        document.body.innerHTML = `
            <select id="globalModelSelect">
                <option value="http://localhost:11434:llama3.1:8b" data-host-url="http://localhost:11434" data-model-name="llama3.1:8b" selected>localhost - llama3.1:8b</option>
            </select>
            <button id="refreshOllamaModelsButton" type="button"></button>
            <div id="ollamaModelStatusMessage" class="status-message"></div>
            <div id="settingsStatusMessage" class="status-message"></div>
        `;
    });

    afterEach(() => {
        jest.restoreAllMocks();
        localStorage.clear();
    });

    test('posts selected Ollama model and host to the probe endpoint', async () => {
        const { postJson } = await import('../../src/frontend/web/von_interface/static/js/apiService.js');
        postJson.mockResolvedValue({
            usable: true,
            model: 'llama3.1:8b',
            reason: 'The selected Ollama model completed a live probe successfully.',
        });
        const {
            __testOnly_testSelectedOllamaModel,
        } = await import('../../src/frontend/web/von_interface/static/js/settingsPage.js');

        const result = await __testOnly_testSelectedOllamaModel();

        expect(postJson).toHaveBeenCalledWith('/api/settings/ollama/test_model', {
            model: 'llama3.1:8b',
            host_url: 'http://localhost:11434',
        });
        expect(result).toEqual({
            usable: true,
            model: 'llama3.1:8b',
            reason: 'The selected Ollama model completed a live probe successfully.',
            failure_kind: null,
        });
        const status = document.getElementById('ollamaModelStatusMessage');
        expect(status.textContent).toContain('llama3.1:8b is usable.');
        expect(status.classList.contains('success')).toBe(true);
        expect(JSON.parse(localStorage.getItem('von:localModelPreference'))).toMatchObject({
            activeSource: 'ollama',
            ollamaSelection: {
                model: 'llama3.1:8b',
                host: 'http://localhost:11434',
            },
        });
    });

    test('reports that no Ollama model is selected without posting', async () => {
        document.body.innerHTML = `
            <select id="globalModelSelect"><option value="" selected>Select an Ollama Model</option></select>
            <div id="ollamaModelStatusMessage" class="status-message"></div>
        `;
        const { postJson } = await import('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_testSelectedOllamaModel,
        } = await import('../../src/frontend/web/von_interface/static/js/settingsPage.js');

        const result = await __testOnly_testSelectedOllamaModel();

        expect(postJson).not.toHaveBeenCalled();
        expect(result).toEqual({
            usable: false,
            model: null,
            reason: 'No Ollama model selected.',
        });
        expect(document.getElementById('ollamaModelStatusMessage').textContent).toContain(
            'Select an Ollama model, then test it before relying on it.',
        );
    });

    test('uses a stored model name when the stored dropdown value is absent', async () => {
        const {
            __testOnly_resolveOllamaDropdownSelectionValue,
        } = await import('../../src/frontend/web/von_interface/static/js/settingsPage.js');

        expect(__testOnly_resolveOllamaDropdownSelectionValue({
            ollamaSelection: {
                model: 'llama3.1:8b',
                host: 'http://localhost:11434',
            },
        })).toBe('llama3.1:8b');
    });

    test('status text distinguishes an active Ollama model from a list-only selection', async () => {
        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            openaiModel: 'gpt-5.4-mini',
            ollamaSelection: {
                model: 'llama3.1:8b',
                host: 'http://localhost:11434',
            },
        }));
        const {
            __testOnly_updateOllamaModelStatusMessage,
        } = await import('../../src/frontend/web/von_interface/static/js/settingsPage.js');

        __testOnly_updateOllamaModelStatusMessage();

        expect(document.getElementById('ollamaModelStatusMessage').textContent).toContain(
            'llama3.1:8b is selected as the active local Ollama model',
        );
    });

    test('refreshes the Ollama dropdown through the cache-bypass endpoint', async () => {
        const { getJsonDetailed } = await import('../../src/frontend/web/von_interface/static/js/apiService.js');
        getJsonDetailed.mockResolvedValue({
            data: {
                success: true,
                models: [
                    {
                        host_url: 'http://localhost:11434',
                        name: 'llama3.1:8b',
                        display_name: 'localhost - llama3.1:8b',
                    },
                ],
            },
        });
        const {
            __testOnly_refreshOllamaModelDropdown,
        } = await import('../../src/frontend/web/von_interface/static/js/settingsPage.js');

        const result = await __testOnly_refreshOllamaModelDropdown();

        expect(getJsonDetailed).toHaveBeenCalledWith('/api/settings/ollama/models?nocache=true');
        expect(result).toMatchObject({ success: true });
        expect(document.getElementById('globalModelSelect').value).toBe(
            'http://localhost:11434:llama3.1:8b',
        );
        expect(document.getElementById('refreshOllamaModelsButton').disabled).toBe(false);
        expect(document.getElementById('refreshOllamaModelsButton').classList.contains('loading')).toBe(false);
    });
});
