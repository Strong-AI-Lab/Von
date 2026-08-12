"""Pure, bounded parsing of a single iCalendar meeting event.

The parser intentionally stops at representation-ready facts.  It performs no
concept lookup or persistence, and it does not try to choose between multiple
events.  Callers therefore receive an explicit typed outcome before deciding
whether the meeting materialisation path applies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Literal
from urllib.parse import unquote

ICS_MEETING_PARSE_SCHEMA_VERSION = "ics_meeting_parse_result.v1"
DEFAULT_ICS_MAX_CHARS = 262_144


class IcsMeetingParseOutcome(str, Enum):
    """Terminal outcomes for the bounded single-event parser."""

    PARSED = "parsed"
    NOT_APPLICABLE = "not_applicable"
    INVALID = "invalid"
    MULTIPLE_EVENTS = "multiple_events"


@dataclass(frozen=True)
class IcsTemporalValue:
    """A JSON-projectable RFC 5545 DATE or DATE-TIME value."""

    raw_value: str
    iso_value: str
    value_type: Literal["date", "date_time"]
    tzid: str | None = None
    is_utc: bool = False

    def to_payload(self) -> dict[str, str | bool | None]:
        return {
            "raw_value": self.raw_value,
            "iso_value": self.iso_value,
            "value_type": self.value_type,
            "tzid": self.tzid,
            "is_utc": self.is_utc,
        }


@dataclass(frozen=True)
class IcsCalendarAddress:
    """An ORGANIZER or ATTENDEE calendar address."""

    uri: str
    common_name: str | None = None
    email: str | None = None

    def to_payload(self) -> dict[str, str | None]:
        return {
            "uri": self.uri,
            "common_name": self.common_name,
            "email": self.email,
        }


@dataclass(frozen=True)
class IcsMeeting:
    """The deterministic facts extracted from one VEVENT."""

    uid: str
    summary: str
    dtstart: IcsTemporalValue | None = None
    dtend: IcsTemporalValue | None = None
    organizer: IcsCalendarAddress | None = None
    attendees: tuple[IcsCalendarAddress, ...] = ()
    location: str | None = None
    description: str | None = None
    url: str | None = None
    status: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "dtstart": self.dtstart.to_payload() if self.dtstart else None,
            "dtend": self.dtend.to_payload() if self.dtend else None,
            "organizer": self.organizer.to_payload() if self.organizer else None,
            "attendees": [attendee.to_payload() for attendee in self.attendees],
            "location": self.location,
            "description": self.description,
            "url": self.url,
            "status": self.status,
        }


@dataclass(frozen=True)
class IcsMeetingParseResult:
    """Typed parse result suitable for a deterministic workflow action."""

    outcome: IcsMeetingParseOutcome
    meeting: IcsMeeting | None = None
    error_code: str | None = None
    message: str | None = None
    event_count: int = 0
    input_char_count: int = 0
    max_chars: int = DEFAULT_ICS_MAX_CHARS

    @property
    def parsed(self) -> bool:
        return self.outcome is IcsMeetingParseOutcome.PARSED

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": ICS_MEETING_PARSE_SCHEMA_VERSION,
            "outcome": self.outcome.value,
            "meeting": self.meeting.to_payload() if self.meeting else None,
            "error_code": self.error_code,
            "message": self.message,
            "event_count": self.event_count,
            "input_char_count": self.input_char_count,
            "max_chars": self.max_chars,
        }


@dataclass(frozen=True)
class _ContentLine:
    name: str
    params: dict[str, str]
    value: str
    line_number: int


class _IcsParseFailure(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_ICALENDAR_MARKER_RE = re.compile(
    r"(?im)^\s*(?:BEGIN|END)\s*:\s*(?:VCALENDAR|VEVENT)\s*$"
)
_CONTENT_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
_DATE_RE = re.compile(r"^\d{8}$")
_DATE_TIME_RE = re.compile(r"^\d{8}T\d{6}Z?$", re.IGNORECASE)
_UNSUPPORTED_RECURRENCE_PROPERTIES = frozenset(
    {"RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"}
)


def _bounded_max_chars(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ICS_MAX_CHARS
    if parsed <= 0:
        return DEFAULT_ICS_MAX_CHARS
    return min(parsed, DEFAULT_ICS_MAX_CHARS)


def _decode_text(value: str) -> str:
    """Decode RFC 5545 TEXT escaping without interpreting URI values."""

    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            output.append(char)
            index += 1
            continue

        escaped = value[index + 1]
        if escaped in {"n", "N"}:
            output.append("\n")
        elif escaped in {"\\", ",", ";"}:
            output.append(escaped)
        else:
            # Unknown escapes are not legal TEXT escaping. Preserve the source
            # bytes rather than silently changing their meaning.
            output.extend(("\\", escaped))
        index += 2
    return "".join(output)


def _decode_parameter_value(value: str) -> str:
    """Decode RFC 6868 parameter escapes plus common legacy TEXT escapes."""

    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "^" and index + 1 < len(value):
            escaped = value[index + 1]
            if escaped in {"n", "N"}:
                output.append("\n")
            elif escaped == "^":
                output.append("^")
            elif escaped == "'":
                output.append('"')
            else:
                output.extend((char, escaped))
            index += 2
            continue
        output.append(char)
        index += 1
    return _decode_text("".join(output))


def _unfold_content_lines(text: str) -> list[tuple[int, str]]:
    physical_lines = re.split(r"\r\n|\n|\r", text.lstrip("\ufeff"))
    logical_lines: list[tuple[int, str]] = []
    for line_number, line in enumerate(physical_lines, start=1):
        if line.startswith((" ", "\t")):
            if not logical_lines:
                raise _IcsParseFailure(
                    "orphaned_folded_line",
                    f"Folded content at physical line {line_number} has no parent line.",
                )
            parent_number, parent = logical_lines[-1]
            logical_lines[-1] = (parent_number, parent + line[1:])
            continue
        if not line:
            continue
        logical_lines.append((line_number, line))
    return logical_lines


def _find_unquoted_delimiter(value: str, delimiter: str) -> int:
    in_quotes = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char == delimiter and not in_quotes:
            return index
    if in_quotes:
        raise _IcsParseFailure(
            "unterminated_parameter_quote",
            "A content-line parameter has an unterminated quoted value.",
        )
    return -1


def _split_unquoted(value: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    remainder = value
    while True:
        index = _find_unquoted_delimiter(remainder, delimiter)
        if index < 0:
            parts.append(remainder)
            return parts
        parts.append(remainder[:index])
        remainder = remainder[index + 1 :]


def _parse_content_line(line_number: int, raw_line: str) -> _ContentLine:
    colon_index = _find_unquoted_delimiter(raw_line, ":")
    if colon_index < 1:
        raise _IcsParseFailure(
            "invalid_content_line",
            f"Physical line {line_number} is not a valid iCalendar content line.",
        )

    head = raw_line[:colon_index]
    value = raw_line[colon_index + 1 :]
    head_parts = _split_unquoted(head, ";")
    raw_name = head_parts[0]
    # RFC 5545 permits an optional group prefix before the property name.
    name = raw_name.rsplit(".", maxsplit=1)[-1].upper()
    if not _CONTENT_NAME_RE.fullmatch(name):
        raise _IcsParseFailure(
            "invalid_property_name",
            f"Physical line {line_number} has an invalid property name.",
        )

    params: dict[str, str] = {}
    for raw_parameter in head_parts[1:]:
        if "=" not in raw_parameter:
            raise _IcsParseFailure(
                "invalid_property_parameter",
                f"Physical line {line_number} has a parameter without a value.",
            )
        raw_key, raw_parameter_value = raw_parameter.split("=", maxsplit=1)
        key = raw_key.strip().upper()
        if not _CONTENT_NAME_RE.fullmatch(key) or key in params:
            raise _IcsParseFailure(
                "invalid_property_parameter",
                f"Physical line {line_number} has an invalid or repeated parameter.",
            )
        parameter_value = raw_parameter_value
        if parameter_value.startswith('"'):
            if len(parameter_value) < 2 or not parameter_value.endswith('"'):
                raise _IcsParseFailure(
                    "unterminated_parameter_quote",
                    f"Physical line {line_number} has an unterminated parameter.",
                )
            parameter_value = parameter_value[1:-1]
        elif '"' in parameter_value:
            raise _IcsParseFailure(
                "invalid_property_parameter",
                f"Physical line {line_number} has an invalid parameter quote.",
            )
        params[key] = _decode_parameter_value(parameter_value)

    return _ContentLine(
        name=name,
        params=params,
        value=value,
        line_number=line_number,
    )


def _extract_single_event_lines(
    logical_lines: list[tuple[int, str]],
) -> tuple[list[_ContentLine], int]:
    event_count = sum(
        1
        for _line_number, raw_line in logical_lines
        if raw_line.strip().upper() == "BEGIN:VEVENT"
    )
    if event_count > 1:
        raise _IcsParseFailure(
            "multiple_vevents",
            f"The iCalendar text contains {event_count} VEVENT components.",
        )
    if event_count == 0:
        raise _IcsParseFailure(
            "missing_vevent",
            "The iCalendar text does not contain a VEVENT component.",
        )

    stack: list[str] = []
    saw_calendar = False
    event_lines: list[_ContentLine] = []
    for line_number, raw_line in logical_lines:
        content_line = _parse_content_line(line_number, raw_line)
        if content_line.name == "BEGIN":
            component = content_line.value.strip().upper()
            if not _CONTENT_NAME_RE.fullmatch(component):
                raise _IcsParseFailure(
                    "invalid_component_name",
                    f"Physical line {line_number} has an invalid component name.",
                )
            if not stack:
                if component != "VCALENDAR" or saw_calendar:
                    raise _IcsParseFailure(
                        "invalid_component_structure",
                        "Exactly one top-level VCALENDAR component is required.",
                    )
                saw_calendar = True
            elif component == "VEVENT" and stack != ["VCALENDAR"]:
                raise _IcsParseFailure(
                    "invalid_component_structure",
                    "VEVENT must be a direct child of VCALENDAR.",
                )
            stack.append(component)
            continue

        if content_line.name == "END":
            component = content_line.value.strip().upper()
            if not stack or stack[-1] != component:
                raise _IcsParseFailure(
                    "invalid_component_structure",
                    f"Physical line {line_number} closes an unexpected component.",
                )
            stack.pop()
            continue

        if not stack:
            raise _IcsParseFailure(
                "invalid_component_structure",
                f"Physical line {line_number} occurs outside VCALENDAR.",
            )
        if stack[-1] == "VEVENT":
            event_lines.append(content_line)

    if stack or not saw_calendar:
        raise _IcsParseFailure(
            "invalid_component_structure",
            "The iCalendar component structure is incomplete.",
        )
    return event_lines, event_count


def _single_property(
    properties: dict[str, list[_ContentLine]],
    name: str,
    *,
    required: bool = False,
) -> _ContentLine | None:
    values = properties.get(name, [])
    if len(values) > 1:
        raise _IcsParseFailure(
            "repeated_singleton_property",
            f"VEVENT contains more than one {name} property.",
        )
    if not values:
        if required:
            raise _IcsParseFailure(
                f"missing_{name.lower()}",
                f"VEVENT requires a non-empty {name} property.",
            )
        return None
    return values[0]


def _required_text_property(
    properties: dict[str, list[_ContentLine]], name: str
) -> str:
    content_line = _single_property(properties, name, required=True)
    assert content_line is not None
    decoded = _decode_text(content_line.value)
    if not decoded.strip():
        raise _IcsParseFailure(
            f"missing_{name.lower()}",
            f"VEVENT requires a non-empty {name} property.",
        )
    return decoded


def _optional_text_property(
    properties: dict[str, list[_ContentLine]], name: str
) -> str | None:
    content_line = _single_property(properties, name)
    return _decode_text(content_line.value) if content_line is not None else None


def _parse_temporal(content_line: _ContentLine) -> IcsTemporalValue:
    raw_value = content_line.value.strip()
    raw_value_type = content_line.params.get("VALUE")
    value_type = raw_value_type.upper() if raw_value_type else None
    tzid = content_line.params.get("TZID")
    if tzid is not None:
        tzid = tzid.strip()
        if not tzid:
            raise _IcsParseFailure(
                "invalid_tzid",
                f"{content_line.name} has an empty TZID parameter.",
            )

    if value_type is None:
        if _DATE_RE.fullmatch(raw_value):
            value_type = "DATE"
        elif _DATE_TIME_RE.fullmatch(raw_value):
            value_type = "DATE-TIME"
        else:
            raise _IcsParseFailure(
                "invalid_temporal_value",
                f"{content_line.name} is not an RFC 5545 DATE or DATE-TIME.",
            )

    if value_type == "DATE":
        if tzid is not None or not _DATE_RE.fullmatch(raw_value):
            raise _IcsParseFailure(
                "invalid_temporal_value",
                f"{content_line.name} DATE values cannot carry TZID or time syntax.",
            )
        try:
            parsed_date = date(
                int(raw_value[0:4]),
                int(raw_value[4:6]),
                int(raw_value[6:8]),
            )
        except ValueError as exc:
            raise _IcsParseFailure(
                "invalid_temporal_value",
                f"{content_line.name} contains an invalid calendar date.",
            ) from exc
        return IcsTemporalValue(
            raw_value=raw_value,
            iso_value=parsed_date.isoformat(),
            value_type="date",
        )

    if value_type != "DATE-TIME" or not _DATE_TIME_RE.fullmatch(raw_value):
        raise _IcsParseFailure(
            "invalid_temporal_value",
            f"{content_line.name} has an unsupported VALUE parameter or time syntax.",
        )

    is_utc = raw_value.upper().endswith("Z")
    if is_utc and tzid is not None:
        raise _IcsParseFailure(
            "invalid_temporal_value",
            f"{content_line.name} cannot combine UTC syntax with TZID.",
        )
    parse_value = raw_value[:-1] if is_utc else raw_value
    try:
        parsed_date = date(
            int(parse_value[0:4]),
            int(parse_value[4:6]),
            int(parse_value[6:8]),
        )
        hour = int(parse_value[9:11])
        minute = int(parse_value[11:13])
        second = int(parse_value[13:15])
        if hour > 23 or minute > 59 or second > 60:
            raise ValueError("date-time component outside RFC 5545 range")
    except ValueError as exc:
        raise _IcsParseFailure(
            "invalid_temporal_value",
            f"{content_line.name} contains an invalid date-time.",
        ) from exc
    iso_value = f"{parsed_date.isoformat()}T{hour:02d}:{minute:02d}:{second:02d}"
    if is_utc:
        iso_value += "Z"
    return IcsTemporalValue(
        raw_value=raw_value,
        iso_value=iso_value,
        value_type="date_time",
        tzid=tzid,
        is_utc=is_utc,
    )


def _optional_temporal_property(
    properties: dict[str, list[_ContentLine]], name: str
) -> IcsTemporalValue | None:
    content_line = _single_property(properties, name)
    return _parse_temporal(content_line) if content_line is not None else None


def _parse_calendar_address(content_line: _ContentLine) -> IcsCalendarAddress:
    uri = content_line.value.strip()
    if not uri:
        raise _IcsParseFailure(
            "invalid_calendar_address",
            f"{content_line.name} requires a non-empty calendar address.",
        )
    common_name = content_line.params.get("CN")
    if common_name is not None and not common_name.strip():
        common_name = None

    email: str | None = None
    if uri.lower().startswith("mailto:"):
        email = unquote(uri[7:].split("?", maxsplit=1)[0]).strip()
        if not email:
            raise _IcsParseFailure(
                "invalid_calendar_address",
                f"{content_line.name} has an empty mailto address.",
            )
    return IcsCalendarAddress(
        uri=uri,
        common_name=common_name,
        email=email,
    )


def _parse_event(event_lines: list[_ContentLine]) -> IcsMeeting:
    properties: dict[str, list[_ContentLine]] = {}
    for content_line in event_lines:
        properties.setdefault(content_line.name, []).append(content_line)

    uid = _required_text_property(properties, "UID")
    summary = _required_text_property(properties, "SUMMARY")
    dtstart = _optional_temporal_property(properties, "DTSTART")
    dtend = _optional_temporal_property(properties, "DTEND")
    if dtstart and dtend and dtstart.value_type != dtend.value_type:
        raise _IcsParseFailure(
            "temporal_value_type_mismatch",
            "DTSTART and DTEND must use the same DATE or DATE-TIME value type.",
        )

    organizer_line = _single_property(properties, "ORGANIZER")
    organizer = (
        _parse_calendar_address(organizer_line) if organizer_line is not None else None
    )
    attendees = tuple(
        _parse_calendar_address(content_line)
        for content_line in properties.get("ATTENDEE", [])
    )

    url_line = _single_property(properties, "URL")
    url = url_line.value.strip() if url_line is not None else None
    if url_line is not None and not url:
        raise _IcsParseFailure("invalid_url", "VEVENT URL cannot be empty.")

    status_line = _single_property(properties, "STATUS")
    status = status_line.value.strip().upper() if status_line is not None else None
    if status_line is not None and not status:
        raise _IcsParseFailure("invalid_status", "VEVENT STATUS cannot be empty.")

    return IcsMeeting(
        uid=uid,
        summary=summary,
        dtstart=dtstart,
        dtend=dtend,
        organizer=organizer,
        attendees=attendees,
        location=_optional_text_property(properties, "LOCATION"),
        description=_optional_text_property(properties, "DESCRIPTION"),
        url=url,
        status=status,
    )


def parse_ics_meeting(
    text: str | None,
    *,
    max_chars: int = DEFAULT_ICS_MAX_CHARS,
) -> IcsMeetingParseResult:
    """Parse one bounded VEVENT without performing any external read or write.

    Plain non-calendar text returns ``not_applicable``.  Calendar-shaped input
    that is malformed, missing required UID/SUMMARY values, or outside the hard
    text bound returns ``invalid``.  Multiple VEVENTs receive their own outcome
    so callers never silently select an arbitrary meeting.
    """

    bounded_max_chars = _bounded_max_chars(max_chars)
    if text is None or (isinstance(text, str) and not text.strip()):
        return IcsMeetingParseResult(
            outcome=IcsMeetingParseOutcome.NOT_APPLICABLE,
            error_code="not_icalendar_text",
            message="No iCalendar text was supplied.",
            max_chars=bounded_max_chars,
        )
    if not isinstance(text, str):
        return IcsMeetingParseResult(
            outcome=IcsMeetingParseOutcome.INVALID,
            error_code="invalid_input_type",
            message="iCalendar input must be text.",
            max_chars=bounded_max_chars,
        )

    input_char_count = len(text)
    if input_char_count > bounded_max_chars:
        return IcsMeetingParseResult(
            outcome=IcsMeetingParseOutcome.INVALID,
            error_code="input_too_large",
            message=(
                f"iCalendar text contains {input_char_count} characters; "
                f"the parser limit is {bounded_max_chars}."
            ),
            input_char_count=input_char_count,
            max_chars=bounded_max_chars,
        )
    if "\x00" in text:
        return IcsMeetingParseResult(
            outcome=IcsMeetingParseOutcome.INVALID,
            error_code="invalid_control_character",
            message="iCalendar text contains a NUL character.",
            input_char_count=input_char_count,
            max_chars=bounded_max_chars,
        )
    if not _ICALENDAR_MARKER_RE.search(text):
        return IcsMeetingParseResult(
            outcome=IcsMeetingParseOutcome.NOT_APPLICABLE,
            error_code="not_icalendar_text",
            message="The supplied text has no iCalendar component markers.",
            input_char_count=input_char_count,
            max_chars=bounded_max_chars,
        )

    event_count = 0
    logical_lines: list[tuple[int, str]] = []
    try:
        logical_lines = _unfold_content_lines(text)
        event_lines, event_count = _extract_single_event_lines(logical_lines)
        recurrence_properties = sorted(
            {
                content_line.name
                for content_line in event_lines
                if content_line.name in _UNSUPPORTED_RECURRENCE_PROPERTIES
            }
        )
        if recurrence_properties:
            return IcsMeetingParseResult(
                outcome=IcsMeetingParseOutcome.NOT_APPLICABLE,
                error_code="unsupported_recurrence",
                message=(
                    "The deterministic meeting parser does not support "
                    "recurrence-bearing VEVENT properties: "
                    f"{', '.join(recurrence_properties)}."
                ),
                event_count=event_count,
                input_char_count=input_char_count,
                max_chars=bounded_max_chars,
            )
        meeting = _parse_event(event_lines)
    except _IcsParseFailure as exc:
        multiple = exc.code == "multiple_vevents"
        if multiple and event_count == 0:
            event_count = sum(
                1
                for _line_number, raw_line in logical_lines
                if raw_line.strip().upper() == "BEGIN:VEVENT"
            )
        return IcsMeetingParseResult(
            outcome=(
                IcsMeetingParseOutcome.MULTIPLE_EVENTS
                if multiple
                else IcsMeetingParseOutcome.INVALID
            ),
            error_code=exc.code,
            message=exc.message,
            event_count=event_count,
            input_char_count=input_char_count,
            max_chars=bounded_max_chars,
        )

    return IcsMeetingParseResult(
        outcome=IcsMeetingParseOutcome.PARSED,
        meeting=meeting,
        event_count=event_count,
        input_char_count=input_char_count,
        max_chars=bounded_max_chars,
    )


__all__ = [
    "DEFAULT_ICS_MAX_CHARS",
    "ICS_MEETING_PARSE_SCHEMA_VERSION",
    "IcsCalendarAddress",
    "IcsMeeting",
    "IcsMeetingParseOutcome",
    "IcsMeetingParseResult",
    "IcsTemporalValue",
    "parse_ics_meeting",
]
