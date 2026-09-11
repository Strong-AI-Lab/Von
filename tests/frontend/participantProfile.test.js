jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({ getJson: jest.fn(), postJson: jest.fn(), ensureUniqueWindowSessionId: jest.fn() }));
const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
const { openParticipantProfile } = require('../../src/frontend/web/von_interface/static/js/components/participantProfile.js');

beforeEach(() => {
    document.body.innerHTML = '';
    HTMLDialogElement.prototype.showModal = function () { this.open = true; };
    HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event('close')); };
    api.getJson.mockResolvedValue({ profile: { concept_id: '#V#alice', display_name: 'Alice', can_edit: true,
        available_scopes: ['user_org_default', 'user_only_default', 'organisation_general', 'global_general'] } });
    api.postJson.mockReset();
    api.ensureUniqueWindowSessionId.mockResolvedValue("window-1");
});

test('the common editor exposes all supported scopes', async () => {
    await openParticipantProfile();
    expect([...document.querySelectorAll('select option')].map(el => el.textContent)).toEqual(['User and organisation', 'User only', 'Organisation', 'Global']);
    expect(document.querySelector('input[type=file]').accept).toContain('image/png');
});

test('generated image remains a preview until the user chooses to use it', async () => {
    await openParticipantProfile();
    document.querySelector('textarea').value = 'A geometric fox';
    api.postJson.mockResolvedValueOnce({ image: { concept_id: '#V#private_preview', url: '/private/preview' } });
    [...document.querySelectorAll('button')].find(el => el.textContent === 'Generate preview').click();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(api.postJson).toHaveBeenCalledTimes(1);
    expect(api.postJson.mock.calls[0][0]).toBe('/von/api/participants/avatar/generate');
    expect(document.querySelector('.participant-avatar-preview').hidden).toBe(false);
    document.querySelector('select').value = 'user_only_default';
    api.postJson.mockResolvedValueOnce({ profile: { concept_id: '#V#alice', display_name: 'Alice' } });
    [...document.querySelectorAll('button')].find(el => el.textContent === 'Use avatar').click();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(api.postJson).toHaveBeenLastCalledWith('/von/api/participants/avatar', { concept_id: '#V#alice', scope: 'user_only_default', image_concept_id: '#V#private_preview' });
});

test('viewing another participant offers their concept without edit controls', async () => {
    api.getJson.mockResolvedValue({ profile: { concept_id: '#V#bob', display_name: 'Bob', can_edit: false } });
    await openParticipantProfile('#V#bob');
    expect(document.querySelector('input[type=file]')).toBeNull();
    expect(document.body.textContent).toContain('Open concept details');
});


test('static and scoped avatar URLs both retain a valid query separator', async () => {
    const { participantAvatar } = require('../../src/frontend/web/von_interface/static/js/components/participantProfile.js');
    const staticAvatar = participantAvatar({ avatar_url: '/static/VonImageBig.png' });
    const scopedAvatar = participantAvatar({ avatar_url: '/von/api/participants/alice/avatar?v=hash' });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(staticAvatar.querySelector('img').getAttribute('src')).toBe('/static/VonImageBig.png?window_session_id=window-1');
    expect(scopedAvatar.querySelector('img').getAttribute('src')).toBe('/von/api/participants/alice/avatar?v=hash&window_session_id=window-1');
});
