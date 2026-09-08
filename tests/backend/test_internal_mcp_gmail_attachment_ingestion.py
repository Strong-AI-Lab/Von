from __future__ import annotations

import asyncio
import base64
import hashlib
import json


def _gmail_message(
    *,
    attachment_id: str = "attachment-1",
    filename: str = "bin-research-summary.txt",
    content_type: str = "text/plain",
    size: int = 28,
) -> dict:
    return {
        "id": "message-1",
        "threadId": "thread-1",
        "payload": {
            "headers": [
                {"name": "From", "value": "Bin Zhang <bin@example.test>"},
                {"name": "Subject", "value": "Research Summary – Bin Zhang"},
                {"name": "Date", "value": "Wed, 5 Aug 2026 12:00:00 +1200"},
            ],
            "parts": [
                {
                    "filename": filename,
                    "mimeType": content_type,
                    "body": {"attachmentId": attachment_id, "size": size},
                }
            ],
        },
    }


def _install_actor(
    monkeypatch,
    *,
    authorised_profiles: tuple[str, ...] = ("represented-profile",),
    configured_profiles: tuple[str, ...] | None = None,
) -> None:
    from src.backend.integrations.google import gmail_service
    from src.backend.security import access_control
    from src.backend.services import mail_profile_resource_vontology_service

    monkeypatch.setattr(
        access_control,
        "get_effective_user_concept_id",
        lambda: "#V#michael_witbrock",
    )
    monkeypatch.setattr(
        access_control,
        "get_effective_organisation_concept_id",
        lambda: "#V#sail",
    )
    authorised = set(authorised_profiles)

    def _resolve_profile(*, user_concept_id, requested_profile_id=None):
        assert user_concept_id == "#V#michael_witbrock"
        requested = str(requested_profile_id or "").strip()
        if requested in authorised:
            return {
                "success": True,
                "reason_code": "authorised_mail_profile_resolved",
                "profile_id": requested,
                "profile_resource_concept_id": f"#V#gmail_profile_{requested}",
                "selection_source": "request",
            }
        return {
            "success": False,
            "reason_code": "mail_profile_not_authorised_for_actor",
            "profile_id": None,
        }

    monkeypatch.setattr(
        mail_profile_resource_vontology_service,
        "resolve_authorised_gmail_profile_for_user",
        _resolve_profile,
    )
    monkeypatch.setattr(
        gmail_service,
        "list_profile_ids_from_env",
        lambda: list(
            authorised_profiles if configured_profiles is None else configured_profiles
        ),
    )
    configured = (
        authorised_profiles if configured_profiles is None else configured_profiles
    )
    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: {
            profile_id: gmail_service.GmailProfile(
                profile_id=profile_id,
                token_path=f"/nonexistent/{profile_id}.json",
            )
            for profile_id in configured
        },
    )


def _install_gmail_source(
    monkeypatch,
    *,
    source_bytes: bytes = b"Bin research description",
    attachment_id: str = "attachment-1",
    filename: str = "bin-research-summary.txt",
    content_type: str = "text/plain",
) -> None:
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue

    raw_data = base64.urlsafe_b64encode(source_bytes).decode("ascii").rstrip("=")
    monkeypatch.setattr(
        gmail_service,
        "get_message",
        lambda **_kwargs: _gmail_message(
            attachment_id=attachment_id,
            filename=filename,
            content_type=content_type,
            size=len(source_bytes),
        ),
    )
    monkeypatch.setattr(
        gmail_service,
        "get_attachment",
        lambda **_kwargs: {"data": raw_data, "size": len(source_bytes)},
    )
    monkeypatch.setattr(
        catalogue,
        "_gmail_authorised_email",
        lambda _profile: "michael@example.test",
    )


