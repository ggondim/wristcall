"""Splits the response text into sentences so TTS can start before the LLM finishes."""

import re

_BOUNDARY = re.compile(r"[.!?…:]+[\"'”’)\]]*\s+")


class SentenceSplitter:
    def __init__(self, max_chars: int = 200) -> None:
        self._max = max_chars
        self._buf = ""

    def push(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        while True:
            m = _BOUNDARY.search(self._buf)
            if m and m.start() < self._max:
                piece, self._buf = self._buf[: m.end()], self._buf[m.end() :]
            elif len(self._buf) >= self._max:
                cut = self._buf.rfind(" ", 0, self._max)
                if cut <= 0:
                    cut = self._max
                piece, self._buf = self._buf[:cut], self._buf[cut:]
            else:
                break
            piece = piece.strip()
            if piece:
                out.append(piece)
        return out

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []
