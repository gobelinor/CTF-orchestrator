from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import sys
from urllib import parse, request

from .models import ImportRequest, SourceDocument


def load_source_document(import_request: ImportRequest) -> SourceDocument:
    if import_request.input_file is not None:
        return SourceDocument(
            source_type="local_file",
            source_label=str(import_request.input_file),
            raw_text=import_request.input_file.read_text(encoding="utf-8"),
        )

    if import_request.source == "-":
        return SourceDocument(
            source_type="stdin",
            source_label="stdin",
            raw_text=sys.stdin.read(),
        )

    if import_request.source and _is_http_url(import_request.source):
        return _load_url_document(import_request.source, import_request)

    if import_request.source:
        candidate = Path(import_request.source).expanduser()
        if candidate.exists():
            resolved = candidate.resolve()
            return SourceDocument(
                source_type="local_file",
                source_label=str(resolved),
                raw_text=resolved.read_text(encoding="utf-8"),
            )

    if not sys.stdin.isatty():
        return SourceDocument(
            source_type="stdin",
            source_label="stdin",
            raw_text=sys.stdin.read(),
        )

    raise SystemExit("Provide a source URL/path, --input-file, or pipe text on stdin.")


def _load_url_document(url: str, import_request: ImportRequest) -> SourceDocument:
    cookie_header = resolve_cookie_header(import_request)
    headers = {"User-Agent": "ctf-orchestrator-import/0.1"}
    if cookie_header:
        headers["Cookie"] = cookie_header

    req = request.Request(url, headers=headers)
    with request.urlopen(req) as response:
        content_type = response.headers.get("Content-Type", "")
        charset = response.headers.get_content_charset() or "utf-8"
        raw_body = response.read().decode(charset, errors="replace")
        final_url = response.geturl()

    if "html" not in content_type.lower():
        return SourceDocument(
            source_type="url_text",
            source_label=url,
            raw_text=raw_body,
            fetched_url=final_url,
        )

    parser = _HTMLTextExtractor(base_url=final_url)
    parser.feed(raw_body)
    return SourceDocument(
        source_type="url_html",
        source_label=url,
        raw_text=parser.text(),
        urls=parser.urls,
        fetched_url=final_url,
        raw_html=raw_body,
    )


def resolve_cookie_header(import_request: ImportRequest) -> str | None:
    if import_request.session_cookie and import_request.cookie_file:
        raise SystemExit("Use either --session-cookie or --cookie-file, not both.")
    if import_request.session_cookie:
        cookie_value = import_request.session_cookie.strip()
        if not cookie_value:
            return None
        if "=" not in cookie_value:
            return f"session={cookie_value}"
        return cookie_value
    if import_request.cookie_file is None:
        return None
    raw_text = import_request.cookie_file.read_text(encoding="utf-8").strip()
    return raw_text or None


def _is_http_url(value: str) -> bool:
    parsed = parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


_BLOCK_TAGS = {
    "br",
    "p",
    "div",
    "section",
    "article",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "thead",
    "tbody",
    "table",
    "blockquote",
}

_DROP_TAGS = {"script", "style", "noscript", "svg", "canvas"}

_PRESERVE_WHITESPACE_TAGS = {"pre", "code", "textarea"}


class _HTMLTextExtractor(HTMLParser):
    """HTML → plain text extractor that preserves code blocks and table cells.

    Key improvements vs the naive version:
    - `<pre>` and `<code>` content is kept verbatim (whitespace, newlines).
    - `<script>`, `<style>`, `<noscript>`, `<svg>`, `<canvas>` are dropped.
    - `<table>` cells become tab-separated rows so the structure survives.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._chunks: list[str] = []
        self.urls: list[str] = []
        self._preserve_depth = 0
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag in _PRESERVE_WHITESPACE_TAGS:
            if self._preserve_depth == 0:
                self._chunks.append("\n```\n")
            self._preserve_depth += 1
            return
        if tag == "a":
            for key, value in attrs:
                if key != "href" or not value:
                    continue
                resolved = parse.urljoin(self.base_url, value)
                if resolved not in self.urls:
                    self.urls.append(resolved)
            return
        if tag == "td" or tag == "th":
            self._chunks.append("\t")
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return
        if tag in _PRESERVE_WHITESPACE_TAGS:
            if self._preserve_depth:
                self._preserve_depth -= 1
            if self._preserve_depth == 0:
                self._chunks.append("\n```\n")
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._preserve_depth:
            self._chunks.append(data)
            return
        if data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        cleaned: list[str] = []
        inside_code = False
        for line in raw.splitlines():
            stripped = line.rstrip()
            if stripped.strip() == "```":
                inside_code = not inside_code
                cleaned.append(stripped.strip())
                continue
            if inside_code:
                cleaned.append(line.rstrip())
            else:
                normalized = " ".join(stripped.split())
                if normalized:
                    cleaned.append(normalized)
        # Collapse accidental double-blank-lines while keeping code fences intact.
        output: list[str] = []
        for line in cleaned:
            if line or (output and output[-1] != ""):
                output.append(line)
        return "\n".join(output).strip()
