/** @jest-environment jsdom */
import { createTurnModelPicker } from '../../src/frontend/web/von_interface/static/js/components/turnModelPicker.js';

describe('temporary turn model picker', () => {
    let picker, select, status, clearButton;
    beforeEach(() => {
        document.body.innerHTML = '<select><option value="">Default</option></select><p></p><button></button>';
        select = document.querySelector('select');
        status = document.querySelector('p');
        clearButton = document.querySelector('button');
    });
    async function setup(loadModels = async provider => provider === 'openai' ? ['gpt-6-astra', 'gpt-5.6-luna'] : []) {
        picker = createTurnModelPicker({ select, status, clearButton, loadModels });
        await picker.load();
    }
    function choose(model) {
        select.value = JSON.stringify({ model, model_provider: 'openai' });
        select.dispatchEvent(new Event('change'));
    }
    test.each(['gpt-6-astra', 'gpt-5.6-luna'])('consumes %s for exactly one turn without writing preferences', async model => {
        localStorage.setItem('default-model', 'unchanged');
        const write = jest.spyOn(Storage.prototype, 'setItem');
        await setup();
        choose(model);
        expect(status.hidden).toBe(false);
        expect(status.textContent).toContain(`Next turn only: ${model}`);
        const submitted = picker.take();
        expect(submitted).toEqual({ model, model_provider: 'openai', model_parameters: {} });
        expect(picker.take()).toBeNull();
        expect(status.hidden).toBe(true);
        expect(select.value).toBe('');
        expect(write).not.toHaveBeenCalled();
        write.mockRestore();
    });
    test('cancel does not submit an override', async () => {
        await setup(); choose('gpt-6-astra'); clearButton.click();
        expect(picker.take()).toBeNull();
    });
    test('catalogue failure leaves other providers usable and permits retry', async () => {
        let unavailable = true;
        const loadModels = jest.fn(async provider => {
            if (provider === 'gemini' && unavailable) throw new Error('offline');
            return provider === 'openai' ? ['gpt-6-astra'] : [];
        });
        await setup(loadModels);
        expect(select.title).toContain('gemini');
        choose('gpt-6-astra');
        expect(picker.peek().model).toBe('gpt-6-astra');
        unavailable = false;
        await picker.load();
        expect(select.options.length).toBe(2);
        expect(select.title).toBe('');
    });
});

describe('temporary reasoning control', () => {
    let picker, select, reasoningSelect, resolveCapabilities;
    beforeEach(async () => {
        document.body.innerHTML = '<select id="model"><option value="">Default</option></select><select id="reasoning"></select><p></p><button></button>';
        select = document.getElementById('model');
        reasoningSelect = document.getElementById('reasoning');
        picker = createTurnModelPicker({ select, reasoningSelect,
            status: document.querySelector('p'), clearButton: document.querySelector('button'),
            loadModels: async provider => provider === 'openai' ? ['gpt-6-astra', 'other'] : [],
            loadCapabilities: () => new Promise(resolve => { resolveCapabilities = resolve; })
        });
        await picker.load();
    });
    function choose(model = 'gpt-6-astra') {
        select.value = JSON.stringify({ model, model_provider: 'openai' });
        select.dispatchEvent(new Event('change'));
    }
    async function capability(value = {}) {
        resolveCapabilities({ parameters: { reasoning_effort: { supported: true, allowed_values: ['low', 'high'], ...value } } });
        await Promise.resolve();
    }
    test('passes selected effort once and resets on send', async () => {
        choose(); await capability();
        reasoningSelect.value = 'high'; reasoningSelect.dispatchEvent(new Event('change'));
        expect(picker.take().model_parameters).toEqual({ reasoning_effort: 'high' });
        expect(picker.take()).toBeNull();
        expect(reasoningSelect.disabled).toBe(true);
    });
    test('changing model discards effort and ignores stale capability reads', async () => {
        choose(); const stale = resolveCapabilities;
        choose('other'); await capability({ supported: false });
        stale({ parameters: { reasoning_effort: { supported: true, allowed_values: ['high'] } } });
        await Promise.resolve();
        expect(reasoningSelect.disabled).toBe(true);
        expect(picker.take().model_parameters).toEqual({});
    });
    test('fixed registry value is visible, read-only and propagated', async () => {
        choose(); await capability({ fixed_value: 'high', read_only: true });
        expect(reasoningSelect.disabled).toBe(true);
        expect(reasoningSelect.value).toBe('high');
        expect(picker.take().model_parameters).toEqual({ reasoning_effort: 'high' });
    });
    test('actor/settings invalidation clears choice and stale asynchronous metadata', async () => {
        choose(); picker.invalidate(); await capability();
        expect(picker.peek()).toBeNull();
        expect(select.options.length).toBe(1);
        expect(reasoningSelect.disabled).toBe(true);
        await picker.load();
        expect(select.options.length).toBe(3);
    });
});
