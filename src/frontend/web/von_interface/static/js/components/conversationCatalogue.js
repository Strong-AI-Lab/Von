/** Message-source adapter for the existing conversation tray. */
import { copyTextWithClipboardFallback } from '../utils/copyJsonButtonState.js';
import { buildMessageStreamReference } from '../utils/messageStreamReference.js';
import { showToast } from '../utils/toast.js';
import { getJson, postJson } from '../apiService.js';
import { getSessionScopedOrgId } from '../utils/sessionScopedStorage.js';
import { participantAvatar, openParticipantProfile } from './participantProfile.js';
import { openMessageExchange, refreshOpenMessageExchange, showMessageComposer, resetMessagePanelContext } from './messagePanel.js';

let rows = [];
let catalogueRows = [];
let nextCursor = null;
let acceptChatRows = () => {};
let generation = 0;
let activeId = null;
let refreshPromise = null;
let profiles = new Map();
let profilesReadAt = 0;
let sourceError = '';
let more = false;
const sourceLimit = 100;
let filterText = '';
let unreadOnly = false;
let allContexts = false;
let onChange = () => {};
let chatReadObserver = null;
let chatReadPending = false;

let searchRows = [];
let searchCursor = null;
let searchGeneration = 0;
let searchTimer = null;
let searchStatus = '';
let searchPending = false;

export function catalogueSearchActive() { return Boolean(filterText); }

function metadataMatch(row) {
    const text = `${row.session_name || ''} ${(row.participant_ids || []).join(' ')} ${(row.participant_profiles || []).map(profile => profile.display_name || '').join(' ')}`.toLocaleLowerCase();
    const searchable = `${text} ${text.replaceAll('_', ' ')}`;
    return filterText.split(/\s+/).every(term => searchable.includes(term))
        || (row.match?.fields || []).some(field => ['title', 'display_name', 'display_name_override', 'participant'].includes(field));
}

export function rankCatalogueSearchRows(input) {
    if (!filterText) return input;
    return input.slice().sort((a, b) => Number(metadataMatch(b)) - Number(metadataMatch(a))
        || (Number(b.match?.score) || 0) - (Number(a.match?.score) || 0)
        || (Date.parse(b.last_message_at) || 0) - (Date.parse(a.last_message_at) || 0)
        || String(a.session_id).localeCompare(String(b.session_id)));
}

export function filterCatalogueRows(input) {
    const combined = new Map(input.map(row => [row.session_id, row]));
    if (filterText) searchRows.forEach(row => combined.set(row.session_id, { ...row, ...combined.get(row.session_id), match: row.match }));
    const matchedIds = new Set(searchRows.map(row => row.session_id));
    return rankCatalogueSearchRows([...combined.values()].filter(row => (!unreadOnly || row.shared_unread_count > 0)
        && (!filterText || metadataMatch(row) || matchedIds.has(row.session_id))));
}

export async function searchCatalogueContent({ append = false } = {}) {
    if (!filterText || (append && !searchCursor)) return;
    const expected = ++searchGeneration;
    const contextGeneration = generation;
    const org = getSessionScopedOrgId();
    const query = filterText;
    const cursor = append ? searchCursor : null;
    searchPending = true;
    searchStatus = 'Searching conversation content…';
    onChange();
    try {
        const params = new URLSearchParams({ q: query, match_mode: 'hybrid', sort: 'relevance', page_size: '100' });
        if (cursor) params.set('cursor', cursor);
        const data = await getJson(`/von/api/session/conversation_search?${params}`);
        if (expected !== searchGeneration || contextGeneration !== generation || org !== getSessionScopedOrgId()) return;
        if (data.success !== true) throw new Error('Search unavailable');
        searchRows = append ? [...new Map([...searchRows, ...(data.results || [])].map(row => [row.session_id, row])).values()] : data.results || [];
        searchCursor = data.next_cursor || null;
        const unavailable = ['unavailable', 'partial_results'].includes(data.retrieval?.semantic?.status);
        searchStatus = unavailable
            ? 'Content search is unavailable or incomplete. Title and participant matches remain available.'
            : data.coverage_complete === false ? 'Searching indexed chat content; some history may not be indexed.' : '';
    } catch (_) {
        if (expected !== searchGeneration || contextGeneration !== generation || org !== getSessionScopedOrgId()) return;
        searchStatus = 'Content search could not finish. Title and participant matches remain available. Retry search.';
    } finally {
        if (expected === searchGeneration && contextGeneration === generation && org === getSessionScopedOrgId()) {
            searchPending = false;
            onChange();
        }
    }
}

