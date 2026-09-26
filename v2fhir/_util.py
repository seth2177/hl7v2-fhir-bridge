from __future__ import annotations

import re

_EOL = re.compile(r"\r\n|\r|\n")


def first_line(text: str) -> str:
    m = _EOL.search(text)
    return text[:m.start()] if m else text
