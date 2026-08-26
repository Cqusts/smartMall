#!/usr/bin/env python3
"""给 013 的每个三级类目抓几张真实拍图（Openverse，CC 授权）。

    python deploy/scripts/fetch_images.py            # 抓（已抓过的跳过）
    python deploy/scripts/fetch_images.py --force    # 全部重抓

---------------------------------------------------------------- 为什么要下下来

**不外链。** 页面直接引 flickr 的图看着更省事，但这个项目有一条硬约束：
整页零外部请求（``test_page_has_no_external_dependencies`` 会把
``http(s)://`` 整片挡下）。理由写在 index.html 的文件头：演示环境常常
没有外网，"打不开"比"不好看"严重得多。外链还有第二个问题——图随时会
被删、被换，而那时候没人知道页面上曾经是什么。

所以：从网上找，下载进仓库，页面引本地路径。仓库里原有那 12 张实拍
（9001–9012）本来就是这么来的。

---------------------------------------------------------------- 许可证

只取 CC0 / 公有领域 / CC BY 三种。**CC BY 要署名**，所以每一张的作者、
许可证、原始链接都记进 web/img/CREDITS.md —— 不记的话这批图就是不合规的，
而且事后谁也查不出来它们从哪来。

不取 CC BY-SA：它要求衍生作品同样以 BY-SA 发布，而这里会裁剪缩放
（构成衍生），把整个仓库拖进 share-alike 不值当。

---------------------------------------------------------------- 一张图一个类目，不是一个商品

570 个商品逐个搜图要 570 次请求，而且"手工曲奇 独立包装"和"低糖曲奇
办公室零食"搜出来的本来就是同一类照片。所以按**三级类目**抓 4 张，
同类目的 10 个商品轮着用。

同一个类目里 10 个商品共用 4 张图，重复是看得见的——但一张真实的曲奇
照片配一个曲奇商品，比一个灰色占位块强，也比配一张针织衫照片诚实。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
IMG_DIR = ROOT / "apps" / "python" / "ai-agent" / "web" / "img"
CREDITS = IMG_DIR / "CREDITS.md"
CACHE = Path(__file__).resolve().parent / ".image_cache.json"

API = "https://api.openverse.org/v1/images/"

#: 每个类目抓几张。4 张 × 57 个类目 ≈ 228 张，压到 500px 宽 JPEG
#: 之后每张 30–50KB，总共 8MB 上下——仓库扛得住的量级。
PER_CATEGORY = 4

#: 卡片是 4:5，按这个裁。不裁的话横图竖图混在一起，网格会参差不齐
OUT_W, OUT_H = 500, 625

#: 三级类目 → 英文搜索词。**用英文**：Openverse 的元数据绝大多数是英文的，
#: 中文词搜出来的结果既少又不相关。
#:
#: **而且要短。** 第一版写的是 "mixed nuts almonds cashews bowl" 这种四五个
#: 词的查询，14 个类目一张都没搜到 —— 它是 AND 匹配，词越多命中越少，
#: 再叠上只要三种许可证的过滤，就什么都不剩了。两个词最稳。
QUERIES: dict[int, tuple[str, str]] = {
    1031: ("卫衣", "hoodie"),
    1032: ("牛仔裤", "blue jeans"),
    1033: ("风衣", "trench coat"),
    3051: ("休闲鞋", "casual sneakers shoes pair"),
    3052: ("单鞋", "women flat shoes loafers"),
    2051: ("双肩包", "backpack rucksack bag"),
    2052: ("行李箱", "suitcase luggage travel"),

    4060: ("饼干糕点", "cookies biscuits baked"),
    4061: ("坚果炒货", "almonds"),
    4062: ("肉干肉脯", "beef jerky"),
    4063: ("米面粮油", "white rice"),
    4064: ("调味品", "sauce bottle"),
    4065: ("咖啡茶饮", "coffee beans roasted"),

    5070: ("乳品", "milk bottle glass dairy"),
    5071: ("果汁饮料", "orange juice glass bottle"),
    5072: ("饮用水", "water bottle"),

    6080: ("新鲜水果", "apples"),
    6081: ("新鲜蔬菜", "fresh vegetables broccoli market"),
    6082: ("牛羊肉", "raw beef steak butcher"),
    6083: ("海鲜水产", "salmon fillet"),

    7090: ("积木拼装", "building blocks toy bricks"),
    7091: ("桌游棋牌", "board game chess pieces"),
    7092: ("毛绒公仔", "teddy bear"),
    7093: ("遥控模型", "toy car"),

    8100: ("耳机音箱", "headphones audio speaker"),
    8101: ("手机配件", "usb cable"),
    8102: ("电脑外设", "computer keyboard mouse desk"),

    9110: ("小家电", "blender"),
    9111: ("清洁电器", "vacuum cleaner cleaning"),
    9112: ("个护小电", "hairdryer"),

    10120: ("桌椅", "wooden chair"),
    10121: ("收纳整理", "storage boxes organizer shelf"),
    10122: ("家纺", "bedding pillows duvet bedroom"),

    11130: ("面部护理", "face cream"),
    11131: ("身体洗护", "shampoo"),
    11132: ("彩妆", "lipstick makeup cosmetics"),

    12140: ("婴童服装", "baby onesie"),
    12141: ("喂养用品", "baby bottle"),
    12142: ("婴童洗护", "baby bath"),

    13150: ("健身器材", "yoga mat dumbbell fitness"),
    13151: ("户外装备", "tent"),
    13152: ("运动服饰", "running clothes"),

    14160: ("小说文学", "stack of books novel"),
    14161: ("人文社科", "books library shelf reading"),
    14162: ("科普读物", "science book"),

    15170: ("笔类", "pens pencils stationery"),
    15171: ("笔记本", "notebook journal paper"),
    15172: ("办公用品", "stapler"),

    16180: ("宠物主粮", "dog food bowl"),
    16181: ("宠物零食", "dog treats"),
    16182: ("宠物用品", "pet bowl"),

    17190: ("银饰", "silver necklace jewelry"),
    17191: ("配饰", "hair accessories brooch"),

    18200: ("营养补充", "vitamin pills"),
    18201: ("家用器械", "blood pressure monitor"),

    19210: ("车内用品", "car interior"),
    19211: ("汽车养护", "car wash"),
}

#: 只要这三种。BY-SA 会把衍生作品也拖进 share-alike，见文件头
LICENSES = "cc0,pdm,by"

#: 人工挑过的结果：每个类目留下哪几张。
#:
#: **这一步没法自动化，也不能省。** 搜索词写得再准，CC 图库里也没有干净的
#: 电商产品图——它是业余摄影的聚合，"hoodie"能搜出一个赤膊男人，"soy sauce"
#: 能搜出草坪洒水器。改词是打地鼠：修好五个又坏三个（实测两轮都是这样）。
#:
#: 所以最后一道是看图。做法：``python deploy/scripts/review_images.py`` 生成
#: 联系表，人眼过一遍，把对得上的编号填进这里，``--prune`` 删掉其余的。
#: 删掉之后那个位置退回占位块——**宁可空着也不放一张不相干的照片**，
#: 那和给商品画一个它没有的颜色块是同一类错误。
#:
#: 228 张里留下 160 张，57 个类目每个至少 1 张。
KEEP: dict[int, tuple[int, ...]] = {
    1031: (3,), 1032: (3, 4), 1033: (1, 2, 3),
    2051: (1, 4), 2052: (1, 2, 4),
    3051: (1, 2, 3, 4), 3052: (1, 2, 3, 4),
    4060: (1, 2, 3, 4), 4061: (1, 3, 4), 4062: (1, 2), 4063: (1,),
    4064: (2, 3), 4065: (1, 2, 3, 4),
    5070: (1, 2, 3), 5071: (1, 2, 4), 5072: (1, 3),
    6080: (1, 2, 3, 4), 6081: (1, 2, 3, 4), 6082: (1, 2), 6083: (1, 2, 3, 4),
    7090: (1, 2, 3, 4), 7091: (1, 2, 3, 4), 7092: (3, 4), 7093: (1, 3, 4),
    8100: (1, 2, 3), 8101: (1, 2, 3, 4), 8102: (1, 2, 3, 4),
    9110: (2, 4), 9111: (1, 2, 3), 9112: (4,),
    10120: (1, 2, 3, 4), 10121: (1, 2, 3), 10122: (1, 2, 4),
    11130: (2, 3), 11131: (1,), 11132: (2,),
    12140: (1, 2, 3), 12141: (3, 4), 12142: (1, 2),
    13150: (1, 2, 3, 4), 13151: (1, 4), 13152: (1, 2),
    14160: (1, 2, 3, 4), 14161: (1, 2, 3, 4), 14162: (2, 3),
    15170: (1, 2, 3, 4), 15171: (1, 2, 3, 4), 15172: (1, 2),
    16180: (4,), 16181: (1, 2, 3), 16182: (2, 4),
    17190: (1, 2, 3, 4), 17191: (1, 2, 3, 4),
    18200: (1, 2, 3, 4), 18201: (1, 2),
    19210: (1, 4), 19211: (1, 2),
}


def prune(log) -> int:
    """按 KEEP 删掉没挑中的。"""
    gone = 0
    for cid, keep in KEEP.items():
        for f in sorted(IMG_DIR.glob(f"c{cid}-*.jpg")):
            n = int(f.stem.rsplit("-", 1)[1])
            if n not in keep:
                f.unlink()
                gone += 1
    left = len(list(IMG_DIR.glob("c*.jpg")))
    empty = [c for c in QUERIES if not list(IMG_DIR.glob(f"c{c}-*.jpg"))]
    log(f"删掉 {gone} 张，留下 {left} 张")
    log(f"一张都不剩的类目：{empty or '无'}（这些会退回占位块）")
    return gone


def search(query: str, want: int, log) -> list[dict]:
    """搜一次，返回可用的候选。"""
    import httpx

    url = (f"{API}?q={quote(query)}&page_size={want * 4}"
           f"&license={LICENSES}&mature=false")
    for attempt in range(4):
        try:
            r = httpx.get(url, timeout=30.0,
                          headers={"User-Agent": "smartMall-demo-seed/1.0"})
        except httpx.HTTPError as exc:
            log(f"    网络错误（{type(exc).__name__}），{2 ** attempt}s 后重试")
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            # 匿名调用有配额。**不能当成"搜不到"**——那会静默地少抓一批图
            wait = 10 * (attempt + 1)
            log(f"    被限流，等 {wait}s")
            time.sleep(wait)
            continue
        if r.status_code != 200:
            log(f"    HTTP {r.status_code}")
            return []
        return [x for x in r.json().get("results", []) if x.get("url")]
    return []


def fetch_one(url: str, log) -> bytes | None:
    import httpx

    try:
        r = httpx.get(url, timeout=45.0, follow_redirects=True,
                      headers={"User-Agent": "smartMall-demo-seed/1.0"})
    except httpx.HTTPError as exc:
        log(f"    下载失败 {type(exc).__name__}")
        return None
    if r.status_code != 200 or not r.content:
        log(f"    下载失败 HTTP {r.status_code}")
        return None
    return r.content


def to_card(raw: bytes) -> bytes | None:
    """裁成 4:5 并压到 500px 宽。

    **要真的用 Pillow 打开一次**，不是改个后缀就存：抓回来的可能是一张
    HTML 错误页，直接存进去，页面上就是一个裂图，而库里那条记录看着
    一切正常。
    """
    from PIL import Image, ImageOps

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:  # noqa: BLE001
        return None
    if im.width < 240 or im.height < 240:
        return None                       # 太小，放大就糊了
    im = ImageOps.exif_transpose(im).convert("RGB")
    im = ImageOps.fit(im, (OUT_W, OUT_H), Image.LANCZOS, centering=(0.5, 0.45))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在的也重抓")
    ap.add_argument("--only", type=int, help="只抓某个类目 id，调词用")
    ap.add_argument("--prune", action="store_true",
                    help="只按 KEEP 删掉没挑中的，不抓新的")
    args = ap.parse_args()

    def log(msg=""):
        print(msg, flush=True)

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    if args.prune:
        prune(log)
        cache = json.loads(CACHE.read_text(encoding="utf-8")) \
            if CACHE.is_file() else {}
        # 署名清单只列还在的那些——删掉的图不该再出现在里面
        write_credits({k: v for k, v in cache.items()
                       if (IMG_DIR / k).is_file()})
        log(f"署名清单 → {CREDITS}")
        return 0

    cache = {}
    if CACHE.is_file() and not args.force:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))

    todo = QUERIES if args.only is None else {
        args.only: QUERIES[args.only]} if args.only in QUERIES else {}
    if not todo:
        log(f"✗ 没有类目 {args.only}")
        return 1

    got = fail = skipped = 0
    for cid, (zh, q) in todo.items():
        have = [n for n in range(1, PER_CATEGORY + 1)
                if (IMG_DIR / f"c{cid}-{n}.jpg").is_file()]
        if len(have) == PER_CATEGORY and not args.force:
            skipped += PER_CATEGORY
            continue

        log(f"[{cid}] {zh} ← {q}")
        results = search(q, PER_CATEGORY, log)
        if not results:
            log("    一条都没搜到")
            fail += PER_CATEGORY
            continue

        n = 0
        for item in results:
            if n >= PER_CATEGORY:
                break
            raw = fetch_one(item["url"], log)
            if raw is None:
                continue
            data = to_card(raw)
            if data is None:
                log("    不是一张能打开的图，跳过")
                continue
            n += 1
            name = f"c{cid}-{n}.jpg"
            (IMG_DIR / name).write_bytes(data)
            cache[name] = {
                "category": zh, "query": q,
                "title": item.get("title") or "",
                "creator": item.get("creator") or "",
                "license": f"{item.get('license', '')} "
                           f"{item.get('license_version', '')}".strip(),
                "source": item.get("source") or "",
                "foreign_landing_url": item.get("foreign_landing_url") or "",
                "url": item.get("url") or "",
            }
            log(f"    ✓ {name}  {len(data) // 1024}KB  "
                f"{item.get('license')} · {item.get('creator')}")
            got += 1
        if n < PER_CATEGORY:
            log(f"    ⚠ 只拿到 {n}/{PER_CATEGORY} 张")
            fail += PER_CATEGORY - n

    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    write_credits(cache)
    log()
    log(f"新抓 {got} 张 · 跳过 {skipped} 张 · 差 {fail} 张")
    log(f"署名清单 → {CREDITS}")
    return 0


def write_credits(cache: dict) -> None:
    """署名清单。**CC BY 要求署名**，不写这份文件这批图就是不合规的。"""
    lines = [
        "# 商品图来源与许可证",
        "",
        "这批图来自 [Openverse](https://openverse.org/)，只取 **CC0 / 公有领域 /",
        "CC BY** 三种许可证。抓取脚本：`deploy/scripts/fetch_images.py`。",
        "",
        "为什么下载进仓库而不是外链：整页零外部请求是这个项目的硬约束",
        "（演示环境常常没有外网，「打不开」比「不好看」严重得多），而且外链的图",
        "随时会被删被换，那时候没人知道页面上曾经是什么。",
        "",
        "图片经过裁剪与缩放（500×625，JPEG）以适配卡片版式。",
        "",
        "`9001.jpg`–`9012.jpg` 与 `banner.jpg` 是更早一批 Unsplash 开放授权图。",
        "",
        "| 文件 | 类目 | 标题 | 作者 | 许可证 | 原始页面 |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted(cache):
        m = cache[name]
        title = (m.get("title") or "").replace("|", "/")[:48]
        landing = m.get("foreign_landing_url") or m.get("url") or ""
        lines.append(
            f"| `{name}` | {m.get('category', '')} | {title} | "
            f"{(m.get('creator') or '—').replace('|', '/')[:28]} | "
            f"{m.get('license', '')} | {landing} |")
    CREDITS.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
