"""Low-level SAP BusinessObjects RESTful Web Service (biprws) helpers.

Everything here is read-only except where a function name says otherwise.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import requests
import urllib3

# Many on-premise BO servers use self-signed certificates.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VERIFY_TLS = False
DEFAULT_TIMEOUT = 15
API_TIMEOUT = 120  # heavy reports can take a while for mapping / data provider calls


class BOError(Exception):
    pass


def short(res) -> str:
    """First 200 characters of an HTTP response body, single line (for logs)."""
    try:
        return (res.text or "").strip().replace("\n", " ")[:200]
    except Exception:
        return ""


def make_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-SAP-LogonToken": token,
        "X-SAP-PVL": "en-US",
    }


def logon(server: str, username: str, password: str, auth: str = "secEnterprise") -> tuple[str | None, str | None]:
    """Returns (token, error)."""
    try:
        res = requests.post(
            f"{server}/logon/long",
            json={"userName": username, "password": password, "auth": auth},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            verify=VERIFY_TLS, timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as e:
        return None, f"Server unreachable: {e}"
    if res.status_code != 200:
        return None, f"Logon failed (HTTP {res.status_code}): {short(res)}"
    token = res.headers.get("X-SAP-LogonToken")
    if not token:
        return None, "Logon returned 200 but no X-SAP-LogonToken header."
    return token, None


def logoff(server: str, headers: dict) -> None:
    """Always log off: BO limits concurrent sessions per licence."""
    try:
        requests.post(f"{server}/logoff", headers=headers, verify=VERIFY_TLS, timeout=10)
    except Exception:
        pass


@contextmanager
def bo_session(server: str, username: str, password: str):
    """`with bo_session(...) as headers:` — logs on, yields request headers, always logs off."""
    token, err = logon(server, username, password)
    if not token:
        raise BOError(err)
    headers = make_headers(token)
    try:
        yield headers
    finally:
        logoff(server, headers)


def _entries(data: dict) -> list:
    entries = data.get("entries", data.get("entry", [])) or []
    return [entries] if isinstance(entries, dict) else entries


def infostore_query(server: str, headers: dict, query: str) -> tuple[list | None, str | None]:
    """Runs a CMS query. Tries POST /v1/cmsquery (BI 4.2 SP03+) then the older GET /infostore?query=."""
    attempts = []
    try:
        res = requests.post(f"{server}/v1/cmsquery", headers=headers, json={"query": query},
                            verify=VERIFY_TLS, timeout=DEFAULT_TIMEOUT)
        if res.status_code == 200:
            try:
                return _entries(res.json()), None
            except ValueError:
                attempts.append("cmsquery: response is not JSON")
        else:
            attempts.append(f"cmsquery HTTP {res.status_code}: {short(res)}")
    except requests.RequestException as e:
        attempts.append(f"cmsquery request failed: {e}")

    try:
        res = requests.get(f"{server}/infostore", headers=headers, params={"query": query},
                           verify=VERIFY_TLS, timeout=DEFAULT_TIMEOUT)
        if res.status_code == 200:
            try:
                return _entries(res.json()), None
            except ValueError:
                attempts.append("infostore: response is not JSON")
        else:
            attempts.append(f"infostore HTTP {res.status_code}: {short(res)}")
    except requests.RequestException as e:
        attempts.append(f"infostore request failed: {e}")

    return None, " | ".join(attempts)


def cmsquery_all(server: str, headers: dict, query: str, page_size: int = 500, max_pages: int = 400):
    """POST /v1/cmsquery across all pages. Returns (entries, error)."""
    out, page, err = [], 1, None
    h = {**headers, "Content-Type": "application/json", "Accept": "application/json"}
    for _ in range(max_pages):
        try:
            resp = requests.post(f"{server}/v1/cmsquery", data=json.dumps({"query": query}).encode("utf-8"),
                                 headers=h, params={"page": page, "pagesize": page_size},
                                 verify=VERIFY_TLS, timeout=60)
        except requests.RequestException as e:
            err = f"request failed: {e}"
            break
        if resp.status_code != 200:
            err = f"HTTP {resp.status_code}: {short(resp)}"
            break
        try:
            entries = resp.json().get("entries", [])
        except ValueError as e:
            err = f"parse error: {e}"
            break
        out.extend(entries)
        if len(entries) < page_size:
            break
        page += 1
    return out, err


def rest_collect_all(url: str, headers: dict, max_pages: int = 500, page_size: int = 200):
    """Collects every record from a paged REST collection.

    Some servers ignore `pageSize` and return smaller pages, so instead of stopping on a
    short page we keep asking until a page brings no new IDs. Returns (records, error).
    """
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        try:
            res = requests.get(url, headers=headers, params={"page": page, "pageSize": page_size},
                               verify=VERIFY_TLS, timeout=30)
        except requests.RequestException as e:
            return out, f"request error (page {page}): {e}"
        if res.status_code != 200:
            if page == 1:
                return [], f"HTTP {res.status_code}: {short(res)}"
            break
        try:
            data = res.json()
        except ValueError:
            return out, f"response is not JSON (page {page})"

        entries = data.get("entries")
        for wrapper, item in (("usergroups", "usergroup"), ("users", "user")):
            if entries is None and isinstance(data.get(wrapper), dict):
                entries = data[wrapper].get(item)
        if entries is None:
            entries = data.get("entry", [])
        entries = [entries] if isinstance(entries, dict) else (entries or [])

        new = 0
        for e in entries:
            key = str(e.get("SI_ID") or e.get("id") or e.get("cuid") or id(e))
            if key not in seen:
                seen.add(key)
                out.append(e)
                new += 1
        if new == 0:
            break
    return out, None
