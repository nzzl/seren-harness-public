"""Probe/response correlation and Discord channel observers.

Discord message fields used here follow the public API message object
(https://discord.com/developers/docs/resources/message): `id` (snowflake),
`channel_id`, `author.id`, `author.bot`, `content`, `timestamp` and
`message_reference.message_id`. Whether Seren's managed connector answers
bot- or webhook-authored mentions is NOT established, so no automated sender
exists: live probes are posted by a human and labelled human-assisted.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
import urllib.request

DISCORD_EPOCH_MS = 1420070400000
API = "https://discord.com/api/v10"


class ObserverUnavailable(Exception):
    pass


def snowflake_ms(snowflake) -> int:
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


def new_marker() -> str:
    return "SH-" + secrets.token_hex(6).upper()


def find_probe(messages, marker, channel_id, not_before_ms, sender_user_id=None):
    """Locate the probe by its unique marker, posted in the channel after the window opened."""
    for message in sorted(messages, key=lambda m: int(m["id"])):
        if (message.get("channel_id") == channel_id and marker in message.get("content", "")
                and snowflake_ms(message["id"]) >= not_before_ms
                and (sender_user_id is None
                     or message.get("author", {}).get("id") == sender_user_id)):
            return message
    return None


REPLY_MESSAGE_TYPE = 19   # Message type REPLY.
DEFAULT_REFERENCE_TYPE = 0  # "A standard reference used by replies"; FORWARD is 1.


def _references(message, probe):
    """A standard (non-forward) message reference to the probe."""
    reference = message.get("message_reference") or {}
    return reference.get("message_id") == probe["id"] \
        and reference.get("type", DEFAULT_REFERENCE_TYPE) == DEFAULT_REFERENCE_TYPE


def _explicit_reply(message, probe):
    return message.get("type") == REPLY_MESSAGE_TYPE and _references(message, probe)


def reply_kind(message, probe):
    return "reply_reference" if _references(message, probe) else "marker"


def correlate_reply(messages, probe, channel_id, employee_user_id, marker, window_ms):
    """Return (reply, rejections). Only a reply bound to this probe can pass.

    Bound means: same channel, authored by the employee, inside the window, and
    either a standard reply reference to the probe or an echo of its unique marker.
    A message must be created after the probe's millisecond. The one exception is
    an explicit reply (type REPLY, standard reference to the probe) in the probe's
    own millisecond: snowflake ids are unique, so it is a distinct message, and it
    names the probe. Id order within a millisecond is not treated as causal order,
    so same-millisecond marker echoes and forwards stay rejected. Everything else
    is counted as a rejection reason.
    """
    probe_ms = snowflake_ms(probe["id"])
    rejections = {}

    def reject(reason):
        rejections[reason] = rejections.get(reason, 0) + 1

    for message in sorted(messages, key=lambda m: int(m["id"])):
        if message["id"] == probe["id"]:
            continue
        created = snowflake_ms(message["id"])
        if message.get("channel_id") != channel_id:
            reject("other_channel")
        elif created < probe_ms or (created == probe_ms
                                    and not _explicit_reply(message, probe)):
            reject("stale_before_probe")
        elif created > probe_ms + window_ms:
            reject("outside_window")
        elif message.get("author", {}).get("id") != employee_user_id:
            reject("wrong_author")
        elif _references(message, probe):
            return message, rejections
        elif marker in message.get("content", ""):
            return message, rejections
        else:
            reject("unrelated_to_probe")
    return None, rejections


def normalize_content(text):
    """Trim and collapse whitespace runs to one space. Case and punctuation are kept."""
    return " ".join(text.split())


def content_matches(text, expected):
    """Whether the complete reply equals the expected response after whitespace
    normalization only: case-sensitive, with no substring, keyword or semantic match."""
    return isinstance(text, str) and normalize_content(text) == normalize_content(expected)


class SimulatedChannel:
    """A JSON-lines file standing in for a Discord channel. Simulation only."""

    def __init__(self, path):
        self.path = path

    def messages(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def post(self, author_id, content, channel_id, reference=None, bot=False):
        message = {"id": str(_next_snowflake(self.messages())), "channel_id": channel_id,
                   "author": {"id": author_id, "bot": bot}, "content": content}
        if reference:
            message["message_reference"] = {"message_id": reference, "channel_id": channel_id}
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(message) + "\n")
        return message


def _next_snowflake(existing):
    now = (int(time.time() * 1000) - DISCORD_EPOCH_MS) << 22
    latest = max((int(m["id"]) for m in existing), default=0)
    return max(now, latest + (1 << 22))


class RestObserver:
    """Read-only channel history through Discord's REST API. Live use is unverified.

    Requires a dedicated reader bot in the test guild with access to the channel
    and, for marker matching on human messages, the Message Content intent.
    It never opens a gateway connection and never posts.
    """

    def __init__(self, channel_id, token_env, opener=None):
        token = os.environ.get(token_env or "")
        if not token:
            raise ObserverUnavailable("reader token environment variable is not set")
        self.channel_id = channel_id
        self._token = token
        self._open = opener or urllib.request.urlopen

    def messages(self):
        query = urllib.parse.urlencode({"limit": 50})
        request = urllib.request.Request(
            API + "/channels/" + urllib.parse.quote(self.channel_id) + "/messages?" + query,
            headers={"Authorization": "Bot " + self._token,
                     "User-Agent": "seren-harness (offline-v1)"})
        try:
            with self._open(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise ObserverUnavailable("channel history unavailable: " + type(error).__name__)
        if not isinstance(data, list) or not all(_message_shape(m) for m in data):
            raise ObserverUnavailable("channel history had an unexpected shape")
        return data


def _message_shape(message):
    """The fields correlation relies on; anything else is not trusted as a message."""
    return (isinstance(message, dict) and isinstance(message.get("id"), str)
            and message["id"].isdigit() and isinstance(message.get("channel_id"), str)
            and isinstance(message.get("author"), dict)
            and isinstance(message.get("content", ""), str))
