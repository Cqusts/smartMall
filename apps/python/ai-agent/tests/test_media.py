"""运营 Agent 的素材生成。

**图比文字更难查，所以关口全在提示词上。** 一张画着蓬松羊毛质感的图配
一件涤纶的衣服，用户收货时的落差和文案写"精选羊毛"一模一样——区别只是
文案里那两个字能被规则揪出来，图里的质感揪不出来。

所以下面钉的第一件事是：**提示词里绝不出现属性表没有的材质**。
剩下的几件都是同一条老判据的分支——生成失败 ≠ 生成不出来，
下载失败 ≠ 任务失败，查不到状态 ≠ 任务挂了。
"""

from __future__ import annotations

import pytest

from app.agent.marketing import media, media_flow
from app.agent.marketing.media_flow import MediaBrief, safe_run_media
from app.agent.marketing.store import StubAssetStore
from app.agent.nodes import Deps
from app.agent.retriever import StubRetriever
from app.agent.tools import StubToolBox


# ---------------------------------------------------------------- 替身


_WOOL = {
    "id": 9001, "name": "米白针织衫", "category": "针织衫",
    "attrs": {"材质": "100%羊毛", "克重": "320g", "颜色": "米白",
              "产地": "江苏南通"},
}

_POLY = {
    "id": 9002, "name": "轻薄外套", "category": "外套",
    "attrs": {"材质": "100%聚酯纤维", "颜色": "藏青"},
}


def build(products=None, *, mediac="ok", store=True, asset_dir=None) -> Deps:
    """装一套素材生成的依赖。

    ``mediac`` 取 ``ok`` / ``none`` / 一个注入故障的字符串。
    """
    box = StubToolBox(products=dict(products or {p["id"]: p
                                                for p in (_WOOL, _POLY)}))
    client = None
    if mediac == "ok":
        client = media.FakeMediaClient()
    elif mediac != "none":
        client = media.FakeMediaClient(fail=mediac)
    return Deps(llm=None, retriever=StubRetriever([]), tools=box,
                media=client, asset_store=StubAssetStore() if store else None,
                asset_dir=asset_dir)


def _prompt_of(deps: Deps) -> str:
    return deps.media.calls[-1][1]


# ---------------------------------------------------------------- 提示词


class TestPrompt:
    """**这一组是整条链路的重点。** 生成完的图，规则一个字也读不了。"""

    def test_不写属性表里没有的材质(self):
        deps = build()
        state = safe_run_media(MediaBrief(product_id=9002), deps)

        assert state.outcome == "generated"
        prompt = _prompt_of(deps)
        assert "聚酯纤维" in prompt
        # 模型最爱干的事就是把"聚酯纤维"顺手升级成"羊毛质感"。
        # 提示词是模板拼的，每个词都追得到属性表的某一行，所以这里必须干净
        for fiber in ("羊毛", "羊绒", "真丝", "亚麻", "棉"):
            assert fiber not in prompt, f"提示词里冒出了属性表没有的「{fiber}」"

    def test_画不出来的属性不进提示词(self):
        """克重、产地画不出来，塞进去只是噪音。"""
        deps = build()
        safe_run_media(MediaBrief(product_id=9001), deps)

        prompt = _prompt_of(deps)
        assert "羊毛" in prompt and "米白" in prompt
        assert "320g" not in prompt and "南通" not in prompt

    def test_用途决定画面而不是形容词(self):
        deps = build()
        safe_run_media(MediaBrief(product_id=9001, usage="scene"), deps)
        scene = _prompt_of(deps)
        safe_run_media(MediaBrief(product_id=9001, usage="white"), deps)
        white = _prompt_of(deps)

        assert scene != white
        assert "纯白背景" in white
        # "高级感""大片质感"对模型是噪音，对商家是幻觉——生成出来不像的时候
        # 没人说得清是哪个词没起作用
        for hollow in ("高级感", "大片质感", "质感大片", "氛围感"):
            assert hollow not in scene and hollow not in white

    def test_画面描述里不混英文(self):
        """实测踩到：``室内木质background虚化``——一个手滑打成英文的词
        原样进了提示词。中文模型不一定按预期理解它，而这种错在跑之前
        完全看不出来（代码里那一行读起来就是"背景"）。"""
        for usage, scene in media_flow.USAGE_SCENES.items():
            latin = [ch for ch in scene if "a" <= ch.lower() <= "z"]
            assert not latin, f"{usage} 的画面描述里混进了英文：{scene}"

    def test_负向提示词挡掉画面里的文字(self):
        """画面里出现文字容易变成"广告语"，而那部分不受属性表约束。"""
        assert "促销文字" in media_flow.NEGATIVE
        assert "价格标签" in media_flow.NEGATIVE

    def test_提示词过同一套合规检查(self, monkeypatch):
        """极限词进了提示词，模型就会往那个方向画。"""
        monkeypatch.setitem(
            media_flow.USAGE_SCENES, "white", "全网最低价的电商主图")
        deps = build()
        state = safe_run_media(MediaBrief(product_id=9001), deps)

        assert state.outcome == "needs_human"
        assert any("最低价" in f for f in state.flags)
        # **一次模型都没调。** 拦在这里的全部意义就是别花那份额度
        assert deps.media.calls == []


