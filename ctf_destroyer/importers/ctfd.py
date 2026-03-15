from __future__ import annotations

import json
from html.parser import HTMLParser
import re
import socket
import time
from typing import Any
from urllib import error, parse, request

from .models import DiscoveredChallenge, ImportRequest, ImportedChallenge, SourceDocument
from .sources import resolve_cookie_header
from .text import _build_warnings, _extract_target_host, _infer_category, _suggest_operator_hint


def try_discover_ctfd_challenges(
    document: SourceDocument,
    import_request: ImportRequest,
) -> list[DiscoveredChallenge] | None:
    if document.source_type != "url_html":
        return None
    base_url = _ctfd_base_url(document)
    if base_url is None:
        return None

    payload = _fetch_json(
        parse.urljoin(base_url, "/api/v1/challenges"),
        resolve_cookie_header(import_request),
    )
    data = payload.get("data")
    if not isinstance(data, list):
        return None

    challenges: list[DiscoveredChallenge] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name", "")).strip()
        if not title:
            continue
        points = item.get("value")
        solves = item.get("solves")
        category = str(item.get("category", "")).strip() or None
        challenges.append(
            DiscoveredChallenge(
                title=title,
                text_block=title,
                challenge_id=_maybe_int(item.get("id")),
                category=category,
                points=_maybe_int(points),
                solves=_maybe_int(solves),
                source_label=document.source_label,
                warnings=[],
            )
        )
    return challenges or None


