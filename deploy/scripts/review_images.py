#!/usr/bin/env python3
"""把抓回来的商品图拼成联系表，供人眼过一遍。

    python deploy/scripts/review_images.py     # → review1..N.png

**这一步没法自动化。** 搜索词写得再准，CC 图库里也没有干净的电商产品图——
它是业余摄影的聚合。实测两轮改词都是打地鼠："hoodie" 搜出赤膊男人，
"soy sauce" 搜出草坪洒水器，修好五个又坏三个。

所以最后一道是看图：跑这个脚本，一行是一个类目的 4 张候选，把对得上的
编号填进 fetch_images.py 的 KEEP，再跑 `fetch_images.py --prune`。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_images import IMG_DIR, QUERIES  # noqa: E402

CELL, PAD, LABEL_W, PER_SHEET = 132, 4, 96, 15


def main() -> int:
    from PIL import Image, ImageDraw

    out = Path.cwd()
    ids = sorted(QUERIES)
    for s in range((len(ids) + PER_SHEET - 1) // PER_SHEET):
        chunk = ids[s * PER_SHEET:(s + 1) * PER_SHEET]
        sheet = Image.new(
            "RGB", (4 * (CELL + PAD) + PAD + LABEL_W,
                    len(chunk) * (CELL + PAD) + PAD), "white")
        d = ImageDraw.Draw(sheet)
        for r, cid in enumerate(chunk):
            y = PAD + r * (CELL + PAD)
            d.text((4, y + CELL // 2 - 14), QUERIES[cid][0], fill="black")
            d.text((4, y + CELL // 2 + 2), str(cid), fill="#888")
            for i in range(1, 5):
                f = IMG_DIR / f"c{cid}-{i}.jpg"
                x = LABEL_W + PAD + (i - 1) * (CELL + PAD)
                if f.is_file():
                    im = Image.open(f).convert("RGB")
                    sheet.paste(im.resize((CELL, CELL), Image.LANCZOS), (x, y))
                d.text((x + 2, y + 2), str(i), fill="red")
        path = out / f"review{s + 1}.png"
        sheet.save(path)
        print(f"→ {path}  {len(chunk)} 个类目")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
