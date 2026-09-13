// Durable image descriptors only; originals remain behind authenticated routes.
const pending = new Map();
const known = new Map();
export function imageItems(sessionId) { return pending.get(sessionId) || []; }
export function imagesBlocked(sessionId) { return imageItems(sessionId).some(x => !x.descriptor); }
export function renderImageAttachments(parent, images) {
    if (!parent || !Array.isArray(images) || !images.length) return;
    const gallery = document.createElement('div');
    gallery.className = 'conversation-image-gallery';
    gallery.style.cssText = 'display:flex;flex-wrap:wrap;gap:12px;margin:8px 0';
    for (const item of images) {
        if (!item?.concept_id) continue;
        const url = `/von/api/images/${encodeURIComponent(item.concept_id)}/original`;
        const link = document.createElement('a');
        link.href = url; link.target = '_blank'; link.rel = 'noopener';
        const img = document.createElement('img');
        img.src = url; img.alt = item.filename || 'Attached image';
        img.style.cssText = 'max-width:min(100%,640px);max-height:420px;object-fit:contain';
        img.addEventListener('error', () => { img.alt = 'Image unavailable or access denied. Open the original to inspect the error.'; });
        link.appendChild(img); gallery.appendChild(link);
        const source = item.provenance;
        if (source?.kind === 'otter_archive' && source.artifact_id) {
            const citation = document.createElement('a');
            citation.href = `/von/api/otter-archive/artifacts/${encodeURIComponent(source.artifact_id)}`;
            citation.target = '_blank'; citation.rel = 'noopener';
            citation.textContent = 'View archive source and extraction provenance';
            gallery.appendChild(citation);
        }
    }
    parent.appendChild(gallery);
}
export function renderImageComposer(parent, sessionId, changed) {
    if (!parent) return;
    parent.replaceChildren();
    for (const item of imageItems(sessionId)) {
        const card = document.createElement('div');
        card.className = 'conversation-image-draft';
        if (item.descriptor) renderImageAttachments(card, [item.descriptor]);
        const label = document.createElement('span');
        label.textContent = item.error || (item.descriptor ? item.name : `Uploading ${item.name}…`);
        label.setAttribute('role', item.error ? 'alert' : 'status');
        card.appendChild(label);
        const remove = document.createElement('button');
        remove.type = 'button'; remove.textContent = 'Remove'; remove.setAttribute('aria-label', `Remove ${item.name}`);
        remove.onclick = () => { item.controller?.abort(); pending.set(sessionId, imageItems(sessionId).filter(x => x !== item)); changed(); };
        if (item.error && item.retry) {
            const retry = document.createElement('button');
            retry.type = 'button'; retry.textContent = 'Retry upload';
            retry.onclick = item.retry; card.appendChild(retry);
        }
        card.appendChild(remove); parent.appendChild(card);
    }
}
export async function uploadConversationImage(file, sessionId, headers, changed) {
    const item = {name: file.name};
    pending.set(sessionId, [...imageItems(sessionId), item]); changed();
    const upload = async () => {
        item.error = null;
        item.retry = null;
        item.controller = new AbortController();
        changed();
        try {
            if (imageItems(sessionId).length > 8) throw new Error('At most eight images per message. Remove an image before sending.');
            if (!file.size || file.size > 8 * 1024 * 1024) throw new Error('Images must be nonempty and at most 8 MiB.');
            if (file.type && !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
                throw new Error('Use a still PNG, JPEG or WebP image. Convert HEIC or other formats before attaching.');
            }
            const form = new FormData(); form.append('file', file, file.name || 'image.png');
            const response = await fetch('/von/api/images/upload', {method:'POST', headers, body:form, signal:item.controller.signal});
            const result = await response.json().catch(() => ({}));
            if (!response.ok || !result.image_attachment) throw new Error(result.message || result.error || `Image upload failed (${response.status}). Retry or remove the image.`);
            // A removed upload must never reappear or become a request attachment.
            if (!imageItems(sessionId).includes(item)) return false;
            item.descriptor = result.image_attachment; known.set(item.descriptor.concept_id, item);
        } catch (error) {
            if (!imageItems(sessionId).includes(item)) return false;
            item.error = String(error.message || error);
            item.retry = upload;
        }
        changed();
        return Boolean(item.descriptor);
    };
    return upload();
}

// Both controls use the existing session-bound upload path supplied by chatTab.
export function initialiseImagePicker(upload, status) {
    const input = document.getElementById('attachImageInput');
    const button = document.getElementById('attachImageButton');
    const paste = document.getElementById('pasteImageButton');
    if (button && input) {
        button.addEventListener('click', () => input.click());
        input.addEventListener('change', () => {
            const files = Array.from(input.files || []);
            input.value = '';
            if (files.length) void upload(files);
        });
        input.addEventListener('cancel', () => status('Image selection cancelled. Your draft is unchanged.'));
    }
    paste?.addEventListener('click', async () => {
        if (!navigator.clipboard?.read) {
            status('Clipboard images are unavailable here. Use Attach image, or paste into the message field.', 'error');
            return;
        }
        try {
            const items = await navigator.clipboard.read();
            const files = [];
            for (const item of items) {
                const type = item.types.find(value => value.startsWith('image/'));
                if (type) files.push(new File([await item.getType(type)], `pasted-image-${files.length + 1}.${type.split('/')[1]}`, {type}));
            }
            if (files.length) await upload(files);
            else status('No image found on the clipboard. Use Attach image to choose a photo.', 'error');
        } catch (_) {
            status('Clipboard access was unavailable or cancelled. Use Attach image, or paste into the message field.', 'error');
        }
    });
}
export function takeImages(sessionId) {
    if (imagesBlocked(sessionId)) throw new Error('Image preparation failed or is still running. Remove or finish the upload before sending.');
    const ids = imageItems(sessionId).map(x => x.descriptor.concept_id);
    pending.delete(sessionId); return ids;
}
export function restoreImages(sessionId, ids) {
    const existing = imageItems(sessionId);
    for (const id of ids || []) {
        const item = known.get(id);
        if (item && !existing.includes(item)) existing.push(item);
    }
    pending.set(sessionId, existing);
}
export function descriptorsForIds(ids) { return (ids || []).map(id => known.get(id)?.descriptor).filter(Boolean); }
