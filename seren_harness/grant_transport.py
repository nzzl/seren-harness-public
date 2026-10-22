"""The one HTTPS route the pinned CLI lacks: preview and apply a typed tool-ref update.

Contract (pinned serenorg/seren v0.10.0, commit 2317949):
- host https://api.serendb.com (sdk/src/config.rs DEFAULT_API_HOST); the seren-agent
  publisher prefix /publishers/seren-agent (openapi-seren-agent.json servers);
- POST /deployments/{id}/managed/preview with AgentSpecUpdate
  -> DataResponse_ManagedAgentDeploymentUpdatePreview;
- PATCH /deployments/{id}/managed with AgentSpecUpdate and an Idempotency-Key header
  -> DataResponse_ManagedAgentDeploymentMutationResponse;
- GET /deployments/{id}/managed/actions/{request_id} for one lost mutation response;
- `Authorization: Bearer <key>` (cli/src/command_context.rs). The CLI cannot send
  tool_refs (MANAGED_AGENT_CONFIG_FIELDS, cli/src/commands/agent.rs:2472).

Only these three operations, one fixed deployment and one frozen body. No redirects,
proxies, environment base-URL override or other host. The key is fetched lazily by the
trusted gate, registered with the sanitizer, and never stored or returned.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

HOST = "api.serendb.com"
BASE = "https://" + HOST + "/publishers/seren-agent"
KEYCHAIN_SERVICE = "seren-harness-api-key"  # docs/LIFECYCLE-SETUP.md P7; owner-controlled
MAX_BYTES = 1_000_000
DEFINITE = (400, 403, 404, 409)  # documented responses that leave no mutation behind


class TransportError(Exception):
    """kind: rejected (documented 4xx), uncertain (outcome unknown) or refused (local)."""

    def __init__(self, kind, detail):
        super().__init__(kind + ": " + detail)
        self.kind, self.detail = kind, detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise TransportError("uncertain", "redirect refused; credential not forwarded")


def keychain_api_key():
    """Proposed runtime source, read only by the trusted gate after reservation."""
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s",
                             KEYCHAIN_SERVICE, "-w"], capture_output=True, text=True,
                            timeout=10)
    if result.returncode or not result.stdout.strip():
        raise TransportError("refused", "API key unavailable")
    return result.stdout.strip()


def _urlopen(request, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


class GrantTransport:
    """Bound to one deployment and one reviewed body; `before_send` journals each call."""

    def __init__(self, deployment_id, body, get_key, sanitizer, before_send, opener=None):
        uuid.UUID(deployment_id)
        self.deployment_id, self.body = deployment_id, json.loads(json.dumps(body))
        self.get_key, self.sanitizer, self.before_send = get_key, sanitizer, before_send
        self.opener = opener or _urlopen
        self.key = None
        self.receipts = []

    def preview(self, body, timeout):
        return self._send("preview", "POST", "/managed/preview", body, timeout)

    def update(self, body, request_id, timeout):
        return self._send("update", "PATCH", "/managed", body, timeout, request_id)

    def action(self, request_id, timeout):
        return self._send("action", "GET", "/managed/actions/" + str(uuid.UUID(request_id)),
                          None, timeout)

    def _send(self, kind, method, suffix, body, timeout, request_id=None):
        if body is not None and body != self.body:
            raise TransportError("refused", "body differs from the reviewed update")
        if not 0 < timeout <= 30:
            raise TransportError("refused", "no time left for a bounded request")
        url = BASE + "/deployments/" + self.deployment_id + suffix
        if self.key is None:
            self.key = self.get_key()
            self.sanitizer.add_secret(self.key)
        headers = {"Authorization": "Bearer " + self.key, "Accept": "application/json",
                   "User-Agent": "SerenHarnessJourney/1"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, sort_keys=True).encode()
        if request_id is not None:
            headers["Idempotency-Key"] = str(uuid.UUID(request_id))
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        receipt = {"kind": kind, "method": method, "url": url, "request_id": request_id}
        self.before_send(receipt)  # journaled before the request leaves this process
        try:
            with self.opener(request, timeout) as response:
                if response.geturl() != url:
                    raise TransportError("uncertain", "response from an unexpected URL")
                raw = response.read(MAX_BYTES + 1)
                status = response.status
        except TransportError as error:
            self._receipt(receipt, error.kind, None)
            raise
        except urllib.error.HTTPError as error:
            kind_ = "rejected" if error.code in DEFINITE else "uncertain"
            self._receipt(receipt, kind_, error.code)
            raise TransportError(kind_, "HTTP %d" % error.code) from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            self._receipt(receipt, "uncertain", None)
            raise TransportError("uncertain", type(error).__name__) from None
        if len(raw) > MAX_BYTES:
            self._receipt(receipt, "uncertain", status)
            raise TransportError("uncertain", "oversized response")
        try:
            value = json.loads(raw)
        except ValueError:
            self._receipt(receipt, "uncertain", status)
            raise TransportError("uncertain", "undecodable response") from None
        data_ = value.get("data") if isinstance(value, dict) and set(value) <= {
            "data", "pagination"} else None
        if status not in (200, 202) or not isinstance(data_, dict):
            self._receipt(receipt, "uncertain", status)
            raise TransportError("uncertain", "unsupported response shape")
        self._receipt(receipt, "ok", status, data_)
        return data_

    def _receipt(self, receipt, outcome, status, data=None):
        saved = dict(receipt, outcome=outcome, http_status=status)
        if data is not None:
            saved["response"] = data
        self.receipts.append(json.loads(self.sanitizer.text(json.dumps(saved))))
