"""Remove secrets and capability links before anything is persisted or printed.

Sanitizing is deliberately lossy: an over-redacted report is recoverable by a
retest, while a leaked token or setup link is not.
"""
from __future__ import annotations

import re

REDACTED = "[redacted]"
_SENSITIVE_KEY = re.compile(
    r"(?i)(token|secret|password|passphrase|authorization|cookie|api[_-]?key|private[_-]?key"
    r"|credential[_-]?value|launch|setup[_-]?(url|link)|capability|signature|session[_-]?key)")
# Identifiers and counts (e.g. `secret_resolution_result_id`) name a secret's record, not its value.
_IDENTIFIER_KEY = re.compile(r"(?i)(_ids?|_count|_counts)$")
_DISCORD_TOKEN =re.compile(r"\b[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,40}\b")
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(token|secret|password|api[_-]?key|authorization)[A-Z0-9_]*)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_PREFIXED_KEY = re.compile(r"\b(sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abpr]|seren)[-_][A-Za-z0-9_-]{12,}")
# A documented tool identifier (for example a Seren tool `name`) may start like a prefixed key.
# Only a whole `name` value of this strict shape is kept: lowercase segments of at most 24
# characters, none a long letter-and-digit mix, which random key material produces.
_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[_-]{1,2}[a-z0-9]+)*")
_NAME_FIELD = re.compile(r'"name"\s*:\s*"$')


def _identifier(value: str) -> bool:
    if len(value) > 64 or not _IDENTIFIER.fullmatch(value):
        return False
    segments = re.split(r"[_-]+", value)
    return all(len(s) <= 24 and not (len(s) >= 8 and re.search(r"[a-z]", s)
                                     and re.search(r"[0-9]", s)) for s in segments)


def _keep_named_identifier(match) -> str:
    """Keep a prefixed-key match only when it is the whole value of a JSON `name` field and
    has identifier shape; anything else stays redacted."""
    whole = match.end() < len(match.string) and match.string[match.end()] == '"'
    named = _NAME_FIELD.search(match.string[max(0, match.start() - 24):match.start()])
    return match.group(0) if whole and named and _identifier(match.group(0)) else REDACTED


_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>)\]]+")
_CAPABILITY_SEGMENT = re.compile(r"^[A-Za-z0-9_~.=-]{20,}$")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


class Sanitizer:
    """Pattern redaction plus exact redaction of secret values known at runtime."""

    def __init__(self, known_secrets=()):
        self._known = set()
        for value in known_secrets:
            self.add_secret(value)

    def add_secret(self, value):
        if value and len(value) >= 4:
            self._known.add(value)

    def text(self, value: str) -> str:
        for secret in sorted(self._known, key=len, reverse=True):
            value = value.replace(secret, REDACTED)
        value = _URL.sub(lambda match: _redact_url(match.group(0)), value)
        value = _DISCORD_TOKEN.sub(REDACTED, value)
        value = _PREFIXED_KEY.sub(_keep_named_identifier, value)
        value = _BEARER.sub(lambda match: match.group(1) + " " + REDACTED, value)
        return _ASSIGNMENT.sub(lambda m: m.group(1) + m.group(3) + REDACTED, value)

    def value(self, value, key=None):
        if key is not None and _SENSITIVE_KEY.search(str(key)) \
                and not _IDENTIFIER_KEY.search(str(key)):
            return REDACTED if value not in (None, "", [], {}) else value
        if key == "name" and isinstance(value, str) and _identifier(value) \
                and not any(secret in value for secret in self._known):
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.value(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


def _redact_url(url: str) -> str:
    """Keep scheme, host and ordinary path words; drop credentials, query and tokens."""
    trailing = ""
    while url and url[-1] in ".,;:":
        trailing, url = url[-1] + trailing, url[:-1]
    scheme, _, rest = url.partition("://")
    authority, slash, path = rest.partition("/")
    host = authority.rsplit("@", 1)[-1]
    query = "?" in path or "#" in path
    path = re.split(r"[?#]", path, maxsplit=1)[0]
    segments = [REDACTED if _CAPABILITY_SEGMENT.match(part) or _UUID.match(part) else part
                for part in path.split("/")] if slash else []
    redacted = scheme + "://" + host + ("/" + "/".join(segments) if slash else "")
    return redacted + (REDACTED if query else "") + trailing


def pseudonymize(value, names=(), mapping=None):
    """Replace UUIDs and configured names with stable placeholders for shareable text."""
    mapping = {} if mapping is None else mapping

    def label(original, kind):
        if original not in mapping:
            count = sum(1 for item in mapping.values() if item.startswith("<" + kind))
            mapping[original] = "<" + kind + "-" + str(count + 1) + ">"
        return mapping[original]

    def text(item):
        for name in sorted((n for n in names if n), key=len, reverse=True):
            item = item.replace(name, label(name, "name"))
        return _UUID.sub(lambda match: label(match.group(0).lower(), "id"), item)

    def walk(item):
        if isinstance(item, str):
            return text(item)
        if isinstance(item, dict):
            return {text(str(k)): walk(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(v) for v in item]
        return item

    return walk(value)
