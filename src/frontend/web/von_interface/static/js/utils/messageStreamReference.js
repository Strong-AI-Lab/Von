/** Participant stream identity; this is not a chat-history session reference. */
export function buildMessageStreamReference({ currentUserId, otherUserId, messages = [], displayName = '', participantIds = null, organisationConceptId = undefined }) {
    if (!currentUserId || !otherUserId) return null;
    const participants = [...new Set(participantIds || [currentUserId, otherUserId])].sort();
    const exact = Array.isArray(participantIds);
    return {
        kind: 'von_message_stream_ref',
        schema_version: exact ? 'message_stream_reference.v2' : 'message_stream_reference.v1',
        message_stream_ref: {
            participant_concept_ids: participants,
            ...(exact ? { organisation_concept_id: organisationConceptId ?? null } : {}),
            viewer_concept_id: currentUserId,
            other_user_concept_id: otherUserId,
        },
        message_lookup: exact ? { method: 'POST /api/messages/exchange', participant_ids: participants, organisation_concept_id: organisationConceptId ?? null } : {
            method: 'message_list_direct',
            other_user_concept_id: otherUserId,
            include_sent: true,
            include_received: true,
        },
        visible_message_ids: messages.map(message => message.concept_id).filter(Boolean),
        display: { name: displayName || `Conversation with ${otherUserId}` },
    };
}
