"""Helpers for interpreting uploaded file-copy content (JVNAUTOSCI-1302).

This module keeps image/document interpretation logic out of MCP handlers so
the same behaviour can be reused by workflow-driven ingestion paths.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def is_image_file(*, content_type: str | None, filename: str | None) -> bool:
    content_type_clean = _normalise_optional_text(content_type)
    if isinstance(content_type_clean, str) and content_type_clean.lower().startswith(
        "image/"
    ):
        return True

    filename_clean = _normalise_optional_text(filename)
    if not filename_clean:
        return False
    lowered = filename_clean.lower()
    return lowered.endswith(
        (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
            ".heic",
            ".heif",
        )
    )


def extract_image_metadata(data_bytes: bytes) -> dict[str, Any]:
    """Return lightweight technical metadata for an image byte payload."""

    try:
        from PIL import Image  # type: ignore[import-not-found]
    except Exception as exc:
        return {"available": False, "error": f"pillow_unavailable:{exc}"}

    try:
        with Image.open(io.BytesIO(bytes(data_bytes))) as image:
            image_for_palette = image.convert("RGB")
            # Quantise to keep palette extraction fast and deterministic.
            palette_image = image_for_palette.quantize(colors=8, method=2)
            palette_data: list[int] = []
            for pixel in palette_image.getdata():
                if isinstance(pixel, (int, float)):
                    palette_data.append(int(pixel))
            palette_counts = Counter(palette_data)
            palette = palette_image.getpalette() or []
            dominant_colours: list[list[int]] = []
            for index_raw, _count in palette_counts.most_common(3):
                if not isinstance(index_raw, (int, float)):
                    continue
                base = int(index_raw) * 3
                if base + 2 < len(palette):
                    dominant_colours.append(
                        [int(palette[base]), int(palette[base + 1]), int(palette[base + 2])]
                    )

            exif_available = False
            exif_tag_count = 0
            try:
                exif_payload = image.getexif()
                if exif_payload:
                    exif_available = True
                    exif_tag_count = len(exif_payload)
            except Exception:
                exif_available = False
                exif_tag_count = 0

            return {
                "available": True,
                "width": int(image.width),
                "height": int(image.height),
                "mode": str(image.mode),
                "format": str(image.format or "").upper() or None,
                "dominant_colours_rgb": dominant_colours,
                "exif_available": exif_available,
                "exif_tag_count": int(exif_tag_count),
            }
    except Exception as exc:
        return {"available": False, "error": f"image_metadata_failed:{exc}"}


def extract_image_ocr_text(data_bytes: bytes) -> dict[str, Any]:
    """Extract OCR text from image bytes using pytesseract when available."""

    try:
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except Exception as exc:
        return {
            "text": None,
            "method": "image_ocr_unavailable",
            "error": str(exc),
        }

    try:
        with Image.open(io.BytesIO(bytes(data_bytes))) as image:
            text = pytesseract.image_to_string(image).strip()
        return {
            "text": text or None,
            "method": "image_ocr",
            "error": None,
        }
    except Exception as exc:
        return {
            "text": None,
            "method": "image_ocr_failed",
            "error": str(exc),
        }


def _resolve_active_provider_and_model(
    *,
    model_override: str | None,
) -> tuple[str | None, str | None]:
    """Resolve active provider/model from settings with optional override."""

    try:
        from .settings_service import resolve_llm_setting
        from ..languagemodels.llm_interface import (
            resolve_openai_model_name,
            resolve_provider_from_model_concept,
        )

        active = resolve_llm_setting()
    except Exception:
        active = None
        resolve_openai_model_name = None  # type: ignore[assignment]
        resolve_provider_from_model_concept = None  # type: ignore[assignment]

    provider: str | None = None
    model: str | None = None
    if isinstance(active, dict):
        provider_raw = active.get("provider")
        if isinstance(provider_raw, str) and provider_raw.strip():
            provider = provider_raw.strip().lower()
        model_raw = active.get("model")
        if isinstance(model_raw, str) and model_raw.strip():
            model = model_raw.strip()
            if model.startswith("#V#") and callable(resolve_provider_from_model_concept):
                resolved = resolve_provider_from_model_concept(model)
                if isinstance(resolved, str) and resolved.strip():
                    provider = resolved.strip().lower()
            if provider == "openai" and callable(resolve_openai_model_name):
                resolved_model = resolve_openai_model_name(model)
                if isinstance(resolved_model, str) and resolved_model.strip():
                    model = resolved_model.strip()

    if isinstance(model_override, str) and model_override.strip():
        model = model_override.strip()

    return provider, model


def _describe_image_with_openai(
    *,
    data_bytes: bytes,
    content_type: str | None,
    model: str | None,
    prompt: str,
) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except Exception as exc:
        return {
            "description": None,
            "method": "openai_vision_unavailable",
            "error": str(exc),
            "provider": "openai",
            "model": model,
        }

    api_key: str | None = None
    try:
        from .settings_service import get_openai_env_var

        env_var = get_openai_env_var()
        if isinstance(env_var, str) and env_var.strip():
            api_key = os.getenv(env_var.strip())
    except Exception:
        api_key = None

    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {
            "description": None,
            "method": "openai_vision_unavailable",
            "error": "missing_openai_api_key",
            "provider": "openai",
            "model": model,
        }

    target_model = model or "gpt-4.1-mini"
    mime = _normalise_optional_text(content_type) or "image/png"
    image_b64 = base64.b64encode(bytes(data_bytes)).decode("ascii")
    data_url = f"data:{mime};base64,{image_b64}"

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=target_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                }
            ],
            max_tokens=500,
            temperature=0.2,
        )
        content = response.choices[0].message.content
        description = content.strip() if isinstance(content, str) else None
        if not description:
            return {
                "description": None,
                "method": "openai_vision_failed",
                "error": "empty_response",
                "provider": "openai",
                "model": target_model,
            }
        return {
            "description": description,
            "method": "openai_vision",
            "error": None,
            "provider": "openai",
            "model": target_model,
        }
    except Exception as exc:
        return {
            "description": None,
            "method": "openai_vision_failed",
            "error": str(exc),
            "provider": "openai",
            "model": target_model,
        }


def _describe_image_with_gemini(
    *,
    data_bytes: bytes,
    content_type: str | None,
    model: str | None,
    prompt: str,
) -> dict[str, Any]:
    try:
        from google import genai  # type: ignore
    except Exception as exc:
        return {
            "description": None,
            "method": "gemini_vision_unavailable",
            "error": str(exc),
            "provider": "gemini",
            "model": model,
        }

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {
            "description": None,
            "method": "gemini_vision_unavailable",
            "error": "missing_gemini_api_key",
            "provider": "gemini",
            "model": model,
        }

    target_model = model or "gemini-2.0-flash"
    mime = _normalise_optional_text(content_type) or "image/png"
    try:
        genai.configure(api_key=api_key)  # type: ignore[attr-defined]
        model_instance = genai.GenerativeModel(target_model)  # type: ignore[attr-defined]
        response = model_instance.generate_content(
            [prompt, {"mime_type": mime, "data": bytes(data_bytes)}]
        )
        response_text = getattr(response, "text", None)
        description = response_text.strip() if isinstance(response_text, str) else None
        if not description:
            return {
                "description": None,
                "method": "gemini_vision_failed",
                "error": "empty_response",
                "provider": "gemini",
                "model": target_model,
            }
        return {
            "description": description,
            "method": "gemini_vision",
            "error": None,
            "provider": "gemini",
            "model": target_model,
        }
    except Exception as exc:
        return {
            "description": None,
            "method": "gemini_vision_failed",
            "error": str(exc),
            "provider": "gemini",
            "model": target_model,
        }


def describe_image_semantics(
    *,
    data_bytes: bytes,
    content_type: str | None,
    model_override: str | None = None,
    prompt_override: str | None = None,
) -> dict[str, Any]:
    """Describe image semantics using the active multimodal-capable provider."""

    provider, model = _resolve_active_provider_and_model(model_override=model_override)
    prompt = (
        _normalise_optional_text(prompt_override)
        or (
            "Describe this image in 2-5 concise sentences using New Zealand English. "
            "Include salient entities, likely setting, visible text, and whether people "
            "or buildings are present. Do not identify real people."
        )
    )

    if provider == "openai":
        return _describe_image_with_openai(
            data_bytes=data_bytes,
            content_type=content_type,
            model=model,
            prompt=prompt,
        )
    if provider == "gemini":
        return _describe_image_with_gemini(
            data_bytes=data_bytes,
            content_type=content_type,
            model=model,
            prompt=prompt,
        )

    return {
        "description": None,
        "method": "semantic_vision_not_configured",
        "error": (
            f"active_provider_not_supported:{provider}"
            if provider
            else "active_provider_not_configured"
        ),
        "provider": provider,
        "model": model,
    }


def _infer_subject_tags(*, semantic_description: str | None, ocr_text: str | None) -> list[str]:
    haystack_parts = []
    if isinstance(semantic_description, str) and semantic_description.strip():
        haystack_parts.append(semantic_description.strip().lower())
    if isinstance(ocr_text, str) and ocr_text.strip():
        haystack_parts.append(ocr_text.strip().lower())
    haystack = "\n".join(haystack_parts)
    if not haystack:
        return []

    tags: list[str] = []
    if re.search(r"\b(face|person|portrait|selfie|people)\b", haystack):
        tags.append("face_or_person")
    if re.search(
        r"\b(building|architecture|office|tower|house|campus|street|facade|fa[cç]ade)\b",
        haystack,
    ):
        tags.append("building_or_structure")
    if re.search(r"\b(screenshot|ui|interface|menu|window|table|form)\b", haystack):
        tags.append("screenshot_or_interface")
    if len(re.findall(r"[A-Za-z0-9]", haystack)) > 80:
        tags.append("text_heavy")
    return tags


def build_image_interpretation(
    *,
    data_bytes: bytes,
    content_type: str | None,
    original_filename: str | None,
    include_semantic_description: bool,
    model_override: str | None = None,
    prompt_override: str | None = None,
) -> dict[str, Any]:
    metadata = extract_image_metadata(data_bytes)
    ocr = extract_image_ocr_text(data_bytes)
    semantic = (
        describe_image_semantics(
            data_bytes=data_bytes,
            content_type=content_type,
            model_override=model_override,
            prompt_override=prompt_override,
        )
        if include_semantic_description
        else {
            "description": None,
            "method": "semantic_disabled",
            "error": None,
            "provider": None,
            "model": model_override,
        }
    )

    ocr_text = _normalise_optional_text(ocr.get("text"))
    semantic_description = _normalise_optional_text(semantic.get("description"))

    width = metadata.get("width")
    height = metadata.get("height")
    format_name = metadata.get("format")
    metadata_bits = []
    if isinstance(width, int) and isinstance(height, int):
        metadata_bits.append(f"{width}x{height}")
    if isinstance(format_name, str) and format_name.strip():
        metadata_bits.append(format_name.strip())
    metadata_suffix = f" ({', '.join(metadata_bits)})" if metadata_bits else ""

    if semantic_description:
        description = semantic_description
    elif ocr_text:
        preview = ocr_text[:220] + ("..." if len(ocr_text) > 220 else "")
        description = f"Image with readable text{metadata_suffix}: {preview}"
    else:
        filename_clean = _normalise_optional_text(original_filename) or "uploaded image"
        description = f"{filename_clean.capitalize()}{metadata_suffix}"

    subject_tags = _infer_subject_tags(
        semantic_description=semantic_description,
        ocr_text=ocr_text,
    )

    return {
        "kind": "image",
        "interpreted_at": _utc_now_iso(),
        "description": description,
        "subject_tags": subject_tags,
        "content_text": ocr_text,
        "content_length": len(ocr_text) if isinstance(ocr_text, str) else 0,
        "image_metadata": metadata,
        "ocr": ocr,
        "semantic": semantic,
    }


def build_document_interpretation(
    *,
    extracted_text: str | None,
    content_type: str | None,
    original_filename: str | None,
) -> dict[str, Any]:
    text = _normalise_optional_text(extracted_text)
    if text:
        preview = text[:260] + ("..." if len(text) > 260 else "")
        description = f"Document text extracted: {preview}"
    else:
        filename_clean = _normalise_optional_text(original_filename) or "uploaded file"
        content_type_clean = _normalise_optional_text(content_type)
        if content_type_clean:
            description = f"{filename_clean} ({content_type_clean}) uploaded"
        else:
            description = f"{filename_clean} uploaded"

    return {
        "kind": "document",
        "interpreted_at": _utc_now_iso(),
        "description": description,
        "subject_tags": ["document"],
        "content_text": text,
        "content_length": len(text) if isinstance(text, str) else 0,
    }
