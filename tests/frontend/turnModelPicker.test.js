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
