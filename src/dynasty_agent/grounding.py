"""The grounding check: every number the model writes must already be in
what Python gave it for that turn (the numbers block, the tool's summary, the
user's own question). A take that quotes anything else is dropped, not shown:
a 4B model is fluent, not reliable with numbers, and the design rule is that
Python computes every number the user sees.
"""

from __future__ import annotations

import re

# "1,890", "-221", "+3.0", "78.1%", "$27", "36th", "2027"; not the 4 in "QB4".
_NUMBER = re.compile(r"(?<![A-Za-z\d.])[-+]?\$?\d[\d,]*(?:\.\d+)?")
# Counting words a sentence may use without the tool saying them: "one or two
# players", "the 2 picks". Kept tiny on purpose.
ALWAYS_ALLOWED = {"0", "1", "2", "3"}


def _canonical(token: str) -> str:
    """'$1,890.0' and '1890' compare equal; so do '+3.0' and '3'."""
    t = token.replace(",", "").replace("$", "").lstrip("+-")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t or "0"


def numbers_in(text: str) -> set[str]:
    return {_canonical(m) for m in _NUMBER.findall(text or "")}


def allowed_numbers(*sources: str) -> set[str]:
    allowed = set(ALWAYS_ALLOWED)
    for source in sources:
        allowed |= numbers_in(source)
    return allowed


def ungrounded(text: str, allowed: set[str]) -> set[str]:
    """The numbers in text that nothing in allowed accounts for."""
    return {n for n in numbers_in(text) if n not in allowed}
