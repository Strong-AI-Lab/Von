function cleanText(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

export function normaliseCanonicalConversationReference(value) {
  const candidate = value?.conversation_reference
    || value?.conversation_ref?.conversation_ref
    || value?.conversation_ref
    || value;
  if (!candidate || typeof candidate !== 'object') return null;
  if (candidate.schema_version !== 'conversation_reference.v1') return null;
  const sessionId = cleanText(candidate.session_id);
  if (!sessionId) return null;
  return {
    schema_version: 'conversation_reference.v1',
    binding_kind: cleanText(candidate.binding_kind) || 'explicit_session_id',
    session_id: sessionId,
    user_concept_id: cleanText(candidate.user_concept_id),
    namespace: cleanText(candidate.namespace),
    organisation_concept_id: cleanText(candidate.organisation_concept_id),
    include_legacy: candidate.include_legacy === true,
  };
}

/**
 * Build the same copyable envelope for every conversation entry point.
 * A server-projected canonical reference wins; context is used only for older
 * session rows that predate conversation_reference.v1 projections.
 */
export function buildConversationReferencePayload(session, context = {}) {
  const sid = cleanText(session?.session_id) || cleanText(session?.chat_session_id);
  if (!sid) return null;

  const canonical = normaliseCanonicalConversationReference(session) || {
    schema_version: 'conversation_reference.v1',
    binding_kind: 'explicit_session_id',
    session_id: sid,
    user_concept_id: cleanText(session?.shared_owner_user_id)
      || cleanText(context.currentUserConceptId),
    namespace: cleanText(session?.namespace) || cleanText(context.namespace),
    organisation_concept_id: cleanText(session?.organisation_concept_id)
      || cleanText(context.organisationConceptId),
    include_legacy: false,
  };

  const ownerUserId = canonical.user_concept_id
    || cleanText(session?.shared_owner_user_id)
    || cleanText(context.currentUserConceptId);
  const namespace = canonical.namespace
    || cleanText(session?.namespace)
    || cleanText(context.namespace);

  return {
    kind: 'von_conversation_ref',
    schema_version: 'conversation_reference.v1',
    conversation_ref: canonical,
    chat_history_lookup: {
      user_id: ownerUserId,
      session_id: canonical.session_id,
      namespace,
      include_legacy: canonical.include_legacy,
    },
    display: {
      session_name: cleanText(session?.session_name) || cleanText(session?.display_name),
      last_message_at: cleanText(session?.last_message_at) || cleanText(session?.updated_at),
      current_user_concept_id: cleanText(context.currentUserConceptId),
    },
  };
}