export function directConversationRows() { return rows; }
export function catalogueHasMore() { return more; }
export function activeMessageConversationId() { return activeId; }

export function showChatConversation() {
    activeId = null;
    document.body.classList.remove('viewing-message-exchange');
    const workspace = document.getElementById('conversationWorkspace');
    workspace?.classList.remove('show-message-exchange');
}

export function resetConversationCatalogue() {
    generation += 1;
    searchGeneration += 1;
    clearTimeout(searchTimer);
    searchRows = [];
    searchCursor = null;
    searchStatus = '';
    searchPending = false;
    filterText = '';
    const searchInput = document.querySelector('.catalogue-filters input[type="search"]');
    if (searchInput) searchInput.value = '';
    rows = [];
    catalogueRows = [];
    nextCursor = null;
    resetMessagePanelContext();
    profiles.clear();
    refreshPromise = null;
    showChatConversation();
}

export async function refreshMessageCatalogue() {
    if (refreshPromise) return refreshPromise;
    const expected = generation;
    const org = getSessionScopedOrgId();
    refreshPromise = (async () => {
        try {
            const data = await getJson(`/api/messages/conversation-catalogue?limit=${sourceLimit}&all_contexts=${allContexts}`);
            if (expected !== generation || org !== getSessionScopedOrgId()) return;
            const incomingRows = data.conversations || [];
            more = data.has_more === true;
            if (!nextCursor || catalogueRows.length <= sourceLimit) nextCursor = data.next_cursor;
            sourceError = data.coverage_complete === false ? `Could not refresh ${Object.entries(data.coverage || {}).filter(([, ok]) => !ok).map(([name]) => name).join(' and ')}. The other conversations remain available; refresh to retry.` : '';
            const ids = [...new Set(incomingRows.flatMap(row => row.participant_ids || []))];
            // One batch for the visible source page; never an HTTP request per row.
            if (Date.now() - profilesReadAt > 60000 || ids.some(id => !profiles.has(id))) {
                const wanted = ids.filter(id => !profiles.has(id) || Date.now() - profilesReadAt > 60000).slice(0, 100);
                try {
                    const batch = await postJson('/von/api/participants/profiles', { concept_ids: wanted });
                    if (expected !== generation || org !== getSessionScopedOrgId()) return;
                    (batch.profiles || []).forEach(profile => profiles.set(profile.concept_id, profile));
                    profilesReadAt = Date.now();
                } catch (_) { /* Identity fallback must not hide otherwise available conversations. */ }
            }
            incomingRows.forEach(row => {
                if (row.source_kind !== 'message_exchange') return;
                row.participant_profiles = row.participant_ids.map(id => profiles.get(id)).filter(Boolean);
                row.session_name = row.other_participant_ids.map(id => profiles.get(id)?.display_name || id.replace(/^#V#/, '').replaceAll('_', ' ')).join(', ') || 'Notes to yourself';
            });
            const incomingIds = new Set(incomingRows.map(row => row.session_id));
            const retained = catalogueRows.filter(row => !incomingIds.has(row.session_id) && (row.session_id === activeId || data.coverage?.[row.source_kind === 'message_exchange' ? 'messages' : 'conversations'] === false || catalogueRows.indexOf(row) >= sourceLimit));
            catalogueRows = [...incomingRows, ...retained];
            rows = catalogueRows.filter(row => row.source_kind === 'message_exchange');
            acceptChatRows(catalogueRows.filter(row => row.source_kind !== 'message_exchange'));
        } catch (_) {
            if (expected === generation) sourceError = 'Conversations could not be refreshed. Showing the last available list.';
        } finally {
            if (expected === generation) {
                refreshPromise = null;
                onChange(true);
            }
        }
    })();
    return refreshPromise;
}

export async function selectMessageConversation(row) {
    if (!rows.some(item => item.session_id === row.session_id)) rows.push(row);
    if (!catalogueRows.some(item => item.session_id === row.session_id)) catalogueRows.push(row);
    activeId = row.session_id;
    document.body.classList.add('viewing-message-exchange');
    document.getElementById('conversationWorkspace')?.classList.add('show-message-exchange');
    onChange();
    await openMessageExchange(row);
}

export function renderMessageConversationRow(row, { selected = false, pinned = false, togglePin, hide, hidden = false, openMenu } = {}) {
    const el = document.createElement('div');
    el.tabIndex = 0;
    el.className = `chat-session-tab message-conversation-row${selected ? ' is-active' : ''}${row.shared_unread_count ? ' has-unread' : ''}`;
    el.dataset.sessionId = row.session_id;
    el.dataset.trayInitial = Array.from(row.session_name || 'V')[0];
    el.setAttribute('role', 'tab');
    el.setAttribute('aria-selected', String(selected));
    const identity = profiles.get(row.other_participant_ids[0]) || { display_name: row.session_name };
    const header = document.createElement('span');
    header.className = 'chat-session-tab-header';
    header.append(participantAvatar(identity));
    const name = document.createElement('span');
    name.className = 'chat-session-tab-name';
    name.textContent = row.session_name;
    header.append(name);
    if (row.shared_unread_count) {
        const badge = document.createElement('span');
        badge.className = 'chat-session-tab-unread';
        badge.textContent = String(row.shared_unread_count);
        badge.setAttribute('aria-label', `${row.shared_unread_count} unread messages`);
        badge.title = 'Messages addressed to you that have not been marked read, including earlier pages. Scroll to an Unread label or load earlier messages to read them.';
        header.append(badge);
    }
    const meta = document.createElement('span');
    meta.className = 'chat-session-tab-meta';
    meta.textContent = `${pinned ? 'Pinned · ' : ''}${new Date(row.last_message_at).toLocaleString()}`;
    const preview = document.createElement('span');
    preview.className = 'chat-session-tab-preview';
    if (allContexts && row.organisation_concept_id) meta.textContent += ` · ${row.organisation_concept_id.replace(/^#V#/, '').replaceAll('_', ' ')}`;
    preview.textContent = `${row.last_author_id === row.viewer_id ? 'You' : profiles.get(row.last_author_id)?.display_name || row.session_name}: ${row.preview || ''}`;
    el.title = `${row.session_name}\n${preview.textContent}\n${meta.textContent}`;
    el.setAttribute('aria-label', el.title);
    el.append(header, meta, preview);
    el.addEventListener('click', () => void selectMessageConversation(row));
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'conversation-menu-trigger';
    trigger.textContent = '⋯';
    trigger.setAttribute('aria-label', `Conversation actions for ${row.session_name}`);
    trigger.setAttribute('aria-haspopup', 'menu');
    header.append(trigger);
    const open = async event => {
        event.preventDefault();
        event.stopPropagation();
        const showMenu = openMenu || (await import('../chatTab.js')).openChatSessionMenu;
        if (!el.isConnected) return;
        const rect = trigger.getBoundingClientRect();
        showMenu(event.type === 'contextmenu' ? event.clientX : rect.left,
            event.type === 'contextmenu' ? event.clientY : rect.bottom,
            buildMessageConversationMenuItems(row, { pinned, togglePin, hide, hidden }),
            { returnFocus: trigger, focusFirst: true });
    };
    trigger.addEventListener('click', open);
    el.addEventListener('contextmenu', open);
    el.addEventListener('keydown', event => {
        if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) void open(event);
        else if (event.target === el && (event.key === 'Enter' || event.key === ' ')) {
            event.preventDefault(); void selectMessageConversation(row);
        }
    });
    return el;
}

export function buildMessageConversationMenuItems(row, { pinned, togglePin, hide, hidden } = {}) {
    const items = [
        { label: 'Open conversation', onClick: () => void selectMessageConversation(row) },
        { label: 'Copy Concept ID', onClick: async () => {
            try {
                const result = await postJson('/api/messages/exchange/reference', {
                    participant_ids: row.participant_ids,
                    organisation_concept_id: row.organisation_concept_id ?? null,
                });
                if (!result?.success || !result.concept_id?.startsWith('#V#')) throw new Error('Unavailable');
                const copied = await copyTextWithClipboardFallback(result.concept_id);
                showToast(copied ? 'Copied conversation Concept ID.' : 'Failed to copy Concept ID.', copied ? 'success' : 'error');
            } catch (_) { showToast('Conversation Concept ID is unavailable. Try again.', 'error'); }
        } },
        { label: 'Copy conversation reference', onClick: async () => {
            const reference = buildMessageStreamReference({
                currentUserId: row.viewer_id, otherUserId: row.other_participant_ids[0] || row.viewer_id,
                participantIds: row.participant_ids, organisationConceptId: row.organisation_concept_id,
                displayName: row.session_name,
            });
            const copied = reference && await copyTextWithClipboardFallback(JSON.stringify(reference, null, 2));
            showToast(copied ? 'Copied conversation reference.' : 'Failed to copy conversation reference.', copied ? 'success' : 'error');
        } },
    ];
    if (togglePin) items.push({ label: pinned ? 'Unpin conversation' : 'Pin conversation', onClick: togglePin });
    if (hide) items.push({ label: hidden ? 'Unhide conversation' : 'Hide conversation', onClick: hide });
    row.participant_ids.forEach(id => items.push({
        label: `Profile: ${profiles.get(id)?.display_name || id}`,
        onClick: () => void openParticipantProfile(id),
    }));
    return items;
}

export function mountCatalogueControls(container) {
    const compose = document.createElement('button');
    compose.type = 'button'; compose.className = 'chat-session-tab';
    compose.textContent = 'Talk to a person or agent';
    compose.onclick = () => { void showMessageComposer(); };
    container.append(compose);
    if (more && nextCursor) {
        const moreButton = document.createElement('button'); moreButton.type = 'button'; moreButton.textContent = 'Load older conversations';
        moreButton.onclick = async () => {
            const expected = generation;
            moreButton.disabled = true;
            try {
                const page = await getJson(`/api/messages/conversation-catalogue?limit=${sourceLimit}&cursor=${encodeURIComponent(nextCursor)}&all_contexts=${allContexts}`);
                if (expected !== generation) return;
                catalogueRows = [...new Map([...catalogueRows, ...(page.conversations || [])].map(row => [row.session_id, row])).values()];
                rows = catalogueRows.filter(row => row.source_kind === 'message_exchange');
                acceptChatRows(catalogueRows.filter(row => row.source_kind !== 'message_exchange'));
                nextCursor = page.next_cursor;
                more = page.has_more; onChange(true);
            } catch (_) { moreButton.textContent = 'Could not load older conversations. Retry'; moreButton.disabled = false; }
        };
        container.append(moreButton);
    }
    if (filterText) {
        const status = document.createElement('div');
        status.className = 'conversation-source-status'; status.setAttribute('role', 'status');
        status.textContent = searchStatus || 'Title and participant matches first. Content search covers indexed chats in the current organisation.';
        container.append(status);
        if (searchCursor || searchStatus.includes('Retry search')) {
            const next = document.createElement('button'); next.type = 'button';
            next.textContent = searchCursor ? 'Load more search results' : 'Retry search';
            next.disabled = searchPending;
            next.onclick = () => void searchCatalogueContent({ append: Boolean(searchCursor) });
            container.append(next);
        }
    }
    if (sourceError) {
        const status = document.createElement('div');
        status.className = 'conversation-source-status'; status.setAttribute('role', 'status');
        status.textContent = sourceError;
        container.append(status);
    }
}

export function initialiseConversationCatalogue({ render, acceptChats, currentChat }) {
    onChange = render;
    acceptChatRows = acceptChats || (() => {});
    const controls = document.createElement('div'); controls.className = 'catalogue-filters';
    const filter = document.createElement('input'); filter.type = 'search'; filter.placeholder = 'Search title, participant or topic'; filter.setAttribute('aria-label', 'Search conversations by title, participant or topic');
    filterText = '';
    filter.oninput = () => {
        filterText = filter.value.trim().toLocaleLowerCase();
        searchGeneration += 1;
        clearTimeout(searchTimer);
        searchRows = []; searchCursor = null; searchStatus = ''; searchPending = false;
        render();
        if (filterText) searchTimer = setTimeout(() => void searchCatalogueContent(), 300);
    };
    const unread = document.createElement('button'); unread.type = 'button'; unread.textContent = 'Unread'; unread.setAttribute('aria-pressed', 'false');
    unread.onclick = () => { unreadOnly = !unreadOnly; unread.setAttribute('aria-pressed', String(unreadOnly)); render(); };
    const contexts = document.createElement('button'); contexts.type = 'button'; contexts.textContent = 'All organisations'; contexts.title = 'Include your message exchanges from other organisations'; contexts.setAttribute('aria-pressed', 'false');
    contexts.onclick = () => { allContexts = !allContexts; contexts.setAttribute('aria-pressed', String(allContexts)); resetConversationCatalogue(); void refreshMessageCatalogue(); void searchCatalogueContent(); };
    controls.append(filter, unread, contexts);
    document.querySelector('.conversation-tray-header')?.after(controls);
    const refresh = async () => {
        if (document.visibilityState === 'hidden') return;
        await Promise.allSettled([refreshMessageCatalogue(), refreshOpenMessageExchange()]);
        chatReadObserver?.disconnect();
        const row = currentChat?.();
        if (!activeId && row?.last_incoming_timestamp && row?.shared_unread_count && typeof IntersectionObserver === 'function') {
            const observed = row.last_incoming_timestamp;
            const expected = generation;
            chatReadObserver = new IntersectionObserver(entries => {
                if (chatReadPending || document.hidden || activeId || expected !== generation || currentChat?.()?.session_id !== row.session_id) return;
                if (!entries.some(entry => entry.isIntersecting && entry.target.getClientRects().length)) return;
                chatReadPending = true;
                void postJson('/von/history/read', { session_id: row.session_id, observed_timestamp: observed, observed_turn_id: row.last_incoming_turn_id || null }).then(() => {
                    if (generation === expected) { row.shared_unread_count = 0; onChange(); }
                }).catch(() => {}).finally(() => { chatReadPending = false; });
            }, { threshold: 0.01 });
            document.querySelectorAll('.message-container').forEach(el => {
                if ((row.last_incoming_turn_id && el.dataset.turnId === row.last_incoming_turn_id) || new Date(el.dataset.contributionTimestamp).getTime() === new Date(observed).getTime()) chatReadObserver.observe(el);
            });
        }
    };
    // Polling is independent of the selected exchange and backend worker process.
    // Schedule after completion so slow networks cannot accumulate requests.
    const poll = async () => { const started = Date.now(); await refresh(); setTimeout(poll, Math.max(500, 4000 - (Date.now() - started))); };
    void poll();
    window.addEventListener('focus', () => void refresh());
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void refresh(); });
    document.addEventListener('von:conversation-contribution', () => void refresh());
    document.addEventListener('von:participant-profile-changed', () => { profilesReadAt = 0; void refreshMessageCatalogue(); });
}
