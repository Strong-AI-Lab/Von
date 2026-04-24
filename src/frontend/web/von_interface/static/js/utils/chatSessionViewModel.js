function normaliseString(value) {
    if (typeof value !== 'string') {
        return '';
    }
    return value.trim();
}

function normaliseNullableString(value) {
    const trimmed = normaliseString(value);
    return trimmed || null;
}

const AGENT_CREATED_ORIGIN_KINDS = new Set([
    'browser_test_fixture',
    'benchmark_harness',
    'coding_agent_test',
    'coding_agent',
    'agent_test'
]);

function normaliseIdentifier(value) {
    const trimmed = normaliseString(value);
    return trimmed ? trimmed.toLowerCase().replace(/[-\s]+/g, '_') : null;
}

function deriveAgentCreatedState({ originKind, isAgentCreated, testArtifactKind }) {
    return Boolean(
        isAgentCreated === true
        || AGENT_CREATED_ORIGIN_KINDS.has(originKind)
        || testArtifactKind
    );
}

function normaliseIsoTimestamp(value) {
    const trimmed = normaliseString(value);
    if (!trimmed) {
        return null;
    }
    return Number.isFinite(Date.parse(trimmed)) ? trimmed : null;
}

function normaliseNonNegativeNumber(value, { fallback = null } = {}) {
    if (!Number.isFinite(value)) {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) {
            return fallback;
        }
        return Math.max(0, Math.trunc(parsed));
    }
    return Math.max(0, Math.trunc(value));
}

function deriveVisibilityScope(namespace, isSharedConversation) {
    const trimmedNamespace = normaliseString(namespace);
    if (!trimmedNamespace) {
        return isSharedConversation ? 'shared' : 'unspecified';
    }
    return trimmedNamespace.includes('@') ? 'organisation' : 'user';
}

function deriveOwnershipState(isSharedConversation, hasSharedParticipants) {
    if (isSharedConversation) {
        return 'participant';
    }
    if (hasSharedParticipants) {
        return 'owner_shared';
    }
    return 'owner_private';
}

function deriveConversationKind(isSharedConversation, hasSharedParticipants) {
    if (isSharedConversation) {
        return 'shared';
    }
    if (hasSharedParticipants) {
        return 'collaborative';
    }
    return 'direct';
}

function deriveRecencyTimestamp({
    lastMessageAt,
    createdAt,
    sharedAcceptedAt
}) {
    return lastMessageAt || createdAt || sharedAcceptedAt || null;
}

function deriveTopicGroupId(session) {
    const candidates = [
        session?.topic_group_id,
        session?.topic_concept_id,
        session?.group_id,
        session?.topic_id
    ];
    for (const value of candidates) {
        const normalised = normaliseNullableString(value);
        if (normalised) {
            return normalised;
        }
    }
    return null;
}

export function normaliseConversationSessionViewModel(rawSession) {
    const session = (rawSession && typeof rawSession === 'object') ? rawSession : null;
    const sessionId = normaliseString(session?.session_id);
    if (!sessionId) {
        return null;
    }

    const sessionName = normaliseNullableString(session?.session_name);
    const namespace = normaliseNullableString(session?.namespace);
    const sharedFromUserId = normaliseNullableString(session?.shared_from_user_id);
    const sharedOwnerUserId = normaliseNullableString(session?.shared_owner_user_id);
    const inviteId = normaliseNullableString(session?.invite_id);
    const sharedAcceptedAt = normaliseIsoTimestamp(session?.shared_accepted_at);
    const isSharedConversation = Boolean(
        session?.shared_with_me
        || sharedFromUserId
        || inviteId
    );
    const hasSharedParticipants = Boolean(session?.has_shared_participants);
    const isCompleted = session?.is_completed === true;
    const lifecycleState = isCompleted ? 'completed' : 'open';
    const topicGroupId = deriveTopicGroupId(session);
    const topicMemberCount = normaliseNonNegativeNumber(session?.topic_member_count, { fallback: null });
    const messageCount = normaliseNonNegativeNumber(session?.message_count, { fallback: null });
    const sharedUnreadCount = normaliseNonNegativeNumber(session?.shared_unread_count, { fallback: 0 });
    const createdAt = normaliseIsoTimestamp(session?.created_at);
    const lastMessageAt = normaliseIsoTimestamp(session?.last_message_at);
    const completedAt = normaliseIsoTimestamp(session?.completed_at);
    const originKind = normaliseIdentifier(session?.origin_kind);
    const createdByActorConceptId = normaliseNullableString(session?.created_by_actor_concept_id);
    const createdByActorType = normaliseNullableString(session?.created_by_actor_type);
    const testArtifactKind = normaliseIdentifier(session?.test_artifact_kind);
    const isAgentCreated = deriveAgentCreatedState({
        originKind,
        isAgentCreated: session?.is_agent_created,
        testArtifactKind
    });
    const recencyTimestamp = deriveRecencyTimestamp({
        lastMessageAt,
        createdAt,
        sharedAcceptedAt
    });

    return {
        ...session,
        session_id: sessionId,
        session_name: sessionName,
        namespace,
        message_count: messageCount,
        created_at: createdAt,
        last_message_at: lastMessageAt,
        shared_accepted_at: sharedAcceptedAt,
        is_completed: isCompleted,
        completed_at: completedAt,
        shared_with_me: isSharedConversation,
        shared_from_user_id: sharedFromUserId,
        shared_owner_user_id: sharedOwnerUserId,
        invite_id: inviteId,
        has_shared_participants: hasSharedParticipants,
        shared_unread_count: sharedUnreadCount,
        conversation_kind: deriveConversationKind(
            isSharedConversation,
            hasSharedParticipants
        ),
        lifecycle_state: lifecycleState,
        visibility_scope: deriveVisibilityScope(namespace, isSharedConversation),
        ownership_state: deriveOwnershipState(
            isSharedConversation,
            hasSharedParticipants
        ),
        topic_group_id: topicGroupId,
        topic_member_count: topicMemberCount,
        has_topic_context: Boolean(topicGroupId),
        origin_kind: originKind,
        created_by_actor_concept_id: createdByActorConceptId,
        created_by_actor_type: createdByActorType,
        is_agent_created: isAgentCreated,
        test_artifact_kind: testArtifactKind,
        recency_timestamp: recencyTimestamp
    };
}

export function normaliseConversationSessionViewModels(rawSessions) {
    if (!Array.isArray(rawSessions) || rawSessions.length === 0) {
        return [];
    }
    const dedupedBySessionId = new Map();
    rawSessions.forEach((rawSession) => {
        const normalised = normaliseConversationSessionViewModel(rawSession);
        if (!normalised) {
            return;
        }
        if (!dedupedBySessionId.has(normalised.session_id)) {
            dedupedBySessionId.set(normalised.session_id, normalised);
        }
    });
    return Array.from(dedupedBySessionId.values());
}
