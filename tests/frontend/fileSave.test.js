/** @jest-environment jsdom */

const modulePath = '../../src/frontend/web/von_interface/static/js/utils/fileSave.js';

describe('file save helpers', () => {
    let originalShowSaveFilePicker;
    let originalCreateObjectURL;
    let originalRevokeObjectURL;

    beforeEach(() => {
        jest.resetModules();
        originalShowSaveFilePicker = window.showSaveFilePicker;
        originalCreateObjectURL = window.URL.createObjectURL;
        originalRevokeObjectURL = window.URL.revokeObjectURL;
        window.URL.createObjectURL = jest.fn(() => 'blob:test');
        window.URL.revokeObjectURL = jest.fn();
    });

    afterEach(() => {
        window.showSaveFilePicker = originalShowSaveFilePicker;
        window.URL.createObjectURL = originalCreateObjectURL;
        window.URL.revokeObjectURL = originalRevokeObjectURL;
        jest.restoreAllMocks();
    });

    test('uses showSaveFilePicker when available', async () => {
        const write = jest.fn().mockResolvedValue(undefined);
        const close = jest.fn().mockResolvedValue(undefined);
        const createWritable = jest.fn().mockResolvedValue({ write, close });

        window.showSaveFilePicker = jest.fn().mockResolvedValue({ createWritable });

        const { saveJsonTextViaDialog } = require(modulePath);
        const result = await saveJsonTextViaDialog('{"hello":"world"}', {
            suggestedName: 'conversation_telemetry'
        });

        expect(window.showSaveFilePicker).toHaveBeenCalledWith(expect.objectContaining({
            suggestedName: 'conversation_telemetry.json'
        }));
        expect(createWritable).toHaveBeenCalledTimes(1);
        expect(write).toHaveBeenCalledWith('{"hello":"world"}');
        expect(close).toHaveBeenCalledTimes(1);
        expect(window.URL.createObjectURL).not.toHaveBeenCalled();
        expect(result).toEqual(expect.objectContaining({
            saved: true,
            cancelled: false,
            method: 'file-picker',
            filename: 'conversation_telemetry.json'
        }));
    });

    test('returns cancelled when save picker is dismissed', async () => {
        const abortError = Object.assign(new Error('cancelled'), { name: 'AbortError' });
        window.showSaveFilePicker = jest.fn().mockRejectedValue(abortError);

        const { saveJsonTextViaDialog } = require(modulePath);
        const result = await saveJsonTextViaDialog('{"hello":"world"}');

        expect(window.URL.createObjectURL).not.toHaveBeenCalled();
        expect(result).toEqual(expect.objectContaining({
            saved: false,
            cancelled: true,
            method: 'file-picker'
        }));
    });

    test('falls back to Blob download when save picker is unavailable', async () => {
        window.showSaveFilePicker = undefined;
        const appendSpy = jest.spyOn(document.body, 'appendChild');

        const { saveJsonTextViaDialog } = require(modulePath);
        const result = await saveJsonTextViaDialog('{"hello":"world"}', {
            suggestedName: 'telemetry.json'
        });

        expect(window.URL.createObjectURL).toHaveBeenCalledTimes(1);
        expect(window.URL.revokeObjectURL).toHaveBeenCalledWith('blob:test');
        expect(appendSpy).not.toHaveBeenCalled();
        expect(result).toEqual(expect.objectContaining({
            saved: true,
            cancelled: false,
            method: 'download',
            filename: 'telemetry.json'
        }));
    });
});
