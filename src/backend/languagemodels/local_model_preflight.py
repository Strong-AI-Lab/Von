"""Preflight capacity check for locally-hosted (e.g. Ollama) models.

JVNAUTOSCI-2384.

This module is a *reusable support surface*, not workflow/prompt policy.  It
gives stage/workflow execution a way to ask, before a local model is loaded,
"does this model plausibly fit the current host's memory?".  When the answer is
a confident "no", callers fail closed for that stage with a clear, structured
reason instead of letting the local runtime crash under memory pressure and
surface an opaque ``plain_response_failed``.

Design notes
------------
* Host capacity is detected from the OS via :mod:`psutil` (cross-platform; on
  Apple Silicon ``virtual_memory().total`` is the unified-memory figure).  No
  platform-specific shelling out.
* Model footprint is estimated primarily from the local runtime's own metadata
  (the on-disk weight size reported by the Ollama API).  Only when that metadata
  is unavailable do we fall back to a minimal, clearly-temporary parameter-count
  heuristic parsed from the model tag.  We deliberately do **not** maintain a
  per-model size table as durable code-side policy.
* The check never substitutes a different model.  When it judges a model too
  large it raises :class:`ModelTooLargeForHostError`; the caller decides how to
  surface that, and there is no silent fallback.
* Because a footprint estimate can be wrong, we only *block* when we are
  confident.  Unknown footprints never block.  Authoritative blocking comes from
  runtime metadata; the parameter-count heuristic is non-authoritative and, by
  default, only *records* that a model would not fit rather than hard-failing the
  load (operators can opt in to strict heuristic blocking via
  ``VON_LOCAL_MODEL_PREFLIGHT_BLOCK_ON_HEURISTIC``).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import threading
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

_GIB = 1024**3


# ---------------------------------------------------------------------------
# Configuration (operator-tunable via environment; no host-specific hacks)
# ---------------------------------------------------------------------------
def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, float(default))
    try:
        return max(minimum, float(str(raw).strip()))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def is_preflight_enabled() -> bool:
    """Whether the local-model preflight check is active."""

    return _bool_env("VON_LOCAL_MODEL_PREFLIGHT_ENABLED", True)


def _usable_memory_fraction() -> float:
    # Reserve headroom for the OS and other resident processes.  A model whose
    # estimated footprint exceeds this fraction of total physical memory cannot
    # realistically run on the host.
    return min(1.0, _float_env("VON_LOCAL_MODEL_PREFLIGHT_USABLE_FRACTION", 0.9))


def _heuristic_block_margin() -> float:
    # Low-confidence (heuristic) footprint estimates must exceed the usable
    # capacity by this factor before we are willing to block, to avoid blocking
    # a legitimately-runnable model on a rough guess.
    return max(1.0, _float_env("VON_LOCAL_MODEL_PREFLIGHT_HEURISTIC_MARGIN", 1.25))


def _metadata_overhead_factor() -> float:
    # Runtime footprint is somewhat larger than the on-disk weight size (KV
    # cache, activations).  Apply a modest overhead to the reported file size.
    return max(1.0, _float_env("VON_LOCAL_MODEL_PREFLIGHT_METADATA_OVERHEAD", 1.15))


def _heuristic_bytes_per_billion_params() -> float:
    # Temporary fallback only, used when no runtime metadata is available.
    # Roughly models a quantised local weight footprint per billion parameters.
    return _float_env(
        "VON_LOCAL_MODEL_PREFLIGHT_BYTES_PER_BILLION_PARAMS",
        float(int(1.3 * _GIB)),
        minimum=1.0,
    )


def _block_on_heuristic() -> bool:
    # The parameter-count heuristic is a *non-authoritative* fallback: a model
    # tag like "70b" tells us nothing about quantisation, so the estimate can be
    # far off in either direction.  By default we therefore never hard-fail a
    # load on the heuristic alone (we only record the concern); authoritative
    # blocking comes from runtime metadata.  Operators who want the stricter
    # behaviour can opt in.
    return _bool_env("VON_LOCAL_MODEL_PREFLIGHT_BLOCK_ON_HEURISTIC", False)


# ---------------------------------------------------------------------------
# Stage context (so a verdict produced deep in the client can name the stage)
# ---------------------------------------------------------------------------
_current_llm_stage: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "von_current_llm_stage", default=None
)


def get_current_llm_stage() -> Optional[str]:
    return _current_llm_stage.get()


@contextlib.contextmanager
def llm_stage(stage: Optional[str]) -> Iterator[None]:
    """Mark the workflow stage requesting an LLM call, for preflight telemetry."""

    token = _current_llm_stage.set(stage)
    try:
        yield
    finally:
        _current_llm_stage.reset(token)


# ---------------------------------------------------------------------------
# Structured results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HostCapacity:
    """Detected host memory capacity."""

    total_bytes: Optional[int]
    available_bytes: Optional[int]
    source: str

    @property
    def usable_bytes(self) -> Optional[int]:
        if self.total_bytes is None:
            return None
        return int(self.total_bytes * _usable_memory_fraction())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "total_bytes": self.total_bytes,
            "available_bytes": self.available_bytes,
            "usable_bytes": self.usable_bytes,
            "source": self.source,
        }


@dataclass(frozen=True)
class ModelFootprint:
    """Estimated memory footprint of a local model."""

    model: str
    estimated_bytes: Optional[int]
    # "metadata" = derived from the runtime's reported weights; "heuristic" =
    # parsed from the model tag; "unknown" = could not be estimated.
    confidence: str
    source: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "estimated_bytes": self.estimated_bytes,
            "confidence": self.confidence,
            "source": self.source,
        }


@dataclass(frozen=True)
class PreflightVerdict:
    """Outcome of a local-model preflight capacity check."""

    model: str
    fits: bool
    stage: Optional[str]
    footprint: ModelFootprint
    capacity: HostCapacity
    reason: str
    checked: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_telemetry(self) -> Dict[str, Any]:
        return {
            "type": "local_model_preflight",
            "model": self.model,
            "fits": self.fits,
            "checked": self.checked,
            "stage": self.stage,
            "reason": self.reason,
            "footprint": self.footprint.as_dict(),
            "capacity": self.capacity.as_dict(),
            "details": dict(self.details),
        }


class ModelTooLargeForHostError(RuntimeError):
    """Raised when a local model is judged too large for the current host.

    The string form is a clean, user-presentable sentence; ``verdict`` carries
    the structured fields for telemetry.
    """

    def __init__(self, verdict: PreflightVerdict, message: Optional[str] = None):
        self.verdict = verdict
        super().__init__(message or build_user_message(verdict))


# ---------------------------------------------------------------------------
# Host capacity detection
# ---------------------------------------------------------------------------
_CAPACITY_CACHE_TTL_SECONDS = 30.0
_capacity_cache: Optional[Tuple[float, HostCapacity]] = None
_capacity_lock = threading.Lock()


def detect_host_capacity(*, use_cache: bool = True) -> HostCapacity:
    """Detect total/available host memory via psutil (cross-platform)."""

    global _capacity_cache
    now = time.monotonic()
    if use_cache and _capacity_cache is not None:
        cached_at, cached = _capacity_cache
        if now - cached_at < _CAPACITY_CACHE_TTL_SECONDS:
            return cached

    capacity = _detect_host_capacity_uncached()
    if use_cache:
        with _capacity_lock:
            _capacity_cache = (now, capacity)
    return capacity


def _detect_host_capacity_uncached() -> HostCapacity:
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        return HostCapacity(
            total_bytes=int(getattr(vm, "total", 0)) or None,
            available_bytes=int(getattr(vm, "available", 0)) or None,
            source="psutil.virtual_memory",
        )
    except Exception as exc:  # pragma: no cover - psutil failure is rare
        logger.debug("Host capacity detection via psutil failed: %s", exc)
        return HostCapacity(
            total_bytes=None, available_bytes=None, source="unavailable"
        )


# ---------------------------------------------------------------------------
# Model footprint estimation
# ---------------------------------------------------------------------------
_FOOTPRINT_CACHE_TTL_SECONDS = 300.0
_footprint_cache: Dict[str, Tuple[float, ModelFootprint]] = {}
_footprint_lock = threading.Lock()

# Matches a parameter-count token in a model tag, e.g. "31b", "9b", "3.8b",
# "70B", "e4b" (gemma effective-parameter naming).
_PARAM_TOKEN_RE = re.compile(
    r"(?:^|[:\-/_ ])e?(\d+(?:\.\d+)?)\s*b(?:\b|[:\-/_ ])", re.IGNORECASE
)


def estimate_model_footprint(
    model: str,
    *,
    ollama_client: Any = None,
    host: Optional[str] = None,
    use_cache: bool = True,
) -> ModelFootprint:
    """Estimate the memory footprint of a local model.

    Primary source is the runtime's own metadata (Ollama on-disk weight size);
    fallback is a minimal parameter-count heuristic parsed from the model tag.
    """

    normalised_model = (model or "").strip()
    if not normalised_model:
        return ModelFootprint(
            model=model,
            estimated_bytes=None,
            confidence="unknown",
            source="empty_model",
        )

    cache_key = f"{host or ''}::{normalised_model}"
    now = time.monotonic()
    if use_cache:
        cached = _footprint_cache.get(cache_key)
        if cached is not None and now - cached[0] < _FOOTPRINT_CACHE_TTL_SECONDS:
            return cached[1]

    try:
        footprint = _estimate_metadata_footprint(normalised_model, ollama_client)
    except Exception as exc:  # never let estimation break the caller's load path
        logger.debug("Metadata footprint estimate failed for %s: %s", model, exc)
        footprint = None
    if footprint is None:
        footprint = _estimate_heuristic_footprint(normalised_model)

    if use_cache and footprint.confidence != "unknown":
        with _footprint_lock:
            _footprint_cache[cache_key] = (now, footprint)
    return footprint


def _coerce_size_bytes(value: Any) -> Optional[int]:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _model_names_match(candidate: Any, target: str) -> bool:
    if not isinstance(candidate, str):
        return False
    candidate = candidate.strip()
    if not candidate:
        return False
    if candidate == target:
        return True
    # Ollama treats an absent tag as ":latest".
    return candidate == f"{target}:latest" or f"{candidate}:latest" == target


def _iter_model_entries(payload: Any) -> Iterator[Mapping[str, Any]]:
    models = None
    if isinstance(payload, Mapping):
        models = payload.get("models")
    else:
        models = getattr(payload, "models", None)
    if not models:
        return
    for entry in models:
        if isinstance(entry, Mapping):
            yield entry
        else:
            data = getattr(entry, "model_dump", None)
            if callable(data):
                try:
                    dumped = data()
                except Exception:
                    dumped = None
                if isinstance(dumped, Mapping):
                    yield dumped
                    continue
            yield {
                "name": getattr(entry, "name", None) or getattr(entry, "model", None),
                "model": getattr(entry, "model", None),
                "size": getattr(entry, "size", None),
                "size_vram": getattr(entry, "size_vram", None),
            }


def _size_from_model_listing(payload: Any, target: str) -> Optional[int]:
    for entry in _iter_model_entries(payload):
        name = entry.get("name") or entry.get("model")
        if _model_names_match(name, target):
            return _coerce_size_bytes(entry.get("size_vram") or entry.get("size"))
    return None


def _estimate_metadata_footprint(
    model: str, ollama_client: Any
) -> Optional[ModelFootprint]:
    if ollama_client is None:
        return None

    overhead = _metadata_overhead_factor()

    # Prefer a model that is already resident (ps) since size_vram is the true
    # in-memory footprint; then fall back to the on-disk listing.
    for method_name, source in (("ps", "ollama.ps"), ("list", "ollama.list")):
        method = getattr(ollama_client, method_name, None)
        if not callable(method):
            continue
        try:
            payload = method()
        except Exception as exc:
            logger.debug(
                "Ollama %s() failed during footprint estimate: %s", method_name, exc
            )
            continue
        size = _size_from_model_listing(payload, model)
        if size is not None:
            estimated = size if source == "ollama.ps" else int(size * overhead)
            return ModelFootprint(
                model=model,
                estimated_bytes=estimated,
                confidence="metadata",
                source=source,
            )

    # show() may expose a parameter_size string we can convert.
    show = getattr(ollama_client, "show", None)
    if callable(show):
        try:
            info = show(model)
        except Exception as exc:
            logger.debug("Ollama show() failed during footprint estimate: %s", exc)
            info = None
        params = _parameter_count_from_show(info)
        if params is not None:
            return ModelFootprint(
                model=model,
                estimated_bytes=int(
                    params / 1e9 * _heuristic_bytes_per_billion_params()
                ),
                confidence="metadata",
                source="ollama.show.parameter_size",
            )
    return None


def _parameter_count_from_show(info: Any) -> Optional[float]:
    details: Any = None
    if isinstance(info, Mapping):
        details = info.get("details")
    else:
        details = getattr(info, "details", None)
    parameter_size = None
    if isinstance(details, Mapping):
        parameter_size = details.get("parameter_size")
    elif details is not None:
        parameter_size = getattr(details, "parameter_size", None)
    if not isinstance(parameter_size, str):
        return None
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*([BMK])", parameter_size.strip(), re.IGNORECASE
    )
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    multiplier = {"K": 1e3, "M": 1e6, "B": 1e9}.get(unit, 1.0)
    return value * multiplier


def _estimate_heuristic_footprint(model: str) -> ModelFootprint:
    match = _PARAM_TOKEN_RE.search(model)
    if not match:
        return ModelFootprint(
            model=model,
            estimated_bytes=None,
            confidence="unknown",
            source="no_param_token",
        )
    billions = float(match.group(1))
    estimated = int(billions * _heuristic_bytes_per_billion_params())
    return ModelFootprint(
        model=model,
        estimated_bytes=estimated,
        confidence="heuristic",
        source="model_tag_param_count",
    )


# ---------------------------------------------------------------------------
# The preflight check
# ---------------------------------------------------------------------------
def preflight_local_model_fits(
    model: str,
    *,
    stage: Optional[str] = None,
    ollama_client: Any = None,
    host: Optional[str] = None,
    capacity: Optional[HostCapacity] = None,
    footprint: Optional[ModelFootprint] = None,
) -> PreflightVerdict:
    """Judge whether ``model`` plausibly fits the current host.

    Returns a :class:`PreflightVerdict`.  The check is conservative: it only
    reports ``fits=False`` when it has a confident footprint estimate that
    clearly exceeds usable host capacity.
    """

    stage = stage if stage is not None else get_current_llm_stage()

    if not is_preflight_enabled():
        return PreflightVerdict(
            model=model,
            fits=True,
            stage=stage,
            footprint=footprint
            or ModelFootprint(model, None, "unknown", "preflight_disabled"),
            capacity=capacity or HostCapacity(None, None, "preflight_disabled"),
            reason="preflight_disabled",
            checked=False,
        )

    capacity = capacity or detect_host_capacity()
    footprint = footprint or estimate_model_footprint(
        model, ollama_client=ollama_client, host=host
    )

    usable = capacity.usable_bytes
    estimated = footprint.estimated_bytes

    if estimated is None or usable is None:
        return PreflightVerdict(
            model=model,
            fits=True,
            stage=stage,
            footprint=footprint,
            capacity=capacity,
            reason="insufficient_information",
            checked=True,
            details={
                "footprint_known": estimated is not None,
                "capacity_known": usable is not None,
            },
        )

    is_heuristic = footprint.confidence == "heuristic"
    margin = _heuristic_block_margin() if is_heuristic else 1.0
    block_threshold = int(usable * margin)
    exceeds = estimated > block_threshold

    # The heuristic is non-authoritative: by default it cannot hard-fail a load,
    # only record that it would have.  Metadata-derived estimates block by
    # default.
    heuristic_advisory_only = is_heuristic and not _block_on_heuristic()
    fits = (not exceeds) or heuristic_advisory_only

    if fits and exceeds and heuristic_advisory_only:
        reason = "heuristic_estimate_exceeds_host_not_authoritative"
    elif fits:
        reason = "fits"
    else:
        reason = "model_estimated_too_large_for_host"

    return PreflightVerdict(
        model=model,
        fits=fits,
        stage=stage,
        footprint=footprint,
        capacity=capacity,
        reason=reason,
        checked=True,
        details={
            "block_threshold_bytes": block_threshold,
            "confidence_margin": margin,
            "estimate_exceeds_host": exceeds,
            "heuristic_advisory_only": heuristic_advisory_only,
        },
    )


def _format_gib(num_bytes: Optional[int]) -> str:
    if not num_bytes or num_bytes <= 0:
        return "unknown"
    return f"{num_bytes / _GIB:.1f} GB"


def build_user_message(verdict: PreflightVerdict) -> str:
    """Build a clean, user-presentable reason from a preflight verdict.

    The structured inputs (model, footprint, capacity, stage) come from the
    verdict; this is a diagnostic message rather than authored task policy.
    """

    stage_suffix = f" requested by stage '{verdict.stage}'" if verdict.stage else ""
    return (
        f"The local model '{verdict.model}'{stage_suffix} is too large to run on "
        f"this host: its estimated memory footprint is "
        f"{_format_gib(verdict.footprint.estimated_bytes)}, but the host only has "
        f"about {_format_gib(verdict.capacity.total_bytes)} of memory "
        f"({_format_gib(verdict.capacity.usable_bytes)} usable). Choose a smaller "
        f"local model or a hosted provider for this stage."
    )


def record_preflight_warning(verdict: PreflightVerdict) -> None:
    """Emit a typed warning for telemetry/logging when a model is too large."""

    payload = verdict.as_telemetry()
    logger.warning("local_model_preflight blocked model: %s", payload)
    try:
        warnings.warn(build_user_message(verdict), RuntimeWarning, stacklevel=2)
    except Exception:  # pragma: no cover - warning machinery should not break flow
        pass


def enforce_local_model_preflight(
    model: str,
    *,
    stage: Optional[str] = None,
    ollama_client: Any = None,
    host: Optional[str] = None,
) -> PreflightVerdict:
    """Run the preflight and raise :class:`ModelTooLargeForHostError` if too big.

    Returns the verdict when the model is acceptable (or the check could not be
    performed); never silently substitutes a different model.
    """

    verdict = preflight_local_model_fits(
        model, stage=stage, ollama_client=ollama_client, host=host
    )
    if verdict.checked and not verdict.fits:
        record_preflight_warning(verdict)
        raise ModelTooLargeForHostError(verdict)
    return verdict