# ---------------------------------------------------------------- 前置条件


class TestPrecondition:
    def test_属性表是空的就不生成(self):
        """凭商品名想象一件商品，正是虚假宣传的定义。"""
        deps = build({7000: {"id": 7000, "name": "神秘商品", "attrs": {}}})
        state = safe_run_media(MediaBrief(product_id=7000), deps)

        assert state.outcome == "needs_human"
        assert deps.media.calls == []
        assert deps.asset_store.staged == []

    def test_商品不存在就跳过(self):
        deps = build()
        state = safe_run_media(MediaBrief(product_id=404404), deps)

        assert state.outcome == "skipped"
        assert deps.media.calls == []

    def test_查商品失败不等于这个商品没属性(self):
        deps = build()
        deps.tools.fail = True
        state = safe_run_media(MediaBrief(product_id=9001), deps)

        # skipped 而不是 needs_human：库连不上是临时故障，
        # 记成"要人处理"等于把它转成一个永远躺在那儿的人工任务
        assert state.outcome == "skipped"
        assert state.trace.error

    def test_没接模型时提示词照出(self):
        """看得见提示词是决定要不要接这条链路的前提，而那不需要先花钱。"""
        deps = build(mediac="none")
        state = safe_run_media(MediaBrief(product_id=9001), deps)

        assert state.outcome == "prompt_only"
        assert "羊毛" in state.prompt
        assert deps.asset_store.staged == []


# ---------------------------------------------------------------- 失败分类


class TestFailure:
    def test_生成失败是跳过不是要人处理(self):
        """限流和超时下次就好了。标成"要人处理"是把临时故障转成永久任务。"""
        deps = build(mediac="image")
        state = safe_run_media(MediaBrief(product_id=9001), deps)

        assert state.outcome == "skipped"
        assert state.outcome != "needs_human"
        assert deps.asset_store.staged == []

    def test_落库失败不谎报成功(self):
        deps = build()
        deps.asset_store.fail = True
        state = safe_run_media(MediaBrief(product_id=9001), deps)

        assert state.outcome == "skipped"
        assert state.asset_id is None

    def test_没接素材库要人处理(self, tmp_path):
        """额度已经花掉了，而结果没有任何地方记着——24 小时后连补下载都做不到。"""
        deps = build(store=False, asset_dir=tmp_path)
        state = safe_run_media(MediaBrief(product_id=9001), deps)

        assert state.outcome == "needs_human"
        assert any("失效" in f for f in state.flags)


# ---------------------------------------------------------------- 落地


