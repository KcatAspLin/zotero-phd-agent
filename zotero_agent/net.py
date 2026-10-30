"""Small HTTP client; secrets are scoped to explicit requests, never redirects."""
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime


class AgentError(Exception):
    pass


class HttpError(AgentError):
    def __init__(self, status, host, *, error_code=None, error_type=None, retry_after=0):
        self.status = status
        quota_hints = {
            "insufficient_quota": "API quota unavailable; check credit balance and spending/usage limits",
            "credit_balance_exhausted": "prepaid API credits exhausted; check API billing and add credits if needed",
            "organization_spend_limit_exceeded": "organization API spending limit reached; check organization limits",
            "project_spend_limit_exceeded": "project API spending limit reached; check this API key's project limits",
            "organization_usage_limit_exceeded": "organization API usage limit reached; request a higher approved usage limit",
            "billing_hard_limit_reached": "API billing limit reached; check API billing and limits",
        }
        rate_codes = {"rate_limit_exceeded", "slow_down"}
        # Only surface known codes, never raw response messages that may include
        # credentials, account identifiers or paper text.
        self.error_code = error_code if isinstance(error_code, str) and error_code in (set(quota_hints) | rate_codes) else None
        self.is_quota = host == "api.openai.com" and (self.error_code in quota_hints or error_type == "insufficient_quota")
        self.is_rate_limit = host == "api.openai.com" and not self.is_quota and (
            self.error_code in rate_codes or error_type in ("rate_limit_error", "rate_limit_exceeded"))
        hints = {401: "check API key", 403: "check permissions", 412: "remote conflict; rerun",
                 413: "Zotero storage quota exceeded", 429: "rate limited; retry later"}
        hint = hints.get(status, "request failed")
        if status == 429 and host == "api.openai.com":
            if self.is_quota:
                hint = quota_hints.get(self.error_code, quota_hints["insufficient_quota"])
                hint += "; waiting/retrying alone will not resolve this. Billing: https://platform.openai.com/settings/organization/billing/overview"
            elif self.is_rate_limit:
                hint = "temporary API request/token rate limit reached"
                hint += f"; wait at least {retry_after:g} seconds before retrying" if retry_after else "; wait before retrying"
                hint += "; if persistent, check https://platform.openai.com/settings/organization/limits"
            else:
                hint = ("rate or quota limit; the response did not identify a recognised cause. "
                        "Check API billing/credits and project limits before retrying")
        code = f" ({self.error_code})" if self.error_code else ""
        super().__init__(f"HTTP {status} from {host}{code}: {hint}")


def response_error(response, host, retry_after):
    code, kind = None, None
    if host == "api.openai.com":
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                code, kind = error.get("code"), error.get("type")
        except AgentError:
            pass
    return HttpError(response.status, host, error_code=code, error_type=kind, retry_after=retry_after)


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeError) as exc:
            raise AgentError("Server returned invalid JSON") from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def seconds(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


class HTTP:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())
        self.local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.blocked_until = {}

    def request(self, method, url, *, headers=None, data=None, json_data=None,
                form=None, retry=False, retry_429=False, redirects=0,
                allow_localhost=False, timeout=180):
        parsed = urllib.parse.urlsplit(url)
        host = parsed.netloc
        local = allow_localhost and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if allow_localhost and (not local or parsed.username or parsed.password or parsed.scheme not in ("http", "https")):
            raise AgentError("Local model requests must use a loopback address without credentials")
        if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
            raise AgentError("HTTPS is required")
        if local and any(k.lower() in ("authorization", "zotero-api-key") for k in (headers or {})):
            raise AgentError("Credentials must not be sent to the local model")
        opener = self.local_opener if local else self.opener
        hdr = {"User-Agent": "PhD-Zotero-Agent/0.1", **(headers or {})}
        if json_data is not None:
            data = json.dumps(json_data).encode()
            hdr["Content-Type"] = "application/json"
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        safe_retry = method == "GET" or retry
        rate_retry = retry_429 and host == "api.openai.com"
        attempts = 4 if safe_retry or rate_retry else 1
        for attempt in range(attempts):
            delay = max(0, self.blocked_until.get(host, 0) - time.monotonic())
            if delay:
                time.sleep(delay)
            req = urllib.request.Request(url, data=data, headers=hdr, method=method)
            try:
                with opener.open(req, timeout=timeout) as result:
                    response = Response(result.status, dict(result.headers.items()), result.read())
            except urllib.error.HTTPError as exc:
                response = Response(exc.code, dict(exc.headers.items()), exc.read())
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # Mutation outcome may be unknown. Caller reconciles saved object keys on rerun.
                if not safe_retry or attempt + 1 == attempts:
                    raise AgentError(f"Connection failed for {host}; rerun to resume") from exc
                time.sleep(2 ** attempt)
                continue
            lower = {k.lower(): v for k, v in response.headers.items()}
            wait = max(seconds(lower.get("backoff")), seconds(lower.get("retry-after")))
            if wait:
                self.blocked_until[host] = time.monotonic() + wait
            if local and response.status in (301, 302, 303, 307, 308):
                raise AgentError("Local model redirects are not followed")
            if response.status in (301, 302, 303, 307, 308) and method == "GET" and redirects < 5:
                target = urllib.parse.urljoin(url, lower.get("location", ""))
                # Metadata-only redirects: never carry credentials across a redirect.
                if any(k.lower() in ("authorization", "zotero-api-key") for k in hdr):
                    raise AgentError("Authenticated redirect refused")
                return self.request("GET", target, headers=hdr, redirects=redirects + 1)
            if not 200 <= response.status < 300:
                error = response_error(response, host, seconds(lower.get("retry-after")))
                retryable = safe_retry and response.status in (429, 500, 502, 503, 504)
                if host == "api.openai.com" and response.status == 429:
                    retryable = (safe_retry or rate_retry) and error.is_rate_limit
                if retryable and attempt + 1 < attempts:
                    self.blocked_until[host] = time.monotonic() + max(wait, 2 ** attempt) + random.uniform(0, 1)
                    continue
                raise error
            return response
