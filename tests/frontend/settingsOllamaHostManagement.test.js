/** @jest-environment jsdom */

const settingsPath = '../../src/frontend/web/von_interface/static/js/settings.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
  getJsonDetailed: jest.fn(),
  postJson: jest.fn(),
}));

const { postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
const {
  renderOllamaHostsList,
  saveOllamaHosts,
  setOllamaHostManagementWritable,
} = require(settingsPath);

describe('shared Ollama host management authority', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    document.body.innerHTML = `
      <p id="ollamaHostManagementAccessNote"></p>
      <div id="ollamaHostsList"></div>
      <input id="newOllamaHostUrl">
      <button id="addOllamaHostButton">Add Host</button>
    `;
    setOllamaHostManagementWritable(false);
  });

  test('defaults shared-host writes to read-only and never posts', async () => {
    renderOllamaHostsList(
      [
        { name: 'Local', url: 'http://127.0.0.1:11434', is_local: true },
        { name: 'Remote', url: 'http://ollama.example:11434', is_local: false },
      ],
      'http://127.0.0.1:11434',
    );

    expect(document.getElementById('newOllamaHostUrl').disabled).toBe(true);
    expect(document.getElementById('addOllamaHostButton').disabled).toBe(true);
    expect(document.getElementById('ollamaHostManagementAccessNote').textContent)
      .toContain('Read-only');
    expect(Array.from(document.querySelectorAll('[data-ollama-host-write-control="true"]'))
      .every(button => button.disabled)).toBe(true);

    await expect(saveOllamaHosts([], null)).rejects.toThrow('Admin or owner');
    expect(postJson).not.toHaveBeenCalled();
  });

  test('enables explicit write controls only after admin authority is supplied', async () => {
    renderOllamaHostsList(
      [
        { name: 'Local', url: 'http://127.0.0.1:11434', is_local: true },
        { name: 'Remote', url: 'http://ollama.example:11434', is_local: false },
      ],
      'http://127.0.0.1:11434',
    );
    setOllamaHostManagementWritable(true);

    expect(document.getElementById('newOllamaHostUrl').disabled).toBe(false);
    expect(document.getElementById('addOllamaHostButton').disabled).toBe(false);
    const writeButtons = Array.from(
      document.querySelectorAll('[data-ollama-host-write-control="true"]'),
    );
    expect(writeButtons.filter(button => button.dataset.ollamaHostActive === 'true')[0].disabled)
      .toBe(true);
    expect(writeButtons.some(button => button.disabled === false)).toBe(true);

    postJson.mockResolvedValueOnce({ success: true });
    await saveOllamaHosts([], null);
    expect(postJson).toHaveBeenCalledWith('/api/settings/ollama/hosts', {
      hosts: [],
      active_host: null,
    });
  });
});
