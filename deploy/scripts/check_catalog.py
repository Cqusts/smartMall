#!/usr/bin/env python3
"""扫一遍生成出来的目录，找类目与属性对不上的地方。

    python deploy/scripts/check_catalog.py

**抽查几个看不出问题。** 570 个商品里我抽查 8 个，找出了「锌合金大颗粒
积木」「竹纤维奶瓶」「金属笔记本」「有页数的圆珠笔」四处——那说明抽查
的命中率高得可怕，也说明剩下 562 个里还有。这个脚本把判据写下来跑全量。

判据都是"这个词和那个词不该出现在同一个商品上"的形式。它抓不到语义上
的细微别扭（比如"天然饮用水"配"水、二氧化碳"），那类只能靠人看；
它抓的是**一眼假**的那种，而那种才会在演示时被当场指出来。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SQL = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "013_catalog_500.sql"

#: (类目名匹配, 属性键, 不该出现的值们, 说明)
FORBIDDEN: list[tuple[str, str, tuple[str, ...], str]] = [
    ("积木|拼装", "材质", ("锌合金", "短绒布+PP棉", "硅胶"), "积木不该是金属或毛绒的"),
    ("毛绒|公仔", "材质", ("ABS塑料", "锌合金", "实木", "硅胶"), "毛绒公仔得是布的"),
    ("奶瓶|喂养", "材质", ("竹纤维", "A类纯棉", "有机棉"), "奶瓶不是布做的"),
    ("笔类", "页数", (), "一支笔没有页数"),
    ("笔记本|本册", "材质", ("金属", "ABS塑料"), "本子是纸做的"),
    ("洗护", "材质", (), "洗发水没有材质，该是主要成分"),
    ("家用器械|医疗", "配料", (), "血压计没有配料"),
    ("宠物用品", "配料", (), "猫砂盆没有配料"),
]

#: 这些属性键只该出现在特定类目下
ONLY_IN: list[tuple[str, str]] = [
    ("尺码表", "服装|卫衣|牛仔裤|风衣|鞋|单鞋|休闲鞋"),
]

#: 食品类不该出现在"想买件衣服"的筛选里——SKU 规格维度是这条的依据
APPAREL_ONLY_SPEC = ("尺码", "鞋码")
FOOD_CATS = "零食|饼干|坚果|肉干|粮油|调味|咖啡|乳品|果汁|饮用水|水果|蔬菜|牛羊肉|海鲜"


def main() -> int:
    if not SQL.is_file():
        print(f"✗ 找不到 {SQL}，先跑 gen_catalog.py")
        return 1
    s = SQL.read_text(encoding="utf-8")

    cats = {int(i): n for i, _, n in
            re.findall(r"\((\d+), (\d+), '([^']*)', \d, '[^']*'\)", s)}
    prods: dict[int, tuple[str, int]] = {}
    for pid, name, _short, cid in re.findall(
            r"\((1\d{4}), 'P\d+', '([^']*)', '([^']*)', (\d+),", s):
        prods[int(pid)] = (name, int(cid))

    attrs: dict[int, dict[str, str]] = {}
    for pid, k, v in re.findall(r"\((1\d{4}), '([^']*)', '([^']*)', 1\)", s):
        attrs.setdefault(int(pid), {})[k] = v

    skus: dict[int, list[str]] = {}
    for pid, spec in re.findall(r"\('S(1\d{4})-\d+', 1\d{4}, '(\{[^']*\})'", s):
        skus.setdefault(int(pid), []).append(spec)

    charts = {int(m) for m in re.findall(r"\((1\d{4}), '\{\"表头\"", s)}

    bad: list[str] = []

    for pid, (name, cid) in prods.items():
        cat = cats.get(cid, "?")
        a = attrs.get(pid, {})

        for cat_re, key, values, why in FORBIDDEN:
            if not re.search(cat_re, cat):
                continue
            if not values:                      # 这个字段整个不该存在
                if key in a:
                    bad.append(f"#{pid} {name}（{cat}）多了「{key}」—— {why}")
            elif a.get(key) in values:
                bad.append(f"#{pid} {name}（{cat}）{key}={a[key]} —— {why}")

        # 尺码表只该给服饰鞋靴
        if pid in charts and not re.search(ONLY_IN[0][1], cat):
            bad.append(f"#{pid} {name}（{cat}）有尺码表 —— 只有服饰鞋靴该有")

        # 食品不该有尺码规格：导购按尺码筛，会把零食筛进"想要件外套"
        if re.search(FOOD_CATS, cat):
            for spec in skus.get(pid, []):
                for dim in APPAREL_ONLY_SPEC:
                    if f'"{dim}"' in spec:
                        bad.append(f"#{pid} {name}（{cat}）规格里有「{dim}」")

        if not a:
            bad.append(f"#{pid} {name}（{cat}）一条属性都没有")
        if not skus.get(pid):
            bad.append(f"#{pid} {name}（{cat}）一个 SKU 都没有")

    # 重名：库里两个同名商品会让"用户问的是哪一件"永远说不清
    seen: dict[str, int] = {}
    for pid, (name, _) in prods.items():
        if name in seen:
            bad.append(f"#{pid} 与 #{seen[name]} 重名：{name}")
        seen[name] = pid

    print(f"商品 {len(prods)} · 属性 {sum(len(v) for v in attrs.values())}"
          f" · SKU {sum(len(v) for v in skus.values())} · 尺码表 {len(charts)}")
    if bad:
        print(f"\n✗ {len(bad)} 处对不上：")
        for line in bad[:40]:
            print("   " + line)
        if len(bad) > 40:
            print(f"   …… 还有 {len(bad) - 40} 处")
        return 1
    print("✅ 没找到类目与属性对不上的地方")
    return 0


if __name__ == "__main__":
    sys.exit(main())
