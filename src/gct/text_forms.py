from __future__ import annotations

import unicodedata


def comparable(text: str) -> str:
    return unicodedata.normalize("NFKC", text)
