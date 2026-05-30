"""Sync repository markdown content to a Google Doc."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


DEFAULT_SOURCE_PATH = "docs/TITAN_FRAMEWORK_DEEP_DIVE.md"
DOC_SCOPE = "https://www.googleapis.com/auth/documents"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync markdown content to a Google Doc using a service account."
    )
    parser.add_argument(
        "--source",
        default=None,
        help=(
            "Markdown source path. Defaults to TITAN_DOC_SOURCE_PATH env var "
            f"or {DEFAULT_SOURCE_PATH}."
        ),
    )
    parser.add_argument(
        "--doc-id",
        default=None,
        help="Google Doc ID. Defaults to GOOGLE_DOC_ID env var.",
    )
    parser.add_argument(
        "--service-account-json",
        default=None,
        help=(
            "Path to service account JSON. Defaults to "
            "GOOGLE_SERVICE_ACCOUNT_JSON env var."
        ),
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append content at the end instead of replacing full document content.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without writing to Google Docs API.",
    )
    return parser.parse_args(argv)


def resolve_source_path(args: argparse.Namespace) -> Path:
    source_raw = (
        str(args.source or "").strip()
        or str(os.environ.get("TITAN_DOC_SOURCE_PATH") or "").strip()
        or DEFAULT_SOURCE_PATH
    )
    source = Path(source_raw).expanduser()
    if not source.exists() or not source.is_file():
        raise RuntimeError(
            f"Source markdown file not found: {source}. "
            "Set --source or TITAN_DOC_SOURCE_PATH to a valid file."
        )
    return source


def resolve_doc_id(args: argparse.Namespace) -> str:
    doc_id = str(args.doc_id or "").strip() or str(os.environ.get("GOOGLE_DOC_ID") or "").strip()
    if not doc_id:
        raise RuntimeError("Missing Google Doc ID. Set --doc-id or GOOGLE_DOC_ID.")
    return doc_id


def resolve_service_account_json(args: argparse.Namespace) -> Path:
    key_raw = str(args.service_account_json or "").strip() or str(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or ""
    ).strip()
    if not key_raw:
        raise RuntimeError(
            "Missing service account credentials path. "
            "Set --service-account-json or GOOGLE_SERVICE_ACCOUNT_JSON."
        )
    key_path = Path(key_raw).expanduser()
    if not key_path.exists() or not key_path.is_file():
        raise RuntimeError(f"Service account JSON file not found: {key_path}.")
    return key_path


def markdown_to_text(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n")
    text = re.sub(r"```.*?```", _strip_fenced_code_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"[image: \1]", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "1. ", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    text = re.sub(r"_{1,2}([^_\n]+)_{1,2}", r"\1", text)
    text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def _strip_fenced_code_block(match: re.Match[str]) -> str:
    block = match.group(0).strip("\n")
    lines = block.splitlines()
    if len(lines) <= 2:
        return "\n"
    payload = "\n".join(lines[1:-1]).strip()
    return f"\n{payload}\n"


def build_docs_service(service_account_json: Path):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Google API dependencies missing. Install requirements.txt first "
            "(google-api-python-client, google-auth, google-auth-httplib2)."
        ) from exc

    creds = service_account.Credentials.from_service_account_file(
        str(service_account_json),
        scopes=[DOC_SCOPE],
    )
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def get_document_end_index(service, doc_id: str) -> int:
    doc = service.documents().get(documentId=doc_id).execute()
    body = doc.get("body", {})
    content = body.get("content", [])
    if not content:
        return 1
    end_index = int(content[-1].get("endIndex", 1))
    return max(1, end_index - 1)


def sync_document(*, service, doc_id: str, content: str, append: bool) -> None:
    if append:
        start_index = get_document_end_index(service, doc_id)
        requests = [{"insertText": {"location": {"index": start_index}, "text": content}}]
    else:
        end_index = get_document_end_index(service, doc_id)
        requests = []
        if end_index > 1:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": 1,
                            "endIndex": end_index,
                        }
                    }
                }
            )
        requests.append({"insertText": {"location": {"index": 1}, "text": content}})

    service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests},
    ).execute()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = resolve_source_path(args)
    doc_id = resolve_doc_id(args)
    markdown_raw = source.read_text(encoding="utf-8")
    converted = markdown_to_text(markdown_raw)

    if args.dry_run:
        print("DRY RUN: no Google Docs API write executed.")
        print(f"mode={'append' if args.append else 'replace'}")
        print(f"source={source}")
        print(f"doc_id={doc_id}")
        print(f"chars={len(converted)}")
        return 0

    key_path = resolve_service_account_json(args)
    service = build_docs_service(key_path)
    sync_document(service=service, doc_id=doc_id, content=converted, append=args.append)
    print(
        f"Sync completed: source={source} doc_id={doc_id} "
        f"mode={'append' if args.append else 'replace'} chars={len(converted)}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
