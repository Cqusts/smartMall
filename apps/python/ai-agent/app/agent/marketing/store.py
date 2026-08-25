"""文案落库通道。

与知识运维的 store 同一个理由：这条链路上最需要测的是**什么情况下
不写**，而那些用例一条数据库记录都不该产生。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from .state import CopyDraft


@runtime_checkable
class CopyStore(Protocol):
    def stage_copy(
        self, *, product_id: int, draft: CopyDraft,
        evidence: Sequence[str] = (), demand: Sequence[dict] = (),
        flags: Sequence[str] = (), model: str = "",
    ) -> int | None: ...


@dataclass
class MySqlCopyStore:
    engine: Any

    @classmethod
    def from_env(cls) -> "MySqlCopyStore":
        from smartmall_pipeline.repository import DwsRepository

        return cls(engine=DwsRepository.from_env().engine)

    def stage_copy(self, *, product_id, draft, evidence=(), demand=(),
                   flags=(), model="") -> int | None:
        """写一条**待审**文案。

        ``review_status`` 写死 pending、``ai_generated`` 写死 1——
        这两个值不做成参数是刻意的：**能传参数就意味着某天会有人传
        approved**，而那正是这道闸门要防的事。
        """
        from sqlalchemy import text

        def dump(x):
            return json.dumps(x, ensure_ascii=False)

        with self.engine.begin() as conn:
            cur = conn.execute(text(
                "INSERT INTO marketing_copy (product_id, title, main_images,"
                " selling_points, detail, script, evidence, demand_signals,"
                " flags, review_status, ai_generated, model) VALUES "
                "(:pid, :title, :imgs, :points, :detail, :script, :ev, :dm,"
                " :flags, 'pending', 1, :model)"),
                {"pid": product_id, "title": draft.title[:120],
                 "imgs": dump(draft.main_images), "points": dump(draft.points),
                 "detail": draft.detail, "script": draft.script,
                 "ev": dump(list(evidence)), "dm": dump(list(demand)),
                 "flags": dump(list(flags)), "model": model[:64]})
            # lastrowid 而不是 LAST_INSERT_ID()：后者是 MySQL 方言，
            # 测试跑在 SQLite 上就炸——这个项目已经栽过一次
            return cur.lastrowid


@dataclass
class StubCopyStore:
    staged: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 700
    fail: bool = False

    def stage_copy(self, **kw) -> int | None:
        if self.fail:
            raise RuntimeError("注入的落库故障")
        self.staged.append(dict(kw))
        self.next_id += 1
        return self.next_id


# ---------------------------------------------------------------- 素材


@runtime_checkable
class AssetStore(Protocol):
    def stage_asset(self, **kw: Any) -> int | None: ...
    def unfinished(self, limit: int = 20) -> list[dict[str, Any]]: ...
    def finish(self, asset_id: int, **kw: Any) -> bool: ...
    def list_assets(self, product_id: int | None = None,
                    limit: int = 50) -> list[dict[str, Any]]: ...


#: 允许写回的任务状态。**白名单而不是原样落库**：状态是被轮询接口
#: 间接驱动的，一个拼错的字符串会让这条任务永远捞不回来（``unfinished``
#: 按状态过滤），而那种故障没有任何报错。
_TASK_STATUS = frozenset({"pending", "running", "succeeded", "failed"})


@dataclass
class MySqlAssetStore:
    """``marketing_asset`` 的读写。

    与文案那张表同一条线：``review_status`` 与 ``ai_generated`` 写死，
    不做成参数——**能传参数就意味着某天会有人传 approved**。
    """

    engine: Any

    @classmethod
    def from_env(cls) -> "MySqlAssetStore":
        from smartmall_pipeline.repository import DwsRepository

        return cls(engine=DwsRepository.from_env().engine)

    def stage_asset(self, *, product_id: int, kind: str, usage_tag: str = "",
                    local_path: str = "", source_url: str = "",
                    prompt: str = "", negative_prompt: str = "",
                    task_id: str | None = None, task_status: str = "succeeded",
                    model: str = "", error: str = "") -> int | None:
        from sqlalchemy import text

        if task_status not in _TASK_STATUS:
            task_status = "failed"
        with self.engine.begin() as conn:
            cur = conn.execute(text(
                "INSERT INTO marketing_asset (product_id, kind, usage_tag,"
                " local_path, source_url, prompt, negative_prompt, task_id,"
                " task_status, error, model, ai_generated, review_status)"
                " VALUES (:pid, :kind, :usage, :path, :url, :prompt, :neg,"
                " :task, :status, :err, :model, 1, 'pending')"),
                {"pid": product_id, "kind": kind[:16], "usage": usage_tag[:32],
                 "path": local_path[:512], "url": source_url or None,
                 "prompt": prompt, "neg": negative_prompt,
                 "task": task_id, "status": task_status,
                 "err": error[:512], "model": model[:64]})
            # lastrowid 而不是 LAST_INSERT_ID()：后者是 MySQL 方言，
            # 测试跑在 SQLite 上就炸——这个项目已经栽过一次
            return cur.lastrowid

    def unfinished(self, limit: int = 20) -> list[dict[str, Any]]:
        """还没跑完的视频任务。

        **只捞 pending/running。** failed 也捞的话，一个已经过期的任务
        会被永远重查——而 24 小时后它的状态是 UNKNOWN，查一万次也不会变。
        """
        from sqlalchemy import text

        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, product_id, kind, usage_tag, task_id, task_status,"
                " model, prompt, negative_prompt FROM marketing_asset"
                " WHERE task_status IN ('pending','running')"
                " AND task_id IS NOT NULL ORDER BY id LIMIT :n"),
                {"n": int(limit)}).mappings().all()
        return [dict(r) for r in rows]

    def finish(self, asset_id: int, *, task_status: str,
               local_path: str = "", source_url: str = "",
               error: str = "") -> bool:
        """轮询回写。

        ``local_path`` 与 ``source_url`` **空值不覆盖**：轮询到 running 时
        这两个字段本来就是空的，直接写进去会把上一次已经落好的文件路径抹掉。
        """
        from sqlalchemy import text

        if task_status not in _TASK_STATUS:
            return False
        with self.engine.begin() as conn:
            cur = conn.execute(text(
                "UPDATE marketing_asset SET task_status = :status,"
                " local_path = CASE WHEN :path = '' THEN local_path ELSE :path END,"
                " source_url = CASE WHEN :url = '' THEN source_url ELSE :url END,"
                " error = :err WHERE id = :id"),
                {"status": task_status, "path": local_path[:512],
                 "url": source_url, "err": error[:512], "id": int(asset_id)})
            return cur.rowcount > 0

    def list_assets(self, product_id: int | None = None,
                    limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import text

        sql = ("SELECT id, product_id, kind, usage_tag, local_path,"
               " task_status, error, model, review_status, ai_generated,"
               " created_at FROM marketing_asset")
        params: dict[str, Any] = {"n": int(limit)}
        if product_id is not None:
            sql += " WHERE product_id = :pid"
            params["pid"] = int(product_id)
        sql += " ORDER BY id DESC LIMIT :n"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]


@dataclass
class StubAssetStore:
    """测试与试跑用的替身。"""

    staged: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 800
    fail: bool = False

    def stage_asset(self, **kw) -> int | None:
        if self.fail:
            raise RuntimeError("注入的落库故障")
        self.next_id += 1
        self.staged.append({"id": self.next_id, "review_status": "pending",
                            "ai_generated": 1, **kw})
        return self.next_id

    def unfinished(self, limit: int = 20) -> list[dict[str, Any]]:
        return [r for r in self.staged
                if r.get("task_id") and
                r.get("task_status") in ("pending", "running")][:limit]

    def finish(self, asset_id: int, *, task_status: str, local_path: str = "",
               source_url: str = "", error: str = "") -> bool:
        for row in self.staged:
            if row["id"] != asset_id:
                continue
            row["task_status"] = task_status
            row["error"] = error
            # 空值不覆盖，与 MySqlAssetStore 保持一致——两个实现在这一点上
            # 分叉的话，测试会绿而线上会把已下好的文件路径抹掉
            if local_path:
                row["local_path"] = local_path
            if source_url:
                row["source_url"] = source_url
            return True
        return False

    def list_assets(self, product_id=None, limit=50) -> list[dict[str, Any]]:
        rows = [r for r in self.staged
                if product_id is None or r.get("product_id") == product_id]
        return list(reversed(rows))[:limit]
