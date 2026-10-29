"""Lossless raw files remain in the snapshot; this is a rebuildable reading view."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3

from .core import VaultError, no_link_ancestors, sha256, verify_payload, write_json


def records(path):
    with Path(path).open("rb") as f:
        for number, raw in enumerate(f, 1):
            if not raw.strip():
                continue
            try:
                item = json.loads(raw)
                if not isinstance(item, dict):
                    raise ValueError("object required")
                yield number, item, None
            except (ValueError, UnicodeError) as e:
                yield number, None, str(e)


def inspect_jsonl(root):
    warnings = []
    for path in Path(root).rglob("*.jsonl"):
        errors = [n for n, _, error in records(path) if error]
        if errors:
            warnings.append(f"JSONL解析不可行を原本のまま保護: {path.relative_to(root)}: {len(errors)}行（先頭位置 {errors[:5]}）")
    return warnings


def text_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text"):
            parts.append(str(part.get("text", "")))
        elif isinstance(part, dict) and "image" in str(part.get("type", "")):
            parts.append("[画像: 元ログ／元ファイルを参照]")
    return "\n".join(parts)


def export_archive(payload, output):
    m = verify_payload(payload)
    output = Path(output).absolute()
    no_link_ancestors(output)
    if output.exists() and any(output.iterdir()):
        raise VaultError("アーカイブ出力先には空フォルダを指定してください")
    output.mkdir(parents=True, exist_ok=True)
    (output / "conversations").mkdir()
    report = {"sessions": 0, "messages": 0, "malformed_lines": [], "unrecognized_files": [],
              "note": "既知のユーザー／アシスタント発言のみ。ツール出力・推論・添付実体は原本を参照。AIによる要約は行いません"}
    with contextlib.closing(sqlite3.connect(output / "index.sqlite")) as db:
        db.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, conversation_id TEXT, cwd TEXT, timestamp TEXT,
                                   raw_path TEXT, sha256 TEXT, markdown TEXT);
            CREATE TABLE messages (session_id TEXT, ordinal INTEGER, role TEXT, text TEXT);
            CREATE INDEX messages_session ON messages(session_id);
        """)
        for source in m["sources"]:
            if source["kind"] != "codex":
                continue
            root = Path(payload) / source["stored_path"]
            for path in sorted(root.rglob("*.jsonl")):
                if not (path.name.startswith("rollout-") or "sessions" in path.relative_to(root).parts
                        or "archived_sessions" in path.relative_to(root).parts):
                    continue
                meta, response_messages, recognized = {}, False, False
                for number, item, error in records(path):
                    if error:
                        report["malformed_lines"].append({"path": str(path.relative_to(payload)), "line": number})
                        continue
                    p = item.get("payload", {})
                    if item.get("type") == "session_meta" and isinstance(p, dict):
                        meta.update(p)
                        recognized = True
                    if (item.get("type") == "response_item" and isinstance(p, dict)
                            and p.get("type") == "message" and p.get("role") in ("user", "assistant")):
                        response_messages = True
                        recognized = True
                    if item.get("type") == "event_msg" and isinstance(p, dict) and p.get("type") in ("user_message", "agent_message"):
                        recognized = True
                if not recognized:
                    report["unrecognized_files"].append(str(path.relative_to(payload)))
                    continue
                rel = path.relative_to(payload).as_posix()
                # Path identity prevents collisions when session ids repeat in raw copies.
                import hashlib
                sid = hashlib.sha256(rel.encode()).hexdigest()[:24]
                markdown = f"conversations/{sid}.md"
                cid = str(meta.get("id", path.stem))
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (
                    sid, cid, str(meta.get("cwd", "")), str(meta.get("timestamp", "")), rel, sha256(path), markdown))
                ordinal = 0
                with (output / markdown).open("w", encoding="utf-8", newline="\n") as f:
                    f.write(f"# Codex会話 {cid}\n\n")
                    f.write("元データ: `" + rel + "`\n\n")
                    f.write("作業パス: `" + str(meta.get("cwd", "")) + "`\n\n")
                    for _, item, error in records(path):
                        if error:
                            continue
                        p = item.get("payload", {})
                        if not isinstance(p, dict):
                            continue
                        role, body = None, ""
                        if response_messages and item.get("type") == "response_item" and p.get("type") == "message":
                            role, body = p.get("role"), text_content(p.get("content"))
                        elif not response_messages and item.get("type") == "event_msg":
                            role = {"user_message": "user", "agent_message": "assistant"}.get(p.get("type"))
                            body = str(p.get("message", ""))
                        if role not in ("user", "assistant") or not body:
                            continue
                        ordinal += 1
                        f.write(f"## {ordinal}. {role}\n\n{body}\n\n")
                        db.execute("INSERT INTO messages VALUES (?,?,?,?)", (sid, ordinal, role, body))
                report["sessions"] += 1
                report["messages"] += ordinal
        db.commit()
    write_json(output / "archive-report.json", report)
    (output / "README.md").write_text(
        "# Codex履歴アーカイブ\n\nconversationsには抽出できた発言、index.sqliteには検索用データを保存しています。\n"
        "原本は復元payload内のJSONLです。archive-report.jsonで解析できなかった箇所を確認してください。\n"
        "このフォルダは平文です。暗号化ドライブで管理してください。\n", encoding="utf-8")
    return report


def search_archive(archive, query):
    p = Path(archive).resolve() / "index.sqlite"
    with contextlib.closing(sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)) as db:
        # Literal LIKE query. Japanese substring search works without tokenizer plugins.
        q = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = db.execute("""SELECT s.conversation_id, s.cwd, s.markdown, m.role, substr(m.text,1,600)
                             FROM messages m JOIN sessions s ON s.id=m.session_id
                             WHERE m.text LIKE ? ESCAPE '\\' LIMIT 50""", (q,)).fetchall()
        return [dict(zip(("conversation_id", "cwd", "markdown", "role", "text"), row)) for row in rows]
