import { renderImageAttachments } from './utils/conversationImages.js';

// Primary retained parts share the existing display contract and image controls.
// The caller supplies the same Markdown pipeline used for ordinary replies.
export function renderRetainedConversationContent(container, contract, renderText) {
    if (contract?.schema_version !== 'turn_display_elements_v1' || !Array.isArray(contract.elements)) return false;
    const elements = contract.elements.filter(e => e.channel === 'screen' && e.provenance?.source === 'retained_content')
        .sort((a, b) => a.order - b.order);
    if (!elements.length) return false;
    const signature = JSON.stringify(elements);
    const previous = container.querySelector(':scope > .chat-retained-content');
    if (previous?.dataset.signature === signature) return true;
    const root = document.createElement('div');
    root.className = 'chat-retained-content'; root.dataset.signature = signature;
    const seen = new Set();
    for (const element of elements) {
        if (seen.has(element.element_id)) continue;
        seen.add(element.element_id);
        const holder = document.createElement('div');
        holder.dataset.elementId = element.element_id;
        root.appendChild(holder);
        if (element.element_type === 'text_block') {
            holder.textContent = String(element.payload?.text || '');
            // Plain source already provides the local fallback if enrichment
            // fails (for example during navigation or an unavailable renderer).
            void Promise.resolve(renderText(holder, element.payload?.text || '')).catch(() => {});
        } else if (element.element_type === 'image' && element.payload?.version === 1 && element.payload.asset?.concept_id) {
            renderImageAttachments(holder, [element.payload.asset]);
        } else {
            holder.textContent = `Unsupported visual format: ${element.element_type}.`;
        }
    }
    container.replaceChildren(root);
    // A provisional attachment gallery may have rendered before debug/display
    // hydration arrived. Keep only the ordered image elements after handoff.
    const turn = container.closest('[data-turn-id]');
    if (turn) for (const gallery of turn.querySelectorAll('.conversation-image-gallery')) {
        if (!root.contains(gallery)) gallery.remove();
    }
    return true;
}
