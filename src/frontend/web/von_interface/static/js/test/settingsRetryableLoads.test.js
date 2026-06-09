jest.mock('../apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    postJson: jest.fn(),
}));

import {
    loadAndRenderOllamaHosts,
    populateModelDropdown,
    populateOpenAIModelDropdown,
    populatePeopleDropdown,
} from '../settings.js';
import { getJsonDetailed } from '../apiService.js';

function flushMicrotasks() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('settings retryable load states', () => {
    let consoleErrorSpy;

    beforeEach(() => {
        document.body.innerHTML = '';
        jest.clearAllMocks();
        consoleErrorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        consoleErrorSpy.mockRestore();
    });

    test('people dropdown shows retryable error state and reloads on focus', async () => {
        document.body.innerHTML = '<select id="currentUserSelect"></select>';

        getJsonDetailed
            .mockRejectedValueOnce({
                message: 'HTTP 503',
                status: 503,
                payload: {
                    error: 'People are temporarily unavailable. Please retry shortly.',
                    retryable: true,
                    retry_after_seconds: 0,
                },
            })
            .mockResolvedValueOnce({
                data: {
                    people: [
                        {
                            id: '682de9200a04490dc7afee09',
                            concept_id: '#V#michael_witbrock',
                            name: 'Michael Witbrock',
                            system_tags: [],
                        },
                    ],
                    total_count: 1,
                },
            });

        await populatePeopleDropdown('currentUserSelect');

        const select = document.getElementById('currentUserSelect');
        expect(select.options[0].textContent).toContain('Click to retry');

        select.dispatchEvent(new Event('focus'));
        await flushMicrotasks();
        await flushMicrotasks();

        expect(getJsonDetailed).toHaveBeenCalledTimes(2);
        expect(select.options[1].textContent).toBe('Michael Witbrock');
    });

    test('openai model dropdown distinguishes load failure from a true empty list', async () => {
        document.body.innerHTML = '<select id="openaiModelSelect"></select>';

        getJsonDetailed.mockRejectedValueOnce({
            message: 'HTTP 503',
            status: 503,
            payload: {
                error: 'OpenAI models are temporarily unavailable.',
                retryable: true,
                retry_after_seconds: 0,
            },
        });

        await populateOpenAIModelDropdown('openaiModelSelect');

        const select = document.getElementById('openaiModelSelect');
        expect(select.options[0].textContent).toContain('temporarily unavailable');
        expect(select.options[0].textContent).not.toContain('No OpenAI models available');
    });

    test('ollama hosts panel shows retry guidance instead of pretending nothing is configured', async () => {
        document.body.innerHTML = '<div id="ollamaHostsList"></div>';

        getJsonDetailed.mockRejectedValueOnce({
            message: 'HTTP 503',
            status: 503,
            payload: {
                error: 'Ollama hosts are temporarily unavailable.',
                retryable: true,
                retry_after_seconds: 0,
            },
        });

        await loadAndRenderOllamaHosts();

        const hostsList = document.getElementById('ollamaHostsList');
        expect(hostsList.textContent).toContain('temporarily unavailable');
        expect(hostsList.textContent).not.toContain('No Ollama hosts configured');
        expect(hostsList.querySelector('button')?.textContent).toBe('Retry host load');
    });

    test('manual Ollama model refresh bypasses the settings route cache', async () => {
        document.body.innerHTML = '<select id="globalModelSelect"></select>';
        getJsonDetailed.mockResolvedValueOnce({
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

        await populateModelDropdown(
            'globalModelSelect',
            'http://localhost:11434:llama3.1:8b',
            { bypassCache: true },
        );

        expect(getJsonDetailed).toHaveBeenCalledWith('/api/settings/ollama/models?nocache=true');
        expect(document.getElementById('globalModelSelect').value).toBe(
            'http://localhost:11434:llama3.1:8b',
        );
    });
});
