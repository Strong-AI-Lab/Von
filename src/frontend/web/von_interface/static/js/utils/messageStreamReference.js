/** Participant stream identity; this is not a chat-history session reference. */
export function buildMessageStreamReference({ currentUserId, otherUserId, messages = [], displayName = '' }) {
    if (!currentUserId || !otherUserId) return null;
    return {
        kind: 'von_message_stream_ref',
        schema_version: 'message_stream_reference.v1',
        message_stream_ref: {
            participant_concept_ids: [...new Set([currentUserId, otherUserId])].sort(),
            viewer_concept_id: currentUserId,
            other_user_concept_id: otherUserId,
        },
        message_lookup: {
            method: 'message_list_direct',
            other_user_concept_id: otherUserId,
            include_sent: true,
            include_received: true,
        },
        visible_message_ids: messages.map(message => message.concept_id).filter(Boolean),
        display: { name: displayName || `Conversation with ${otherUserId}` },
    };
}
