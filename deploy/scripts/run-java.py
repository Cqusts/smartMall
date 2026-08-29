#!/usr/bin/env python3
"""本地起 Java 服务。**不用 Docker，只要 JDK 21 和一个本机 MySQL。**

    python deploy/scripts/run-java.py build            构建全部 jar
    python deploy/scripts/run-java.py up               后台起全部服务并等就绪
    python deploy/scripts/run-java.py up mall-product  只起指定的
    python deploy/scripts/run-java.py down             停掉
    python deploy/scripts/run-java.py status           看状态
    python deploy/scripts/run-java.py run mall-product 前台起一个（Ctrl-C 停）
    python deploy/scripts/run-java.py logs mall-product 看日志尾部

**为什么是 java -jar，不是 mvn spring-boot:run。**

`mvnw -pl mall-product spring-boot:run` 起不来：-pl 只把 mall-product 放进
reactor，它依赖的 mall-common 既不在 reactor 里、本地仓库里也没有，报
"Could not find artifact com.smartmall:mall-common"。加 -am 换个错：parent 也
进了 reactor，而 spring-boot:run 对每个模块都跑一遍，轮到 parent 就报
"Unable to find a suitable main class"。绕过去要 install 再 run，两步。

而五个服务全用这个方式，就是五次 Maven 启动 + 五次依赖解析，每次几十秒；
起完一轮的时间够 java -jar 起十轮。fat jar 构建一次，之后启动只剩 JVM 本身。

**就绪判据是 /health，不是 /actuator/health。**

/actuator/health 会探 MySQL，库没起来时它报 DOWN —— 拿它当判据，服务明明
起来了却会一直等到超时，错误信息还指不到真正的原因。所以：/health 有应答
就算「进程起来了」，再单独查一次 /actuator/health 把 db 的状态原样报出来。
两件事分开说，比一个超时清楚得多。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smartmall_env as env  # noqa: E402

ROOT = env.ROOT
JAVA_DIR = ROOT / "apps" / "java"
LOG_DIR = ROOT / "logs"
IS_WINDOWS = os.name == "nt"

SERVICES = env.SERVICES
ESSENTIAL = env.ESSENTIAL

OK, BAD, WARN = "✓", "✗", "⚠"


# ---------------------------------------------------------------- JDK

def find_java() -> str:
    """定位 java 可执行文件，并确认版本够。

    JDK 版本不够时 java -jar 的报错是 UnsupportedClassVersionError 加一长串
    栈，讲的是 class file version 65.0 —— 得知道 65 对应 21 才看得懂。
    提前查一次，把话说人话。
    """
    exe = None
    if os.environ.get("JAVA_HOME"):
        cand = Path(os.environ["JAVA_HOME"]) / "bin" / ("java.exe" if IS_WINDOWS else "java")
        if cand.is_file():
            exe = str(cand)
    exe = exe or shutil.which("java")
    if not exe:
        die("找不到 java。装一个 JDK 21：https://adoptium.net/temurin/releases/?version=21")

    out = subprocess.run([exe, "-version"], capture_output=True, text=True).stderr
    m = re.search(r'version "?(\d+)', out)
    if m and int(m.group(1)) < 21:
        die(f"JDK 版本是 {m.group(1)}，本项目要 21+（当前 {exe}）。\n"
            "  装 21 之后把 JAVA_HOME 指过去，或让新的 java 排在 PATH 前面。")
    return exe


# ---------------------------------------------------------------- 工具

def die(msg: str) -> None:
    print(f"{BAD} {msg}")
    sys.exit(1)


def jar_of(svc: str) -> Path | None:
    """服务的可执行 jar。

    spring-boot repackage 会把原始 jar 改名成 *.jar.original 留在旁边，
    glob("*.jar") 正好只命中重新打包后的那个。
    """
    target = JAVA_DIR / svc / "target"
    jars = [p for p in target.glob("*.jar") if not p.name.endswith("-sources.jar")]
    return jars[0] if jars else None


def pid_file(svc: str) -> Path:
    return LOG_DIR / f"{svc}.pid"


def log_file(svc: str) -> Path:
    return LOG_DIR / f"{svc}.log"


def _alive(pid: int) -> bool:
    """这个 pid 现在还存在吗。**与「它是不是我们那个服务」分开问。**

    分开是因为踩过一次：``_cmdline`` 对一个**已经死掉**的 pid 返回空字符串，
    而 :func:`running_pid` 里那条「Windows 上取不到命令行就姑且认了」的兜底
    把空字符串当成了「取不到」，于是死进程被判成还在跑——``up`` 打一句
    「已在跑」就跳过启动，接着等 90 秒超时。**症状离病因隔得很远**：
    日志里最后一次启动是十几个小时前而且是成功的，端口上什么都没有，
    而脚本每次都说它在跑。
    """
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10).stdout
            # 查不到时 tasklist 往 stdout 打一句「没有运行的任务…」，
            # 而不是空输出——所以判据是「输出里有没有这个 pid」，
            # 不能是「输出空不空」。这句提示还随系统语言变，更不能拿来匹配
            return f'"{pid}"' in out
        os.kill(pid, 0)                       # 不发信号，只探存活
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _cmdline(pid: int) -> str:
    """尽力取到进程的命令行，用于确认「这个 pid 还是当初那个服务」。

    取不到返回空字符串。**调用方必须先用 :func:`_alive` 确认进程存在**——
    「取不到命令行」和「进程根本不在」在这里是同一个返回值。
    """
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                capture_output=True, text=True, timeout=10).stdout
            if out.strip():
                return out
            # Win11 起 wmic 可能不在了，退到 tasklist（只拿得到进程名）
            return subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10).stdout
        proc = Path("/proc") / str(pid) / "cmdline"
        if proc.is_file():
            return proc.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        return subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                              capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def running_pid(svc: str) -> int | None:
    """读 pid 文件，并确认那个进程真的还是这个服务。

    两步，顺序不能反：

    1. **进程还在吗**（:func:`_alive`）——不在就直接清掉 pid 文件。
    2. **是不是这个服务**——比对命令行里有没有 jar 名。进程退出后 pid 会被
       系统回收给别的程序，照着旧 pid 去 kill 会杀掉无关进程。

    第 2 步在 Windows 上可能取不到命令行（没有 wmic 时 tasklist 只给进程名），
    那时姑且认了——但**前提是第 1 步已经确认进程存在**。少了第 1 步，
    这条兜底会把所有死掉的服务都报成「已在跑」。
    """
    f = pid_file(svc)
    if not f.is_file():
        return None
    try:
        pid = int(f.read_text().strip())
    except ValueError:
        f.unlink(missing_ok=True)
        return None
    if not _alive(pid):
        f.unlink(missing_ok=True)
        return None
    cmd = _cmdline(pid)
    if svc in cmd or (IS_WINDOWS and not cmd.strip()):
        return pid
    f.unlink(missing_ok=True)
    return None


def port_busy(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    busy = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    return busy


#: 查 /actuator/health 用的超时。**必须大于 Hikari 的 connection-timeout（5s）。**
#:
#: db 探针连不上库时会一直等到连接超时才返回，也就是说：**库出问题的时候，
#: 这个接口最慢**。用 2 秒去问，恰恰在最需要答案的场合拿到的是 TimeoutError，
#: 于是「连不上 MySQL」被显示成「端口不应答（还在启动？）」—— 指错了方向。
#: 实测过：curl 5.017s 返回 503+DOWN，而 2 秒的 urllib 什么都没拿到。
HEALTH_TIMEOUT = 12.0


def http_json(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:          # 503 也有 body，照样解析
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return None
    except Exception:
        return None


# ---------------------------------------------------------------- build

def task_build(services: list[str]) -> int:
    mvnw = JAVA_DIR / ("mvnw.cmd" if IS_WINDOWS else "mvnw")
    if not mvnw.is_file():
        die(f"缺 {mvnw}")
    if not IS_WINDOWS:
        mvnw.chmod(mvnw.stat().st_mode | 0o111)
    print("==> 构建（第一次要下依赖，几分钟；之后是增量）")
    # 整个 reactor 一起打，模块间依赖由 Maven 自己解决，不用先 install 再 run
    p = subprocess.run([str(mvnw), "-B", "-DskipTests", "package"], cwd=JAVA_DIR)
    if p.returncode != 0:
        print(f"\n{BAD} 构建失败，上面有具体错误")
        return p.returncode
    for svc in services:
        jar = jar_of(svc)
        print(f"  {OK if jar else BAD} {svc}" + (f"  {jar.name}" if jar else "  没产出 jar"))
    return 0


# ---------------------------------------------------------------- up

def task_up(services: list[str], wait: int) -> int:
    java = find_java()
    LOG_DIR.mkdir(exist_ok=True)

    warning = env.db_env_warning()
    if warning:
        print(f"{WARN} {warning}\n")

    missing = [s for s in services if jar_of(s) is None]
    if missing:
        die(f"这些服务还没构建：{', '.join(missing)}\n"
            "  先跑：python deploy/scripts/run-java.py build")

    failed = []
    for svc in services:
        port = SERVICES[svc]
        pid = running_pid(svc)
        if pid:
            print(f"  ⤼ {svc} 已在跑（pid {pid}，:{port}）")
            continue
        if port_busy(port):
            print(f"  {BAD} :{port} 被别的进程占着，{svc} 没起")
            failed.append(svc)
            continue
        _spawn(java, svc)

    for svc in services:
        if svc in failed:
            continue
        if not _await_ready(svc, wait):
            failed.append(svc)

    print()
    _report_health([s for s in services if s not in failed])
    if failed:
        print(f"\n{BAD} 没起来：{', '.join(failed)}")
        for svc in failed:
            print(f"    日志：{log_file(svc)}")
        return 1
    return 0


def _spawn(java: str, svc: str) -> None:
    jar = jar_of(svc)
    kwargs: dict = {"cwd": ROOT, "stderr": subprocess.STDOUT,
                    "stdin": subprocess.DEVNULL}
    if IS_WINDOWS:
        # 独立进程组：关掉这个 PowerShell 窗口不会顺带带走服务
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    # 追加而不是覆盖：上一次为什么挂的，日志得留着。
    # 子进程继承这个句柄，父进程用完立刻关掉自己那份。
    with log_file(svc).open("a", encoding="utf-8") as fh:
        fh.write(f"\n{'=' * 70}\n"
                 f"启动 {svc} @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                 f"{'=' * 70}\n")
        fh.flush()
        proc = subprocess.Popen([java, "-jar", str(jar)], stdout=fh, **kwargs)
    pid_file(svc).write_text(str(proc.pid), encoding="utf-8")
    print(f"  … {svc} 启动中（pid {proc.pid}，:{SERVICES[svc]}）")


def _await_ready(svc: str, wait: int) -> bool:
    port = SERVICES[svc]
    deadline = time.time() + wait
    while time.time() < deadline:
        if http_json(f"http://127.0.0.1:{port}/health", timeout=1.5):
            print(f"  {OK} {svc} 就绪  http://127.0.0.1:{port}")
            return True
        if running_pid(svc) is None:                     # 进程已经退了，别再等
            print(f"  {BAD} {svc} 启动后就退出了，日志末尾：")
            _tail(svc, 15, indent="      ")
            return False
        time.sleep(1)
    print(f"  {BAD} {svc} 等了 {wait}s 还没应答")
    _tail(svc, 15, indent="      ")
    return False


def _report_health(services: list[str]) -> None:
    """把 /actuator/health 里 db 的状态原样报出来。

    服务起得来但连不上库是最常见的一种「半好」状态：页面能开、商品是空的。
    在这里点破，比等用户点了购买再去翻栈快。
    """
    for svc in services:
        body = http_json(f"http://127.0.0.1:{SERVICES[svc]}/actuator/health",
                         timeout=HEALTH_TIMEOUT)
        if not body:
            continue
        db = (body.get("components") or {}).get("db")
        if db is None:
            continue
        if db.get("status") == "UP":
            print(f"  {OK} {svc} → MySQL 连通")
        else:
            reason = (db.get("details") or {}).get("error", "")
            print(f"  {BAD} {svc} 连不上 MySQL：{reason}")
            print("      跑一次自检看是哪一步：python deploy/scripts/doctor.py")


# ---------------------------------------------------------------- down

def task_down(services: list[str]) -> int:
    stopped = 0
    for svc in services:
        pid = running_pid(svc)
        if pid is None:
            continue
        _kill(pid)
        for _ in range(50):                     # 最多等 5 秒
            if running_pid(svc) is None:
                break
            time.sleep(0.1)
        left = running_pid(svc)
        if left:
            print(f"  {WARN} {svc}（pid {left}）没停下来，手动结束它")
        else:
            pid_file(svc).unlink(missing_ok=True)
            print(f"  {OK} {svc} 已停（pid {pid}）")
            stopped += 1
    if not stopped:
        print("  没有在跑的服务")
    return 0


def _kill(pid: int) -> None:
    if IS_WINDOWS:
        # /T 连子进程一起收；Windows 上没有进程组信号这一说
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
        return
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    time.sleep(2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass


# ---------------------------------------------------------------- status / run / logs

def task_status(services: list[str]) -> int:
    print(f"  {'服务':<16}{'端口':<8}{'进程':<12}状态")
    for svc in services:
        port = SERVICES[svc]
        pid = running_pid(svc)
        if pid is None:
            state = "未启动" if not port_busy(port) else f"{WARN} 端口被别的进程占用"
            print(f"  {svc:<16}{port:<8}{'-':<12}{state}")
            continue
        body = http_json(f"http://127.0.0.1:{port}/actuator/health",
                         timeout=HEALTH_TIMEOUT)
        if body is None:
            state = f"{WARN} 进程在，端口不应答（还在启动？）"
        else:
            overall = body.get("status", "?")
            db = (body.get("components") or {}).get("db", {}).get("status")
            mark = OK if overall == "UP" else BAD
            state = f"{mark} {overall}" + (f"   MySQL {db}" if db else "")
        print(f"  {svc:<16}{port:<8}{pid:<12}{state}")
    return 0


def task_run(svc: str) -> int:
    """前台起一个服务。日志直接打在终端上，Ctrl-C 停。"""
    java = find_java()
    warning = env.db_env_warning()
    if warning:
        print(f"{WARN} {warning}\n")
    jar = jar_of(svc)
    if jar is None:
        die(f"{svc} 还没构建：python deploy/scripts/run-java.py build")
    print(f"==> {svc}  :{SERVICES[svc]}   （Ctrl-C 停止）")
    try:
        return subprocess.run([java, "-jar", str(jar)], cwd=ROOT).returncode
    except KeyboardInterrupt:
        return 0


def _tail(svc: str, n: int, indent: str = "") -> None:
    f = log_file(svc)
    if not f.is_file():
        print(f"{indent}（还没有日志：{f}）")
        return
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    for ln in lines[-n:]:
        print(indent + ln)


def task_logs(svc: str, n: int) -> int:
    print(f"==> {log_file(svc)}")
    _tail(svc, n)
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    env.load_env()
    names = ", ".join(SERVICES)
    ap = argparse.ArgumentParser(
        description="本地起 Java 服务（只需 JDK 21 + 本机 MySQL）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"服务名：{names}\n默认作用于全部服务，也可以在命令后面点名。",
    )
    ap.add_argument("action",
                    choices=["build", "up", "down", "restart", "status", "run", "logs"])
    ap.add_argument("services", nargs="*", help="留空 = 全部")
    ap.add_argument("--wait", type=int, default=90, help="等就绪的秒数（默认 90）")
    ap.add_argument("-n", type=int, default=60, help="logs 显示的行数")
    ap.add_argument("--essential", action="store_true",
                    help=f"只作用于 {ESSENTIAL}（店铺页下单只要它）")
    args = ap.parse_args()

    if args.essential:
        services = [ESSENTIAL]
    elif args.services:
        unknown = [s for s in args.services if s not in SERVICES]
        if unknown:
            die(f"不认识的服务：{', '.join(unknown)}\n  可选：{names}")
        services = [s for s in SERVICES if s in args.services]   # 保持启动顺序
    else:
        services = list(SERVICES)

    if args.action in ("run", "logs") and len(services) != 1:
        die(f"{args.action} 一次只针对一个服务，例如："
            f"python deploy/scripts/run-java.py {args.action} {ESSENTIAL}")

    if args.action == "build":
        return task_build(services)
    if args.action == "up":
        return task_up(services, args.wait)
    if args.action == "down":
        return task_down(services)
    if args.action == "restart":
        task_down(services)
        return task_up(services, args.wait)
    if args.action == "status":
        return task_status(services)
    if args.action == "run":
        return task_run(services[0])
    return task_logs(services[0], args.n)


if __name__ == "__main__":
    sys.exit(main())
