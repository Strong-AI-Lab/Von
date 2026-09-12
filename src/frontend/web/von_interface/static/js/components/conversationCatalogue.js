/** Message-source adapter for the existing conversation tray. */
import { getJson, postJson } from '../apiService.js';
import { getSessionScopedOrgId } from '../utils/sessionScopedStorage.js';
import { participantAvatar, profileButton } from './participantProfile.js';
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

export function filterCatalogueRows(input) {
    return input.filter(row => (!unreadOnly || row.shared_unread_count > 0) && (!filterText || `${row.session_name || ''} ${(row.participant_ids || []).join(' ')}`.toLocaleLowerCase().includes(filterText)));
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

export function renderMessageConversationRow(row, { selected = false, pinned = false, togglePin, hide } = {}) {
    const el = document.createElement('button');
    el.type = 'button';
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
    el.addEventListener('contextmenu', event => {
        event.preventDefault();
        const menu = document.createElement('dialog');
        menu.className = 'participant-profile-dialog';
        const action = (label, fn) => {
            const b = document.createElement('button'); b.type = 'button'; b.textContent = label;
            b.onclick = () => { menu.close(); fn(); }; menu.append(b);
        };
        action(pinned ? 'Unpin conversation' : 'Pin conversation', togglePin);
        action('Hide conversation', hide);
        row.participant_ids.forEach(id => menu.append(profileButton(id, profiles.get(id)?.display_name || id)));
        action('Close', () => {});
        document.body.append(menu); menu.addEventListener('close', () => menu.remove()); menu.showModal();
    });
    return el;
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
    const filter = document.createElement('input'); filter.type = 'search'; filter.placeholder = 'Filter title or participant'; filter.setAttribute('aria-label', 'Filter listed conversations by title or participant');
    filter.oninput = () => { filterText = filter.value.trim().toLocaleLowerCase(); render(); };
    const unread = document.createElement('button'); unread.type = 'button'; unread.textContent = 'Unread'; unread.setAttribute('aria-pressed', 'false');
    unread.onclick = () => { unreadOnly = !unreadOnly; unread.setAttribute('aria-pressed', String(unreadOnly)); render(); };
    const contexts = document.createElement('button'); contexts.type = 'button'; contexts.textContent = 'All organisations'; contexts.title = 'Include your message exchanges from other organisations'; contexts.setAttribute('aria-pressed', 'false');
    contexts.onclick = () => { allContexts = !allContexts; contexts.setAttribute('aria-pressed', String(allContexts)); resetConversationCatalogue(); void refreshMessageCatalogue(); };
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
