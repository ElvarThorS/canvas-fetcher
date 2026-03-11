#!/usr/bin/env python3
"""Fetch Canvas course data and store it locally.

This script focuses on read-only backup behavior for one or more courses:
- Course metadata
- Modules and module items
- Assignments
- Pages
- Folders and files metadata
- Course tabs
- Structured assignment/module exports for easy offline browsing

Optional:
- Download file binaries that are accessible to the authenticated user
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import requests


DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 5
DEFAULT_PER_PAGE = 100
DEFAULT_TOKEN_ENV = "CANVAS_TOKEN"
DEFAULT_OUT_DIR = "backup"
DEFAULT_SAVED_CONFIG = "courses.json"
ENV_OUT_DIR = "CANVAS_FETCHER_OUT_DIR"
ENV_SAVED_CONFIG = "CANVAS_FETCHER_SAVED_CONFIG"
USER_AGENT = "canvas-fetcher/0.1"
DEFAULT_CHANGE_ID_LIMIT = 200
HTML_URL_ATTRIBUTES = {
    "href",
    "src",
    "data-api-endpoint",
    "data-download-url",
    "data-src",
    "data-fullsize",
}


class CanvasAPIError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_base_url(url: str) -> str:
    normalized = url.strip()
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized}"
    return normalized.rstrip("/")


def parse_link_header(header_value: str | None) -> dict[str, str]:
    links: dict[str, str] = {}
    if not header_value:
        return links

    for part in header_value.split(","):
        section = part.strip()
        if not section.startswith("<"):
            continue
        if ">" not in section:
            continue

        url_part, meta = section.split(">", 1)
        url = url_part[1:]

        match = re.search(r'rel="([^"]+)"', meta)
        if match:
            links[match.group(1)] = url

    return links


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"Warning: could not read JSON from {path}: {exc}")
        return None


def stable_json_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def ensure_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _first_present_value(record: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def build_record_index(
    records: list[Any], id_keys: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        identity = _first_present_value(item, id_keys)
        if identity is None:
            continue
        indexed[str(identity)] = item
    return indexed


def summarize_ids(ids: list[str], *, limit: int) -> dict[str, Any]:
    ordered = sorted(ids)
    return {
        "count": len(ordered),
        "sample": ordered[:limit],
        "truncated": len(ordered) > limit,
    }


def records_are_updated(
    previous_record: dict[str, Any],
    current_record: dict[str, Any],
    *,
    updated_keys: tuple[str, ...],
) -> bool:
    for key in updated_keys:
        previous_value = previous_record.get(key)
        current_value = current_record.get(key)
        if (previous_value is not None or current_value is not None) and (
            previous_value != current_value
        ):
            return True
    return previous_record != current_record


def compute_list_changes(
    previous_records: list[Any],
    current_records: list[Any],
    *,
    id_keys: tuple[str, ...],
    updated_keys: tuple[str, ...] = ("updated_at", "modified_at"),
    id_limit: int = DEFAULT_CHANGE_ID_LIMIT,
) -> dict[str, Any]:
    previous_index = build_record_index(previous_records, id_keys)
    current_index = build_record_index(current_records, id_keys)

    previous_ids = set(previous_index.keys())
    current_ids = set(current_index.keys())

    added_ids = list(current_ids - previous_ids)
    removed_ids = list(previous_ids - current_ids)
    updated_ids: list[str] = []
    unchanged_ids: list[str] = []

    for record_id in previous_ids & current_ids:
        previous_record = previous_index[record_id]
        current_record = current_index[record_id]
        if records_are_updated(
            previous_record,
            current_record,
            updated_keys=updated_keys,
        ):
            updated_ids.append(record_id)
        else:
            unchanged_ids.append(record_id)

    return {
        "previous_total": len(previous_index),
        "current_total": len(current_index),
        "added": summarize_ids(added_ids, limit=id_limit),
        "updated": summarize_ids(updated_ids, limit=id_limit),
        "removed": summarize_ids(removed_ids, limit=id_limit),
        "unchanged": summarize_ids(unchanged_ids, limit=id_limit),
    }


def compute_keyed_blob_changes(
    previous_map: dict[str, Any],
    current_map: dict[str, Any],
    *,
    id_limit: int = DEFAULT_CHANGE_ID_LIMIT,
) -> dict[str, Any]:
    previous_ids = set(previous_map.keys())
    current_ids = set(current_map.keys())

    added_ids = list(current_ids - previous_ids)
    removed_ids = list(previous_ids - current_ids)
    updated_ids: list[str] = []
    unchanged_ids: list[str] = []

    for record_id in previous_ids & current_ids:
        if previous_map[record_id] != current_map[record_id]:
            updated_ids.append(record_id)
        else:
            unchanged_ids.append(record_id)

    return {
        "previous_total": len(previous_map),
        "current_total": len(current_map),
        "added": summarize_ids(added_ids, limit=id_limit),
        "updated": summarize_ids(updated_ids, limit=id_limit),
        "removed": summarize_ids(removed_ids, limit=id_limit),
        "unchanged": summarize_ids(unchanged_ids, limit=id_limit),
    }


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "unnamed"


def sanitize_course_dirname(name: str, max_len: int = 80) -> str:
    cleaned = sanitize_filename(name)
    cleaned = cleaned.replace(" ", "-")
    cleaned = re.sub(r"-+", "-", cleaned).strip("-._")
    if not cleaned:
        cleaned = "course"
    return cleaned[:max_len]


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(content)


def format_order_prefix(position: int, total: int) -> str:
    width = max(2, len(str(max(1, total))))
    return f"{position:0{width}d}"


class HTMLURLExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for attr_name, attr_value in attrs:
            if not attr_value:
                continue
            if attr_name.lower() in HTML_URL_ATTRIBUTES:
                self.urls.add(attr_value)


def extract_urls_from_html(html: str) -> set[str]:
    if not html:
        return set()

    parser = HTMLURLExtractor()
    try:
        parser.feed(html)
    except Exception:
        return set()
    return parser.urls


def extract_canvas_file_ids_from_url(value: str) -> set[str]:
    if not value:
        return set()

    decoded = unquote(value)
    file_ids: set[str] = set()
    patterns = [
        r"/api/v1/files/(\d+)",
        r"/courses/\d+/files/(\d+)",
        r"/files/(\d+)",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, decoded):
            file_ids.add(str(match))
    return file_ids


def extract_canvas_file_ids_from_html(html: str) -> set[str]:
    file_ids: set[str] = set()
    for url in extract_urls_from_html(html):
        file_ids.update(extract_canvas_file_ids_from_url(url))
    return file_ids


def extract_canvas_file_ids_from_fields(
    payload: dict[str, Any],
    fields: tuple[str, ...],
) -> set[str]:
    file_ids: set[str] = set()
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str):
            file_ids.update(extract_canvas_file_ids_from_url(value))
    return file_ids


class CanvasClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        per_page: int,
        timeout: float,
        retries: int,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.per_page = per_page
        self.timeout = timeout
        self.retries = retries

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
        )

    def _build_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = f"/{path_or_url}"
        return f"{self.base_url}{path_or_url}"

    def _raise_for_response(self, response: requests.Response) -> None:
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            if "errors" in payload and isinstance(payload["errors"], list):
                messages: list[str] = []
                for err in payload["errors"]:
                    if isinstance(err, dict) and err.get("message"):
                        messages.append(str(err["message"]))
                    elif isinstance(err, str):
                        messages.append(err)
                detail = "; ".join(messages)
            elif "message" in payload:
                detail = str(payload["message"])
            elif "error" in payload:
                detail = str(payload["error"])
        elif response.text:
            detail = response.text.strip()[:300]

        suffix = f": {detail}" if detail else ""
        raise CanvasAPIError(
            f"{response.status_code} {response.reason} for "
            f"{response.request.method} {response.url}{suffix}"
        )

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> requests.Response:
        url = self._build_url(path_or_url)

        attempt = 0
        while True:
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    timeout=self.timeout,
                    stream=stream,
                )
            except requests.RequestException as exc:
                if attempt >= self.retries:
                    raise CanvasAPIError(
                        f"Request failed for {method} {url}: {exc}"
                    ) from exc
                wait_seconds = min(30, 2**attempt)
                time.sleep(wait_seconds)
                attempt += 1
                continue

            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt >= self.retries:
                    self._raise_for_response(response)
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_seconds = int(retry_after)
                else:
                    wait_seconds = min(30, 2**attempt)
                time.sleep(wait_seconds)
                attempt += 1
                continue

            if response.status_code >= 400:
                self._raise_for_response(response)

            return response

    def get_json(
        self, path_or_url: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        response = self.request("GET", path_or_url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise CanvasAPIError(
                f"Expected JSON response for GET {response.url}"
            ) from exc

    def get_paginated(
        self, path_or_url: str, *, params: dict[str, Any] | None = None
    ) -> list[Any]:
        combined: list[Any] = []

        first_params: dict[str, Any] = {"per_page": self.per_page}
        if params:
            first_params.update(params)

        next_url: str | None = path_or_url
        next_params: dict[str, Any] | None = first_params

        while next_url:
            response = self.request("GET", next_url, params=next_params)
            payload = response.json()
            if not isinstance(payload, list):
                raise CanvasAPIError(
                    f"Expected paginated list from {response.url}, got {type(payload).__name__}"
                )

            combined.extend(payload)
            links = parse_link_header(response.headers.get("Link"))
            next_url = links.get("next")
            next_params = None

        return combined

    def download_to_path(self, file_url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self.request("GET", file_url, stream=True)
        with destination.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)


def fetch_optional_list(name: str, func: Any) -> tuple[list[Any], str | None]:
    try:
        return func(), None
    except CanvasAPIError as exc:
        log(f"Warning: failed to fetch {name}: {exc}")
        return [], str(exc)


def read_env_value_from_file(path: Path, key: str) -> str | None:
    if not path.exists() or not path.is_file():
        return None

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        candidate_key, candidate_value = line.split("=", 1)
        if candidate_key.strip() != key:
            continue

        value = candidate_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value

    return None


def resolve_token(
    explicit_token: str | None,
    token_env: str,
    env_file_candidates: list[Path] | None = None,
) -> str:
    if explicit_token:
        return explicit_token
    env_token = os.getenv(token_env)
    if env_token:
        return env_token

    for candidate in env_file_candidates or []:
        token_from_file = read_env_value_from_file(candidate, token_env)
        if token_from_file:
            return token_from_file

    raise SystemExit(
        f"No token provided. Pass --token or set {token_env} in your environment."
    )


def resolve_setting_from_env_files(key: str, env_file_candidates: list[Path]) -> str | None:
    for candidate in env_file_candidates:
        value = read_env_value_from_file(candidate, key)
        if value:
            return value
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Canvas course data and save it locally."
    )
    parser.add_argument(
        "--base-url",
        required=False,
        help="Canvas base URL, e.g. https://canvas.instructure.com",
    )
    parser.add_argument(
        "--course-id",
        action="append",
        help="Canvas course ID. Repeat to fetch multiple courses.",
    )
    parser.add_argument(
        "--use-saved",
        action="store_true",
        help="Load base URL and all course IDs from --saved-config",
    )
    parser.add_argument(
        "--saved-config",
        default=None,
        help=(
            "Path to saved course config JSON "
            f"(default: {DEFAULT_SAVED_CONFIG}, env: {ENV_SAVED_CONFIG})"
        ),
    )
    parser.add_argument(
        "--token",
        help="Canvas access token. If omitted, token is read from --token-env.",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help=f"Environment variable used for token lookup (default: {DEFAULT_TOKEN_ENV})",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help=(
            "Directory where backups are written "
            f"(default: {DEFAULT_OUT_DIR}, env: {ENV_OUT_DIR})"
        ),
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=DEFAULT_PER_PAGE,
        help=f"Canvas API page size (default: {DEFAULT_PER_PAGE})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Retry attempts for transient failures (default: {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "--download-files",
        action="store_true",
        help="Download accessible file binaries into the backup folder",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Reuse previous local snapshots where possible and emit a change summary "
            "between runs"
        ),
    )
    parser.add_argument(
        "--change-id-limit",
        type=int,
        default=DEFAULT_CHANGE_ID_LIMIT,
        help=(
            "Max IDs to include per change bucket in changes.json "
            f"(default: {DEFAULT_CHANGE_ID_LIMIT})"
        ),
    )
    return parser.parse_args()


def page_cache_key(page: dict[str, Any]) -> str | None:
    page_id = page.get("page_id")
    if page_id is not None:
        return f"id:{page_id}"
    page_url = page.get("url")
    if page_url:
        return f"url:{page_url}"
    return None


def normalize_course_ids(raw_course_ids: list[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_course_ids:
        course_id = str(raw).strip()
        if not course_id or course_id in seen:
            continue
        normalized.append(course_id)
        seen.add(course_id)
    return normalized


def load_saved_config(path: Path) -> tuple[str | None, list[str]]:
    payload = read_json(path)
    if payload is None:
        raise SystemExit(f"Saved config not found or unreadable: {path}")
    if not isinstance(payload, dict):
        raise SystemExit(f"Saved config must be a JSON object: {path}")

    base_url = payload.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        raise SystemExit(f"saved_config.base_url must be a string in: {path}")

    raw_course_ids = payload.get("course_ids", [])
    if not isinstance(raw_course_ids, list):
        raise SystemExit(f"saved_config.course_ids must be an array in: {path}")

    return base_url, normalize_course_ids(raw_course_ids)


def resolve_saved_config_path(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()

    cwd_candidate = candidate.resolve()
    if cwd_candidate.exists():
        return cwd_candidate

    script_candidate = (Path(__file__).resolve().parent / candidate).resolve()
    return script_candidate


def export_structured_course_content(
    client: CanvasClient,
    *,
    course_id: str,
    course_dir: Path,
    modules: list[Any],
    module_items_by_module: dict[str, list[Any]],
    assignments: list[Any],
    pages: list[Any],
    page_details: dict[str, Any],
    files: list[Any],
) -> dict[str, Any]:
    assignments_dir = course_dir / "assignments"
    modules_dir = course_dir / "modules"
    linked_cache_dir = course_dir / ".linked_file_cache"

    for generated_dir in (assignments_dir, modules_dir):
        if generated_dir.exists():
            shutil.rmtree(generated_dir)

    assignments_dir.mkdir(parents=True, exist_ok=True)
    modules_dir.mkdir(parents=True, exist_ok=True)
    linked_cache_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[dict[str, Any]] = []
    file_metadata_cache: dict[str, dict[str, Any]] = build_record_index(files, ("id",))
    cached_file_paths: dict[str, Path] = {}

    pages_by_id: dict[str, dict[str, Any]] = {}
    pages_by_url: dict[str, dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_id = page.get("page_id")
        page_url = page.get("url")
        if page_id is not None:
            pages_by_id[str(page_id)] = page
        if isinstance(page_url, str) and page_url:
            pages_by_url[page_url] = page

    assignments_by_id = build_record_index(assignments, ("id",))

    stats = {
        "assignments_exported": 0,
        "modules_exported": 0,
        "module_items_exported": 0,
        "assignment_linked_files_copied": 0,
        "module_linked_files_copied": 0,
        "linked_files_unique_downloaded": 0,
        "linked_files_missing": 0,
    }

    def add_warning(context: str, message: str, *, file_id: str | None = None) -> None:
        warning: dict[str, Any] = {
            "context": context,
            "message": message,
        }
        if file_id is not None:
            warning["file_id"] = file_id
        warnings.append(warning)

    def resolve_file_metadata(file_id: str, *, context: str) -> dict[str, Any] | None:
        if file_id in file_metadata_cache:
            return file_metadata_cache[file_id]

        try:
            file_obj = client.get_json(f"/api/v1/files/{file_id}")
        except CanvasAPIError as exc:
            add_warning(
                context,
                f"Unable to resolve file metadata for file_id={file_id}: {exc}",
                file_id=file_id,
            )
            return None

        if isinstance(file_obj, dict):
            file_metadata_cache[file_id] = file_obj
            return file_obj

        add_warning(
            context,
            f"File metadata response for file_id={file_id} was not an object",
            file_id=file_id,
        )
        return None

    def ensure_file_cached(
        file_obj: dict[str, Any],
        *,
        context: str,
    ) -> Path | None:
        raw_id = file_obj.get("id")
        if raw_id is None:
            add_warning(context, "File object missing id")
            return None
        file_id = str(raw_id)

        if file_id in cached_file_paths:
            return cached_file_paths[file_id]

        display_name = file_obj.get("display_name") or file_obj.get("filename")
        safe_name = sanitize_filename(str(display_name or f"file_{file_id}"))
        cache_path = linked_cache_dir / f"{file_id}_{safe_name}"

        expected_size = file_obj.get("size")
        if (
            cache_path.exists()
            and isinstance(expected_size, int)
            and cache_path.stat().st_size == expected_size
        ):
            cached_file_paths[file_id] = cache_path
            return cache_path
        if cache_path.exists() and not isinstance(expected_size, int):
            cached_file_paths[file_id] = cache_path
            return cache_path

        download_url = file_obj.get("url")
        if not isinstance(download_url, str) or not download_url:
            refreshed = resolve_file_metadata(file_id, context=context)
            if not refreshed:
                return None
            download_url = refreshed.get("url")
            if not isinstance(download_url, str) or not download_url:
                add_warning(
                    context,
                    f"File {file_id} has no downloadable url",
                    file_id=file_id,
                )
                return None

        try:
            client.download_to_path(download_url, cache_path)
            stats["linked_files_unique_downloaded"] += 1
        except CanvasAPIError as exc:
            add_warning(
                context,
                f"Failed downloading file {file_id}: {exc}",
                file_id=file_id,
            )
            return None

        cached_file_paths[file_id] = cache_path
        return cache_path

    def copy_file_id_to_dir(
        file_id: str,
        destination_dir: Path,
        *,
        context: str,
        bucket: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"file_id": file_id}
        file_obj = resolve_file_metadata(file_id, context=context)
        if not file_obj:
            stats["linked_files_missing"] += 1
            result["status"] = "missing"
            return result

        cached_path = ensure_file_cached(file_obj, context=context)
        if not cached_path:
            stats["linked_files_missing"] += 1
            result["status"] = "missing"
            return result

        destination_dir.mkdir(parents=True, exist_ok=True)
        destination_path = destination_dir / cached_path.name
        if not destination_path.exists() or (
            destination_path.stat().st_size != cached_path.stat().st_size
        ):
            shutil.copy2(cached_path, destination_path)

        result["status"] = "copied"
        result["path"] = str(destination_path)
        if bucket == "assignment":
            stats["assignment_linked_files_copied"] += 1
        elif bucket == "module":
            stats["module_linked_files_copied"] += 1
        return result

    def collect_assignment_file_ids(assignment: dict[str, Any]) -> set[str]:
        file_ids: set[str] = set()
        description = assignment.get("description")
        if isinstance(description, str):
            file_ids.update(extract_canvas_file_ids_from_html(description))

        attachments = assignment.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                attachment_id = attachment.get("id")
                if attachment_id is not None:
                    file_ids.add(str(attachment_id))
                file_ids.update(
                    extract_canvas_file_ids_from_fields(
                        attachment,
                        ("url", "preview_url", "thumbnail_url", "html_url"),
                    )
                )

        return file_ids

    def resolve_page_for_module_item(item: dict[str, Any]) -> dict[str, Any] | None:
        page_url = item.get("page_url")
        content_id = item.get("content_id")

        page_obj: dict[str, Any] | None = None
        if isinstance(page_url, str) and page_url:
            page_obj = pages_by_url.get(page_url)
        if page_obj is None and content_id is not None:
            page_obj = pages_by_id.get(str(content_id))

        if isinstance(page_obj, dict) and isinstance(page_obj.get("body"), str):
            return page_obj

        detail_candidates: list[str] = []
        if isinstance(page_obj, dict):
            if page_obj.get("page_id") is not None:
                detail_candidates.append(str(page_obj.get("page_id")))
            if isinstance(page_obj.get("url"), str):
                detail_candidates.append(str(page_obj.get("url")))
                detail_candidates.append(quote(str(page_obj.get("url")), safe=":"))
        if isinstance(page_url, str) and page_url:
            detail_candidates.append(page_url)
            detail_candidates.append(quote(page_url, safe=":"))
        if content_id is not None:
            detail_candidates.append(str(content_id))

        for candidate in detail_candidates:
            detail_obj = page_details.get(candidate)
            if isinstance(detail_obj, dict):
                return detail_obj

        if not (isinstance(page_url, str) and page_url):
            return page_obj

        try:
            detail_obj = client.get_json(
                f"/api/v1/courses/{course_id}/pages/{quote(page_url, safe=':')}"
            )
        except CanvasAPIError as exc:
            add_warning(
                f"module_item:{item.get('id', 'unknown')}",
                f"Failed to fetch page '{page_url}': {exc}",
            )
            return page_obj

        if isinstance(detail_obj, dict):
            if detail_obj.get("page_id") is not None:
                page_details[str(detail_obj["page_id"])] = detail_obj
            if isinstance(detail_obj.get("url"), str):
                page_details[str(detail_obj["url"])] = detail_obj
            return detail_obj
        return page_obj

    assignment_index: list[dict[str, Any]] = []
    total_assignments = len(assignments)
    for position, assignment in enumerate(assignments, start=1):
        if not isinstance(assignment, dict):
            continue

        assignment_id = assignment.get("id")
        assignment_name = str(assignment.get("name") or f"assignment-{position}")
        assignment_id_label = str(assignment_id) if assignment_id is not None else "no-id"
        assignment_dir_name = (
            f"{format_order_prefix(position, total_assignments)}_"
            f"{assignment_id_label}_"
            f"{sanitize_course_dirname(assignment_name, max_len=72)}"
        )
        assignment_dir = assignments_dir / assignment_dir_name
        assignment_dir.mkdir(parents=True, exist_ok=True)

        write_json(assignment_dir / "assignment.json", assignment)
        description = assignment.get("description")
        write_text(assignment_dir / "description.html", str(description or ""))

        linked_file_ids = sorted(collect_assignment_file_ids(assignment))
        linked_results: list[dict[str, Any]] = []
        for file_id in linked_file_ids:
            linked_results.append(
                copy_file_id_to_dir(
                    file_id,
                    assignment_dir / "files",
                    context=f"assignment:{assignment_id_label}",
                    bucket="assignment",
                )
            )

        write_json(assignment_dir / "linked_files.json", linked_results)
        assignment_index.append(
            {
                "id": assignment_id,
                "name": assignment_name,
                "path": str(assignment_dir),
                "linked_files": len(linked_results),
            }
        )
        stats["assignments_exported"] += 1

    write_json(assignments_dir / "index.json", assignment_index)

    module_index: list[dict[str, Any]] = []
    total_modules = len(modules)
    for module_position, module in enumerate(modules, start=1):
        if not isinstance(module, dict):
            continue

        module_id = module.get("id")
        module_name = str(module.get("name") or f"module-{module_position}")
        module_id_label = str(module_id) if module_id is not None else "no-id"
        module_dir_name = (
            f"{format_order_prefix(module_position, total_modules)}_"
            f"{module_id_label}_"
            f"{sanitize_course_dirname(module_name, max_len=72)}"
        )
        module_dir = modules_dir / module_dir_name
        module_dir.mkdir(parents=True, exist_ok=True)
        write_json(module_dir / "module.json", module)

        module_items = module.get("items")
        if not isinstance(module_items, list) and module_id is not None:
            module_items = module_items_by_module.get(str(module_id), [])
        if not isinstance(module_items, list):
            module_items = []

        module_item_entries: list[dict[str, Any]] = []
        total_items = len(module_items)
        for item_position, item in enumerate(module_items, start=1):
            if not isinstance(item, dict):
                continue

            item_id = item.get("id")
            item_title = str(item.get("title") or f"item-{item_position}")
            item_id_label = str(item_id) if item_id is not None else "no-id"
            item_dir_name = (
                f"{format_order_prefix(item_position, total_items)}_"
                f"{item_id_label}_"
                f"{sanitize_course_dirname(item_title, max_len=72)}"
            )
            item_dir = module_dir / item_dir_name
            item_dir.mkdir(parents=True, exist_ok=True)
            write_json(item_dir / "item.json", item)

            item_type = str(item.get("type") or "")
            content_id = item.get("content_id")

            linked_file_ids = extract_canvas_file_ids_from_fields(
                item,
                ("url", "html_url", "external_url"),
            )
            content_details = item.get("content_details")
            if isinstance(content_details, dict):
                linked_file_ids.update(
                    extract_canvas_file_ids_from_fields(
                        content_details,
                        ("url", "html_url"),
                    )
                )
                detail_files = content_details.get("files")
                if isinstance(detail_files, list):
                    for detail_file in detail_files:
                        if isinstance(detail_file, dict) and detail_file.get("id") is not None:
                            linked_file_ids.add(str(detail_file.get("id")))

            if item_type == "File" and content_id is not None:
                linked_file_ids.add(str(content_id))

            if item_type == "Assignment" and content_id is not None:
                assignment_obj = assignments_by_id.get(str(content_id))
                if isinstance(assignment_obj, dict):
                    write_json(item_dir / "content_assignment.json", assignment_obj)
                    assignment_html = str(assignment_obj.get("description") or "")
                    write_text(item_dir / "content.html", assignment_html)
                    linked_file_ids.update(collect_assignment_file_ids(assignment_obj))

            if item_type == "Page":
                page_obj = resolve_page_for_module_item(item)
                if isinstance(page_obj, dict):
                    write_json(item_dir / "content_page.json", page_obj)
                    page_html = str(page_obj.get("body") or "")
                    write_text(item_dir / "content.html", page_html)
                    linked_file_ids.update(extract_canvas_file_ids_from_html(page_html))

            linked_results: list[dict[str, Any]] = []
            for file_id in sorted(linked_file_ids):
                linked_results.append(
                    copy_file_id_to_dir(
                        file_id,
                        item_dir / "files",
                        context=f"module_item:{item_id_label}",
                        bucket="module",
                    )
                )
            write_json(item_dir / "linked_files.json", linked_results)

            module_item_entries.append(
                {
                    "id": item_id,
                    "title": item_title,
                    "type": item_type,
                    "path": str(item_dir),
                    "linked_files": len(linked_results),
                }
            )
            stats["module_items_exported"] += 1

        write_json(module_dir / "index.json", module_item_entries)
        module_index.append(
            {
                "id": module_id,
                "name": module_name,
                "path": str(module_dir),
                "items": len(module_item_entries),
            }
        )
        stats["modules_exported"] += 1

    write_json(modules_dir / "index.json", module_index)

    return {
        "paths": {
            "assignments": str(assignments_dir),
            "modules": str(modules_dir),
            "linked_file_cache": str(linked_cache_dir),
        },
        "counts": stats,
        "warnings": warnings,
    }


def sync_course(
    client: CanvasClient,
    args: argparse.Namespace,
    *,
    base_url: str,
    course_id: str,
) -> dict[str, Any]:
    out_root = Path(args.out_dir).expanduser().resolve()
    log(f"Fetching course {course_id} from {normalize_base_url(base_url)}")
    try:
        course = client.get_json(f"/api/v1/courses/{course_id}")
    except CanvasAPIError as exc:
        error_message = f"Failed to fetch course metadata for {course_id}: {exc}"
        log(error_message)
        return {
            "course_id": course_id,
            "status": "failed",
            "error": str(exc),
        }

    course_name = str(course.get("name") or course.get("course_code") or "course")
    course_dir_name = f"{course_id}_{sanitize_course_dirname(course_name)}"
    course_dir = out_root / course_dir_name
    legacy_course_dir = out_root / course_id
    if (
        not course_dir.exists()
        and legacy_course_dir.exists()
        and legacy_course_dir.is_dir()
    ):
        try:
            legacy_course_dir.rename(course_dir)
            log(f"Renamed legacy course folder to: {course_dir.name}")
        except OSError as exc:
            log(
                "Warning: could not rename legacy folder "
                f"{legacy_course_dir} to {course_dir}: {exc}"
            )
            course_dir = legacy_course_dir

    raw_dir = course_dir / "raw"
    files_dir = course_dir / "files"

    previous_course = read_json(raw_dir / "course.json")
    previous_modules = ensure_list(read_json(raw_dir / "modules.json"))
    previous_module_items_by_module = ensure_dict(
        read_json(raw_dir / "module_items_by_module.json")
    )
    previous_assignments = ensure_list(read_json(raw_dir / "assignments.json"))
    previous_pages = ensure_list(read_json(raw_dir / "pages.json"))
    previous_page_details = ensure_dict(read_json(raw_dir / "page_details.json"))
    previous_folders = ensure_list(read_json(raw_dir / "folders.json"))
    previous_files = ensure_list(read_json(raw_dir / "files.json"))
    previous_tabs = ensure_list(read_json(raw_dir / "tabs.json"))

    previous_snapshot_exists = any(
        [
            previous_course is not None,
            bool(previous_modules),
            bool(previous_assignments),
            bool(previous_pages),
            bool(previous_folders),
            bool(previous_files),
            bool(previous_tabs),
        ]
    )

    endpoint_errors: dict[str, str] = {}
    incremental_stats = {
        "reused_module_item_lists": 0,
        "fetched_module_item_lists": 0,
        "reused_page_details": 0,
        "fetched_page_details": 0,
    }

    if args.incremental:
        if previous_snapshot_exists:
            log(
                "Incremental mode enabled: attempting to reuse unchanged nested resources"
            )
        else:
            log(
                "Incremental mode enabled: no previous snapshot found, running full first sync"
            )

    modules, error = fetch_optional_list(
        "modules",
        lambda: client.get_paginated(
            f"/api/v1/courses/{course_id}/modules",
            params={"include[]": ["items", "content_details"]},
        ),
    )
    if error:
        endpoint_errors["modules"] = error

    module_items_by_module: dict[str, list[Any]] = {}
    previous_modules_index = build_record_index(previous_modules, ("id",))
    for module in modules:
        if not isinstance(module, dict):
            continue

        module_id = module.get("id")
        inline_items = module.get("items")
        if module_id is None or inline_items is not None:
            continue

        module_key = str(module_id)
        if args.incremental and previous_snapshot_exists:
            previous_module = previous_modules_index.get(module_key)
            previous_items = previous_module_items_by_module.get(module_key)
            if previous_module == module and isinstance(previous_items, list):
                module_items_by_module[module_key] = previous_items
                incremental_stats["reused_module_item_lists"] += 1
                continue

        try:
            module_items = client.get_paginated(
                f"/api/v1/courses/{course_id}/modules/{module_id}/items",
                params={"include[]": "content_details"},
            )
            module_items_by_module[module_key] = module_items
            incremental_stats["fetched_module_item_lists"] += 1
        except CanvasAPIError as exc:
            endpoint_errors[f"module_items_{module_id}"] = str(exc)
            log(f"Warning: failed module item fetch for module {module_id}: {exc}")

    assignments, error = fetch_optional_list(
        "assignments",
        lambda: client.get_paginated(
            f"/api/v1/courses/{course_id}/assignments",
            params={"order_by": "position"},
        ),
    )
    if error:
        endpoint_errors["assignments"] = error

    pages, error = fetch_optional_list(
        "pages",
        lambda: client.get_paginated(
            f"/api/v1/courses/{course_id}/pages",
            params={"include[]": "body"},
        ),
    )
    if error:
        endpoint_errors["pages"] = error

    page_details: dict[str, Any] = {}
    previous_pages_index: dict[str, dict[str, Any]] = {}
    for previous_page in previous_pages:
        if not isinstance(previous_page, dict):
            continue
        cache_key = page_cache_key(previous_page)
        if cache_key:
            previous_pages_index[cache_key] = previous_page

    for page in pages:
        if not isinstance(page, dict):
            continue

        if "body" in page:
            continue

        page_id = page.get("page_id")
        raw_identifier = page.get("url")
        if raw_identifier:
            identifier = quote(str(raw_identifier), safe=":")
        elif page_id is not None:
            identifier = f"page_id:{page_id}"
        else:
            continue

        detail_key = str(page_id if page_id is not None else identifier)
        cache_key = page_cache_key(page)
        if args.incremental and previous_snapshot_exists and cache_key:
            previous_page = previous_pages_index.get(cache_key)
            if (
                previous_page is not None
                and previous_page.get("updated_at") == page.get("updated_at")
                and detail_key in previous_page_details
            ):
                page_details[detail_key] = previous_page_details[detail_key]
                incremental_stats["reused_page_details"] += 1
                continue

        try:
            details = client.get_json(f"/api/v1/courses/{course_id}/pages/{identifier}")
            page_details[detail_key] = details
            incremental_stats["fetched_page_details"] += 1
        except CanvasAPIError as exc:
            endpoint_errors[f"page_{identifier}"] = str(exc)
            log(f"Warning: failed page detail fetch for {identifier}: {exc}")

    folders, error = fetch_optional_list(
        "folders",
        lambda: client.get_paginated(f"/api/v1/courses/{course_id}/folders"),
    )
    if error:
        endpoint_errors["folders"] = error

    files, error = fetch_optional_list(
        "files",
        lambda: client.get_paginated(f"/api/v1/courses/{course_id}/files"),
    )
    if error:
        endpoint_errors["files"] = error

    tabs, error = fetch_optional_list(
        "tabs",
        lambda: client.get_paginated(f"/api/v1/courses/{course_id}/tabs"),
    )
    if error:
        endpoint_errors["tabs"] = error

    download_results: list[dict[str, Any]] = []
    if args.download_files and files:
        log(f"Downloading up to {len(files)} files...")
        for index, file_obj in enumerate(files, start=1):
            file_id = file_obj.get("id")
            file_url = file_obj.get("url")
            display_name = file_obj.get("display_name") or file_obj.get("filename")

            if not file_id or not file_url:
                download_results.append(
                    {
                        "id": file_id,
                        "status": "skipped",
                        "reason": "missing id or url",
                    }
                )
                continue

            safe_name = sanitize_filename(str(display_name or f"file_{file_id}"))
            output_path = files_dir / f"{file_id}_{safe_name}"

            expected_size = file_obj.get("size")
            if (
                output_path.exists()
                and isinstance(expected_size, int)
                and output_path.stat().st_size == expected_size
            ):
                download_results.append(
                    {
                        "id": file_id,
                        "status": "skipped",
                        "reason": "already_downloaded",
                        "path": str(output_path),
                    }
                )
                continue

            try:
                client.download_to_path(file_url, output_path)
                download_results.append(
                    {
                        "id": file_id,
                        "status": "downloaded",
                        "path": str(output_path),
                    }
                )
            except CanvasAPIError as exc:
                download_results.append(
                    {
                        "id": file_id,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                log(f"Warning: file download failed for id={file_id}: {exc}")

            if index % 25 == 0:
                log(f"Processed {index}/{len(files)} files")

    write_json(raw_dir / "course.json", course)
    write_json(raw_dir / "modules.json", modules)
    write_json(raw_dir / "module_items_by_module.json", module_items_by_module)
    write_json(raw_dir / "assignments.json", assignments)
    write_json(raw_dir / "pages.json", pages)
    write_json(raw_dir / "page_details.json", page_details)
    write_json(raw_dir / "folders.json", folders)
    write_json(raw_dir / "files.json", files)
    write_json(raw_dir / "tabs.json", tabs)
    write_json(raw_dir / "download_results.json", download_results)

    structured_export = export_structured_course_content(
        client,
        course_id=course_id,
        course_dir=course_dir,
        modules=modules,
        module_items_by_module=module_items_by_module,
        assignments=assignments,
        pages=pages,
        page_details=page_details,
        files=files,
    )
    write_json(
        raw_dir / "structured_export_warnings.json",
        structured_export.get("warnings", []),
    )

    generated_at = utc_now_iso()
    changes = {
        "generated_at": generated_at,
        "base_url": normalize_base_url(base_url),
        "course_id": course_id,
        "incremental_mode": bool(args.incremental),
        "id_limit": args.change_id_limit,
        "resources": {
            "course": {
                "had_previous": previous_course is not None,
                "updated": previous_course != course
                if previous_course is not None
                else True,
            },
            "modules": compute_list_changes(
                previous_modules,
                modules,
                id_keys=("id",),
                id_limit=args.change_id_limit,
            ),
            "module_items_by_module": compute_keyed_blob_changes(
                {
                    str(key): value
                    for key, value in previous_module_items_by_module.items()
                },
                module_items_by_module,
                id_limit=args.change_id_limit,
            ),
            "assignments": compute_list_changes(
                previous_assignments,
                assignments,
                id_keys=("id",),
                id_limit=args.change_id_limit,
            ),
            "pages": compute_list_changes(
                previous_pages,
                pages,
                id_keys=("page_id", "url"),
                id_limit=args.change_id_limit,
            ),
            "page_details": compute_keyed_blob_changes(
                {str(key): value for key, value in previous_page_details.items()},
                page_details,
                id_limit=args.change_id_limit,
            ),
            "folders": compute_list_changes(
                previous_folders,
                folders,
                id_keys=("id",),
                id_limit=args.change_id_limit,
            ),
            "files": compute_list_changes(
                previous_files,
                files,
                id_keys=("id",),
                id_limit=args.change_id_limit,
            ),
            "tabs": compute_list_changes(
                previous_tabs,
                tabs,
                id_keys=("id",),
                updated_keys=("updated_at", "position"),
                id_limit=args.change_id_limit,
            ),
        },
    }
    write_json(raw_dir / "changes.json", changes)

    sync_state = {
        "last_success_at": generated_at,
        "base_url": normalize_base_url(base_url),
        "course_id": course_id,
        "course_name": course_name,
        "incremental_mode": bool(args.incremental),
        "raw_hashes": {
            "course": stable_json_hash(course),
            "modules": stable_json_hash(modules),
            "module_items_by_module": stable_json_hash(module_items_by_module),
            "assignments": stable_json_hash(assignments),
            "pages": stable_json_hash(pages),
            "page_details": stable_json_hash(page_details),
            "folders": stable_json_hash(folders),
            "files": stable_json_hash(files),
            "tabs": stable_json_hash(tabs),
            "structured_export_counts": stable_json_hash(
                structured_export.get("counts", {})
            ),
        },
    }
    write_json(course_dir / "sync_state.json", sync_state)

    manifest = {
        "generated_at": generated_at,
        "base_url": normalize_base_url(base_url),
        "course_id": course_id,
        "course_name": course_name,
        "course_directory_name": course_dir.name,
        "output_directory": str(course_dir),
        "download_files": bool(args.download_files),
        "incremental_mode": bool(args.incremental),
        "incremental_stats": incremental_stats,
        "counts": {
            "modules": len(modules),
            "module_items_fallback_modules": len(module_items_by_module),
            "assignments": len(assignments),
            "pages": len(pages),
            "page_details": len(page_details),
            "folders": len(folders),
            "files": len(files),
            "tabs": len(tabs),
            "downloaded_files": sum(
                1 for item in download_results if item.get("status") == "downloaded"
            ),
            "assignments_exported": structured_export.get("counts", {}).get(
                "assignments_exported", 0
            ),
            "modules_exported": structured_export.get("counts", {}).get(
                "modules_exported", 0
            ),
            "module_items_exported": structured_export.get("counts", {}).get(
                "module_items_exported", 0
            ),
            "assignment_linked_files_copied": structured_export.get("counts", {}).get(
                "assignment_linked_files_copied", 0
            ),
            "module_linked_files_copied": structured_export.get("counts", {}).get(
                "module_linked_files_copied", 0
            ),
            "linked_files_unique_downloaded": structured_export.get("counts", {}).get(
                "linked_files_unique_downloaded", 0
            ),
            "linked_files_missing": structured_export.get("counts", {}).get(
                "linked_files_missing", 0
            ),
        },
        "paths": {
            "changes": str(raw_dir / "changes.json"),
            "sync_state": str(course_dir / "sync_state.json"),
            "assignments": str(course_dir / "assignments"),
            "modules": str(course_dir / "modules"),
            "structured_export_warnings": str(
                raw_dir / "structured_export_warnings.json"
            ),
        },
        "structured_export": {
            "paths": structured_export.get("paths", {}),
            "warnings_count": len(structured_export.get("warnings", [])),
        },
        "errors": endpoint_errors,
    }
    write_json(course_dir / "manifest.json", manifest)

    log(f"Done. Backup saved to: {course_dir}")
    if endpoint_errors:
        log(
            f"Completed with {len(endpoint_errors)} endpoint warnings (see manifest.json)."
        )
    structured_warnings_count = len(structured_export.get("warnings", []))
    if structured_warnings_count:
        log(
            "Structured export had "
            f"{structured_warnings_count} warning(s); see raw/structured_export_warnings.json"
        )

    if args.incremental:
        log(
            "Incremental stats: "
            f"reused {incremental_stats['reused_module_item_lists']} module-item lists, "
            f"fetched {incremental_stats['fetched_module_item_lists']} module-item lists, "
            f"reused {incremental_stats['reused_page_details']} page details, "
            f"fetched {incremental_stats['fetched_page_details']} page details"
        )

    return {
        "course_id": course_id,
        "course_name": course_name,
        "status": "ok",
        "output_directory": str(course_dir),
        "error_count": len(endpoint_errors),
        "downloaded_files": sum(
            1 for item in download_results if item.get("status") == "downloaded"
        ),
        "assignments_exported": structured_export.get("counts", {}).get(
            "assignments_exported", 0
        ),
        "modules_exported": structured_export.get("counts", {}).get(
            "modules_exported", 0
        ),
        "module_items_exported": structured_export.get("counts", {}).get(
            "module_items_exported", 0
        ),
        "linked_files_missing": structured_export.get("counts", {}).get(
            "linked_files_missing", 0
        ),
    }


def main() -> int:
    args = parse_args()
    if args.change_id_limit < 1:
        raise SystemExit("--change-id-limit must be at least 1")

    env_file_candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
    ]
    deduped_candidates: list[Path] = []
    seen_paths: set[str] = set()
    for candidate in env_file_candidates:
        resolved = candidate.resolve()
        resolved_str = str(resolved)
        if resolved_str in seen_paths:
            continue
        seen_paths.add(resolved_str)
        deduped_candidates.append(resolved)

    token = resolve_token(
        args.token,
        args.token_env,
        env_file_candidates=deduped_candidates,
    )

    resolved_out_dir = (
        args.out_dir
        or os.getenv(ENV_OUT_DIR)
        or resolve_setting_from_env_files(ENV_OUT_DIR, deduped_candidates)
        or DEFAULT_OUT_DIR
    )
    resolved_saved_config = (
        args.saved_config
        or os.getenv(ENV_SAVED_CONFIG)
        or resolve_setting_from_env_files(ENV_SAVED_CONFIG, deduped_candidates)
        or DEFAULT_SAVED_CONFIG
    )
    args.out_dir = resolved_out_dir
    args.saved_config = resolved_saved_config

    saved_config_path = resolve_saved_config_path(args.saved_config)
    saved_base_url: str | None = None
    saved_course_ids: list[str] = []
    if args.use_saved:
        saved_base_url, saved_course_ids = load_saved_config(saved_config_path)
        log(f"Loaded {len(saved_course_ids)} course IDs from {saved_config_path}")

    cli_course_ids = normalize_course_ids(args.course_id or [])
    target_course_ids = normalize_course_ids(
        cli_course_ids + (saved_course_ids if args.use_saved else [])
    )

    base_url = args.base_url or saved_base_url
    if not base_url:
        raise SystemExit(
            "No base URL provided. Use --base-url or --use-saved with base_url in saved config."
        )
    if not target_course_ids:
        if args.use_saved:
            raise SystemExit(
                f"No course IDs found in saved config: {saved_config_path}"
            )
        raise SystemExit(
            "No course IDs provided. Use --course-id (repeatable) or --use-saved."
        )

    client = CanvasClient(
        base_url=base_url,
        token=token,
        per_page=args.per_page,
        timeout=args.timeout,
        retries=args.retries,
    )

    log(f"Starting sync for {len(target_course_ids)} course(s)")
    results: list[dict[str, Any]] = []
    had_failures = False
    for index, course_id in enumerate(target_course_ids, start=1):
        log(f"[{index}/{len(target_course_ids)}] Syncing course {course_id}")
        result = sync_course(client, args, base_url=base_url, course_id=course_id)
        results.append(result)
        if result.get("status") != "ok":
            had_failures = True

    summary = {
        "generated_at": utc_now_iso(),
        "base_url": normalize_base_url(base_url),
        "course_count": len(target_course_ids),
        "use_saved": bool(args.use_saved),
        "saved_config": str(saved_config_path) if args.use_saved else None,
        "results": results,
    }
    summary_path = Path(args.out_dir).expanduser().resolve() / "run_summary.json"
    write_json(summary_path, summary)
    log(f"Run summary saved to: {summary_path}")

    if had_failures:
        log(
            "Completed with at least one failed course sync. See run_summary.json for details."
        )
        return 1

    log("All course syncs completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