class TestStore:
    def test_立刻下载到本地(self, tmp_path, monkeypatch):
        """URL 24 小时后失效，只存 URL 的话演示第二天就是一片裂图。"""
        seen: list[str] = []

        def fake_download(url, dest, **kw):
            seen.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake-png")
            return 8

        monkeypatch.setattr(media, "download", fake_download)
        deps = build(asset_dir=tmp_path)
        state = safe_run_media(MediaBrief(product_id=9001), deps)

        assert state.outcome == "generated"
        assert len(seen) == 1
        assert state.local_path.startswith("generated/")
        assert (tmp_path / state.local_path.split("/")[-1]).is_file()

    def test_落库的是本地路径源URL只作溯源(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "download",
                            lambda url, dest, **kw: dest.write_bytes(b"x"))
        deps = build(asset_dir=tmp_path)
        safe_run_media(MediaBrief(product_id=9001), deps)

        row = deps.asset_store.staged[-1]
        assert row["local_path"].startswith("generated/")
        assert row["source_url"].startswith("https://")
        assert row["local_path"] != row["source_url"]

    def test_下载失败留记录不丢(self, tmp_path, monkeypatch):
        """URL 还在，24 小时内可以补下载——把记录丢了才是真的没救。"""
        def boom(url, dest, **kw):
            raise media.MediaUnavailableError("注入的下载故障")

        monkeypatch.setattr(media, "download", boom)
        deps = build(asset_dir=tmp_path)
        state = safe_run_media(MediaBrief(product_id=9001), deps)

        assert state.asset_id is not None
        assert deps.asset_store.staged[-1]["source_url"]
        assert any("下载失败" in f for f in state.flags)

    def test_没配落地目录要说出来(self):
        """不说的话，库里会静静躺着一批明天就打不开的 URL。"""
        deps = build(asset_dir=None)
        state = safe_run_media(MediaBrief(product_id=9001), deps)

        assert any("24 小时" in f for f in state.flags)

    def test_一律待审且标记为AI生成(self, tmp_path, monkeypatch):
        """《人工智能生成合成内容标识办法》：生成内容必须可识别。"""
        monkeypatch.setattr(media, "download",
                            lambda url, dest, **kw: dest.write_bytes(b"x"))
        deps = build(asset_dir=tmp_path)
        safe_run_media(MediaBrief(product_id=9001), deps)

        row = deps.asset_store.staged[-1]
        assert row["review_status"] == "pending"
        assert row["ai_generated"] == 1
        # **审核状态不是入参。** 能传就意味着某天会有人传 approved
        assert "review_status" not in row.get("kw", {})

    def test_同一秒内生成两次不互相覆盖(self, tmp_path, monkeypatch):
        """只用秒级时间戳的话，后一个文件会把前一个盖掉，
        而库里两条记录都指向它——看起来像模型画了两张一样的图。"""
        monkeypatch.setattr(media, "download",
                            lambda url, dest, **kw: dest.write_bytes(b"x"))
        monkeypatch.setattr(media_flow.time, "time", lambda: 1755000000.0)
        deps = build(asset_dir=tmp_path)
        a = safe_run_media(MediaBrief(product_id=9001), deps)
        b = safe_run_media(MediaBrief(product_id=9001), deps)

        assert a.local_path and b.local_path
        assert a.local_path != b.local_path

    def test_商品名不能拼进文件路径(self):
        """商品名是商家自己填的，直接拼进路径就是任意文件写入。"""
        name = media_flow._filename(9001, "image", "../../deploy/.env")
        assert "/" not in name and ".." not in name
        assert name.endswith(".png")


# ---------------------------------------------------------------- 视频


