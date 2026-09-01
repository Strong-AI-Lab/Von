from __future__ import annotations

import sys
from types import SimpleNamespace

from scripts import verify_tesseract_ocr


def _completed(
    stdout: str,
    *,
    returncode: int = 0,
    stderr: str = "",
):
    return SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


def test_verifier_requires_exact_eng_and_runs_real_image_through_pytesseract(
    monkeypatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    smoke_call: dict[str, object] = {}
    fake_pytesseract = SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd=""),
    )

    def fake_run(executable: str, *arguments: str):
        commands.append((executable, *arguments))
        if arguments == ("--version",):
            return _completed("tesseract 5.3.4\n leptonica-1.82.0\n")
        return _completed(
            'List of available languages in "/usr/share/tesseract-ocr/5/tessdata/" (2):\n'
            "eng\n"
            "osd\n"
        )

    def fake_image_to_string(image, *, lang: str, config: str) -> str:
        smoke_call.update(image=image, lang=lang, config=config)
        return "VON OCR 42\n"

    fake_pytesseract.image_to_string = fake_image_to_string
    monkeypatch.setattr(verify_tesseract_ocr.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(verify_tesseract_ocr, "_run_tesseract", fake_run)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)

    result = verify_tesseract_ocr.verify_tesseract()

    assert result.available is True
    assert result.version == "5.3.4"
    assert commands == [
        ("/usr/bin/tesseract", "--version"),
        ("/usr/bin/tesseract", "--list-langs"),
    ]
    assert fake_pytesseract.pytesseract.tesseract_cmd == "/usr/bin/tesseract"
    assert smoke_call["lang"] == "eng"
    assert smoke_call["config"] == "--psm 7"
    assert smoke_call["image"].width > 200
    assert smoke_call["image"].height > 40


def test_similarly_named_language_does_not_satisfy_exact_eng(monkeypatch) -> None:
    def fake_run(_executable: str, *arguments: str):
        if arguments == ("--version",):
            return _completed("tesseract 5.3.4\n")
        return _completed("List of available languages (2):\nenm\nosd\n")

    monkeypatch.setattr(verify_tesseract_ocr.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(verify_tesseract_ocr, "_run_tesseract", fake_run)

    result = verify_tesseract_ocr.verify_tesseract()

    assert result.available is False
    assert result.reason == "eng_language_missing"
    assert "enm, osd" in result.detail


def test_cli_reports_stable_actionable_missing_executable_reason(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(verify_tesseract_ocr.shutil, "which", lambda _: None)

    exit_code = verify_tesseract_ocr.main([])

    assert exit_code == 1
    assert capsys.readouterr().out.splitlines() == [
        "VON_SETUP_CAPABILITY name=ocr state=unavailable reason=executable_not_found",
        "Tesseract OCR check failed (executable_not_found): executable 'tesseract' was not found on PATH",
    ]


def test_smoke_text_mismatch_is_a_typed_failure(monkeypatch) -> None:
    fake_pytesseract = SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd=""),
        image_to_string=lambda *_args, **_kwargs: "VON OGR 42\n",
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)

    passed, reason, detail = verify_tesseract_ocr._run_ocr_smoke("/usr/bin/tesseract")

    assert passed is False
    assert reason == "smoke_text_mismatch"
    assert detail == "expected 'VON OCR 42' but Tesseract recognised 'VON OGR 42'"
