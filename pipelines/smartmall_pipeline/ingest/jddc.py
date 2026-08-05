"""JDDC 语料适配器。

⚠️ **授权前置**：JDDC / JDDC 2.0 为学术授权数据集，使用前必须完成授权申请，
授权范围登记在 `ds_source` 表。本适配器只负责格式解析，不负责合规判断——
`dag_ingest_public_dataset` 会在导入前校验 `ds_source.expires_at`。

支持两种输入格式（JDDC 各版本发布形式不一致）：

* **TSV**：``session_id \\t sentence_id \\t role \\t content \\t timestamp``
  role 约定 ``0`` = 用户，``1`` = 客服。
* **JSONL**：每行一个会话对象，见 :func:`parse_jsonl_line`。

如果拿到的发布包字段顺序不同，改 :data:`TSV_COLUMNS` 即可，不要改解析逻辑。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import Dialogue, Turn

TSV_COLUMNS = ["session_id", "sentence_id", "role", "content", "timestamp"]

ROLE_MAP = {"0": "user", "1": "agent", "user": "user", "agent": "agent", "sys": "system"}


def _parse_ts(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    # 纯数字按 Unix 时间戳处理（秒或毫秒）
    if raw.isdigit():
        v = int(raw)
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v)
    return None


def parse_tsv(path: Path | str, source_type: str = "jddc") -> Iterator[Dialogue]:
    """解析 TSV 格式。按 session_id 分组，行内顺序即对话顺序。

    逐会话 yield 而非一次性读入内存——JDDC 有 100 万+ 会话，全量读入会 OOM。
    """
    path = Path(path)
    current_id: str | None = None
    buffer: list[tuple[str, str, str]] = []

    def flush() -> Dialogue | None:
        if not current_id or not buffer:
            return None
        turns = [
            Turn(turn_index=i, role=role, content=content, said_at=_parse_ts(ts))  # type: ignore[arg-type]
            for i, (role, content, ts) in enumerate(buffer)
        ]
        return Dialogue(
            session_no=f"{source_type.upper()}-{current_id}",
            source_type=source_type,
            channel="jd",
            agent_type="human",
            turns=turns,
            started_at=turns[0].said_at if turns else None,
            ended_at=turns[-1].said_at if turns else None,
            external_id=current_id,
        )

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            row = dict(zip(TSV_COLUMNS, parts))
            sid = row["session_id"].strip()
            if sid != current_id:
                done = flush()
                if done is not None:
                    yield done
                current_id, buffer = sid, []
            role = ROLE_MAP.get(row["role"].strip(), "user")
            content = row["content"].strip()
            if content:
                buffer.append((role, content, row.get("timestamp", "")))

    done = flush()
    if done is not None:
        yield done


def parse_jsonl_line(obj: dict[str, Any], source_type: str = "jddc2") -> Dialogue | None:
    """解析 JSONL 格式的单行。

    期望形状（JDDC 2.0 的多模态会话）::

        {
          "session_id": "...",
          "turns": [
            {"role": "user", "content": "...", "images": ["..."], "time": "..."},
            ...
          ]
        }
    """
    raw_turns = obj.get("turns") or obj.get("dialogue") or []
    if not raw_turns:
        return None

    turns: list[Turn] = []
    for i, t in enumerate(raw_turns):
        content = (t.get("content") or t.get("text") or "").strip()
        images = t.get("images") or t.get("image_urls") or []
        # JDDC 2.0 的图片轮次可能没有文本，但图片本身是信息，不能丢
        if not content and not images:
            continue
        turns.append(
            Turn(
                turn_index=i,
                role=ROLE_MAP.get(str(t.get("role", "user")), "user"),  # type: ignore[arg-type]
                content=content,
                image_urls=list(images),
                said_at=_parse_ts(str(t.get("time", ""))),
            )
        )

    if not turns:
        return None

    sid = str(obj.get("session_id") or obj.get("id") or "")
    return Dialogue(
        session_no=f"{source_type.upper()}-{sid}",
        source_type=source_type,
        channel="jd",
        agent_type="human",
        turns=turns,
        started_at=turns[0].said_at,
        ended_at=turns[-1].said_at,
        external_id=sid,
    )


def parse_jsonl(path: Path | str, source_type: str = "jddc2") -> Iterator[Dialogue]:
    path = Path(path)
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            dlg = parse_jsonl_line(obj, source_type)
            if dlg is not None:
                yield dlg


def load(path: Path | str, source_type: str = "jddc") -> Iterator[Dialogue]:
    """按扩展名自动选择解析器。"""
    path = Path(path)
    if path.suffix in (".jsonl", ".json"):
        yield from parse_jsonl(path, source_type)
    else:
        yield from parse_tsv(path, source_type)