class TestVideo:
    def test_只创建任务不等结果(self):
        """生成要 1–5 分钟。挂在请求上就是超时，挂在页面上就是转圈五分钟。"""
        deps = build()
        state = safe_run_media(MediaBrief(product_id=9001, kind="video"), deps)

        assert state.outcome == "queued"
        assert state.task.task_id
        row = deps.asset_store.staged[-1]
        assert row["task_status"] in ("pending", "running")
        assert row["local_path"] == ""

    def test_视频不带图的用途标签(self):
        """默认值 white 会一路落进库里，于是列表页把一条视频显示成
        「白底主图」——实测就是这样。"""
        deps = build()
        safe_run_media(MediaBrief(product_id=9001, kind="video"), deps)

        assert deps.asset_store.staged[-1]["usage_tag"] == ""
        assert MediaBrief(product_id=1, kind="video", usage="scene").usage == ""
        # 文件名不能因此退化成没有信息的 "asset"
        assert "video" in media_flow._filename(9001, "video", "")

    def test_轮询到成功才下载(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "download",
                            lambda url, dest, **kw: dest.write_bytes(b"mp4"))
        deps = build(asset_dir=tmp_path)
        safe_run_media(MediaBrief(product_id=9001, kind="video"), deps)

        # FakeMediaClient 第二次轮询才成功——中间那次要如实报"还在跑"
        first = media_flow.poll_pending(deps)
        assert first.running == 1 and first.succeeded == 0
        assert deps.asset_store.staged[-1]["local_path"] == ""

        second = media_flow.poll_pending(deps)
        assert second.succeeded == 1
        row = deps.asset_store.staged[-1]
        assert row["task_status"] == "succeeded"
        assert row["local_path"].endswith(".mp4")

    def test_查不到状态不判任务失败(self, tmp_path):
        """限流、超时都会让这次查询拿不到结果。记成 failed 就是永久判死刑，
        而实际上下一次查可能就成了。"""
        deps = build(asset_dir=tmp_path)
        safe_run_media(MediaBrief(product_id=9001, kind="video"), deps)
        deps.media.fail = "poll"

        report = media_flow.poll_pending(deps)

        assert report.unknown == 1 and report.failed == 0
        # 状态没被改写，下一轮还会再捞到它
        assert deps.asset_store.staged[-1]["task_status"] in ("pending", "running")
        assert deps.asset_store.unfinished()

    def test_下载失败不改判任务失败(self, tmp_path, monkeypatch):
        """任务确实成功了，失败的是下载这一步。记成 failed 会让人去改提示词，
        而该做的是重下一次。"""
        def boom(url, dest, **kw):
            raise media.MediaUnavailableError("注入的下载故障")

        deps = build(asset_dir=tmp_path)
        safe_run_media(MediaBrief(product_id=9001, kind="video"), deps)
        monkeypatch.setattr(media, "download", boom)

        media_flow.poll_pending(deps)
        media_flow.poll_pending(deps)

        row = deps.asset_store.staged[-1]
        assert row["task_status"] == "succeeded"
        assert "下载失败" in row["error"]

    def test_失败的任务不再重查(self, tmp_path):
        """24 小时后过期的任务状态是 UNKNOWN，查一万次也不会变。"""
        deps = build(asset_dir=tmp_path)
        safe_run_media(MediaBrief(product_id=9001, kind="video"), deps)
        deps.asset_store.finish(deps.asset_store.staged[-1]["id"],
                                task_status="failed", error="任务已过期")

        assert deps.asset_store.unfinished() == []
        assert media_flow.poll_pending(deps).checked == 0

    def test_轮询空值不覆盖已落好的路径(self, tmp_path):
        """轮询到 running 时这两个字段本来就是空的，直接写会把文件路径抹掉。"""
        store = StubAssetStore()
        aid = store.stage_asset(product_id=9001, kind="video",
                                local_path="generated/a.mp4",
                                source_url="https://x/a.mp4",
                                task_id="t1", task_status="running")
        store.finish(aid, task_status="running")

        row = store.staged[-1]
        assert row["local_path"] == "generated/a.mp4"
        assert row["source_url"] == "https://x/a.mp4"


# ---------------------------------------------------------------- 客户端


class TestClient:
    def test_没有key时说清楚去哪申请(self, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(media.MediaConfigError) as exc:
            media.DashScopeMediaClient()
        assert "DASHSCOPE_API_KEY" in str(exc.value)

    @pytest.mark.parametrize("code,cls", [
        (401, media.MediaConfigError),   # 改提示词没用，去看 key
        (403, media.MediaConfigError),
        (400, media.MediaConfigError),
        (429, media.MediaUnavailableError),  # 等一会儿就好
        (500, media.MediaUnavailableError),
        (503, media.MediaUnavailableError),
    ])
    def test_按状态码分类而不是按响应体字眼(self, code, cls):
        """响应体的措辞会变（不同模型、不同版本都不一样），状态码不会。"""
        class Resp:
            status_code = code
            text = "whatever"

        with pytest.raises(cls):
            media._raise_for(Resp(), "文生图")

    def test_响应少字段不抛KeyError(self):
        """响应结构是外部契约，字段缺失是要处理的情况而不是异常——
        直接下标抛的 KeyError 完全看不出是响应变了。"""
        assert media._dig({"output": {}}, "output", "choices", 0) is None
        assert media._dig({"output": {"choices": []}},
                          "output", "choices", 0, "message") is None
        assert media._dig(None, "a") is None

    def test_过期任务归到失败而不是永远pending(self):
        """UNKNOWN 是任务不存在或已过期，等下去不会变好。"""
        assert media._norm_status("UNKNOWN") == "failed"
        assert media._norm_status("RUNNING") == "running"
        assert media._norm_status("SUCCEEDED") == "succeeded"

    def test_水印不是可选项(self):
        """《人工智能生成合成内容标识办法》要求生成内容可识别。"""
        import inspect

        src = inspect.getsource(media.DashScopeMediaClient)
        assert src.count('"watermark": True') == 2  # 图与视频两条路都要有
        # **做成入参就意味着某天会被关掉**，所以两个方法的签名里都不该有它
        for fn in (media.DashScopeMediaClient.generate_image,
                   media.DashScopeMediaClient.create_video_task):
            assert "watermark" not in inspect.signature(fn).parameters

    def test_视频必须带异步头(self):
        """不加这个头会直接报"不支持同步调用"，而报错看起来像账号权限问题。"""
        import inspect

        src = inspect.getsource(media.DashScopeMediaClient.create_video_task)
        assert "X-DashScope-Async" in src


# ---------------------------------------------------------------- 真 SQL

#: 与 011_marketing_asset.sql 对应的 SQLite 版本。
#:
#: **替身测不出 SQL 的问题。** StubAssetStore 里 finish 是几行 Python，
#: 而线上那条 UPDATE 里有 CASE WHEN、有重复的绑定参数、有 lastrowid——
#: 这三样每一样都栽过（``LAST_INSERT_ID()`` 那次就是 MySQL 方言在
#: SQLite 上直接炸）。所以这一组跑真的引擎。
_ASSET_DDL = """
CREATE TABLE marketing_asset (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL,
  kind TEXT NOT NULL, usage_tag TEXT DEFAULT '',
  local_path TEXT DEFAULT '', source_url TEXT,
  prompt TEXT, negative_prompt TEXT,
  task_id TEXT, task_status TEXT DEFAULT 'succeeded',
  error TEXT DEFAULT '', model TEXT DEFAULT '',
  ai_generated INTEGER DEFAULT 1, review_status TEXT DEFAULT 'pending',
  reviewer_id INTEGER, reviewed_at TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP)
"""


@pytest.fixture
def sql_store():
    from sqlalchemy import create_engine, text

    from app.agent.marketing.store import MySqlAssetStore

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(_ASSET_DDL))
    return MySqlAssetStore(engine=engine)


