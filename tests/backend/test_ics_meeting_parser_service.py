from __future__ import annotations

import pytest

from src.backend.services.ics_meeting_parser_service import (
    DEFAULT_ICS_MAX_CHARS,
    IcsMeetingParseOutcome,
    parse_ics_meeting,
)


def _calendar(*event_lines: str) -> str:
    return "\r\n".join(
        (
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Von//Meeting parser tests//EN",
            "BEGIN:VEVENT",
            *event_lines,
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        )
    )


def test_parse_ics_meeting_unfolds_and_extracts_full_utc_event() -> None:
    text = _calendar(
        r"UID:london-rhul-123@example.org",
        r"SUMMARY:London RHUL meeting\, planning\; review\\follow-up",
        "DTSTART:20260812T090000Z",
        "DTEND;VALUE=DATE-TIME:20260812T103000Z",
        'ORGANIZER;CN="Dr Alice Smith":mailto:alice%2Bmeetings@example.org',
        'ATTENDEE;CN="Doe, Bob":MAILTO:bob@example.org',
        "ATTENDEE;CN=Carol^'CJ^' Jones:mailto:carol@example.org?subject=Meeting",
        r"LOCATION:Room 2\, Main Building",
        r"DESCRIPTION:First line\nSecond line with a long ",
        r" folded\; detail",
        "URL:https://example.org/meetings/london-rhul",
        "STATUS:confirmed",
        "BEGIN:VALARM",
        "DESCRIPTION:This nested alarm description must be ignored",
        "END:VALARM",
    )

    result = parse_ics_meeting(text)

    assert result.outcome is IcsMeetingParseOutcome.PARSED
    assert result.parsed is True
    assert result.event_count == 1
    assert result.error_code is None
    assert result.meeting is not None
    meeting = result.meeting
    assert meeting.uid == "london-rhul-123@example.org"
    assert meeting.summary == "London RHUL meeting, planning; review\\follow-up"
    assert meeting.dtstart is not None
    assert meeting.dtstart.to_payload() == {
        "raw_value": "20260812T090000Z",
        "iso_value": "2026-08-12T09:00:00Z",
        "value_type": "date_time",
        "tzid": None,
        "is_utc": True,
    }
    assert meeting.dtend is not None
    assert meeting.dtend.iso_value == "2026-08-12T10:30:00Z"
    assert meeting.organizer is not None
    assert meeting.organizer.common_name == "Dr Alice Smith"
    assert meeting.organizer.email == "alice+meetings@example.org"
    assert meeting.organizer.uri == "mailto:alice%2Bmeetings@example.org"
    assert [attendee.common_name for attendee in meeting.attendees] == [
        "Doe, Bob",
        'Carol"CJ" Jones',
    ]
    assert [attendee.email for attendee in meeting.attendees] == [
        "bob@example.org",
        "carol@example.org",
    ]
    assert meeting.location == "Room 2, Main Building"
    assert meeting.description == "First line\nSecond line with a long folded; detail"
    assert meeting.url == "https://example.org/meetings/london-rhul"
    assert meeting.status == "CONFIRMED"
    assert result.to_payload()["meeting"] == meeting.to_payload()


def test_parse_ics_meeting_preserves_tzid_local_time() -> None:
    result = parse_ics_meeting(
        _calendar(
            "UID:tzid-event",
            "SUMMARY:Local seminar",
            "DTSTART;TZID=Europe/London:20261020T141500",
            'DTEND;TZID="Europe/London":20261020T154500',
        )
    )

    assert result.outcome is IcsMeetingParseOutcome.PARSED
    assert result.meeting is not None
    assert result.meeting.dtstart is not None
    assert result.meeting.dtstart.iso_value == "2026-10-20T14:15:00"
    assert result.meeting.dtstart.tzid == "Europe/London"
    assert result.meeting.dtstart.is_utc is False
    assert result.meeting.dtend is not None
    assert result.meeting.dtend.tzid == "Europe/London"


def test_parse_ics_meeting_parses_all_day_date_values() -> None:
    result = parse_ics_meeting(
        _calendar(
            "UID:all-day-event",
            "SUMMARY:Annual workshop",
            "DTSTART;VALUE=DATE:20261103",
            "DTEND;VALUE=DATE:20261105",
        )
    )

    assert result.outcome is IcsMeetingParseOutcome.PARSED
    assert result.meeting is not None
    assert result.meeting.dtstart is not None
    assert result.meeting.dtstart.to_payload() == {
        "raw_value": "20261103",
        "iso_value": "2026-11-03",
        "value_type": "date",
        "tzid": None,
        "is_utc": False,
    }
    assert result.meeting.dtend is not None
    assert result.meeting.dtend.iso_value == "2026-11-05"


