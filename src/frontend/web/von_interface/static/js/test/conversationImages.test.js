/** @jest-environment jsdom */
import { uploadConversationImage, renderImageComposer, takeImages, imagesBlocked, imageItems, renderImageAttachments } from '../utils/conversationImages.js';
import { simpleMarkdownToHtml } from '../markdownUtils.js';

test('multiple uploads retain previews and removal excludes only the removed image', async () => {
    let next = 0;
    global.fetch = jest.fn(async () => ({ok:true, json:async () => ({image_attachment:{concept_id:`#V#image-${++next}`, filename:'fixture.png'}})}));
    const panel = document.createElement('div');
    const render = () => renderImageComposer(panel, 'multi', render);
    await uploadConversationImage(new File(['bytes'], 'one.png', {type:'image/png'}), 'multi', {}, render);
    await uploadConversationImage(new File(['bytes'], 'two.png', {type:'image/png'}), 'multi', {}, render);
    expect(panel.querySelectorAll('img')).toHaveLength(2);
    panel.querySelector('button').click();
    expect(panel.querySelectorAll('img')).toHaveLength(1);
    expect(takeImages('multi')).toEqual(['#V#image-2']);
    expect(imageItems('different-session')).toEqual([]);
});

test('failed preparation remains visible and prevents text-only submission until removed', async () => {
    global.fetch = jest.fn(async () => ({ok:false, json:async () => ({message:'Use a still PNG, JPEG or WebP image.'})}));
    const panel = document.createElement('div');
    const render = () => renderImageComposer(panel, 'failed', render);
    await uploadConversationImage(new File(['bad'], 'bad.svg', {type:'image/svg+xml'}), 'failed', {}, render);
    expect(panel.querySelector('[role=alert]').textContent).toMatch(/still PNG/);
    expect(imagesBlocked('failed')).toBe(true);
    expect(() => takeImages('failed')).toThrow(/preparation failed/);
    panel.querySelector('button[aria-label^=Remove]').click();
    expect(imagesBlocked('failed')).toBe(false);
});

test('history images use authenticated original routes, with filenames rendered as text', () => {
    const parent = document.createElement('div');
    renderImageAttachments(parent, [{concept_id:'#V#image', filename:'<script>bad</script>',url:'https://untrusted.example/'}]);
    expect(parent.querySelector('a').getAttribute('href')).toBe('/von/api/images/%23V%23image/original');
    expect(parent.querySelector('script')).toBeNull();
    expect(parent.querySelector('img').alt).toBe('<script>bad</script>');
});

test('archive images provide a canonical source link even when the answer omits one', () => {
    const parent = document.createElement('div');
    renderImageAttachments(parent, [{concept_id:'#V#image', provenance:{kind:'otter_archive', artifact_id:'source-id', source_url:'https://untrusted.example/'}}]);
    const citation = parent.querySelectorAll('a')[1];
    expect(citation.getAttribute('href')).toBe('/von/api/otter-archive/artifacts/source-id');
    expect(citation.textContent).toMatch(/archive source/);
});

test('fallback Markdown resolves exact archive citations without allowing arbitrary schemes', () => {
    const artifact = 'a'.repeat(64);
    const parent = document.createElement('div');
    parent.innerHTML = simpleMarkdownToHtml(`[Source](otter-archive://artifact/${artifact}) [Bad](otter-archive://other/private)`);
    expect(parent.querySelector('a').getAttribute('href')).toBe(`/von/api/otter-archive/artifacts/${artifact}`);
    expect(parent.querySelectorAll('a')).toHaveLength(1);
});

test('removal cancels in-flight upload and ignores a late response', async () => {
    let finish;
    global.fetch = jest.fn(() => new Promise(resolve => { finish = resolve; }));
    const panel = document.createElement('div');
    const render = () => renderImageComposer(panel, 'cancel', render);
    const upload = uploadConversationImage(new File(['bytes'], 'cancel.png', {type:'image/png'}), 'cancel', {}, render);
    panel.querySelector('button').click();
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    finish({ok:true, json:async () => ({image_attachment:{concept_id:'#V#late'}})});
    expect(await upload).toBe(false);
    expect(takeImages('cancel')).toEqual([]);
});

test('network failure can retry without duplicating or crossing conversation bindings', async () => {
    global.fetch = jest.fn().mockRejectedValueOnce(new Error('Network unavailable')).mockResolvedValueOnce({ok:true, json:async () => ({image_attachment:{concept_id:'#V#retried'}})});
    const panel = document.createElement('div');
    const render = () => renderImageComposer(panel, 'retry', render);
    await uploadConversationImage(new File(['bytes'], 'retry.png', {type:'image/png'}), 'retry', {}, render);
    expect(imagesBlocked('retry')).toBe(true);
    await imageItems('retry')[0].retry();
    expect(imageItems('retry')).toHaveLength(1);
    expect(takeImages('elsewhere')).toEqual([]);
    expect(takeImages('retry')).toEqual(['#V#retried']);
});

test.each([
    ['large.png', 'image/png', 8 * 1024 * 1024 + 1, /8 MiB/],
    ['phone.heic', 'image/heic', 1, /Convert HEIC/]
])('invalid selection %s reports actionable error before upload', async (name, type, size, message) => {
    global.fetch = jest.fn();
    await uploadConversationImage(new File([new Uint8Array(size)], name, {type}), name, {}, () => {});
    expect(fetch).not.toHaveBeenCalled();
    expect(imageItems(name)[0].error).toMatch(message);
});