def test_gmail_attachment_inspection_is_ephemeral_and_compact(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import (
        blob_uploads,
        computer_file_copy_service,
        concept_service,
        workflow_event_integration_service,
    )

    _install_actor(monkeypatch)
    opaque_attachment_id = "opaque-" + ("x" * 400)
    source_bytes = b"Bin research description"
    _install_gmail_source(
        monkeypatch,
        source_bytes=source_bytes,
        attachment_id=opaque_attachment_id,
    )

    def _unexpected_effect(**_kwargs):
        raise AssertionError("attachment inspection must not create durable state")

    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        _unexpected_effect,
    )
    monkeypatch.setattr(blob_uploads, "put_bytes_durable", _unexpected_effect)
    monkeypatch.setattr(concept_service, "create_concept", _unexpected_effect)
    monkeypatch.setattr(
        workflow_event_integration_service,
        "maybe_launch_file_copy_uploaded_workflow",
        _unexpected_effect,
    )
    monkeypatch.setattr(catalogue, "_read_file_copy", _unexpected_effect)

    result = catalogue._gmail_get_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id=opaque_attachment_id,
        namespace="#V#untrusted_override@other_org",
        max_text_chars=12,
    )

    assert result == {
        "success": True,
        "profile": "represented-profile",
        "authorised_email": "michael@example.test",
        "message_id": "message-1",
        "thread_id": "thread-1",
        "sender": "Bin Zhang <bin@example.test>",
        "subject": "Research Summary – Bin Zhang",
        "date": "Wed, 5 Aug 2026 12:00:00 +1200",
        "attachment_id": opaque_attachment_id,
        "filename": "bin-research-summary.txt",
        "content_type": "text/plain",
        "size_bytes": len(source_bytes),
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "text": "Bin research",
        "encoding": "utf-8",
        "text_length": 12,
        "text_truncated": True,
        "text_extraction": "text_decode",
    }
    assert "computer_file_copy" not in result
    assert "read_result" not in result
    assert "data" not in result
    assert base64.urlsafe_b64encode(source_bytes).decode("ascii") not in repr(result)

    definition = catalogue.build_default_catalogue().get("gmail_get_attachment")
    from src.backend.integrations.internal_mcp.schemas import validate_payload

    assert definition.category == "read"
    assert validate_payload(definition.output_schema, result) == (True, [])


def test_gmail_attachment_inspection_rejects_large_source_before_blob_read(
    monkeypatch,
):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue

    _install_actor(monkeypatch)
    monkeypatch.setattr(
        gmail_service,
        "get_message",
        lambda **_kwargs: _gmail_message(size=5_000),
    )
    attachment_read = False

    def _get_attachment(**_kwargs):
        nonlocal attachment_read
        attachment_read = True
        raise AssertionError("reported oversized attachment must not be downloaded")

    monkeypatch.setattr(gmail_service, "get_attachment", _get_attachment)

    result = catalogue._gmail_get_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
        max_bytes=1_000,
    )

    assert result["error_code"] == "gmail_attachment_too_large"
    assert result["error_details"]["reported_size_bytes"] == 5_000
    assert attachment_read is False


def test_gmail_attachment_pdf_text_is_extracted_without_import(monkeypatch):
    import fitz

    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import computer_file_copy_service
    from src.backend.services import file_bytes_text_projection_service

    _install_actor(monkeypatch)
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Flight EZY8159 departs at 12:15")
    source_bytes = document.tobytes()
    document.close()
    _install_gmail_source(
        monkeypatch,
        source_bytes=source_bytes,
        filename="booking.pdf",
        content_type="application/pdf",
    )

    def _unexpected_import(**_kwargs):
        raise AssertionError("PDF inspection must not import")

    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        _unexpected_import,
    )

    result = catalogue._gmail_get_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
    )

    assert result["success"] is True
    if file_bytes_text_projection_service._pdf_parser_isolation_supported():
        assert result["text_extraction"] == "pymupdf"
        assert "Flight EZY8159 departs at 12:15" in result["text"]
    else:
        assert result["text_extraction"] == "pdf_text_projection_unsupported"
        assert result["text_extraction_error"] == ("pdf_parser_isolation_unavailable")
        assert "text" not in result
    assert "computer_file_copy" not in result


