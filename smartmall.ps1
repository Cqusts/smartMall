<#
.SYNOPSIS
    smartMall 的 Windows 任务入口，等价于 Makefile 里那几个目标。

.DESCRIPTION
    Windows 上没有 make。与其让人对着 Makefile 手工翻译命令（那正是踩坑的
    来源：mvn 少了 install 前置、mysql 少了 utf8mb4 参数），不如把同样的
    几步在这里固化一份。

    每个动作都只是转发到跨平台的实现（docker compose / migrate.py /
    mvnw.cmd），不重复业务逻辑 —— 逻辑只写一次，Windows 与 Linux 不会漂。

.EXAMPLE
    .\smartmall.ps1 db-up
    .\smartmall.ps1 db-migrate
    .\smartmall.ps1 run-product
    .\smartmall.ps1 serve
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'db-up', 'db-migrate', 'db-status', 'db-baseline',
                 'run-product', 'serve', 'test')]
    [string]$Task = 'help',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# 控制台输出编码设成 UTF-8。中文机器上 PowerShell 5.1 的控制台默认是 GBK(936)，
# 脚本里的中文提示会打成乱码。这一句只影响本进程的输出，不改系统设置。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

# python / python3 在 Windows 上通常是前者，Linux 上是后者
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

function Task-DbUp {
    Invoke-Checked 'docker' @('compose', '-f', 'deploy/docker-compose.dev.yml', 'up', '-d', 'mysql')
    Write-Host '等待 MySQL 就绪…'
    # 判据是「能查到业务库」，不是 mysqladmin ping —— 首次启动时 MySQL 会先跑
    # 一个临时服务器执行建表脚本，那个临时服务器对 ping 是有应答的，于是
    # ping 通了、库却还不存在，紧接着的迁移就会「连不上数据库」。
    for ($i = 0; $i -lt 60; $i++) {
        docker exec smdev-mysql mysql -uroot -proot -N -e 'SELECT 1' smartmall *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host '  ✓ MySQL 就绪（建表已完成）'; return }
        Start-Sleep -Seconds 3
    }
    throw 'MySQL 超时未就绪，看日志：docker logs smdev-mysql'
}

# 参数名不能叫 $Args —— 那是 PowerShell 的自动变量，形参会被它盖掉，
# 传进来的 --status / --baseline 会静默丢失（实测：db-status 跑出的是
# 普通模式的结果）。这类冲突不报错，只是行为不对，所以特别标一笔。
function Task-Migrate { param([string[]]$MigrateArgs)
    Invoke-Checked (Get-Python) (@('deploy/scripts/migrate.py') + $MigrateArgs)
}

function Task-RunProduct {
    # **必须分两步。**`mvnw -pl mall-product spring-boot:run` 会失败：-pl 只把
    # mall-product 放进 reactor，它依赖的 mall-common 既不在 reactor 里、
    # 本地仓库也没有。加 -am 也不行 —— 那会把 parent 拉进 reactor，而
    # spring-boot:run 对每个模块都跑一遍，轮到 parent 就没有 main class。
    Push-Location (Join-Path $Root 'apps/java')
    try {
        Invoke-Checked '.\mvnw.cmd' @('-B', '-q', '-DskipTests', '-pl', 'mall-product', '-am', 'install')
        Invoke-Checked '.\mvnw.cmd' @('-B', '-pl', 'mall-product', 'spring-boot:run')
    } finally { Pop-Location }
}

function Task-Serve {
    if (-not $env:MYSQL_HOST)     { $env:MYSQL_HOST = '127.0.0.1' }
    if (-not $env:MYSQL_USER)     { $env:MYSQL_USER = 'smartmall' }
    if (-not $env:MYSQL_PASSWORD) { $env:MYSQL_PASSWORD = 'smartmall' }
    if (-not $env:MYSQL_DATABASE) { $env:MYSQL_DATABASE = 'smartmall' }
    Push-Location (Join-Path $Root 'apps/python/ai-agent')
    try {
        Invoke-Checked (Get-Python) @('-m', 'pip', 'install', '-q', '-e', '.[server]')
        Write-Host '店铺页 → http://127.0.0.1:9002/'
        Invoke-Checked 'smartmall-agent' @('serve')
    } finally { Pop-Location }
}

function Task-Test {
    Push-Location (Join-Path $Root 'apps/java')
    try { Invoke-Checked '.\mvnw.cmd' @('-B', 'test') } finally { Pop-Location }
    Invoke-Checked (Get-Python) @('-m', 'pytest', '-q', 'apps/python/ai-agent/tests')
}

switch ($Task) {
    'db-up'       { Task-DbUp }
    'db-migrate'  { Task-Migrate @() }
    'db-status'   { Task-Migrate @('--status') }
    'db-baseline' { Task-Migrate @('--baseline') }
    'run-product' { Task-RunProduct }
    'serve'       { Task-Serve }
    'test'        { Task-Test }
    default {
        Write-Host 'smartMall —— Windows 任务入口（Makefile 的等价物）'
        Write-Host ''
        Write-Host '  .\smartmall.ps1 db-up         起 MySQL，等到建表完成'
        Write-Host '  .\smartmall.ps1 db-migrate    应用数据库迁移（可反复执行）'
        Write-Host '  .\smartmall.ps1 db-status     看哪些迁移还没应用'
        Write-Host '  .\smartmall.ps1 db-baseline   已手工迁移过的库：登记现状，不执行'
        Write-Host '  .\smartmall.ps1 run-product   起订单服务 :8081（前台）'
        Write-Host '  .\smartmall.ps1 serve         起店铺页 :9002（前台）'
        Write-Host '  .\smartmall.ps1 test          跑 Java 与 ai-agent 测试'
        Write-Host ''
        Write-Host '完整启动：db-up → db-migrate → run-product（新终端）→ serve（新终端）'
        Write-Host '然后打开 http://127.0.0.1:9002/'
    }
}
