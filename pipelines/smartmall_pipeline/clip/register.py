"""出口①：切片进素材中心，以及主播叫法的回写队列。

到这里 M5 的双出口才算齐：话术那一路在 :mod:`.segment`，视频这一路在这里。

**素材落在磁盘上不算入库。** 只有进了 ``asset`` 表，切片才谈得上
被商品详情页挂载、被客服答案引用、被审核流管起来——否则它就是一堆
没人知道存在的 mp4。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .cut import ClipFile
from .segment import Segment

#: 自动绑定的置信度。低于 0.85 按 schema 的约定进人工确认队列
#: （见 02_asset.sql 的 bind_conf 注释）。
#:
#: 分成两档而不是一个值：主播**说出口**的叫法命中，比模型看上下文猜的
#: 可信得多——前者是字面匹配，后者是概率。
CONF_SPOKEN = 0.95
CONF_MODEL = 0.70


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    """SHA256。素材表拿它去重——同一段直播重跑一次不该产生两份素材。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


@dataclass
class RegisterResult:
    assets: int = 0
    relations: int = 0
    aliases: int = 0
    skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)


def register_clips(
    repo: Any, clips: Sequence[ClipFile], *, live_id: int, live_title: str = "",
    source_ref: str = "",
) -> RegisterResult:
    """把切片写进 ``asset`` / ``asset_product_rel`` / ``asset_clip_meta``。

    三张表一起写，在同一个事务里——只写主表不写关联的话，素材存在但
    挂不到商品上，看起来"入库成功"实际没用。
    """
    from sqlalchemy import text

    result = RegisterResult()
    if not clips:
        return result

    with repo.engine.begin() as conn:
        for i, clip in enumerate(clips):
            seg = clip.segment
            digest = file_hash(clip.path)

            # 同一段直播重跑一次不该产生两份素材
            existing = conn.execute(
                text("SELECT id FROM asset WHERE file_hash = :h AND deleted = 0"),
                {"h": digest},
            ).scalar()
            if existing:
                result.skipped_duplicate += 1
                continue

            asset_no = f"clip-{source_ref or live_id}-{i + 1:03d}"
            # 用 cursor 的 lastrowid 而不是 LAST_INSERT_ID()：后者是 MySQL
            # 方言，测试就没法在 SQLite 上真跑这几条 SQL——而"真跑一遍"
            # 恰恰是这里唯一能证明 INSERT 写对了的办法
            ins = conn.execute(text(
                "INSERT INTO asset (asset_no, modality, scene, oss_key, "
                " file_size, mime_type, duration_ms, file_hash, source, "
                " status, vlm_description, desc_review_status, tags) "
                "VALUES (:no, 'video', 'clip', :key, :size, 'video/mp4', "
                " :dur, :hash, 'live_clip', 'draft', :desc, 'pending', :tags)"
            ), {
                "no": asset_no, "key": str(clip.path), "size": clip.bytes,
                "dur": clip.duration_ms, "hash": digest,
                "desc": seg.topic or "",
                # 直播切片不是 AI 生成的，是真人录像的片段——ai_generated
                # 保持 0。标错的话会给它加上本不该有的 AI 标识角标，
                # 那是另一种意义上的不实标注
                "tags": _json(["直播切片", seg.kind]),
            })
            asset_id = ins.lastrowid
            result.assets += 1

            if seg.product_id:
                conf = CONF_SPOKEN if seg.matched_alias else CONF_MODEL
                conn.execute(text(
                    "INSERT INTO asset_product_rel "
                    "(asset_id, product_id, rel_type, bind_source, bind_conf) "
                    "VALUES (:a, :p, 'clip', 'auto', :c)"
                ), {"a": asset_id, "p": seg.product_id, "c": conf})
                result.relations += 1

            conn.execute(text(
                "INSERT INTO asset_clip_meta "
                "(asset_id, live_id, live_title, start_ms, end_ms, "
                " transcript, objection_qa) "
                "VALUES (:a, :l, :t, :s, :e, :tr, :qa)"
            ), {
                "a": asset_id, "l": live_id, "t": live_title[:250],
                "s": clip.begin_ms, "e": clip.end_ms,
                "tr": seg.raw_text,
                # 异议处理话术是价值最高的一类，单独存一份便于捞取
                "qa": _json([seg.script]) if seg.kind == "qa" else None,
            })
    return result


def propose_aliases(
    repo: Any, segments: Sequence[Segment], *, source_ref: str = ""
) -> int:
    """把主播实际用的叫法记进待确认队列。

    **不直接回写 product.alias。** 对齐命中的词可能是巧合——主播说
    "这个颜色跟刚才那件米白很像"，讲的其实是另一件商品。自动回写会把
    错的叫法喂回热词表，而热词带权重：一个错别名会让往后每一场直播都
    朝错的方向偏。**这类错误自己会放大**，必须有人看一眼。
    """
    from sqlalchemy import text

    rows = [
        (s.product_id, s.matched_alias, s.raw_text[:400])
        for s in segments if s.product_id and s.matched_alias
    ]
    if not rows:
        return 0

    with repo.engine.begin() as conn:
        for pid, alias, sample in rows:
            alias = alias[:60]
            # 先查再写，不用 ON DUPLICATE KEY UPDATE —— 那是 MySQL 方言，
            # 用了测试就没法在 SQLite 上真跑这段。并发下有竞态，
            # 但这条链路是批处理，一场直播只跑一次
            hit = conn.execute(text(
                "SELECT id FROM clip_alias_proposal "
                "WHERE product_id = :p AND alias = :a"
            ), {"p": pid, "a": alias}).scalar()
            if hit:
                conn.execute(text(
                    "UPDATE clip_alias_proposal SET times = times + 1 "
                    "WHERE id = :i"), {"i": hit})
            else:
                conn.execute(text(
                    "INSERT INTO clip_alias_proposal "
                    "(product_id, alias, times, source_ref, sample) "
                    "VALUES (:p, :a, 1, :r, :s)"
                ), {"p": pid, "a": alias, "r": source_ref[:250], "s": sample})
    return len(rows)


def apply_alias(repo: Any, proposal_id: int) -> tuple[int, str] | None:
    """人工确认一个叫法，写进 ``product.alias``。**闭环的最后一步。**

    写进去之后，下一次 ``build_hotwords`` 就会把它算进热词表，
    下一场直播的转写就不会再听错这个词。
    """
    from sqlalchemy import text

    import json as _json_mod

    with repo.engine.begin() as conn:
        row = conn.execute(text(
            "SELECT product_id, alias FROM clip_alias_proposal "
            "WHERE id = :i AND status = 'pending'"
        ), {"i": proposal_id}).mappings().first()
        if not row:
            return None

        pid, alias = int(row["product_id"]), str(row["alias"])
        raw = conn.execute(text("SELECT alias FROM product WHERE id = :p"),
                           {"p": pid}).scalar()
        try:
            current = _json_mod.loads(raw) if raw else []
        except (TypeError, ValueError):
            current = []
        if alias not in current:
            current.append(alias)
            conn.execute(text("UPDATE product SET alias = :a WHERE id = :p"),
                         {"a": _json_mod.dumps(current, ensure_ascii=False),
                          "p": pid})
        conn.execute(text(
            "UPDATE clip_alias_proposal SET status = 'approved' WHERE id = :i"
        ), {"i": proposal_id})
    return pid, alias


def _json(value: Any) -> str | None:
    import json as _json_mod

    return None if value is None else _json_mod.dumps(value, ensure_ascii=False)