def test_gmail_attachment_import_is_explicit_effect_with_canonical_readback(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import computer_file_copy_service

    _install_actor(monkeypatch)
    source_bytes = b"Bin research description"
    _install_gmail_source(monkeypatch, source_bytes=source_bytes)

    imported: dict = {}

    def _import_bytes_file_copy(**kwargs):
        imported.update(kwargs)
        return {
            "success": True,
            "concept_id": "#V#computer_file_copy_gmail_1",
            "type_concept_id": "#V#computer_file_copy",
            "reused_existing": False,
            "storage": {
                "backend": "local",
                "key": "private/storage/key",
                "uri": "/private/host/path/gmail-1.pdf",
                "metadata": {"user_concept_id": "#V#private_actor"},
            },
            "artifact_record": {
                "artifact_id": "#V#computer_file_copy_gmail_1",
                "blob": {"uri": "/private/host/path/gmail-1.pdf"},
            },
        }

    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        _import_bytes_file_copy,
    )
    monkeypatch.setattr(
        computer_file_copy_service,
        "build_file_copy_artifact_record",
        lambda **_kwargs: {
            "artifact_id": "#V#computer_file_copy_gmail_1",
            "concept_id": "#V#computer_file_copy_gmail_1",
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "size_bytes": len(source_bytes),
            "content_type": "text/plain",
            "type_concept_ids": ["#V#computer_file_copy"],
            "blob": {
                "backend": "local",
                "key": "private/storage/key",
                "uri": "/private/host/path/gmail-1.pdf",
            },
            "provenance": {
                "namespace": "#V#private_actor@private_org",
                "user_concept_id": "#V#private_actor",
            },
        },
    )
    monkeypatch.setattr(
        computer_file_copy_service,
        "fetch_file_copy_bytes",
        lambda **_kwargs: {
            "success": True,
            "data": source_bytes,
        },
    )

    result = catalogue._gmail_import_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
        allow_import=True,
        namespace="#V#untrusted_override@other_org",
    )

    assert result["success"] is True
    assert result["effect"] == {
        "schema_version": "gmail_attachment_import_effect.v1",
        "effect_type": "computer_file_copy_import",
        "status": "succeeded",
        "concept_id": "#V#computer_file_copy_gmail_1",
        "changed": True,
        "reused_existing": False,
    }
    assert result["computer_file_copy"] == {
        "concept_id": "#V#computer_file_copy_gmail_1",
        "type_concept_id": "#V#computer_file_copy",
        "reused_existing": False,
    }
    assert result["canonical_readback"]["concept_id"] == (
        "#V#computer_file_copy_gmail_1"
    )
    assert result["canonical_readback"]["metadata_readback"] == {
        "success": True,
        "matches_import": True,
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "size_bytes": len(source_bytes),
        "content_type": "text/plain",
        "type_concept_ids": ["#V#computer_file_copy"],
        "error": None,
    }
    assert result["effect_status"] == "succeeded"
    assert result["mutation_outcome"] == "completed"
    assert result["outcome_finality"] == "canonical_durable_readback"
    assert result["canonical_readback"]["blob_readback"] == {
        "success": True,
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "size_bytes": len(source_bytes),
        "error": None,
    }
    assert "/private/host/path" not in repr(result)
    assert "private/storage/key" not in repr(result)
    assert "#V#private_actor" not in repr(result)
    assert imported["data"] == source_bytes
    assert imported["user_concept_id"] == "#V#michael_witbrock"
    assert imported["organisation_concept_id"] == "#V#sail"
    assert imported["namespace"] == "#V#michael_witbrock@sail"
    assert imported["namespace_source"] == "authenticated_gmail_attachment_import"
    assert imported["source_system"] == "gmail_attachment"
    assert imported["source_identifier"] == (
        "represented-profile:message-1:content-sha256:"
        f"{hashlib.sha256(source_bytes).hexdigest()}"
    )
    assert imported["source_uri"] == ("gmail://represented-profile/messages/message-1")
    assert imported["metadata"]["gmail_attachment_id_sha256"] == (
        hashlib.sha256(b"attachment-1").hexdigest()
    )

    definition = catalogue.build_default_catalogue().get("gmail_import_attachment")
    from src.backend.integrations.internal_mcp.schemas import validate_payload
    from src.backend.workflows.write_tool_policy import (
        WRITE_RISK_ADDITIVE_LOW_RISK,
        classify_write_tool_risk,
    )

    assert definition.category == "write"
    assert definition.ordinary_turn_effect is True
    assert definition.ordinary_turn_trusted_argument_bindings == {
        "namespace": "turn_namespace",
    }
    assert definition.ordinary_turn_trusted_argument_choice_bindings == {
        "profile": "gmail_profile",
    }
    assert (
        classify_write_tool_risk("gmail_import_attachment")
        == WRITE_RISK_ADDITIVE_LOW_RISK
    )
    assert validate_payload(definition.output_schema, result) == (True, [])


