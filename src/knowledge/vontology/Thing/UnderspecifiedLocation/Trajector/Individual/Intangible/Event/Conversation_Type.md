# Conversation

**SubConcept Of**: Event

**Description**: A Conversation represents a dialogue session between one or more participants and the Von assistant. It serves as a referenceable entity for linking tasks, documents, and other artefacts that arise from or relate to the conversation. Conversations are created lazily when needed (e.g., when a task references a conversation) and can be linked to chat history via session_id.

**Salient Predicates**:
- hasSessionId (the chat history session_id linking to stored messages)
- hasParticipant (persons involved in the conversation)
- hasOwner (the user who initiated/owns the conversation)
- hasOrganisation (the organisation context, if applicable)
- hasNamespace (the composite namespace for multi-tenant scoping)
- hasStartTime (when the conversation began)
- hasTopic (optional topic or summary extracted from conversation)

**Implementation Notes**:
- Conversation concepts are created lazily when first referenced (e.g., by a task)
- RAG-indexed conversation segments should also become conversation concepts for unified querying
- The session_id stored via hasSessionId links to the chat_history MongoDB collection
- Future: functional terms may allow referencing "the conversation with session_id X" without explicit creation
