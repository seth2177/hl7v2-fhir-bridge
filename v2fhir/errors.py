"""Errors that carry enough to build an HL7 ACK: an ACK code, an HL7 table 0357 error code and a location.

  AR  (application reject)  the message could not be understood: framing, MSH, encoding, version, type.
                            Nothing in it was processed.
  AE  (application error)   the message was understood, but its content could not be processed
                            (a required field is missing, a code is unknown, the FHIR server refused it).
"""
from __future__ import annotations

from dataclasses import dataclass

# HL7 table 0357 (message error condition codes) -- the subset this bridge emits.
SEGMENT_SEQUENCE_ERROR = "100"
REQUIRED_FIELD_MISSING = "101"
DATA_TYPE_ERROR = "102"
TABLE_VALUE_NOT_FOUND = "103"
UNSUPPORTED_MESSAGE_TYPE = "200"
UNSUPPORTED_EVENT_CODE = "201"
UNSUPPORTED_VERSION_ID = "203"
UNKNOWN_KEY_IDENTIFIER = "204"
DUPLICATE_KEY_IDENTIFIER = "205"
APPLICATION_INTERNAL_ERROR = "207"

ERROR_TEXT = {
    "0": "Message accepted",
    SEGMENT_SEQUENCE_ERROR: "Segment sequence error",
    REQUIRED_FIELD_MISSING: "Required field missing",
    DATA_TYPE_ERROR: "Data type error",
    TABLE_VALUE_NOT_FOUND: "Table value not found",
    UNSUPPORTED_MESSAGE_TYPE: "Unsupported message type",
    UNSUPPORTED_EVENT_CODE: "Unsupported event code",
    UNSUPPORTED_VERSION_ID: "Unsupported version id",
    UNKNOWN_KEY_IDENTIFIER: "Unknown key identifier",
    DUPLICATE_KEY_IDENTIFIER: "Duplicate key identifier",
    APPLICATION_INTERNAL_ERROR: "Application internal error",
}


@dataclass(frozen=True)
class Location:
    """Where in the message a problem is (ERR-2 / ERR-1 components)."""
    segment: str = ""
    sequence: int = 1          # which occurrence of the segment (1-based)
    field: int | None = None
    component: int | None = None

    def __str__(self) -> str:
        s = f"{self.segment}[{self.sequence}]" if self.segment else "?"
        if self.field is not None:
            s += f"-{self.field}"
            if self.component is not None:
                s += f".{self.component}"
        return s


@dataclass
class Issue:
    """One problem or warning, ready to go into an ERR segment."""
    code: str
    text: str
    location: Location | None = None
    severity: str = "E"        # E error, W warning, I information (HL7 table 0516)

    def __str__(self) -> str:
        where = f" at {self.location}" if self.location else ""
        return f"{self.text}{where}"


class HL7Error(Exception):
    """Base error. `ack_code` says which NAK it becomes."""
    ack_code = "AE"

    def __init__(self, text: str, code: str = APPLICATION_INTERNAL_ERROR, location: Location | None = None):
        super().__init__(text)
        self.issue = Issue(code, text, location, "E")


class ParseError(HL7Error):
    """The bytes are not a usable HL7 v2 message. Always an AR: nothing was processed."""
    ack_code = "AR"

    def __init__(self, text: str, code: str = SEGMENT_SEQUENCE_ERROR, location: Location | None = None):
        super().__init__(text, code, location)


class RejectError(HL7Error):
    """Understood, but refused for reasons unrelated to content (unsupported version or message type)."""
    ack_code = "AR"


class MappingError(HL7Error):
    """The content cannot be turned into valid FHIR (e.g. no patient identifier). AE."""
    ack_code = "AE"


class CharsetError(HL7Error):
    """MSH-18 declares a character set the bytes are not in. AE: guessing would corrupt names."""
    ack_code = "AE"
