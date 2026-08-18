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
    [ValidateSet('help', 'doctor', 'db-init', 'db-up', 'db-migrate', 'db-status',
                 'db-baseline', 'run-product', 'serve', 'test')]
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

<#
 .SYNOPSIS 拦住「只设了 MYSQL_PASSWORD、没设 MYSQL_USER」这个陷阱。

 应用侧 MYSQL_USER 的默认值是 smartmall。用户为了跑迁移设了 root 的密码，
 忘了同时设用户名，起应用时就变成 smartmall/<root密码> —— Access denied，
 而报错指向应用账号，看起来像"账号没建好"。这一轮真卡在这里过。
#>
function Assert-DbEnvSane {
    if ($env:MYSQL_PASSWORD -and -not $env:MYSQL_USER) {
        Write-Host ''
        Write-Host '⚠ 这个终端只设了 MYSQL_PASSWORD，没设 MYSQL_USER。' -ForegroundColor Yellow
        Write-Host '  应用默认用 smartmall 这个账号，于是会拿 smartmall + 你设的密码去连，' -ForegroundColor Yellow
        Write-Host '  多半会 Access denied。两种改法选一个：' -ForegroundColor Yellow
        Write-Host '    1) 清掉它，用默认账号：  $env:MYSQL_PASSWORD=$null' -ForegroundColor Yellow
        Write-Host '    2) 用户名也一起设：      $env:MYSQL_USER="root"' -ForegroundColor Yellow
        Write-Host '  （给 db-init 用的管理员密码请设 MYSQL_ADMIN_PASSWORD，不要用 MYSQL_PASSWORD）' -ForegroundColor Yellow
        Write-Host ''
    }
}

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') 失败（退出码 $LASTEXITCODE）" }
}

function Task-DbUp {
    # 只在用 Docker 跑 MySQL 时需要。本机装了 MySQL 服务的直接用 db-init。
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "没有 docker。本机已装 MySQL 的话跳过这步，直接跑：.\smartmall.ps1 db-init"
    }
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
    Assert-DbEnvSane
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
    Assert-DbEnvSane
    if (-not $env:MYSQL_HOST)     { $env:MYSQL_HOST = '127.0.0.1' }
    if (-not $env:MYSQL_USER)     { $env:MYSQL_USER = 'smartmall' }
    if (-not $env:MYSQL_PASSWORD) { $env:MYSQL_PASSWORD = 'smartmall' }
    if (-not $env:MYSQL_DATABASE) { $env:MYSQL_DATABASE = 'smartmall' }
    Push-Location (Join-Path $Root 'apps/python/ai-agent')
    try {
        # **三个本地包要按这个顺序装。**smartmall-pipeline 与 ai-common 都不在
        # PyPI 上，只能按路径装；ai-agent[server] 声明依赖它们，先装好才解析得了。
        # 少装 pipelines 的后果很隐蔽：页面照常打开，商品列表是空的，点购买没反应，
        # 只有 /api/products 的响应体里留一句 "error":"ModuleNotFoundError" ——
        # 那是被降级分支吞掉的，日志里连异常都看不到。
        Invoke-Checked (Get-Python) @('-m', 'pip', 'install', '-q',
            '-e', "$Root/pipelines",
            '-e', "$Root/apps/python/ai-common",
            '-e', "$Root/apps/python/ai-agent[server]")
        Write-Host '店铺页 → http://127.0.0.1:9002/'
        Invoke-Checked 'smartmall-agent' @('serve')
    } finally { Pop-Location }
}

function Task-Test {
    Push-Location (Join-Path $Root 'apps/java')
    try { Invoke-Checked '.\mvnw.cmd' @('-B', 'test') } finally { Pop-Location }
    Invoke-Checked (Get-Python) @('-m', 'pytest', '-q', 'apps/python/ai-agent/tests')
}

function Task-Doctor {
    Invoke-Checked (Get-Python) @("$Root/deploy/scripts/doctor.py")
}

switch ($Task) {
    'doctor'      { Task-Doctor }
    # db-init 是「建库 + 建表 + 迁移」一步到位，本机 MySQL 用这个。
    # 底层就是 migrate.py：它检测到库是空的会先跑基础建表脚本
    # （容器版由 MySQL 镜像的 initdb 自动完成，本机 MySQL 没有这机制）。
    'db-init'     { Task-Migrate @() }
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
        Write-Host '  .\smartmall.ps1 db-init       建库 + 建表 + 迁移，一步到位（本机 MySQL 用这个）'
        Write-Host '  .\smartmall.ps1 db-up         用 Docker 起 MySQL（没有 Docker 就跳过）'
        Write-Host '  .\smartmall.ps1 db-migrate    只跑迁移（可反复执行）'
        Write-Host '  .\smartmall.ps1 db-status     看哪些迁移还没应用'
        Write-Host '  .\smartmall.ps1 db-baseline   已手工迁移过的库：登记现状，不执行'
        Write-Host '  .\smartmall.ps1 run-product   起订单服务 :8081（前台）'
        Write-Host '  .\smartmall.ps1 serve         起店铺页 :9002（前台）'
        Write-Host '  .\smartmall.ps1 test          跑 Java 与 ai-agent 测试'
        Write-Host '  .\smartmall.ps1 doctor        自检：数据库、账号、迁移、端口、依赖'
        Write-Host ''
        Write-Host '本机 MySQL：  db-init → run-product（新终端）→ serve（新终端）'
        Write-Host '用 Docker：   db-up → db-migrate → run-product → serve'
        Write-Host ''
        Write-Host '给 db-init 的管理员密码：  $env:MYSQL_ADMIN_PASSWORD="你的root密码"'
        Write-Host '（起服务的终端里不要设 MYSQL_PASSWORD —— 应用用的是 smartmall 账号）'
        Write-Host '排查环境问题：            .\smartmall.ps1 doctor'
        Write-Host '然后打开 http://127.0.0.1:9002/'
    }
}