def import_ctfd_challenge(
    candidate: DiscoveredChallenge,
    document: SourceDocument,
    import_request: ImportRequest,
) -> ImportedChallenge | None:
    if candidate.challenge_id is None:
        return None
    base_url = _ctfd_base_url(document)
    if base_url is None:
        return None

    payload = _fetch_json(
        parse.urljoin(base_url, f"/api/v1/challenges/{candidate.challenge_id}"),
        resolve_cookie_header(import_request),
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    title = str(data.get("name", "")).strip() or candidate.title
    description_html = str(data.get("description", "") or "")
    description_text = _html_to_text(description_html)
    category = str(data.get("category", "")).strip() or candidate.category or "misc"
    category = _infer_category(title, description_text, None) if not category else category.lower()
    files = [
        parse.urljoin(base_url, str(item))
        for item in list(data.get("files", []))
        if str(item).strip()
    ]
    description_files, description_references = _extract_description_links(description_html, base_url)
    for url in description_files:
        if url not in files:
            files.append(url)
    connection_info = str(data.get("connection_info", "") or "").strip()
    access_entries = _fetch_current_container_access(base_url, candidate.challenge_id, import_request)
    start_result = {
        "status": "not_requested",
        "access_entries": [],
        "warning": None,
    }
    if import_request.start_instance:
        if access_entries:
            start_result = {
                "status": "reused_current",
                "access_entries": list(access_entries),
                "warning": None,
            }
        else:
            start_result = _ensure_container_instance(base_url, candidate.challenge_id, document, import_request)
            if start_result["access_entries"]:
                access_entries = list(start_result["access_entries"])
    target_host = _extract_target_host(connection_info or description_text) or _pick_target_host_from_access(access_entries)
    warnings = _build_warnings(
        _maybe_int(data.get("value")),
        _maybe_int(data.get("solves")),
        target_host,
        files,
    )
    if start_result["warning"]:
        warnings.append(str(start_result["warning"]))

    return ImportedChallenge(
        title=title,
        description=description_text or title,
        category=category,
        target_host=target_host,
        files=files,
        operator_hint=_suggest_operator_hint(category, has_target=bool(target_host), has_files=bool(files)),
        points=_maybe_int(data.get("value")),
        solves=_maybe_int(data.get("solves")),
        play_url=document.fetched_url or document.source_label,
        references=description_references,
        source_snippet=None,
        import_metadata={
            "source_type": document.source_type,
            "source_url": document.fetched_url or document.source_label,
            "extractor": "ctfd_api",
            "challenge_id": candidate.challenge_id,
            "confidence": "high",
            "instance_access": access_entries,
            "start_instance_requested": bool(import_request.start_instance),
            "start_instance_result": start_result["status"],
        },
        warnings=warnings,
    )


def _fetch_json(
    url: str,
    cookie_header: str | None,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    csrf_token: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    headers = {
        "User-Agent": "ctf-destroyer-import/0.1",
        "Accept": "application/json",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if csrf_token:
        headers["CSRF-Token"] = csrf_token
    req = request.Request(url, headers=headers, data=data, method=method)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        return json.loads(payload)


def _fetch_current_container_access(
    base_url: str,
    challenge_id: int,
    import_request: ImportRequest,
) -> list[dict[str, str]]:
    data = _fetch_current_container(base_url, import_request)
    if data is None:
        return []
    current_challenge = _maybe_int(data.get("challenge"))
    if current_challenge != challenge_id:
        return []
    return _normalize_access_entries(data.get("access"))


def _fetch_current_container(
    base_url: str,
    import_request: ImportRequest,
) -> dict[str, Any] | None:
    try:
        payload = _fetch_json(
            parse.urljoin(base_url, "/api/v1/containers/current"),
            resolve_cookie_header(import_request),
        )
    except Exception:
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return data


def _normalize_access_entries(access: Any) -> list[dict[str, str]]:
    if not isinstance(access, list):
        return []
    entries: list[dict[str, str]] = []
    for item in access:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not name and not url:
            continue
        entries.append({"name": name, "url": url})
    return entries


def _ensure_container_instance(
    base_url: str,
    challenge_id: int,
    document: SourceDocument,
    import_request: ImportRequest,
) -> dict[str, Any]:
    csrf_token = _extract_csrf_nonce(document)
    if not csrf_token:
        return {
            "status": "failed",
            "access_entries": [],
            "warning": "Unable to start container instance because no CSRF token was found on the source page.",
        }

    try:
        payload = _fetch_json(
            parse.urljoin(base_url, "/api/v1/containers"),
            resolve_cookie_header(import_request),
            method="POST",
            body={"challenge": challenge_id, "action": "start"},
            csrf_token=csrf_token,
            timeout_seconds=5.0,
        )
    except (TimeoutError, socket.timeout) as exc:
        access_entries = _poll_current_container_access(base_url, challenge_id, import_request, attempts=8, delay_seconds=1.0)
        if access_entries:
            return {
                "status": "started_after_timeout",
                "access_entries": access_entries,
                "warning": None,
            }
        return {
            "status": "failed",
            "access_entries": [],
            "warning": f"Container start request timed out before access became available: {exc}",
        }
    except error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            access_entries = _poll_current_container_access(base_url, challenge_id, import_request, attempts=8, delay_seconds=1.0)
            if access_entries:
                return {
                    "status": "started_after_timeout",
                    "access_entries": access_entries,
                    "warning": None,
                }
        return {
            "status": "failed",
            "access_entries": [],
            "warning": f"Failed to start container instance: {exc}",
        }
    except Exception as exc:
        access_entries = _poll_current_container_access(base_url, challenge_id, import_request, attempts=8, delay_seconds=1.0)
        if access_entries:
            return {
                "status": "started_after_timeout",
                "access_entries": access_entries,
                "warning": None,
            }
        return {
            "status": "failed",
            "access_entries": [],
            "warning": f"Failed to start container instance: {exc}",
        }
    if not payload.get("success"):
        return {
            "status": "failed",
            "access_entries": [],
            "warning": f"Failed to start container instance: {payload.get('error') or 'unknown error'}",
        }

    data = payload.get("data")
    if isinstance(data, dict):
        access_entries = _normalize_access_entries(data.get("access"))
        if access_entries:
            return {
                "status": "started",
                "access_entries": access_entries,
                "warning": None,
            }

    access_entries = _poll_current_container_access(base_url, challenge_id, import_request)
    if access_entries:
        return {
            "status": "started",
            "access_entries": access_entries,
            "warning": None,
        }

    return {
        "status": "started_no_access",
        "access_entries": [],
        "warning": "Container instance started but no access information became available yet.",
    }


def _poll_current_container_access(
    base_url: str,
    challenge_id: int,
    import_request: ImportRequest,
    *,
    attempts: int = 5,
    delay_seconds: float = 1.0,
) -> list[dict[str, str]]:
    for attempt in range(attempts):
        access_entries = _fetch_current_container_access(base_url, challenge_id, import_request)
        if access_entries:
            return access_entries
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    return []


def _extract_csrf_nonce(document: SourceDocument) -> str | None:
    raw_html = document.raw_html or ""
    if not raw_html:
        return None
    patterns = [
        r"csrfNonce['\"]?\]\s*=\s*['\"]([^'\"]+)['\"]",
        r"['\"]csrfNonce['\"]\s*:\s*['\"]([^'\"]+)['\"]",
        r"csrfNonce\s*:\s*['\"]([^'\"]+)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_html)
        if match:
            return match.group(1).strip() or None
    return None


def _extract_description_links(description_html: str, base_url: str) -> tuple[list[str], list[str]]:
    raw_links = re.findall(r'href=["\']([^"\']+)["\']', description_html, flags=re.IGNORECASE)
    files: list[str] = []
    references: list[str] = []
    for raw_link in raw_links:
        url = parse.urljoin(base_url, raw_link.strip())
        if not url:
            continue
        target = files if _looks_like_file_url(url) else references
        if url not in target:
            target.append(url)
    return files, references


def _ctfd_base_url(document: SourceDocument) -> str | None:
    origin = document.fetched_url or document.source_label
    if not origin:
        return None
    parsed = parse.urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _looks_like_file_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.endswith(
        (
            ".py",
            ".zip",
            ".tar",
            ".tar.gz",
            ".tgz",
            ".gz",
            ".bz2",
            ".xz",
            ".txt",
            ".pcap",
            ".png",
            ".jpg",
            ".jpeg",
            ".pdf",
            ".cpp",
            ".c",
            ".rs",
            ".go",
            ".js",
            ".java",
            ".sage",
            ".bin",
        )
    )


def _maybe_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _pick_target_host_from_access(access_entries: list[dict[str, str]]) -> str | None:
    for entry in access_entries:
        raw_url = entry.get("url", "").strip()
        if not raw_url:
            continue
        normalized = raw_url.replace("tcp://", "").replace("tcp/", "")
        host_port = normalized.replace(" ", ":")
        if _looks_like_host_port(host_port):
            return host_port
    return None


def _looks_like_host_port(value: str) -> bool:
    if ":" not in value:
        return False
    host, port = value.rsplit(":", 1)
    if not host or not port.isdigit():
        return False
    return True


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "section", "article", "li", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        lines = [" ".join(chunk.split()) for chunk in "".join(self._chunks).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return parser.text()
