<#
.SYNOPSIS
    smartMall 的 Windows 任务入口。**只用本机 MySQL，全部服务本地起，不涉及 Docker。**

.DESCRIPTION
    Windows 上没有 make。与其让人对着 Makefile 手工翻译命令（那正是踩坑的来源），
    不如把同样的几步固化一份。

    每个动作都只是转发到 deploy/scripts/ 下的 Python 实现，不重复业务逻辑 ——
    逻辑只写一次，Windows 与 Linux 不会漂。

.EXAMPLE
    第一次：
        $env:MYSQL_ADMIN_PASSWORD="你的 root 密码"
        .\smartmall.ps1 db-init
        .\smartmall.ps1 build
        .\smartmall.ps1 up
        .\smartmall.ps1 serve       # 另开一个终端

    之后每天：
        .\smartmall.ps1 up
        .\smartmall.ps1 serve
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'doctor', 'db-init', 'db-status',
                 'build', 'up', 'down', 'restart', 'status', 'run', 'logs',
                 'serve', 'test', 'verify')]
    [string]$Task = 'help',

    # 形参名不能叫 $Args —— 那是 PowerShell 的自动变量，形参会被它盖掉，
    # 传进来的东西静默丢失。这类冲突不报错，只是行为不对，所以特别标一笔。
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# 控制台输出编码设成 UTF-8。中文机器上 PowerShell 5.1 的控制台默认是 GBK(936)，
# 脚本里的中文提示会打成乱码。这一句只影响本进程的输出，不改系统设置。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Get-Python {
    foreach ($c in @('python', 'python3', 'py')) {
        if (Get-Command $c -ErrorAction SilentlyContinue) { return $c }
    }
    throw "找不到 Python。装一个 3.11+：https://www.python.org/downloads/"
}

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') 失败（退出码 $LASTEXITCODE）" }
}

# 所有 Java 相关动作都转给同一个编排器，PowerShell 这边不碰 jar、不碰进程管理
function Invoke-RunJava {
    param([string[]]$JavaArgs)
    Invoke-Checked (Get-Python) (@("$Root/deploy/scripts/run-java.py") + $JavaArgs)
}

function Task-Serve {
    # 店铺页连库用的是应用账号。这几个默认值必须和 application.yml、
    # repository.py 里写死的保持一致，否则「自检说能连、页面说连不上」。
    if (-not $env:MYSQL_HOST)     { $env:MYSQL_HOST = '127.0.0.1' }
    if (-not $env:MYSQL_USER)     { $env:MYSQL_USER = 'smartmall' }
    if (-not $env:MYSQL_PASSWORD) { $env:MYSQL_PASSWORD = 'smartmall' }
    if (-not $env:MYSQL_DATABASE) { $env:MYSQL_DATABASE = 'smartmall' }

    # **三个本地包要按这个顺序装。**smartmall-pipeline 与 ai-common 都不在 PyPI 上，
    # 只能按路径装；ai-agent[server] 声明依赖它们，先装好才解析得了。
    # 少装 pipelines 的后果很隐蔽：页面照常打开，商品列表是空的，点购买没反应，
    # 只有 /api/products 的响应体里留一句 ModuleNotFoundError。
    Invoke-Checked (Get-Python) @('-m', 'pip', 'install', '-q',
        '-e', "$Root/pipelines",
        '-e', "$Root/apps/python/ai-common",
        '-e', "$Root/apps/python/ai-agent[server]")
    Write-Host '店铺页 → http://127.0.0.1:9002/'
    Invoke-Checked 'smartmall-agent' @('serve')
}

function Task-Test {
    Push-Location (Join-Path $Root 'apps/java')
    try { Invoke-Checked '.\mvnw.cmd' @('-B', 'test') } finally { Pop-Location }
    Invoke-Checked (Get-Python) @('-m', 'pytest', '-q', 'apps/python/ai-agent/tests')
}

switch ($Task) {
    'doctor'    { Invoke-Checked (Get-Python) @("$Root/deploy/scripts/doctor.py") }
    # db-init = 建库 + 建应用账号 + 建表 + 跑迁移，一步到位，可反复执行
    'db-init'   { Invoke-Checked (Get-Python) @("$Root/deploy/scripts/migrate.py") }
    'db-status' { Invoke-Checked (Get-Python) @("$Root/deploy/scripts/migrate.py", '--status') }
    'build'     { Invoke-RunJava (@('build') + $Rest) }
    'up'        { Invoke-RunJava (@('up') + $Rest) }
    'down'      { Invoke-RunJava (@('down') + $Rest) }
    'restart'   { Invoke-RunJava (@('restart') + $Rest) }
    'status'    { Invoke-RunJava (@('status') + $Rest) }
    'run'       { Invoke-RunJava (@('run') + $Rest) }
    'logs'      { Invoke-RunJava (@('logs') + $Rest) }
    'serve'     { Task-Serve }
    'verify'    { Invoke-Checked (Get-Python) @("$Root/deploy/scripts/verify-orders.py") }
    'test'      { Task-Test }
    default {
        Write-Host 'smartMall —— Windows 任务入口（只用本机 MySQL，不用 Docker）'
        Write-Host ''
        Write-Host '  数据库'
        Write-Host '    db-init            建库 + 建应用账号 + 建表 + 跑迁移（可反复执行）'
        Write-Host '    db-status          看哪些迁移还没应用'
        Write-Host ''
        Write-Host '  Java 服务（5 个，:8080-:8084）'
        Write-Host '    build              构建全部 jar'
        Write-Host '    up   [服务名...]   后台起并等就绪，留空 = 全部'
        Write-Host '    down [服务名...]   停掉'
        Write-Host '    restart [服务名...] 重起'
        Write-Host '    status             看状态（含各服务与 MySQL 的连通性）'
        Write-Host '    run  <服务名>      前台起一个，日志直接打在终端上'
        Write-Host '    logs <服务名>      看日志尾部'
        Write-Host ''
        Write-Host '  其它'
        Write-Host '    serve              店铺页 :9002（前台）'
        Write-Host '    test               跑 Java 与 ai-agent 测试'
        Write-Host '    verify             对真库复核订单链路（状态机 + 防超卖）'
        Write-Host '    doctor             自检：JDK、数据库、账号、迁移、端口'
        Write-Host ''
        Write-Host '第一次：'
        Write-Host '    $env:MYSQL_ADMIN_PASSWORD="你的 root 密码"   # 只给 db-init 用'
        Write-Host '    .\smartmall.ps1 db-init'
        Write-Host '    .\smartmall.ps1 build'
        Write-Host '    .\smartmall.ps1 up'
        Write-Host '    .\smartmall.ps1 serve                        # 另开一个终端'
        Write-Host ''
        Write-Host '之后每天： up  →  serve  →  http://127.0.0.1:9002/'
        Write-Host ''
        Write-Host '注意：起服务的终端里不要设 MYSQL_PASSWORD —— 应用用的是 smartmall 账号，'
        Write-Host '      把 root 的密码设进去会变成 smartmall/<root密码>，必然 Access denied。'
    }
}
