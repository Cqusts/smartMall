#!/usr/bin/env python3
"""对**真实本机 MySQL** 复核订单链路。跨平台，不需要 bash、docker、curl。

    python deploy/scripts/verify-orders.py              状态机 + 防超卖
    python deploy/scripts/verify-orders.py lifecycle    只跑状态机
    python deploy/scripts/verify-orders.py concurrency  只跑防超卖
    python deploy/scripts/verify-orders.py plan         超时回收查询的执行计划（不需要起服务）

**为什么 73 个单元测试之外还要这个：单元测试跑在 H2 上，H2 不是 MySQL。**
两处已经真实咬过人：

· **UPDATE 的 SET 子句求值顺序。**MySQL 从左到右，后面的赋值看得见前面刚写入
  的新值；H2 用标准 SQL 语义，整行读旧值。于是

      SET status = 'refunding', status_before_refund = status

  在 H2 上把旧状态存进 status_before_refund（对），在 MySQL 上存的却是
  'refunding' 自己（错）—— 驳回时"还原"成 refunding，订单永远卡在审核中。
  这个 bug 真实发生过，而当时 73 个单元测试全绿。

· **行锁语义。**防超卖的全部保证压在

      UPDATE sku SET stock = stock - ? WHERE sku_no = ? AND stock >= ?

  这一条语句的原子性上，而它原子不原子取决于存储引擎在持锁状态下怎么求值
  谓词。H2 通过不等于 InnoDB 通过。

· **执行计划。**H2 没有 MySQL 优化器的行为，查询"走没走索引"单元测试永远看不见。
  超时回收那条查询原先写成 ORDER BY id LIMIT，优化器宁愿扫主键也不走 idx_status_created，
  30 万行时 310ms、而且随表增大线性变慢；改成 ORDER BY created_at, id + 游标后是 4ms。
  ``plan`` 模式把 Mapper 源码里的真实 SQL 抠出来、在一张灌了 20 万行的表上 EXPLAIN。

前置（除 plan 外）：mall-product 已启动（.\\smartmall.ps1 up），本机 MySQL 可连，种子数据已导入。
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smartmall_env as env  # noqa: E402

env.load_env()

BASE = f"http://127.0.0.1:{env.SERVICES['mall-product']}"
API = f"{BASE}/api/product"


def post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        f"{API}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"code": e.code, "message": str(e)}
    except Exception as exc:
        return {"code": -1, "message": str(exc)}


def admin() -> str:
    user, password = env.admin_credentials()
    return user, password


def q(statement: str, args: tuple = ()) -> list[tuple]:
    """用管理员账号查/改库。复核脚本要摆布库存，权限比应用账号高。"""
    user, password = env.admin_credentials()
    with env.connect(user, password, env.database()) as conn, conn.cursor() as cur:
        # args 为空时传 None：pymysql 对空元组也会做 % 格式化，SQL 里有字面的 % 就炸
        # （比如 LIKE 'PLAN%'）。非空时照旧，调用方用 %% 转义字面百分号
        cur.execute(statement, args or None)
        return list(cur.fetchall() or ())


def one(statement: str, args: tuple = ()):
    rows = q(statement, args)
    return rows[0][0] if rows else None


def service_up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE}/health", timeout=3).read()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 状态机

def lifecycle(sku_no: str = "S9003-FLORAL-S", user_id: int = 10086) -> int:
    qty, stock0 = 2, 20
    fail = 0
    print(f"==> 订单状态机全链路复核（{sku_no}）")
    q("UPDATE sku SET stock=%s, status='on_sale' WHERE sku_no=%s", (stock0, sku_no))

    resp = post("/orders", {"requestId": f"lc-{int(time.time() * 1000)}",
                            "userId": user_id, "skuNo": sku_no, "quantity": qty})
    if resp.get("code") != 0:
        print(f"  ✗ 下单失败：{resp}")
        return 1
    no = resp["data"]["orderNo"]
    print(f"  订单号 {no}\n")

    def check(label: str, want_status: str, want_stock: int) -> None:
        nonlocal fail
        got_status = one("SELECT status FROM mall_order WHERE order_no=%s", (no,))
        got_stock = one("SELECT stock FROM sku WHERE sku_no=%s", (sku_no,))
        if got_status == want_status and got_stock == want_stock:
            print(f"  ✓ {label:<22} 状态={got_status:<16} 库存={got_stock}")
        else:
            print(f"  ✗ {label:<22} 状态={got_status:<16} 库存={got_stock}"
                  f"（期望 {want_status} / {want_stock}）")
            fail = 1

    held = stock0 - qty
    check("下单（预占）", "pending_payment", held)

    post(f"/orders/{no}/pay?userId={user_id}")
    check("支付", "paid", held)

    post(f"/admin/orders/{no}/ship", {"company": "顺丰", "expressNo": "SF7788123"})
    check("发货", "shipped", held)

    post(f"/admin/orders/{no}/deliver")
    check("送达", "delivered", held)

    post(f"/orders/{no}/confirm?userId={user_id}")
    check("确认收货", "completed", held)

    post(f"/orders/{no}/refund?userId={user_id}", {"reason": "七天无理由"})
    check("申请退款（未回补）", "refunding", held)

    # 这一步就是 H2 抓不到的那个：驳回必须回到 completed，不能还是 refunding
    post(f"/admin/orders/{no}/refund/reject", {"reason": "已拆封"})
    check("驳回（回到申请前）", "completed", held)

    post(f"/orders/{no}/refund?userId={user_id}", {"reason": "重新申请"})
    check("再次申请", "refunding", held)

    post(f"/admin/orders/{no}/refund/approve")
    check("同意退款（回补）", "refunded", stock0)

    post(f"/admin/orders/{no}/refund/approve")
    check("重复同意（幂等）", "refunded", stock0)

    post(f"/orders/{no}/cancel?userId={user_id}")
    check("已退款后取消（拒）", "refunded", stock0)

    post(f"/orders/{no}/pay?userId={user_id}")
    check("已退款后支付（拒）", "refunded", stock0)

    print()
    # 物流轨迹的形状要与 004 种子一致 —— 客服工具层直接把它读出来讲给用户听
    tracks = one("SELECT tracks FROM mall_order WHERE order_no=%s", (no,)) or ""
    if '"ts"' in str(tracks) and '"desc"' in str(tracks):
        print("  ✓ 物流轨迹形状正确（含 ts/desc），客服可直接读")
    else:
        print(f"  ✗ 物流轨迹形状不对：{tracks}")
        fail = 1

    # 时区一致性。created_at 由 Java 写（LocalDateTime.now()），shipped_at 由
    # SQL 的 NOW() 写 —— 两个时钟不一致时，同一行里会出现「15:25 下单、23:25 发货」，
    # 而客服正是照着这些字段回答"我的货什么时候发的"。
    # 单元测试抓不到：H2 里两者都走 JVM 时钟，永远一致。
    gap = one("SELECT ABS(TIMESTAMPDIFF(MINUTE, created_at, shipped_at)) "
              "FROM mall_order WHERE order_no=%s", (no,))
    if gap is not None and gap <= 5:
        print(f"  ✓ Java 与 MySQL 时钟一致（created_at 与 shipped_at 相差 {gap} 分钟）")
    else:
        print(f"  ✗ 时区不一致：created_at 与 shipped_at 相差 {gap} 分钟")
        print("    应用启动时会调 AppTimeZone.apply() 钉死为 Asia/Shanghai，")
        print("    检查本机 MySQL 的 time_zone 是否也是 +08:00。")
        fail = 1
    return fail


# ---------------------------------------------------------------- 防超卖

def concurrency(sku_no: str = "S9001-BEIGE-M", stock: int = 5,
                threads: int = 50, user_id: int = 10086) -> int:
    print(f"==> 复核防超卖：{threads} 个并发请求抢 {stock} 件（{sku_no}）")
    tag = f"verify-{int(time.time() * 1000)}"
    q("DELETE FROM mall_order WHERE sku_no=%s AND request_id LIKE 'verify-%%'", (sku_no,))
    q("UPDATE sku SET stock=%s, status='on_sale' WHERE sku_no=%s", (stock, sku_no))
    print(f"  初始库存：{one('SELECT stock FROM sku WHERE sku_no=%s', (sku_no,))}")

    def buy(i: int) -> dict:
        return post("/orders", {"requestId": f"{tag}-{i}", "userId": user_id,
                                "skuNo": sku_no, "quantity": 1})

    # 线程池 + 栅栏：让请求尽量落在同一个窗口里，撞上同一行
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(buy, range(threads)))

    ok = sum(1 for r in results if r.get("code") == 0)
    rejected = sum(1 for r in results if "库存不足" in str(r.get("message", "")))
    final_stock = one("SELECT stock FROM sku WHERE sku_no=%s", (sku_no,))
    orders = one("SELECT COUNT(*) FROM mall_order WHERE sku_no=%s "
                 "AND request_id LIKE %s", (sku_no, tag + "-%"))
    units = one("SELECT COALESCE(SUM(quantity),0) FROM mall_order WHERE sku_no=%s "
                "AND request_id LIKE %s", (sku_no, tag + "-%"))

    print(f"\n  成功下单      {ok}")
    print(f"  库存不足被拒  {rejected}")
    print(f"  订单表记录数  {orders}（{units} 件）")
    print(f"  终态库存      {final_stock}\n")

    fail = 0

    def check(label: str, got, want) -> None:
        nonlocal fail
        if got == want:
            print(f"  ✓ {label}")
        else:
            print(f"  ✗ {label}（期望 {want}，实际 {got}）")
            fail = 1

    check("成功数恰好等于库存", ok, stock)
    check("订单表记录数与成功数一致", orders, ok)
    check("售出件数与初始库存一致", int(units), stock)
    check("终态库存归零", final_stock, 0)
    check("全部请求都有结果", ok + rejected, threads)
    if final_stock is not None and final_stock < 0:
        print("  ✗✗ 出现负库存 —— 超卖了")
        fail = 1
    return fail


# ---------------------------------------------------------------- 执行计划

MAPPER = env.ROOT / "apps/java/mall-product/src/main/java/com/smartmall/product/order/mapper/MallOrderMapper.java"


def extract_select(method: str) -> str:
    """从 Mapper 源码里抠出某个方法上 @Select 的 SQL 文本。

    **抠源码而不是在这里再写一份。**脚本里写一份拷贝，检查的就是拷贝：Mapper 里的 SQL
    被改回 ORDER BY id，这里照样通过。

    先定位方法，再往回找**最近**的 @Select(。第一版写成一个非贪心正则
    ``@Select\\((.*?)\\)\\s*List<..> method``——它从文件里第一个 @Select 开始匹配，一路吃到
    目标方法，把中间所有别的 SQL 拼在一起，EXPLAIN 直接报语法错误。
    """
    import re

    src = MAPPER.read_text(encoding="utf-8")
    m = re.search(r"List<[^>]+>\s+" + re.escape(method) + r"\(", src)
    if not m:
        raise SystemExit(f"在 {MAPPER.name} 里找不到方法 {method}")
    start = src.rfind("@Select(", 0, m.start())
    if start < 0:
        raise SystemExit(f"{method} 前面没有 @Select")
    annotation = src[start + len("@Select("):m.start()]
    lits = re.findall(r'"((?:[^"\\]|\\.)*)"', annotation)
    return "".join(lits)


def plan(rows: int = 200_000, expired: int = 3_000) -> int:
    print(f"==> 超时回收查询的执行计划（灌 {rows:,} 行，其中超期待支付 {expired:,} 笔，且 id 最大）")
    fail = 0
    sql = extract_select("findExpiredPending")
    # 参数替换成字面量。游标用「第一批」的起点——最坏情况，要扫的范围最大
    sql = (sql.replace("#{before}", "NOW() - INTERVAL 30 MINUTE")
              .replace("#{afterCreatedAt}", "'1000-01-01 00:00:00'")
              .replace("#{afterId}", "0")
              .replace("#{limit}", "200"))

    q("DELETE FROM mall_order WHERE order_no LIKE 'PLAN%'")
    sku_no, pid = q("SELECT sku_no, product_id FROM sku WHERE deleted=0 ORDER BY id LIMIT 1")[0]
    try:
        user, password = env.admin_credentials()
        with env.connect(user, password, env.database()) as conn, conn.cursor() as cur:
            cur.execute("SET SESSION cte_max_recursion_depth = 1000000")
            cur.execute(
                "INSERT INTO mall_order (order_no, request_id, user_id, product_id, sku_no, spec, quantity,"
                " amount, status, express_company, express_no, created_at) "
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < %s) "
                "SELECT CONCAT('PLAN', LPAD(i, 16, '0')), CONCAT('plan-', i), 1, %s, %s, '', 1, 1,"
                " IF(MOD(i, 3) = 0, 'pending_payment', 'paid'), '', '', NOW() FROM n", (rows, pid, sku_no))
            cur.execute(
                "INSERT INTO mall_order (order_no, request_id, user_id, product_id, sku_no, spec, quantity,"
                " amount, status, express_company, express_no, created_at) "
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < %s) "
                "SELECT CONCAT('PLANE', LPAD(i, 15, '0')), CONCAT('plan-e-', i), 1, %s, %s, '', 1, 1,"
                " 'pending_payment', '', '', NOW() - INTERVAL 2 HOUR FROM n", (expired, pid, sku_no))
            cur.execute("ANALYZE TABLE mall_order")
            cur.fetchall()

            cur.execute("EXPLAIN " + sql)
            row = dict(zip([d[0] for d in cur.description], cur.fetchall()[0]))
            xs = []
            for _ in range(10):
                t0 = time.perf_counter()
                cur.execute(sql)
                cur.fetchall()
                xs.append((time.perf_counter() - t0) * 1000)
            med = sorted(xs)[len(xs) // 2]
    finally:
        q("DELETE FROM mall_order WHERE order_no LIKE 'PLAN%'")

    print(f"  EXPLAIN: type={row['type']}  key={row['key']}  rows≈{row['rows']}  Extra={row['Extra']}")
    print(f"  耗时中位数 {med:.1f} ms")
    if row["key"] == "idx_status_created" and row["type"] == "range":
        print("  ✓ 走 idx_status_created 的范围扫描")
    else:
        print(f"  ✗ 没走 idx_status_created（实际 key={row['key']}, type={row['type']}）。"
              "每次回收都会按主键顺扫，订单表越大越慢。")
        fail = 1
    if med > 50:
        print(f"  ✗ {med:.0f}ms 太慢了（走索引时应在个位数毫秒）")
        fail = 1
    else:
        print("  ✓ 耗时在预期内")
    return fail


def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what not in ("all", "lifecycle", "concurrency", "plan"):
        print(f"未知参数：{what}（可用：lifecycle / concurrency / plan）")
        return 2

    if what == "plan":              # 只碰数据库，不需要 mall-product 在跑
        fail = plan()
        print("\n✅ 符合预期" if not fail else "\n❌ 复核失败")
        return fail

    if not service_up():
        print(f"✗ mall-product 不可用：{BASE}")
        print("  先起它：python deploy/scripts/run-java.py up mall-product")
        return 1

    fail = 0
    if what in ("all", "lifecycle"):
        fail |= lifecycle()
        print()
    if what in ("all", "concurrency"):
        fail |= concurrency()
        print()

    print("✅ 全部符合预期" if not fail else "❌ 复核失败")
    return fail


if __name__ == "__main__":
    sys.exit(main())
