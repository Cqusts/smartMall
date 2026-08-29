"""``run-java.py`` 的进程判活。

**这一组是被一个真的事故逼出来的。** 五个服务全都死了，而 ``up`` 每次都
打印「已在跑（pid 59740）」然后跳过启动，接着等 90 秒超时。日志里最后一次
启动是十几个小时前、而且是成功的（``Tomcat started on port 8081``），
端口上却什么都没有——症状离病因隔了三层，光看输出根本查不到。

根因是 ``running_pid`` 里那条兜底：

    if svc in cmd or (IS_WINDOWS and not cmd.strip()):

``_cmdline`` 对**已经死掉**的 pid 返回空字符串，而这条兜底本意是「Windows 上
取不到命令行就姑且认了」——于是死进程被判成还在跑。**判据失败的方向是最糟
的那个**：不启动，而且不报错。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def rj():
    spec = importlib.util.spec_from_file_location(
        "run_java", ROOT / "deploy/scripts/run-java.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: 一个确定不存在的 pid。Linux 默认 pid_max 是 32768，Windows 的 pid
#: 是 4 的倍数且远小于这个数
DEAD_PID = 999999


class TestAlive:
    def test_自己活着(self, rj):
        assert rj._alive(os.getpid()) is True

    def test_不存在的pid不算活着(self, rj):
        assert rj._alive(DEAD_PID) is False

    def test_判活不看命令行(self, rj):
        """``_alive`` 只回答「进程在不在」，不回答「是不是那个服务」。

        两件事混在一个函数里正是当初出事的原因——「取不到命令行」和
        「进程根本不在」返回了同一个值。

        看字节码引用的名字而不是源码文本：函数的文档字符串里正好解释了
        「为什么不用 _cmdline」，按文本匹配会被自己的注释绊倒。
        """
        names = set(rj._alive.__code__.co_names)
        assert "_cmdline" not in names, f"判活不能依赖取命令行，实际引用了 {names}"
        # 这条断言得真的看到了函数体，否则上一句等于没验
        assert "IS_WINDOWS" in names


class TestRunningPid:
    @pytest.fixture
    def pidfile(self, rj, tmp_path, monkeypatch):
        monkeypatch.setattr(rj, "LOG_DIR", tmp_path)
        return rj.pid_file("mall-product")

    def test_没有pid文件就是没在跑(self, rj, pidfile):
        assert rj.running_pid("mall-product") is None

    def test_死掉的进程不算在跑(self, rj, pidfile):
        pidfile.write_text(str(DEAD_PID))
        assert rj.running_pid("mall-product") is None

    def test_windows下取不到命令行的死进程也不算在跑(
            self, rj, pidfile, monkeypatch):
        """**这条才是这次修复的核心，而且必须模拟 Windows 才验得到。**

        那条 bug 只在 ``IS_WINDOWS`` 为真时成立：Linux 上 ``_cmdline`` 返回
        空字符串时，兜底条件 ``IS_WINDOWS and not cmd.strip()`` 直接短路为
        False，于是 Linux 跑这组用例**怎么写都是绿的**——第一版就是这样，
        把修复回退掉照样全过。

        所以这里把两个条件都摆出来：``IS_WINDOWS=True`` + 取不到命令行。
        这正是线上那台机器的状态。
        """
        monkeypatch.setattr(rj, "IS_WINDOWS", True)
        monkeypatch.setattr(rj, "_cmdline", lambda pid: "")
        pidfile.write_text(str(DEAD_PID))

        assert rj.running_pid("mall-product") is None, (
            "死进程被判成了「已在跑」——up 会跳过启动然后等到超时")
        assert not pidfile.is_file(), "过期的 pid 文件要清掉"

    def test_windows下取不到命令行但进程活着就放行(
            self, rj, pidfile, monkeypatch):
        """兜底本身要留着：Win11 没有 wmic 时确实取不到命令行，
        那时不能因为「认不出是谁」就把一个真在跑的服务判死。

        与上一条的唯一差别是进程存不存在——这两条一起才说明
        判据卡在了正确的位置上。
        """
        monkeypatch.setattr(rj, "IS_WINDOWS", True)
        monkeypatch.setattr(rj, "_cmdline", lambda pid: "")
        monkeypatch.setattr(rj, "_alive", lambda pid: True)
        pidfile.write_text(str(DEAD_PID))

        assert rj.running_pid("mall-product") == DEAD_PID

    def test_死掉之后要清掉pid文件(self, rj, pidfile):
        """留着的话，下次 ``down`` 会照着这个 pid 去 kill——
        而 pid 会被系统回收给别的程序。"""
        pidfile.write_text(str(DEAD_PID))
        rj.running_pid("mall-product")
        assert not pidfile.is_file()

    def test_pid文件内容不是数字(self, rj, pidfile):
        pidfile.write_text("我不是数字")
        assert rj.running_pid("mall-product") is None
        assert not pidfile.is_file()

    def test_活着但不是这个服务(self, rj, pidfile):
        """当前进程活着，但命令行里没有 mall-product。

        Windows 上取不到命令行时会姑且认了（那条兜底还在），所以这条
        断言只在能取到命令行的平台上成立。
        """
        if rj.IS_WINDOWS:
            pytest.skip("Windows 上取不到命令行时按兜底放行，见 running_pid")
        pidfile.write_text(str(os.getpid()))
        assert rj.running_pid("mall-product") is None


class TestWindowsTasklistParsing:
    """Windows 判活靠解析 tasklist 的输出，这里把两种输出形状钉住。

    不能按「输出空不空」判——查不到时 tasklist 往 **stdout** 打一句
    「没有运行的任务匹配指定标准」，而且那句话随系统语言变。
    """

    FOUND = '"java.exe","59740","Console","1","1,234,567 K"\n'
    NOT_FOUND_ZH = "信息: 没有运行的任务匹配指定标准。\n"
    NOT_FOUND_EN = "INFO: No tasks are running which match the specified criteria.\n"

    @pytest.mark.parametrize("out,expected", [
        (FOUND, True),
        (NOT_FOUND_ZH, False),
        (NOT_FOUND_EN, False),
        ("", False),
    ])
    def test_按pid是否出现在输出里判断(self, out, expected):
        assert ('"59740"' in out) is expected

    def test_提示语里不含带引号的pid(self):
        """如果哪天提示语变了、恰好含 "59740"，这条会先炸。"""
        for msg in (self.NOT_FOUND_ZH, self.NOT_FOUND_EN):
            assert "59740" not in msg