def test_gmail_attachment_import_requires_explicit_allow_before_mail_read(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue

    _install_actor(monkeypatch)
    gmail_called = False

    def _get_message(**_kwargs):
        nonlocal gmail_called
        gmail_called = True
        raise AssertionError("mail must not be read before import authority")

    monkeypatch.setattr(gmail_service, "get_message", _get_message)

    result = catalogue._gmail_import_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
        allow_import=False,
    )

    assert result["error_code"] == "gmail_attachment_import_not_allowed"
    assert gmail_called is False


def test_gmail_attachment_import_failure_is_honest_and_has_no_readback(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import computer_file_copy_service

    _install_actor(monkeypatch)
    _install_gmail_source(monkeypatch, source_bytes=b"failed import")
    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        lambda **_kwargs: {"success": False, "error": "blob_store_upload_failed"},
    )
    readback_called = False

    def _readback(**_kwargs):
        nonlocal readback_called
        readback_called = True
        raise AssertionError("read-back must not run after failed import")

    monkeypatch.setattr(
        computer_file_copy_service,
        "build_file_copy_artifact_record",
        _readback,
    )

    result = catalogue._gmail_import_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
        allow_import=True,
    )

    assert result["error_code"] == "gmail_attachment_file_copy_import_failed"
    assert result["error_details"]["cause"] == "blob_store_upload_failed"
    assert readback_called is False


def test_gmail_attachment_import_preserves_partial_blob_registration_failure(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue, transport
    from src.backend.services import computer_file_copy_service

    _install_actor(monkeypatch)
    _install_gmail_source(monkeypatch, source_bytes=b"stored before registration")
    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        lambda **_kwargs: {
            "success": False,
            "error": "file_copy_register_failed",
            "effect_status": "partial",
            "changed": True,
            "mutation_outcome": "partial",
            "outcome_finality": "blob_persisted_registration_failed",
        },
    )
    receipts: list[dict] = []
    monkeypatch.setattr(
        transport,
        "record_internal_mcp_effect_receipt",
        lambda receipt: receipts.append(dict(receipt)) or True,
    )

    result = catalogue._gmail_import_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
        allow_import=True,
    )

    assert result["success"] is False
    assert result["effect_status"] == "partial"
    assert result["changed"] is True
    assert result["mutation_outcome"] == "partial"
    assert result["outcome_finality"] == "blob_persisted_registration_failed"
    assert result["effect"]["status"] == "partial"
    assert result["recovery_affordances"] == [
        {
            "action_type": "retry_idempotent_attachment_import",
            "capability": "gmail_import_attachment",
            "arguments": {
                "profile": "represented-profile",
                "message_id": "message-1",
                "attachment_id": "attachment-1",
                "allow_import": True,
            },
        }
    ]
    assert receipts[-1]["effect_status"] == "partial"
    assert receipts[-1]["changed"] is True


def test_gmail_attachment_import_preserves_indeterminate_blob_write(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue, transport
    from src.backend.services import computer_file_copy_service

    _install_actor(monkeypatch)
    _install_gmail_source(monkeypatch, source_bytes=b"unknown blob outcome")
    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        lambda **_kwargs: {
            "success": False,
            "error": "blob_store_upload_failed",
            "effect_status": "indeterminate",
            "mutation_outcome": "unknown",
            "outcome_finality": "blob_write_indeterminate",
        },
    )
    receipts: list[dict] = []
    monkeypatch.setattr(
        transport,
        "record_internal_mcp_effect_receipt",
        lambda receipt: receipts.append(dict(receipt)) or True,
    )

    result = catalogue._gmail_import_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
        allow_import=True,
    )

    assert result["success"] is False
    assert result["effect_status"] == "indeterminate"
    assert "changed" not in result
    assert result["mutation_outcome"] == "unknown"
    assert result["outcome_finality"] == "blob_write_indeterminate"
    assert receipts[-1]["effect_status"] == "indeterminate"


