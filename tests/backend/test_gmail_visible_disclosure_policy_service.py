from __future__ import annotations

import pytest

from src.backend.services import gmail_visible_disclosure_policy_service as policy


def _concept(raw_policy=None):
    attributes = {}
    if raw_policy is not None:
        attributes[policy.GMAIL_VISIBLE_DISCLOSURE_POLICY_ATTRIBUTE] = raw_policy
    return {"attributes": attributes}


def _policy(
    *,
    mode="required",
    policy_id="policy-1",
    policy_version="1",
    binding=False,
    default_language="en",
    templates=None,
):
    value = {
        "schema_version": policy.GMAIL_VISIBLE_DISCLOSURE_POLICY_SCHEMA_VERSION,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "mode": mode,
        "binding": binding,
    }
    if mode == "required":
        value["default_language"] = default_language
        value["templates"] = templates or {
            "en": {
                "von_sent": "Sent by the assistant.",
                "von_drafted_and_sent": "Drafted and sent by the assistant.",
                "user_authored_von_sent": (
                    "Sent by the assistant using user-supplied text."
                ),
            }
        }
    return value


def _resolve(concepts, **kwargs):
    return policy.resolve_gmail_visible_disclosure_policy(
        profile_resource_concept_id="#V#mailbox",
        acting_user_concept_id="#V#actor",
        organisation_concept_id="#V#organisation",
        concept_loader=concepts.get,
        **kwargs,
    )


def test_absent_policy_defaults_to_no_visible_disclosure_and_preserves_body():
    resolution = _resolve({})

    assert resolution.mode == policy.DISCLOSURE_MODE_OFF
    assert resolution.selection_reason == "default_absence"
    assert resolution.authority_scope == "default"
    assert resolution.visible_text is None
    assert policy.apply_gmail_visible_disclosure("Body.\n", resolution) == "Body.\n"


def test_actor_preference_can_disable_only_non_binding_policy():
    concepts = {
        "#V#organisation": _concept(
            _policy(policy_id="organisation-default", binding=False)
        ),
        "#V#actor": _concept(_policy(mode="off", policy_id="actor-off")),
    }

    resolution = _resolve(concepts)

    assert resolution.mode == policy.DISCLOSURE_MODE_OFF
    assert resolution.policy_id == "actor-off"
    assert resolution.selection_reason == "actor_preference"
    assert [item["policy_id"] for item in resolution.considered_policies] == [
        "organisation-default",
        "actor-off",
    ]


def test_binding_organisation_requirement_cannot_be_disabled_by_actor():
    concepts = {
        "#V#organisation": _concept(
            _policy(policy_id="binding-organisation", binding=True)
        ),
        "#V#actor": _concept(_policy(mode="off", policy_id="actor-off")),
    }

    resolution = _resolve(
        concepts,
        body_language="en-NZ",
        body_authorship=policy.BODY_AUTHORSHIP_USER_SUPPLIED,
    )

    assert resolution.mode == policy.DISCLOSURE_MODE_REQUIRED
    assert resolution.policy_id == "binding-organisation"
    assert resolution.authority_scope == "organisation"
    assert resolution.binding is True
    assert resolution.selection_reason == "binding_required"
    assert resolution.resolved_language == "en"
    assert resolution.template_role == "user_authored_von_sent"
    assert resolution.visible_text == "Sent by the assistant using user-supplied text."


def test_required_policy_adds_exactly_one_role_specific_block():
    concepts = {"#V#mailbox": _concept(_policy(policy_id="mailbox-required"))}
    resolution = _resolve(
        concepts,
        body_authorship=policy.BODY_AUTHORSHIP_VON_DRAFTED,
    )
    disclosure = "Drafted and sent by the assistant."

    added = policy.apply_gmail_visible_disclosure("Body.", resolution)
    already_present = policy.apply_gmail_visible_disclosure(added, resolution)
    duplicated = policy.apply_gmail_visible_disclosure(
        f"Body.\n\n{disclosure}\n\n{disclosure}",
        resolution,
    )

    assert added == f"Body.\n\n{disclosure}"
    assert already_present == added
    assert duplicated.count(disclosure) == 1


@pytest.mark.parametrize(
    ("language", "notice"),
    [
        ("es", "Enviado por el asistente."),
        ("mi", "I tukuna e te kaiāwhina."),
        ("zh-Hans", "由助理发送。"),
    ],
)
def test_required_policy_uses_configured_non_english_language_templates(
    language,
    notice,
):
    templates = {
        language: {
            "von_sent": notice,
            "von_drafted_and_sent": f"{notice}（草拟并发送）",
            "user_authored_von_sent": f"{notice}（用户提供文本）",
        }
    }
    concepts = {
        "#V#mailbox": _concept(
            _policy(
                policy_id=f"required-{language}",
                default_language=language,
                templates=templates,
            )
        )
    }

    resolution = _resolve(concepts, body_language=language)

    assert resolution.resolved_language == language.lower()
    assert resolution.visible_text == notice
    assert policy.apply_gmail_visible_disclosure("正文。", resolution).endswith(notice)


def test_required_policy_does_not_fall_back_to_another_message_language():
    concepts = {"#V#mailbox": _concept(_policy())}

    with pytest.raises(
        policy.GmailVisibleDisclosurePolicyError,
        match="has no template for message language 'zh-hans'",
    ):
        _resolve(concepts, body_language="zh-Hans")


def test_malformed_binding_off_policy_fails_closed():
    concepts = {
        "#V#organisation": _concept(
            _policy(mode="off", policy_id="invalid-binding-off", binding=True)
        )
    }

    with pytest.raises(
        policy.GmailVisibleDisclosurePolicyError,
        match="only a required disclosure policy may be binding",
    ):
        _resolve(concepts)


def test_policy_store_failure_is_not_treated_as_absent(monkeypatch):
    monkeypatch.setattr(
        policy.concept_service,
        "get_concepts_by_concept_ids_exact",
        lambda _concept_ids: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    with pytest.raises(
        policy.GmailVisibleDisclosurePolicyError,
        match="policy could not be resolved",
    ) as error:
        policy.resolve_gmail_visible_disclosure_policy(
            profile_resource_concept_id="#V#mailbox",
            acting_user_concept_id="#V#actor",
            organisation_concept_id="#V#organisation",
        )

    assert (
        error.value.reason_code
        == "gmail_visible_disclosure_policy_resolution_unavailable"
    )
