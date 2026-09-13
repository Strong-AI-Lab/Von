/** @jest-environment jsdom */
import { initialiseImagePicker } from '../utils/conversationImages.js';
let upload, status;
beforeEach(() => {
    document.body.innerHTML = '<button id="attachImageButton"></button><input id="attachImageInput" type="file"><button id="pasteImageButton"></button>';
    upload = jest.fn(); status = jest.fn();
    Object.defineProperty(navigator, 'clipboard', {configurable:true, value:undefined});
    initialiseImagePicker(upload, status);
});
test('picker cancellation preserves draft; selection uses shared upload path', () => {
    const input = document.querySelector('input');
    const click = jest.spyOn(input, 'click');
    document.querySelector('#attachImageButton').click();
    expect(click).toHaveBeenCalled();
    input.dispatchEvent(new Event('cancel'));
    expect(upload).not.toHaveBeenCalled();
    expect(status).toHaveBeenCalledWith(expect.stringContaining('unchanged'));
    const file = new File(['image'], 'photo.png', {type:'image/png'});
    Object.defineProperty(input, 'files', {value:[file]});
    input.dispatchEvent(new Event('change'));
    expect(upload).toHaveBeenCalledWith([file]);
    expect(input.value).toBe('');
});
test('missing clipboard API offers the picker', () => {
    document.querySelector('#pasteImageButton').click();
    expect(status).toHaveBeenCalledWith(expect.stringContaining('Use Attach image'), 'error');
});
test.each(['denied', 'empty', 'image'])('clipboard %s', async mode => {
    Object.defineProperty(navigator, 'clipboard', {value:{read:jest.fn(async () => {
        if (mode === 'denied') throw new Error('NotAllowedError');
        return mode === 'empty' ? [] : [{types:['image/png'], getType:async () => new Blob(['image'], {type:'image/png'})}];
    })}});
    document.querySelector('#pasteImageButton').click();
    await new Promise(resolve => setTimeout(resolve, 0));
    if (mode === 'image') expect(upload.mock.calls[0][0][0].type).toBe('image/png');
    else expect(status).toHaveBeenCalledWith(expect.stringContaining('Attach image'), 'error');
});
