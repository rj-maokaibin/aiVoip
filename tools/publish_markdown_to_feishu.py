from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from app.core.config import settings
from app.integrations.feishu.transport import FeishuLiveTransport


MAX_TEXT_CHARS = 1800


def _text_block(content: str, block_type: int = 2) -> dict:
    key = {
        2: "text",
        3: "heading1",
        4: "heading2",
        5: "heading3",
        12: "bullet",
        13: "ordered",
    }.get(block_type, "text")
    return {
        "block_type": block_type,
        key: {
            "elements": [
                {
                    "text_run": {
                        "content": str(content),
                        "text_element_style": {},
                    }
                }
            ],
            "style": {},
        },
    }


def _plain_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[图片：\1]", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    return text.strip()


def _split_text(text: str, limit: int = MAX_TEXT_CHARS) -> list[str]:
    text = str(text).strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("。", 0, limit), text.rfind("；", 0, limit), text.rfind("\n", 0, limit))
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def markdown_to_blocks(markdown: str) -> tuple[str, list[dict]]:
    lines = markdown.replace("\r\n", "\n").split("\n")
    title = ""
    blocks: list[dict] = []
    paragraph: list[str] = []
    code_lines: list[str] = []
    in_code = False
    code_lang = ""

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        text = _plain_markdown(" ".join(x.strip() for x in paragraph if x.strip()))
        for part in _split_text(text):
            blocks.append(_text_block(part, 2))
        paragraph = []

    def flush_code() -> None:
        nonlocal code_lines, code_lang
        if not code_lines:
            return
        prefix = f"[{code_lang}]\n" if code_lang else ""
        body = prefix + "\n".join(code_lines)
        for part in _split_text(body):
            blocks.append(_text_block(part, 2))
        code_lines = []
        code_lang = ""

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_paragraph()
                in_code = True
                code_lang = stripped[3:].strip()
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not stripped:
            flush_paragraph()
            continue

        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush_paragraph()
            continue

        m = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if m:
            flush_paragraph()
            level = len(m.group(1))
            text = _plain_markdown(m.group(2))
            if level == 1:
                if not title:
                    title = text
                blocks.append(_text_block(text, 3))
            elif level == 2:
                blocks.append(_text_block(text, 4))
            else:
                blocks.append(_text_block(text, 5))
            continue

        m = re.match(r"^[-*+]\s+(.+)$", stripped)
        if m:
            flush_paragraph()
            for part in _split_text(_plain_markdown(m.group(1))):
                blocks.append(_text_block(part, 12))
            continue

        m = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if m:
            flush_paragraph()
            for part in _split_text(_plain_markdown(m.group(1))):
                blocks.append(_text_block(part, 13))
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            text = _plain_markdown(stripped.lstrip("> "))
            for part in _split_text("引用：" + text):
                blocks.append(_text_block(part, 2))
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells):
                continue
            row = " ｜ ".join(_plain_markdown(c) for c in cells)
            blocks.append(_text_block(row, 2))
            continue

        paragraph.append(line)

    flush_paragraph()
    if in_code:
        flush_code()

    if not title:
        title = "Markdown 报告"
    return title, blocks


async def _add_collaborator(
    transport: FeishuLiveTransport,
    document_id: str,
    *,
    member_type: str,
    member_id: str,
    perm: str,
) -> None:
    await transport._request(
        "POST",
        f"/drive/v1/permissions/{quote(document_id, safe='')}/members",
        params={"type": "docx", "need_notification": "false"},
        json_body={
            "member_type": member_type,
            "member_id": member_id,
            "perm": perm,
            "type": "chat" if member_type == "openchat" else "user",
        },
    )


async def publish(source: Path, result_path: Path) -> dict:
    markdown = source.read_text(encoding="utf-8")
    title, blocks = markdown_to_blocks(markdown)
    transport = FeishuLiveTransport()

    created = await transport._request("POST", "/docx/v1/documents", json_body={"title": title})
    doc = (created.get("data") or {}).get("document") or {}
    document_id = str(doc.get("document_id") or doc.get("token") or "")
    if not document_id:
        raise RuntimeError("FEISHU_DOCX_CREATE_MISSING_DOCUMENT_ID")
    url = str(doc.get("url") or f"https://feishu.cn/docx/{document_id}")

    current = 0
    for pos in range(0, len(blocks), 40):
        chunk = blocks[pos : pos + 40]
        await transport._request(
            "POST",
            f"/docx/v1/documents/{quote(document_id, safe='')}/blocks/{quote(document_id, safe='')}/children",
            json_body={"index": current, "children": chunk},
        )
        current += len(chunk)
        await asyncio.sleep(0.4)

    acl: list[str] = []
    admin_ids = [x.strip() for x in str(settings.feishu_document_acl_admin_open_ids or "").split(",") if x.strip()]
    for open_id in admin_ids:
        try:
            await _add_collaborator(
                transport,
                document_id,
                member_type="openid",
                member_id=open_id,
                perm="full_access",
            )
            acl.append(f"admin:{open_id[:10]}")
        except Exception as exc:
            print(f"WARN_ACL_ADMIN={type(exc).__name__}")

    if settings.feishu_receive_id_type == "chat_id" and settings.feishu_default_receive_id:
        try:
            await _add_collaborator(
                transport,
                document_id,
                member_type="openchat",
                member_id=str(settings.feishu_default_receive_id),
                perm=str(os.getenv("FEISHU_PUBLISH_CHAT_PERMISSION") or "edit"),
            )
            acl.append("default_chat")
        except Exception as exc:
            print(f"WARN_ACL_CHAT={type(exc).__name__}")

    result = {
        "status": "PASS",
        "source": str(source),
        "title": title,
        "document_id": document_id,
        "url": url,
        "block_count": len(blocks),
        "acl_grants": acl,
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"FEISHU_DOCUMENT_ID={document_id}")
    print(f"FEISHU_DOC_URL={url}")
    print(f"FEISHU_BLOCK_COUNT={len(blocks)}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish one Markdown file as a Feishu Docx document")
    parser.add_argument("--source", required=True)
    parser.add_argument("--result", default="validation/feishu_markdown_publish_result.json")
    args = parser.parse_args()
    source = Path(args.source).resolve()
    if not source.is_file():
        raise SystemExit(f"source not found: {source}")
    asyncio.run(publish(source, Path(args.result).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
