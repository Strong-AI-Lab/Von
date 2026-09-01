"""Verify that Von's Tesseract-backed English OCR capability really works.

This script deliberately performs more than an executable-presence check.  It
checks the engine version, requires the exact ``eng`` trained-data language,
and sends a generated image through Pillow and pytesseract to Tesseract.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

SMOKE_TEXT = "VON OCR 42"
_TESSERACT_VERSION_RE = re.compile(r"^tesseract\s+v?(\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class VerificationResult:
    available: bool
    reason: str
    detail: str
    version: str = ""

    def machine_line(self) -> str:
        if self.available:
            return (
                "VON_SETUP_CAPABILITY name=ocr state=available "
                f"engine=tesseract version={self.version} language=eng smoke=passed"
            )
        return (
            "VON_SETUP_CAPABILITY name=ocr state=unavailable "
            f"reason={self.reason}"
        )

    def human_line(self) -> str:
        if self.available:
            return (
                f"Tesseract OCR check passed: version {self.version}; exact 'eng' "
                f"language data found; smoke recognised '{SMOKE_TEXT}'."
            )
        return f"Tesseract OCR check failed ({self.reason}): {self.detail}"


def _failure(reason: str, detail: str) -> VerificationResult:
    return VerificationResult(
        available=False,
        reason=reason,
        detail=_one_line(detail),
    )


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _command_failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    output = _one_line(completed.stderr or completed.stdout or "")
    if output:
        return f"command exited {completed.returncode}: {output}"
    return f"command exited {completed.returncode} without diagnostic output"


def _run_tesseract(
    executable: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _parse_version(output: str) -> str | None:
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    match = _TESSERACT_VERSION_RE.match(first_line)
    return match.group(1) if match is not None else None


def _parse_languages(output: str) -> set[str]:
    return {
        line.strip()
        for line in output.splitlines()
        if line.strip() and not line.lstrip().lower().startswith("list of available languages")
    }


def _render_smoke_image():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError as exc:
        raise RuntimeError(f"Python dependency is unavailable: {exc.name}") from exc

    font = ImageFont.load_default()
    probe = Image.new("L", (1, 1), color=255)
    bounds = ImageDraw.Draw(probe).textbbox((0, 0), SMOKE_TEXT, font=font)
    margin = 5
    width = bounds[2] - bounds[0] + (2 * margin)
    height = bounds[3] - bounds[1] + (2 * margin)
    image = Image.new("L", (width, height), color=255)
    ImageDraw.Draw(image).text(
        (margin - bounds[0], margin - bounds[1]),
        SMOKE_TEXT,
        fill=0,
        font=font,
    )

    # Pillow 8 exposes LANCZOS directly on Image; newer versions also expose
    # it under Image.Resampling.  Enlarge the embedded default font so this
    # check does not depend on platform font packages.
    resampling = getattr(Image, "Resampling", Image)
    return image.resize((width * 5, height * 5), resample=resampling.LANCZOS)


def _run_ocr_smoke(executable: str) -> tuple[bool, str, str]:
    try:
        import pytesseract
    except ModuleNotFoundError as exc:
        return (
            False,
            "python_dependency_missing",
            f"Python dependency is unavailable: {exc.name}",
        )

    try:
        image = _render_smoke_image()
    except RuntimeError as exc:
        return False, "python_dependency_missing", str(exc)

    try:
        pytesseract.pytesseract.tesseract_cmd = executable
        recognised = pytesseract.image_to_string(
            image,
            lang="eng",
            config="--psm 7",
        )
    except (OSError, RuntimeError) as exc:
        return (
            False,
            "smoke_execution_failed",
            f"pytesseract smoke raised {type(exc).__name__}: {exc}",
        )

    normalised = " ".join(re.findall(r"[A-Z0-9]+", recognised.upper()))
    if normalised != SMOKE_TEXT:
        actual = normalised or "<empty>"
        return (
            False,
            "smoke_text_mismatch",
            f"expected '{SMOKE_TEXT}' but Tesseract recognised '{actual}'",
        )
    return True, "", ""


def verify_tesseract(command: str = "tesseract") -> VerificationResult:
    executable = shutil.which(command)
    if executable is None:
        return _failure(
            "executable_not_found",
            f"executable '{command}' was not found on PATH",
        )

    try:
        version_check = _run_tesseract(executable, "--version")
    except (OSError, subprocess.SubprocessError) as exc:
        return _failure(
            "version_check_failed",
            f"could not run '{executable} --version': {exc}",
        )
    if version_check.returncode != 0:
        return _failure("version_check_failed", _command_failure_detail(version_check))

    version = _parse_version(version_check.stdout or version_check.stderr)
    if version is None:
        return _failure(
            "version_unrecognised",
            "'tesseract --version' did not report a recognisable Tesseract version",
        )

    try:
        language_check = _run_tesseract(executable, "--list-langs")
    except (OSError, subprocess.SubprocessError) as exc:
        return _failure(
            "language_check_failed",
            f"could not run '{executable} --list-langs': {exc}",
        )
    if language_check.returncode != 0:
        return _failure("language_check_failed", _command_failure_detail(language_check))

    languages = _parse_languages(language_check.stdout)
    if "eng" not in languages:
        available = ", ".join(sorted(languages)) or "none"
        return _failure(
            "eng_language_missing",
            f"exact language 'eng' is unavailable; reported languages: {available}",
        )

    smoke_passed, reason, detail = _run_ocr_smoke(executable)
    if not smoke_passed:
        return _failure(reason, detail)

    return VerificationResult(
        available=True,
        reason="",
        detail="",
        version=version,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tesseract-command",
        default="tesseract",
        help="Tesseract command or path to verify. Default: tesseract",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the stable machine-readable capability line.",
    )
    args = parser.parse_args(argv)

    result = verify_tesseract(args.tesseract_command)
    print(result.machine_line())
    if not args.quiet:
        print(result.human_line())
    return 0 if result.available else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
