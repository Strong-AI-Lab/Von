import { copyCompactConversationReference, conversationReferenceMetadata, buildConversationReferencePayload } from '../utils/conversationReference.js';
import { postJson } from '../apiService.js';

jest.mock('../apiService.js', () => ({ postJson: jest.fn() }));

beforeEach(() => jest.clearAllMocks());

test('copies the server-verified identity and retains the legacy compatibility builder', async () => {
  postJson.mockResolvedValue({ success: true, concept_reference: '#V#conversation_0123456789ab' });
  expect(await copyCompactConversationReference({ session_id: 'source' })).toBe('#V#conversation_0123456789ab');
  expect(postJson).toHaveBeenCalledWith('/von/api/session/conversation_reference', { session_id: 'source', metadata_only: true });
  expect(buildConversationReferencePayload({ session_id: 'source' }).conversation_ref.session_id).toBe('source');
});

test('a local turn ID is validated by the server and cannot silently become a reference', async () => {
  postJson.mockResolvedValue({ success: false });
  await expect(copyCompactConversationReference({ session_id: 'source' }, 'not-stored')).rejects.toThrow();
  expect(postJson).toHaveBeenCalledWith(expect.any(String), { session_id: 'source', metadata_only: true, turn_id: 'not-stored' });
});

test('rename and access changes are read freshly without caching private titles', async () => {
  const reference = '#V#conversation_0123456789ab';
  postJson.mockResolvedValueOnce({ success: true, session_name: 'Private title' })
    .mockResolvedValueOnce({ success: true, session_name: 'Renamed' })
    .mockRejectedValueOnce(new Error('denied'));
  expect((await conversationReferenceMetadata(reference)).name).toBe('Private title');
  expect((await conversationReferenceMetadata(reference)).name).toBe('Renamed');
  expect((await conversationReferenceMetadata(reference)).name).toBe('Conversation unavailable');
});
