"""External credential handling and redaction.

A key is read only from an environment variable or a file named by the operator,
never from the command line, a corpus or a results directory. It is sent only as
`Authorization: Bearer <key>` to the configured endpoint. It is never written:
error text is reduced to an exception class and a fixed phrase, replies that
contain it are suppressed before writing, and after every run the output files
are checked byte for byte for the key (a hit makes the run fail with exit 4).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REDACTED = "[redacted]"
# Generic shapes that look like credentials. Lexical, so they have false positives.
GENERIC = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*[\"']?[^\s\"']{6,}"
    r"|\bsk-[A-Za-z0-9_-]{16,})")


class Credential:
    """Holds a key in memory with a redacting repr; `source` says where it came from (no value)."""

    def __init__(self, value: str | None, source: str):
        if value is not None:
            value = value.strip()
            if not value or any(c in value for c in "\r\n") or len(value) > 4096:
                raise ValueError("credential is empty, multi-line or too long")
        self._value = value
        self.source = source

    @property
    def present(self) -> bool:
        return self._value is not None

    def header(self) -> dict:
        return {"Authorization": "Bearer " + self._value} if self._value else {}

    def redact(self, text: str) -> str:
        if self._value:
            text = text.replace(self._value, REDACTED)
        return GENERIC.sub(REDACTED, text)

    def appears_in(self, data: bytes) -> bool:
        return bool(self._value) and self._value.encode() in data

    def value_for_check(self) -> str | None:
        return self._value

    def __repr__(self):
        return f"Credential(source={self.source!r}, present={self.present})"

    __str__ = __repr__


def load(env_name: str | None = None, file_path: str | None = None) -> Credential:
    if env_name and file_path:
        raise ValueError("choose either --api-key-env or --api-key-file")
    if file_path:
        text = Path(file_path).read_text(encoding="utf-8-sig")
        lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if len(lines) != 1:
            raise ValueError("key file must contain exactly one non-comment line")
        line = lines[0].strip()
        if "=" in line and not line.startswith("sk-"):  # NAME=value form
            line = line.split("=", 1)[1].strip().strip("\"'")
        return Credential(line, "file")
    if env_name:
        value = os.environ.get(env_name)
        if not value:
            raise ValueError(f"environment variable {env_name} is not set")
        return Credential(value, "environment")
    return Credential(None, "none")


def scan_outputs(folder: Path, credential: Credential) -> list[str]:
    """Names of files under `folder` that contain the key's bytes. Must be empty."""
    if not credential.present:
        return []
    return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob("*") if p.is_file()
                  and credential.appears_in(p.read_bytes()))