def test_gmail_attachment_import_reports_indeterminate_when_canonical_readback_fails(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue, transport
    from src.backend.services import computer_file_copy_service

    _install_actor(monkeypatch)
    _install_gmail_source(monkeypatch, source_bytes=b"durably imported")
    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        lambda **_kwargs: {
            "success": True,
            "concept_id": "#V#computer_file_copy_gmail_partial",
            "type_concept_id": "#V#computer_file_copy",
            "reused_existing": False,
            "storage": {"backend": "s3", "uri": "s3://private/key"},
        },
    )
    monkeypatch.setattr(
        computer_file_copy_service,
        "build_file_copy_artifact_record",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        computer_file_copy_service,
        "fetch_file_copy_bytes",
        lambda **_kwargs: {"success": False, "error": "blob_fetch_failed"},
    )
    receipts: list[dict] = []
    monkeypatch.setattr(
        transport,
        "record_internal_mcp_effect_receipt",
        lambda receipt: receipts.append(dict(receipt)) or True,
    )

    result = catalogue._gmail_import_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
        allow_import=True,
    )

    assert result["success"] is False
    assert result["error_code"] == "gmail_attachment_import_readback_failed"
    assert result["effect_status"] == "indeterminate"
    assert result["mutation_outcome"] == "unknown"
    assert result["outcome_finality"] == "canonical_readback_indeterminate"
    assert result["changed"] is True
    assert result["effect"]["status"] == "indeterminate"
    assert result["computer_file_copy"]["concept_id"] == (
        "#V#computer_file_copy_gmail_partial"
    )
    assert result["canonical_readback"] == {
        "concept_id": "#V#computer_file_copy_gmail_partial",
        "metadata_readback": {
            "success": False,
            "matches_import": False,
            "sha256": None,
            "size_bytes": None,
            "content_type": None,
            "type_concept_ids": [],
            "error": "canonical_artifact_missing",
        },
        "blob_readback": {
            "success": False,
            "sha256": None,
            "size_bytes": None,
            "error": "file_copy_blob_readback_failed",
        },
    }
    assert receipts[0]["concept_id"] == "#V#computer_file_copy_gmail_partial"
    assert receipts[0]["effect_status"] == "partial"
    assert receipts[0]["outcome_finality"] == "pending_canonical_readback"
    assert receipts[-1]["concept_id"] == "#V#computer_file_copy_gmail_partial"
    assert receipts[-1]["effect_status"] == "indeterminate"


def test_gmail_attachment_requires_authenticated_actor_before_mail_read(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.security import access_control

    monkeypatch.setattr(access_control, "get_effective_user_concept_id", lambda: None)
    monkeypatch.setattr(
        access_control,
        "get_effective_organisation_concept_id",
        lambda: None,
    )
    gmail_called = False

    def _get_message(**_kwargs):
        nonlocal gmail_called
        gmail_called = True
        raise AssertionError("mail must not be read without an actor scope")

    monkeypatch.setattr(gmail_service, "get_message", _get_message)

    for operation, extra in (
        (catalogue._gmail_get_attachment, {}),
        (catalogue._gmail_import_attachment, {"allow_import": True}),
    ):
        result = operation(
            profile="represented-profile",
            message_id="message-1",
            attachment_id="attachment-1",
            **extra,
        )
        assert result["error_code"] == "authenticated_actor_context_required"
    assert gmail_called is False


def test_gmail_attachment_rejects_configured_but_foreign_profile_before_mail_read(
    monkeypatch,
):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue

    _install_actor(
        monkeypatch,
        authorised_profiles=("represented-profile",),
        configured_profiles=("represented-profile", "configured-foreign-profile"),
    )
    gmail_called = False

    def _get_message(**_kwargs):
        nonlocal gmail_called
        gmail_called = True
        raise AssertionError("foreign configured profile must not reach Gmail")

    monkeypatch.setattr(gmail_service, "get_message", _get_message)

    for operation, extra in (
        (catalogue._gmail_get_attachment, {}),
        (catalogue._gmail_import_attachment, {"allow_import": True}),
    ):
        result = operation(
            profile="configured-foreign-profile",
            message_id="message-1",
            attachment_id="attachment-1",
            **extra,
        )
        assert result["error_code"] == "gmail_profile_not_authorised"
        assert result["error_details"]["authority_reason"] == (
            "mail_profile_not_authorised_for_actor"
        )
    assert gmail_called is False


def test_gmail_attachment_gateway_rechecks_profile_authority_before_mail_read(
    monkeypatch,
):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    _install_actor(
        monkeypatch,
        authorised_profiles=("represented-profile",),
        configured_profiles=("represented-profile", "configured-foreign-profile"),
    )
    gmail_called = False

    def _get_message(**_kwargs):
        nonlocal gmail_called
        gmail_called = True
        raise AssertionError("gateway must not bypass actor/profile authority")

    monkeypatch.setattr(gmail_service, "get_message", _get_message)
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "gmail_get_attachment",
        {
            "profile": "configured-foreign-profile",
            "message_id": "message-1",
            "attachment_id": "attachment-1",
        },
    )

    assert result.payload["error_code"] == "gmail_profile_not_authorised"
    assert gmail_called is False


