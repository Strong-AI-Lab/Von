"""Authenticated original image and archive citation views on the Von blueprint."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from html import escape

from flask import jsonify, request, send_file, Response


def register_image_routes(blueprint):
    from ...services.conversation_image_service import (
        load_image,
        store_image,
        MAX_IMAGE_BYTES,
    )
    from ...security.access_control import get_effective_user_concept_id

    @blueprint.route("/api/images/upload", methods=["POST"])
    def upload_conversation_image():
        actor = get_effective_user_concept_id()
        if not actor:
            return jsonify(error="authentication_required"), 401
        uploaded = request.files.get("file")
        if uploaded is None:
            return jsonify(error="missing_image"), 400
        try:
            descriptor = store_image(
                data=uploaded.read(MAX_IMAGE_BYTES + 1),
                filename=uploaded.filename or "image.png",
                user_concept_id=actor,
            )
            return (
                jsonify(success=True, uploaded=descriptor, image_attachment=descriptor),
                201,
            )
        except ValueError as exc:
            return jsonify(error="invalid_image", message=str(exc)), 400

    @blueprint.route("/api/images/<path:concept_id>/original", methods=["GET"])
    def conversation_image_original(concept_id):
        actor = get_effective_user_concept_id()
        if not actor:
            return jsonify(error="authentication_required"), 401
        try:
            info, data = load_image(concept_id, actor)
        except PermissionError:
            return jsonify(error="not_found"), 404
        except ValueError as exc:
            return jsonify(error="image_unavailable", message=str(exc)), 409
        response = send_file(
            io.BytesIO(data),
            mimetype=info["content_type"],
            as_attachment=request.args.get("download") == "1",
            download_name=info["filename"],
            max_age=0,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def archive_read(name, arguments):
        from ...integrations.internal_mcp.otter_archive_proxy_mcp import (
            otter_archive_resource_binding_for_user,
        )
        from ...integrations.internal_mcp.catalogue import build_default_catalogue
        from ...integrations.internal_mcp.gateway import InternalMCPGateway
        from ...integrations.internal_mcp.transport import InternalMCPTransport

        actor = get_effective_user_concept_id()
        binding = otter_archive_resource_binding_for_user(actor)
        if not binding:
            raise PermissionError("Source unavailable.")
        return (
            InternalMCPGateway(
                catalogue=build_default_catalogue(),
                transport=InternalMCPTransport(),
                enabled=True,
            )
            .invoke("otter_archive_" + name, {"resource_id": binding, **arguments})
            .payload
        )

    @blueprint.route("/api/otter-archive/artifacts/<artifact_id>", methods=["GET"])
    def archive_source(artifact_id):
        try:
            result = archive_read(
                "get_artifact",
                {
                    "artifact_id": artifact_id,
                    "offset": int(request.args.get("offset", 0)),
                },
            )
            if not result.get("success"):
                return jsonify(result), 404
            image = ""
            if result.get("kind") == "image":
                media = archive_read("get_screenshot", {"artifact_id": artifact_id})
                if not media.get("success"):
                    return jsonify(media), 409
                item = media["image_attachments"][0]
                image = f'<a href="{escape(item["url"])}"><img style="max-width:100%" src="{escape(item["url"])}" alt="Original source screenshot"></a>'
            sections = []
            for title, value in (
                ("Source text", result.get("text")),
                (
                    "OCR transcription (may contain errors)",
                    (result.get("ocr") or {}).get("text"),
                ),
            ):
                if value:
                    sections.append(
                        f"<h2>{escape(title)}</h2><pre>{escape(str(value))}</pre>"
                    )
            enrichment = result.get("enrichment") or {}
            if enrichment:
                sections.append(
                    "<h2>Cached visual interpretation</h2><p>Model inference; compare claims with the original image.</p>"
                    + f"<pre>{escape(json.dumps(enrichment.get('result'), ensure_ascii=False, indent=2))}</pre>"
                )
            next_offset = result.get("next_offset")
            if isinstance(next_offset, int):
                sections.append(
                    f'<p><a href="?offset={next_offset}">Read the next source-text segment</a></p>'
                )
            body = (
                "<!doctype html><meta charset=utf-8><title>Von archive source</title>"
                "<style>body{font-family:system-ui;max-width:1280px;margin:auto;padding:1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:100%}</style>"
                "<main>"
                f'<h1>{escape(str(result.get("title") or "Archive source"))}</h1>'
                f'<p>Source conversation: {escape(str(result.get("conversation_id") or "Unresolved association"))}</p>{image}'
                + "".join(sections)
                + f"<details><summary>Source identity and extraction provenance</summary><pre>{escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre></details></main>"
            )
            response = Response(body, mimetype="text/html")
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"
            )
            return response
        except PermissionError:
            return jsonify(error="not_found"), 404
        except ValueError:
            return jsonify(error="invalid_offset"), 400

    @blueprint.route(
        "/api/otter-archive/artifacts/<artifact_id>/chunk", methods=["GET"]
    )
    def archive_original_chunk(artifact_id):
        from ...integrations.internal_mcp.otter_archive_proxy_mcp import (
            otter_archive_resource_binding_for_user,
            get_otter_archive_proxy,
        )
        from ...integrations.internal_mcp.catalogue import _run_async_compat

        actor = get_effective_user_concept_id()
        binding = otter_archive_resource_binding_for_user(actor)
        if not binding:
            return jsonify(error="not_found"), 404
        try:
            offset = int(request.args.get("offset", 0))
            length = int(request.args.get("length", 1048576))
            if offset < 0 or not 1 <= length <= 4 * 1024 * 1024:
                raise ValueError("Invalid chunk bounds")

            async def read():
                proxy = await get_otter_archive_proxy(resource_id=binding)
                return await proxy.call(
                    "read_original_chunk",
                    {"artifact_id": artifact_id, "offset": offset, "length": length},
                )

            result = _run_async_compat(read)
            data = base64.b64decode(result["base64"], validate=True)
            response = send_file(
                io.BytesIO(data),
                mimetype="application/octet-stream",
                as_attachment=True,
                download_name=f"{artifact_id}-{offset}.chunk",
                max_age=0,
            )
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Chunk-SHA256"] = hashlib.sha256(data).hexdigest()
            return response
        except Exception:
            return jsonify(error="original_chunk_unavailable"), 409