class TestRealSql:
    def test_写入的是待审且带AI标识(self, sql_store):
        aid = sql_store.stage_asset(product_id=9001, kind="image",
                                    usage_tag="white",
                                    local_path="generated/a.png",
                                    source_url="https://x/a.png",
                                    prompt="米白针织衫", model="qwen-image")
        rows = sql_store.list_assets(9001)
        assert len(rows) == 1 and rows[0]["id"] == aid
        assert rows[0]["review_status"] == "pending"
        assert rows[0]["ai_generated"] == 1

    def test_只捞没跑完的视频任务(self, sql_store):
        running = sql_store.stage_asset(product_id=1, kind="video",
                                        task_id="t-run", task_status="running")
        sql_store.stage_asset(product_id=1, kind="video", task_id="t-fail",
                              task_status="failed")
        sql_store.stage_asset(product_id=1, kind="image")  # 图没有任务

        ids = [r["id"] for r in sql_store.unfinished()]
        assert ids == [running]

    def test_轮询空值不覆盖已落好的路径(self, sql_store):
        aid = sql_store.stage_asset(product_id=1, kind="video",
                                    local_path="generated/a.mp4",
                                    source_url="https://x/a.mp4",
                                    task_id="t1", task_status="running")
        assert sql_store.finish(aid, task_status="running")

        row = sql_store.list_assets()[0]
        assert row["local_path"] == "generated/a.mp4"

    def test_写坏的状态不会让任务永远捞不回来(self, sql_store):
        """状态是被轮询接口间接驱动的，一个拼错的字符串会让 unfinished
        永远漏掉它——而那种故障不报任何错。"""
        aid = sql_store.stage_asset(product_id=1, kind="video", task_id="t1",
                                    task_status="谁知道呢")
        assert sql_store.list_assets()[0]["task_status"] == "failed"
        assert sql_store.finish(aid, task_status="也不对") is False


# ---------------------------------------------------------------- HTTP 口