def test_gmail_attachment_gateway_rejects_forged_namespace_actor(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    gmail_called = False

    def _get_message(**_kwargs):
        nonlocal gmail_called
        gmail_called = True
        raise AssertionError("payload namespace must not establish Gmail authority")

    monkeypatch.setattr(gmail_service, "get_message", _get_message)
    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: (_ for _ in ()).throw(
            AssertionError("forged actor must be rejected before profile lookup")
        ),
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "gmail_get_attachment",
        {
            "profile": "victim-profile",
            "message_id": "message-1",
            "attachment_id": "attachment-1",
            "namespace": "#V#victim@victim_org",
        },
    )

    assert result.payload["error_code"] == "authenticated_actor_context_required"
    assert gmail_called is False


def test_stdio_gmail_attachment_surface_returns_same_ephemeral_projection(monkeypatch):
    from src.backend.mcp_server import mcp_stdio_server

    source_bytes = b"Operator attachment inspection"
    _install_actor(monkeypatch)
    _install_gmail_source(monkeypatch, source_bytes=source_bytes)

    blocks = asyncio.run(
        mcp_stdio_server._handle_gmail_get_attachment(
            {
                "profile": "represented-profile",
                "message_id": "message-1",
                "attachment_id": "attachment-1",
            }
        )
    )
    payload = json.loads(blocks[0].text)

    assert payload["success"] is True
    assert payload["text"] == "Operator attachment inspection"
    assert payload["text_extraction"] == "text_decode"
    assert "computer_file_copy" not in payload
    assert "data" not in payload
    assert base64.urlsafe_b64encode(source_bytes).decode("ascii") not in repr(payload)


def test_rotated_attachment_handle_recovers_metadata_by_bytes(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    _install_actor(monkeypatch)
    _install_gmail_source(
        monkeypatch,
        source_bytes=b"original deck bytes",
        attachment_id="fresh-handle",
        filename="deck.pptx",
        content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    result = catalogue._gmail_attachment_source_payload(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="old-handle",
        namespace="#V#michael_witbrock@sail",
        tool_name="gmail_import_attachment",
        max_bytes=1000,
    )
    assert result["success"] is True
    assert result["filename"] == "deck.pptx"
    assert "presentationml" in result["content_type"]
    assert result["_bytes"] == b"original deck bytes"


def test_rotated_handle_rejects_ambiguous_identical_attachment_parts(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue

    _install_actor(monkeypatch)
    _install_gmail_source(monkeypatch, source_bytes=b"same", attachment_id="fresh")
    message = _gmail_message(attachment_id="fresh", size=4)
    message["payload"]["parts"].append(
        {
            "filename": "other.pptx",
            "mimeType": "text/plain",
            "body": {"attachmentId": "another", "size": 4},
        }
    )
    monkeypatch.setattr(gmail_service, "get_message", lambda **kwargs: message)
    result = catalogue._gmail_attachment_source_payload(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="old",
        namespace="#V#michael_witbrock@sail",
        tool_name="gmail_import_attachment",
        max_bytes=1000,
    )
    assert result["success"] is False
    assert result["error_code"] == "gmail_attachment_metadata_unresolved"


def test_rotated_handle_does_not_associate_same_size_different_bytes(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue

    _install_actor(monkeypatch)
    _install_gmail_source(monkeypatch, source_bytes=b"same", attachment_id="fresh")
    monkeypatch.setattr(
        gmail_service,
        "get_attachment",
        lambda **kwargs: {
            "data": base64.urlsafe_b64encode(
                b"same" if kwargs["attachment_id"] == "old" else b"else"
            ).decode()
        },
    )
    result = catalogue._gmail_attachment_source_payload(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="old",
        namespace="#V#michael_witbrock@sail",
        tool_name="gmail_import_attachment",
        max_bytes=1000,
    )
    assert result["success"] is False
    assert result["error_code"] == "gmail_attachment_metadata_unresolved"