@pytest.mark.parametrize(
    "text", [None, "", "Ordinary meeting notes without ICS markers"]
)
def test_parse_ics_meeting_reports_non_calendar_text_as_not_applicable(
    text: str | None,
) -> None:
    result = parse_ics_meeting(text)

    assert result.outcome is IcsMeetingParseOutcome.NOT_APPLICABLE
    assert result.error_code == "not_icalendar_text"
    assert result.meeting is None


def test_parse_ics_meeting_rejects_text_above_the_requested_bound() -> None:
    text = _calendar("UID:bounded", "SUMMARY:" + ("x" * 100))

    result = parse_ics_meeting(text, max_chars=64)

    assert result.outcome is IcsMeetingParseOutcome.INVALID
    assert result.error_code == "input_too_large"
    assert result.input_char_count == len(text)
    assert result.max_chars == 64
    assert result.meeting is None


def test_parse_ics_meeting_does_not_allow_callers_to_raise_the_hard_bound() -> None:
    text = "BEGIN:VCALENDAR\r\n" + ("x" * DEFAULT_ICS_MAX_CHARS)

    result = parse_ics_meeting(text, max_chars=DEFAULT_ICS_MAX_CHARS * 2)

    assert result.outcome is IcsMeetingParseOutcome.INVALID
    assert result.error_code == "input_too_large"
    assert result.max_chars == DEFAULT_ICS_MAX_CHARS


def test_parse_ics_meeting_refuses_to_choose_between_multiple_events() -> None:
    text = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:first\r\n"
        "SUMMARY:First event\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:second\r\n"
        "SUMMARY:Second event\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR"
    )

    result = parse_ics_meeting(text)

    assert result.outcome is IcsMeetingParseOutcome.MULTIPLE_EVENTS
    assert result.error_code == "multiple_vevents"
    assert result.event_count == 2
    assert result.meeting is None


@pytest.mark.parametrize(
    "recurrence_property",
    [
        "RRULE:FREQ=WEEKLY;COUNT=4",
        "RDATE:20260819T090000Z",
        "EXDATE:20260819T090000Z",
        "RECURRENCE-ID:20260812T090000Z",
    ],
)
def test_parse_ics_meeting_reports_recurrence_as_not_applicable(
    recurrence_property: str,
) -> None:
    result = parse_ics_meeting(
        _calendar(
            "UID:recurring-event",
            "SUMMARY:Recurring meeting",
            "DTSTART:20260812T090000Z",
            recurrence_property,
        )
    )

    assert result.outcome is IcsMeetingParseOutcome.NOT_APPLICABLE
    assert result.error_code == "unsupported_recurrence"
    assert result.event_count == 1
    assert result.meeting is None


def test_parse_ics_meeting_reports_calendar_without_event_as_invalid() -> None:
    result = parse_ics_meeting("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")

    assert result.outcome is IcsMeetingParseOutcome.INVALID
    assert result.error_code == "missing_vevent"
    assert result.event_count == 0


@pytest.mark.parametrize(
    ("event_lines", "error_code"),
    [
        (("SUMMARY:Missing UID",), "missing_uid"),
        (("UID:missing-summary",), "missing_summary"),
        (
            (
                "UID:bad-time",
                "SUMMARY:Bad time",
                "DTSTART;TZID=Europe/London:20261340T250000",
            ),
            "invalid_temporal_value",
        ),
        (
            (
                "UID:mixed-time",
                "SUMMARY:Mixed time",
                "DTSTART;TZID=Europe/London:20260812T100000Z",
            ),
            "invalid_temporal_value",
        ),
        (
            (
                "UID:type-mismatch",
                "SUMMARY:Type mismatch",
                "DTSTART;VALUE=DATE:20260812",
                "DTEND:20260812T110000Z",
            ),
            "temporal_value_type_mismatch",
        ),
        (
            (
                "UID:duplicate-summary",
                "SUMMARY:One",
                "SUMMARY:Two",
            ),
            "repeated_singleton_property",
        ),
    ],
)
def test_parse_ics_meeting_reports_malformed_single_events_as_invalid(
    event_lines: tuple[str, ...],
    error_code: str,
) -> None:
    result = parse_ics_meeting(_calendar(*event_lines))

    assert result.outcome is IcsMeetingParseOutcome.INVALID
    assert result.error_code == error_code
    assert result.event_count == 1
    assert result.meeting is None


def test_parse_ics_meeting_requires_complete_calendar_component_structure() -> None:
    result = parse_ics_meeting(
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:incomplete\r\n"
        "SUMMARY:Incomplete event\r\n"
        "END:VCALENDAR"
    )

    assert result.outcome is IcsMeetingParseOutcome.INVALID
    assert result.error_code == "invalid_component_structure"
