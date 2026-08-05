from __future__ import annotations

import base64
import hashlib


def _gmail_message(*, attachment_id: str = "attachment-1") -> dict:
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
                    "filename": "bin-research-summary.pdf",
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": attachment_id, "size": 28},
                }
            ],
        },
    }


def _install_actor(monkeypatch) -> None:
    from src.backend.security import access_control

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


def test_gmail_attachment_is_imported_and_read_back_without_raw_base64(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import computer_file_copy_service

    _install_actor(monkeypatch)
    opaque_attachment_id = "opaque-" + ("x" * 400)
    source_bytes = b"%PDF-1.7\nBin research description"
    raw_data = base64.urlsafe_b64encode(source_bytes).decode("ascii").rstrip("=")
    monkeypatch.setattr(
        gmail_service,
        "get_message",
        lambda **_kwargs: _gmail_message(attachment_id=opaque_attachment_id),
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

    imported: dict = {}

    def _import_bytes_file_copy(**kwargs):
        imported.update(kwargs)
        return {
            "success": True,
            "concept_id": "#V#computer_file_copy_gmail_1",
            "type_concept_id": "#V#computer_file_copy",
            "reused_existing": False,
            "storage": {"backend": "memory", "uri": "memory://gmail-1"},
            "artifact_record": {
                "artifact_id": "#V#computer_file_copy_gmail_1",
                "provenance": {"source": "gmail_attachment"},
            },
        }

    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        _import_bytes_file_copy,
    )
    monkeypatch.setattr(
        catalogue,
        "_read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "concept_id": "#V#computer_file_copy_gmail_1",
            "original_filename": "bin-research-summary.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(source_bytes),
            "text": "Bin research description",
            "text_extraction": "pymupdf",
        },
    )

    result = catalogue._gmail_get_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id=opaque_attachment_id,
        namespace="#V#untrusted_override@other_org",
    )

    assert result["success"] is True
    assert result["filename"] == "bin-research-summary.pdf"
    assert result["content_type"] == "application/pdf"
    assert result["text"] == "Bin research description"
    assert result["text_extraction"] == "pymupdf"
    assert result["computer_file_copy"]["concept_id"] == (
        "#V#computer_file_copy_gmail_1"
    )
    assert "data" not in result
    assert raw_data not in repr(result)
    assert imported["data"] == source_bytes
    assert imported["user_concept_id"] == "#V#michael_witbrock"
    assert imported["organisation_concept_id"] == "#V#sail"
    assert imported["namespace"] == "#V#michael_witbrock@sail"
    assert imported["source_system"] == "gmail_attachment"
    attachment_id_sha256 = hashlib.sha256(
        opaque_attachment_id.encode("utf-8")
    ).hexdigest()
    assert imported["source_identifier"] == (
        "represented-profile:message-1:content-sha256:"
        f"{hashlib.sha256(source_bytes).hexdigest()}"
    )
    assert imported["source_uri"] == (
        "gmail://represented-profile/messages/message-1"
    )
    assert imported["metadata"]["gmail_attachment_id_sha256"] == (
        attachment_id_sha256
    )
    assert opaque_attachment_id not in repr(imported)
    assert all(
        len(value) <= 256
        for value in imported["metadata"].values()
        if isinstance(value, str)
    )
    assert imported["metadata"]["gmail_subject"] == (
        "Research Summary \\u2013 Bin Zhang"
    )
    definition = catalogue.build_default_catalogue().get("gmail_get_attachment")
    from src.backend.integrations.internal_mcp.schemas import validate_payload

    assert validate_payload(definition.output_schema, result) == (True, [])


def test_gmail_attachment_fallback_identity_ignores_rotating_attachment_token(
    monkeypatch,
):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import computer_file_copy_service

    _install_actor(monkeypatch)
    source_bytes = b"%PDF-1.7\nStable research description"
    raw_data = base64.urlsafe_b64encode(source_bytes).decode("ascii").rstrip("=")
    monkeypatch.setattr(
        gmail_service,
        "get_message",
        lambda **_kwargs: _gmail_message(attachment_id="current-token"),
    )
    monkeypatch.setattr(
        gmail_service,
        "get_attachment",
        lambda **_kwargs: {"data": raw_data, "size": len(source_bytes)},
    )

    imports: list[dict] = []

    def _import_bytes_file_copy(**kwargs):
        imports.append(kwargs)
        return {
            "success": True,
            "concept_id": "#V#computer_file_copy_gmail_stable",
            "type_concept_id": "#V#computer_file_copy",
            "reused_existing": len(imports) > 1,
        }

    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        _import_bytes_file_copy,
    )
    monkeypatch.setattr(
        catalogue,
        "_read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "concept_id": "#V#computer_file_copy_gmail_stable",
            "text": "Stable research description",
            "text_extraction": "pymupdf",
        },
    )

    for rotating_token in ("old-token-1", "old-token-2"):
        result = catalogue._gmail_get_attachment(
            profile="represented-profile",
            message_id="message-1",
            attachment_id=rotating_token,
        )
        assert result["success"] is True

    expected_sha256 = hashlib.sha256(source_bytes).hexdigest()
    assert [item["original_filename"] for item in imports] == [
        f"gmail-attachment-message-1-{expected_sha256[:12]}.pdf",
        f"gmail-attachment-message-1-{expected_sha256[:12]}.pdf",
    ]
    assert [item["source_identifier"] for item in imports] == [
        f"represented-profile:message-1:content-sha256:{expected_sha256}",
        f"represented-profile:message-1:content-sha256:{expected_sha256}",
    ]
    assert imports[0]["metadata"]["gmail_attachment_id_sha256"] != (
        imports[1]["metadata"]["gmail_attachment_id_sha256"]
    )


def test_gmail_attachment_import_failure_is_honest_and_does_not_read(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import computer_file_copy_service

    _install_actor(monkeypatch)
    monkeypatch.setattr(
        gmail_service,
        "get_message",
        lambda **_kwargs: _gmail_message(),
    )
    monkeypatch.setattr(
        gmail_service,
        "get_attachment",
        lambda **_kwargs: {
            "data": base64.urlsafe_b64encode(b"%PDF-failed").decode("ascii")
        },
    )
    monkeypatch.setattr(
        computer_file_copy_service,
        "import_bytes_file_copy",
        lambda **_kwargs: {"success": False, "error": "blob_store_upload_failed"},
    )
    read_called = False

    def _read_file_copy(**_kwargs):
        nonlocal read_called
        read_called = True
        raise AssertionError("read-back must not run after failed registration")

    monkeypatch.setattr(catalogue, "_read_file_copy", _read_file_copy)

    result = catalogue._gmail_get_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
    )

    assert result["error_code"] == "gmail_attachment_file_copy_import_failed"
    assert result["error_details"]["cause"] == "blob_store_upload_failed"
    assert read_called is False
    definition = catalogue.build_default_catalogue().get("gmail_get_attachment")
    from src.backend.integrations.internal_mcp.schemas import validate_payload

    assert validate_payload(definition.output_schema, result) == (True, [])


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

    result = catalogue._gmail_get_attachment(
        profile="represented-profile",
        message_id="message-1",
        attachment_id="attachment-1",
    )

    assert result["error_code"] == "authenticated_actor_context_required"
    assert gmail_called is False
