/** @jest-environment jsdom */
import { modelSettingsRequest, renderModelInventory, TRANSCRIPTION_MODEL_KEY } from '../../src/frontend/web/von_interface/static/js/modelInventory.js';

beforeEach(() => {
    localStorage.clear();
    document.body.innerHTML = '<div id="additionalModelInventory"></div><select id="settingsTranscriptionModelSelect"></select>';
});

const audio = { provider: 'openai', model: 'future-transcriber', available: true, audio_transcription: true };
const chat = { provider: 'openrouter', model: 'vendor/chat', available: true };
const local = { provider: 'ollama', model: 'local-model', host: 'http://localhost:11434', available: true };
const inventories = [{ models: [audio, chat, local] }];

test('all providers, hosts, roles and enablement are visible; only transcription metadata enables selection', () => {
    const allow = jest.fn();
    renderModelInventory({ inventories, enabled: [audio, local], roles: [{ entry: local, label: 'Browser chat' }], allow });
    const rows = [...document.querySelectorAll('.settings-model-pool-entry')];
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain('No role assigned');
    expect(rows[1].textContent).toContain('openrouter: vendor/chat');
    expect(rows[2].textContent).toContain('ollama: local-model (http://localhost:11434)');
    expect(rows[2].textContent).toContain('Browser chat');
    rows[1].querySelector('button').click();
    expect(allow).toHaveBeenCalledWith(chat);
    const select = document.getElementById('settingsTranscriptionModelSelect');
    expect([...select.options].map(o => o.value)).toEqual(['', audio.model]);
    select.value = audio.model;
    select.dispatchEvent(new Event('change'));
    expect(localStorage.getItem(TRANSCRIPTION_MODEL_KEY)).toBe(audio.model);
    expect(document.getElementById('additionalModelInventory').textContent).toContain('Recorded transcription preference');
});

test('stale, disabled and unallowed selections stay visible but cannot be selected', () => {
    localStorage.setItem(TRANSCRIPTION_MODEL_KEY, 'stale-chat');
    renderModelInventory({ inventories, enabled: [], roles: [], allow: jest.fn() });
    const select = document.getElementById('settingsTranscriptionModelSelect');
    expect(select.value).toBe('stale-chat');
    expect(select.selectedOptions[0].disabled).toBe(true);
    expect(select.options[1].disabled).toBe(true);
    renderModelInventory({ inventories: [{ models: [{ ...audio, available: false }] }], enabled: [audio], roles: [], allow: jest.fn() });
    expect(select.options[1].disabled).toBe(true);
    expect(document.querySelector('button').disabled).toBe(true);
});

test('organisation save target does not grant transcription to a user override', () => {
    renderModelInventory({ inventories, enabled: [audio], effectiveEnabled: [local], roles: [], allow: jest.fn() });
    expect(document.querySelector('button').disabled).toBe(true);
    expect(document.getElementById('settingsTranscriptionModelSelect').options[1].disabled).toBe(true);
});

test('unresponsive requests terminate visibly, ignore late results, and permit retry', async () => {
    jest.useFakeTimers();
    let finish;
    global.fetch = jest.fn(() => new Promise(resolve => { finish = resolve; }));
    const request = modelSettingsRequest('/api/settings/', {}, 60000);
    const check = expect(request).rejects.toThrow('outcome is unknown');
    await jest.advanceTimersByTimeAsync(60000);
    await check;
    expect(global.fetch.mock.calls[0][1].signal.aborted).toBe(true);
    finish({ ok: true, json: async () => ({ stale: true }) });
    global.fetch.mockResolvedValue({ ok: true, json: async () => ({ saved: true }) });
    expect(await modelSettingsRequest('/api/settings/')).toEqual({ saved: true });
    jest.useRealTimers();
});

test('provider-specific errors survive the transport', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 400, json: async () => ({ message: 'OpenRouter: check model access.' }) });
    await expect(modelSettingsRequest('/api/settings/')).rejects.toThrow('OpenRouter: check model access.');
});
