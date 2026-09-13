// Durable attachment descriptors (historical image API); originals remain behind authenticated routes.
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
        const url = item.message_id
            ? `/api/messages/${encodeURIComponent(item.message_id)}/attachments/${encodeURIComponent(item.concept_id)}`
            : `/von/api/images/${encodeURIComponent(item.concept_id)}/original`;
        const link = document.createElement('a');
        link.href = url; link.target = '_blank'; link.rel = 'noopener';
        if (item.content_type && !item.content_type.startsWith('image/')) {
            link.textContent = `${item.filename || 'Attachment'} · ${item.content_type} · ${item.size_bytes} bytes`;
            gallery.appendChild(link);
            continue;
        }
        const img = document.createElement('img');
        img.src = url; img.alt = item.filename || 'Attached image';
        img.style.cssText = 'max-width:100%;width:auto;max-height:420px;object-fit:contain';
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
    parent.classList.add('conversation-attachment-composer');
    parent.replaceChildren();
    for (const item of imageItems(sessionId)) {
        const card = document.createElement('div');
        card.style.cssText = 'display:inline-flex;flex-direction:column;gap:4px;margin:6px;max-width:180px;min-width:0;overflow-wrap:anywhere';
        if (item.descriptor) {
            renderImageAttachments(card, [item.descriptor]);
            card.querySelectorAll('img').forEach(img => { img.style.maxHeight = '100px'; });
        }
        else if (item.previewUrl) {
            const preview = document.createElement('img');
            preview.src = item.previewUrl;
            preview.alt = item.name;
            preview.style.cssText = 'max-width:100%;max-height:140px;object-fit:contain';
            card.appendChild(preview);
        }
        const label = document.createElement('span');
        label.textContent = item.error ? `${item.name}: ${item.error}` : (item.descriptor ? item.name : `Uploading ${item.name}…`);
        label.setAttribute('role', item.error ? 'alert' : 'status');
        card.appendChild(label);
        const details = document.createElement('span');
        details.textContent = `${item.type || 'Unknown type'} · ${item.size.toLocaleString()} bytes`;
        card.appendChild(details);
        if (item.error) {
            const retry = document.createElement('button');
            retry.type = 'button'; retry.textContent = 'Retry upload';
            retry.setAttribute('aria-label', `Retry upload of ${item.name}`);
            retry.onclick = () => { void item.retry(); };
            card.appendChild(retry);
        }
        const remove = document.createElement('button');
        remove.type = 'button'; remove.textContent = 'Remove'; remove.setAttribute('aria-label', `Remove ${item.name}`);
        remove.onclick = () => {
            pending.set(sessionId, imageItems(sessionId).filter(x => x !== item));
            releasePreview(item);
            changed();
        };
        card.appendChild(remove); parent.appendChild(card);
    }
}
export async function uploadConversationImage(file, sessionId, headers, changed) {
    const item = {
        name: file.name, type: file.type, size: file.size,
        previewUrl: file.type.startsWith('image/') && typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : null,
    };
    item.retry = () => uploadItem(item, file, sessionId, headers, changed);
    pending.set(sessionId, [...imageItems(sessionId), item]); changed();
    return item.retry();
}
function releasePreview(item) {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    item.previewUrl = null;
}
async function uploadItem(item, file, sessionId, headers, changed) {
    if (item.uploading || !imageItems(sessionId).includes(item)) return false;
    item.uploading = true;
    item.error = null;
    changed();
    try {
        if (imageItems(sessionId).length > 8) throw new Error('At most eight attachments per message. Remove an attachment before sending.');
        const form = new FormData(); form.append('file', file, file.name || 'image.png');
        form.append('conversation_attachment', '1');
        const endpoint = file.type.startsWith('image/') ? '/von/api/images/upload' : '/von/api/files/upload';
        const response = await fetch(endpoint, {method:'POST', headers, body:form});
        const result = await response.json();
        const descriptor = result.image_attachment || (result.uploaded?.concept_id ? {...result.uploaded, filename: file.name, content_type: file.type || 'application/octet-stream', size_bytes: file.size} : null);
        if (!response.ok || !descriptor) throw new Error(result.message || result.error || 'Attachment upload failed');
        // Removing an in-flight upload must not restore it when the response arrives.
        if (imageItems(sessionId).includes(item)) {
            item.descriptor = descriptor;
            item.retry = () => Promise.resolve(true);
            known.set(item.descriptor.concept_id, item);
        }
        releasePreview(item);
    } catch (error) { item.error = String(error.message || error); }
    item.uploading = false;
    changed();
    return Boolean(item.descriptor);
}
export function takeImages(sessionId) {
    if (imagesBlocked(sessionId)) throw new Error('Attachment preparation failed or is still running. Remove or finish the upload before sending.');
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

// Acknowledge only the submitted snapshot, preserving edits made during send.
export function removeSentAttachments(sessionId, ids) {
    const sent = new Set(ids);
    pending.set(sessionId, imageItems(sessionId).filter(item => {
        if (!sent.has(item.descriptor?.concept_id)) return true;
        releasePreview(item); return false;
    }));
}

export function bindAttachmentComposer(root, input, sessionKey, headers, changed) {
    const picker = document.createElement('input');
    picker.type = 'file'; picker.multiple = true; picker.hidden = true;
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = 'Attach files';
    button.onclick = () => picker.click();
    const pendingRoot = document.createElement('div');
    pendingRoot.className = 'pending-message-attachments';
    const refresh = () => { renderImageComposer(pendingRoot, sessionKey(), refresh); changed?.(); };
    const upload = async files => {
        const key = sessionKey();
        for (const file of files) await uploadConversationImage(file, key, headers(), refresh);
    };
    picker.onchange = () => { void upload(Array.from(picker.files)); picker.value = ''; };
    root.append(button, picker, pendingRoot);
    input.addEventListener('paste', event => {
        const files = Array.from(event.clipboardData?.files || []).filter(f => f.type.startsWith('image/'));
        if (!files.length) return;
        event.preventDefault(); event.stopPropagation();
        const text = event.clipboardData.getData('text/plain');
        if (text) { input.setRangeText(text, input.selectionStart, input.selectionEnd, 'end'); input.dispatchEvent(new Event('input', {bubbles:true})); }
        void upload(files);
    });
    root.addEventListener('dragover', event => {
        if (Array.from(event.dataTransfer?.types || []).includes('Files')) { event.preventDefault(); event.stopPropagation(); }
    });
    root.addEventListener('drop', event => {
        const files = Array.from(event.dataTransfer?.files || []);
        if (!files.length) return;
        event.preventDefault(); event.stopPropagation(); void upload(files);
    });
    refresh(); return refresh;
}
