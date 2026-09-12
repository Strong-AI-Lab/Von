import {
    uploadConversationImage, imageItems, imagesBlocked, renderImageComposer,
    takeImages, restoreImages,
} from '../../src/frontend/web/von_interface/static/js/utils/conversationImages.js';

function response(id) {
    return { ok: true, json: async () => ({ image_attachment: { concept_id: id, filename: 'diagram.png' } }) };
}

test('a failed upload retains the file and retries in the same draft without duplicates', async () => {
    const file = new File(['image'], 'diagram.png', { type: 'image/png' });
    const parent = document.createElement('div');
    const changed = () => renderImageComposer(parent, 'retry', changed);
    global.fetch = jest.fn()
        .mockRejectedValueOnce(new Error('Network unavailable'))
        .mockResolvedValueOnce(response('#V#retried'));
    expect(await uploadConversationImage(file, 'retry', {}, changed)).toBe(false);
    expect(imagesBlocked('retry')).toBe(true);
    expect(parent.textContent).toContain('image/png · 5 bytes');
    expect(parent.textContent).toContain('Network unavailable');
    const retry = parent.querySelector('[aria-label="Retry upload of diagram.png"]');
    retry.click();
    // A stale double-click cannot launch a second in-flight retry.
    retry.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1][1].body.get('file').name).toBe(file.name);
    expect(imagesBlocked('retry')).toBe(false);
    expect(imageItems('retry')).toHaveLength(1);
    expect(takeImages('retry')).toEqual(['#V#retried']);
    restoreImages('retry', ['#V#retried']);
    restoreImages('retry', ['#V#retried']);
    expect(imageItems('retry')).toHaveLength(1);
    takeImages('retry');
});

test('removing an in-flight image cannot resurrect it or move it to another conversation', async () => {
    let finish;
    global.fetch = jest.fn(() => new Promise(resolve => { finish = resolve; }));
    const parent = document.createElement('div');
    const changed = () => renderImageComposer(parent, 'removed', changed);
    const uploading = uploadConversationImage(new File(['x'], 'remove.png'), 'removed', {}, changed);
    parent.querySelector('[aria-label="Remove remove.png"]').click();
    finish(response('#V#removed'));
    expect(await uploading).toBe(false);
    expect(imageItems('removed')).toEqual([]);
    expect(imageItems('another-conversation')).toEqual([]);
    restoreImages('removed', ['#V#removed']);
    expect(imageItems('removed')).toEqual([]);
});

test('plain text paste stays native and a send acknowledgement preserves newer files', async () => {
    const { bindAttachmentComposer, removeSentAttachments } = await import('../../src/frontend/web/von_interface/static/js/utils/conversationImages.js');
    const root = document.createElement('div');
    const input = document.createElement('textarea'); root.appendChild(input);
    bindAttachmentComposer(root, input, () => 'acknowledgement', () => ({}), () => {});
    const paste = new Event('paste', { bubbles:true, cancelable:true });
    Object.defineProperty(paste, 'clipboardData', {value:{files:[],getData:()=> 'ordinary text'}});
    input.dispatchEvent(paste);
    expect(paste.defaultPrevented).toBe(false);
    global.fetch = jest.fn().mockResolvedValueOnce(response('#V#sent')).mockResolvedValueOnce(response('#V#newer'));
    await uploadConversationImage(new File(['a'], 'one.txt', {type:'text/plain'}), 'acknowledgement', {}, () => {});
    await uploadConversationImage(new File(['b'], 'two.txt', {type:'text/plain'}), 'acknowledgement', {}, () => {});
    removeSentAttachments('acknowledgement', ['#V#sent']);
    expect(imageItems('acknowledgement').map(i=>i.descriptor.concept_id)).toEqual(['#V#newer']);
});