class TestHttp:
    """商家后台那三个接口。

    **这一层单独测，是因为它是全项目唯一一处「本地跑、没有下游替它把关」
    的写操作。** ws.py 里那批 /api/admin/* 都是纯转发，权限在 mall-product
    的 @RequireMerchant 上；这里没有转发，闸门只能长在自己身上——
    而它守的恰恰是花钱的那个动作。
    """

    def _client(self, monkeypatch, deps=None, *, role="merchant"):
        """替身用 monkeypatch 装，**不直接改模块属性**：那样改完不还原，
        泄到同一进程里后跑的用例上——而"某个接口在别人跑过之后才失败"
        是最难查的一类。
        """
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from app.main import app
        from app.routers import media as router

        async def fake_principal(request):
            if not request.headers.get("Authorization"):
                return None
            return {"userId": 1, "username": "u", "role": role}

        monkeypatch.setattr(router, "_principal", fake_principal)
        if deps is not None:
            monkeypatch.setattr(router, "get_deps", lambda: deps)
        return TestClient(app)

    def test_不带令牌不能生成(self, monkeypatch):
        deps = build()
        r = self._client(monkeypatch, deps).post(
            "/api/admin/media", json={"productId": 9001})

        assert r.json()["code"] == 1401
        # **一次模型都没调。** 这道闸门守的就是这件事
        assert deps.media.calls == []

    def test_买家令牌不能生成(self, monkeypatch):
        deps = build()
        r = self._client(monkeypatch, deps, role="customer").post(
            "/api/admin/media", json={"productId": 9001},
            headers={"Authorization": "Bearer x"})

        assert r.json()["code"] == 1403
        assert deps.media.calls == []

    def test_问不到身份就当没登录(self, monkeypatch):
        """下游不可用时放行，等于给了任何人一条「把订单服务打挂就能生成」的路。

        这条**不替换 _principal**，走真的那一份，只把地址指到一个确定
        没人监听的端口——要验的正是它连不上时的选择。
        """
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from app.config import settings
        from app.main import app
        from app.routers import media as router

        deps = build()
        monkeypatch.setattr(router, "get_deps", lambda: deps)
        monkeypatch.setattr(settings, "order_base_url", "http://127.0.0.1:1")

        r = TestClient(app).post("/api/admin/media", json={"productId": 9001},
                                 headers={"Authorization": "Bearer x"})
        assert r.json()["code"] == 1401
        assert deps.media.calls == []

    def test_商家可以生成(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "download",
                            lambda url, dest, **kw: dest.write_bytes(b"x"))
        deps = build(asset_dir=tmp_path)
        r = self._client(monkeypatch, deps).post(
            "/api/admin/media", json={"productId": 9001, "kind": "image"},
            headers={"Authorization": "Bearer x"})

        body = r.json()
        assert body["code"] == 0
        assert body["data"]["outcome"] == "generated"
        assert body["data"]["aiGenerated"] is True

    def test_审核状态不是入参(self, monkeypatch):
        """能传就意味着某天会有人传 approved。"""
        deps = build()
        self._client(monkeypatch, deps).post(
            "/api/admin/media",
            json={"productId": 9001, "reviewStatus": "approved",
                  "aiGenerated": False},
            headers={"Authorization": "Bearer x"})

        row = deps.asset_store.staged[-1]
        assert row["review_status"] == "pending"
        assert row["ai_generated"] == 1

    def test_没生成出来也要把原因原样返回(self, monkeypatch):
        """压成一句"生成失败"，商家就无从判断该改什么。"""
        deps = build({7000: {"id": 7000, "name": "神秘商品", "attrs": {}}})
        r = self._client(monkeypatch, deps).post(
            "/api/admin/media", json={"productId": 7000},
            headers={"Authorization": "Bearer x"})

        body = r.json()
        assert body["code"] != 0
        assert "属性" in body["message"]

    def test_拒绝时也用统一信封(self, monkeypatch):
        """抛 HTTPException 会渲染成 {"detail": ...}，页面拿到的 message
        是 undefined，于是只能显示一句「操作失败」。"""
        r = self._client(monkeypatch, build()).post(
            "/api/admin/media", json={"productId": 9001})
        body = r.json()
        assert set(body) == {"code", "message", "data"}
        assert body["message"]

    def test_素材文件不能穿越到env(self, monkeypatch):
        """.env 里躺着 API key。"""
        client = self._client(monkeypatch, build())
        for bad in ("..%2F..%2Fdeploy%2F.env", "....//.env", "a.txt",
                    "%2e%2e%2f%2e%2e%2fdeploy%2f.env"):
            assert client.get(f"/generated/{bad}").status_code == 404


# ---------------------------------------------------------------- 埋点


class TestTrace:
    def test_步骤标签是中文不是函数名(self):
        """这个面板是给人看的，演示时对方不该需要先读一遍源码。"""
        for node, label in media_flow.NODE_LABELS.items():
            assert label != node
            assert any("一" <= ch <= "鿿" for ch in label)

    def test_每步都报告了自己的产出(self):
        deps = build()
        events: list[dict] = []
        deps.on_event = events.append
        safe_run_media(MediaBrief(product_id=9001), deps)

        exits = [e for e in events if e.get("phase") == "exit"]
        assert {e["node"] for e in exits} >= {"load", "prompt", "check", "generate"}
        # 光有节点名说明不了问题——"拼提示词"跑完了，拼成什么样？
        assert all(e["detail"] for e in exits)
