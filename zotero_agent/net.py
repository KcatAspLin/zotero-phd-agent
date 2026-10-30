"""Small HTTP client; secrets are scoped to explicit requests, never redirects."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime


class AgentError(Exception):
    pass


class HttpError(AgentError):
    def __init__(self, status, host):
        self.status = status
        hints = {401: "check API key", 403: "check permissions", 412: "remote conflict; rerun",
                 413: "Zotero storage quota exceeded", 429: "rate limited; retry later"}
        super().__init__(f"HTTP {status} from {host}: {hints.get(status, 'request failed')}")


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
        self.blocked_until = {}

    def request(self, method, url, *, headers=None, data=None, json_data=None,
                form=None, retry=False, redirects=0):
        host = urllib.parse.urlsplit(url).netloc
        if not url.startswith("https://"):
            raise AgentError("HTTPS is required")
        hdr = {"User-Agent": "PhD-Zotero-Agent/0.1", **(headers or {})}
        if json_data is not None:
            data = json.dumps(json_data).encode()
            hdr["Content-Type"] = "application/json"
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        attempts = 4 if method == "GET" or retry else 1
        for attempt in range(attempts):
            delay = max(0, self.blocked_until.get(host, 0) - time.monotonic())
            if delay:
                time.sleep(delay)
            req = urllib.request.Request(url, data=data, headers=hdr, method=method)
            try:
                with self.opener.open(req, timeout=180) as result:
                    response = Response(result.status, dict(result.headers.items()), result.read())
            except urllib.error.HTTPError as exc:
                response = Response(exc.code, dict(exc.headers.items()), exc.read())
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # Mutation outcome may be unknown. Caller reconciles saved object keys on rerun.
                if attempt + 1 == attempts:
                    raise AgentError(f"Connection failed for {host}; rerun to resume") from exc
                time.sleep(2 ** attempt)
                continue
            lower = {k.lower(): v for k, v in response.headers.items()}
            wait = max(seconds(lower.get("backoff")), seconds(lower.get("retry-after")))
            if wait:
                self.blocked_until[host] = time.monotonic() + wait
            if response.status in (301, 302, 303, 307, 308) and method == "GET" and redirects < 5:
                target = urllib.parse.urljoin(url, lower.get("location", ""))
                # Metadata-only redirects: never carry credentials across a redirect.
                if any(k.lower() in ("authorization", "zotero-api-key") for k in hdr):
                    raise AgentError("Authenticated redirect refused")
                return self.request("GET", target, headers=hdr, redirects=redirects + 1)
            if response.status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                self.blocked_until[host] = max(self.blocked_until.get(host, 0),
                                             time.monotonic() + 2 ** attempt)
                continue
            if not 200 <= response.status < 300:
                raise HttpError(response.status, host)
            return response
