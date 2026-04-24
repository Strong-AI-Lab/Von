import {
    normaliseConversationSessionViewModel,
    normaliseConversationSessionViewModels
} from '../utils/chatSessionViewModel.js';

describe('chatSessionViewModel', () => {
    test('normalises legacy private conversation rows with stable defaults', () => {
        const session = normaliseConversationSessionViewModel({
            session_id: '  s-private  ',
            session_name: '   ',
            created_at: '2026-02-26T10:00:00Z',
            message_count: '6',
            namespace: '#V#alice'
        });

        expect(session).toMatchObject({
            session_id: 's-private',
            session_name: null,
            message_count: 6,
            shared_with_me: false,
            conversation_kind: 'direct',
            lifecycle_state: 'open',
            visibility_scope: 'user',
            ownership_state: 'owner_private',
            has_topic_context: false,
            recency_timestamp: '2026-02-26T10:00:00Z'
        });
    });

    test('marks inbound shared conversations and derives shared visibility when namespace is absent', () => {
        const session = normaliseConversationSessionViewModel({
            session_id: 'shared-1',
            shared_from_user_id: '#V#owner',
            invite_id: 'invite-123',
            last_message_at: '2026-02-26T11:30:00Z',
            shared_unread_count: '4'
        });

        expect(session).toMatchObject({
            session_id: 'shared-1',
            shared_with_me: true,
            conversation_kind: 'shared',
            lifecycle_state: 'open',
            visibility_scope: 'shared',
            ownership_state: 'participant',
            shared_unread_count: 4,
            recency_timestamp: '2026-02-26T11:30:00Z'
        });
    });

    test('keeps completed collaborative owner sessions in a canonical lifecycle state', () => {
        const session = normaliseConversationSessionViewModel({
            session_id: 'shared-owner',
            namespace: '#V#alice@lab',
            has_shared_participants: true,
            is_completed: true,
            completed_at: '2026-02-26T11:40:00Z'
        });

        expect(session).toMatchObject({
            session_id: 'shared-owner',
            conversation_kind: 'collaborative',
            lifecycle_state: 'completed',
            visibility_scope: 'organisation',
            ownership_state: 'owner_shared',
            is_completed: true,
            completed_at: '2026-02-26T11:40:00Z'
        });
    });

    test('normalises topic context fields and de-duplicates rows by session id', () => {
        const sessions = normaliseConversationSessionViewModels([
            {
                session_id: 'topic-1',
                topic_concept_id: '#V#alignment_workstream',
                topic_member_count: '12'
            },
            {
                session_id: 'topic-1',
                topic_concept_id: '#V#overridden_duplicate'
            },
            {
                session_id: '',
                session_name: 'Invalid'
            }
        ]);

        expect(sessions).toHaveLength(1);
        expect(sessions[0]).toMatchObject({
            session_id: 'topic-1',
            topic_group_id: '#V#alignment_workstream',
            has_topic_context: true,
            topic_member_count: 12
        });
    });

    test('normalises agent-created conversation provenance', () => {
        const session = normaliseConversationSessionViewModel({
            session_id: 'browser-fixture-1',
            origin_kind: 'Browser Test Fixture',
            created_by_actor_concept_id: '#V#von_system',
            created_by_actor_type: '#V#coding_agent',
            test_artifact_kind: 'Browser Test Fixture Chat Session'
        });

        expect(session).toMatchObject({
            session_id: 'browser-fixture-1',
            origin_kind: 'browser_test_fixture',
            created_by_actor_concept_id: '#V#von_system',
            created_by_actor_type: '#V#coding_agent',
            is_agent_created: true,
            test_artifact_kind: 'browser_test_fixture_chat_session'
        });
    });
});
