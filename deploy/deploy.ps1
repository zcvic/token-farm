# ============================================================
# token-farm 一键部署（Windows / Docker Desktop）
#
# 一条命令起全套：管理端容器 + 兄弟目录里的上游 workbuddy2api。
# 重复执行是安全的：已有 .env / config.json / 账号与数据一律保留。
#
# 用法（在仓库根目录，或任意位置指定 -Root）：
#   powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Action build
#   powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Action logs
#   powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Action password
#
# 可覆盖的环境变量：
#   TF_UPSTREAM_DIR   上游目录，默认 <仓库>\..\workbuddy2api
#   TF_UPSTREAM_SRC   上游源码来源（目录 / .tar.gz）；留空则用已有目录或仓库内 upstream\
#   TF_SKIP_UPSTREAM  1 = 只起管理端
#   TF_UPSTREAM_BIND  上游端口绑哪个宿主地址，默认 127.0.0.1（Docker Desktop
#                     会把 host.docker.internal 转发到宿主回环）
#   TF_NO_BUILD       1 = 跳过 build，直接用已有镜像
#   TF_HEALTH_TIMEOUT 等待就绪的秒数，默认 180
#
# 没有 Docker？本仓库还有原生 Windows 路径（windows\start.ps1），不需要容器。
# ============================================================
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'build', 'down', 'restart', 'ps', 'logs', 'password', 'help')]
    [string]$Action = 'up',

    [string]$UpstreamDir = $env:TF_UPSTREAM_DIR,
    [string]$UpstreamSrc = $env:TF_UPSTREAM_SRC,
    [string]$Python = $env:TF_PYTHON,

    [switch]$SkipUpstream,
    [switch]$NoBuild,
    [int]$HealthTimeout = $(if ($env:TF_HEALTH_TIMEOUT) { [int]$env:TF_HEALTH_TIMEOUT } else { 180 })
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ── 输出 helpers ─────────────────────────────────────────
function Write-Info { param([string]$Message) Write-Host "[·] $Message" -ForegroundColor Blue }
function Write-Ok { param([string]$Message) Write-Host "[√] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[!] $Message" -ForegroundColor Yellow }
function Write-Step { param([string]$Message) Write-Host "`n══ $Message" -ForegroundColor Cyan }
function Stop-WithError { param([string]$Message) Write-Host "[×] $Message" -ForegroundColor Red; exit 1 }

# 脚本住在 deploy\ 里：仓库根是它的上一级
$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root 'docker-compose.yml'
$upstreamPort = if ($env:UPSTREAM_PORT) { $env:UPSTREAM_PORT } else { '7863' }

# 管理端跑在容器里，容器内的 127.0.0.1 是它自己。Docker Desktop 会把
# host.docker.internal 转发到宿主回环，所以 Windows 上绑 127.0.0.1 就够；
# 换成别的地址（很少见）时面板得直接连那个 IP。Linux 上没有这个转发，
# 由 deploy.sh 负责改成 docker0 网关地址。
$upstreamBind = if ($env:TF_UPSTREAM_BIND) { $env:TF_UPSTREAM_BIND } else { '127.0.0.1' }
$wb2apiTarget = if ($env:TF_UPSTREAM_BIND) { "http://${upstreamBind}:${upstreamPort}" } else { "http://host.docker.internal:${upstreamPort}" }

# .env 里若把上游写成 127.0.0.1 / localhost，容器里那份配置连的是自己，必然失败。
# 只改这种明确的回环值；服务名、host.docker.internal、自定义 IP 都是有意为之。
function Update-Wb2ApiBase {
    param([string]$Path, [string]$Target)
    if (-not (Test-Path $Path)) { return }
    $text = [System.IO.File]::ReadAllText($Path)
    $pattern = '(?m)^([ \t]*WB2API_BASE[ \t]*=[ \t]*)(.+?)[ \t]*$'
    $m = [regex]::Match($text, $pattern)
    if (-not $m.Success) { return }
    $cur = $m.Groups[2].Value.Trim('"')
    if ($cur -notmatch '127\.0\.0\.1|localhost|\[::1\]|//0\.0\.0\.0') { return }
    $text = $text.Substring(0, $m.Index) + $m.Groups[1].Value + $Target +
    $text.Substring($m.Index + $m.Length)
    [System.IO.File]::WriteAllText($Path, $text, (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok "WB2API_BASE 由 $cur 改为 $Target（容器里的回环地址连不到上游）"
}

if (-not (Test-Path $composeFile)) { Stop-WithError "找不到 $composeFile" }
if (-not $UpstreamDir) { $UpstreamDir = [System.IO.Path]::GetFullPath((Join-Path $root '..\workbuddy2api')) }
if (-not $Python) { $Python = 'python' }

if ($Action -eq 'help') {
    Get-Content $PSCommandPath | Select-String -Pattern '^# 用法：' -Context 0, 20 | ForEach-Object { $_.Line; $_.Context.PostContext }
    exit 0
}

# ── 0. 预检 ──────────────────────────────────────────────
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Stop-WithError "未找到 docker。装 Docker Desktop 再来：https://www.docker.com/products/docker-desktop/"
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Stop-WithError 'docker 守护进程没在跑（Docker Desktop 启动后再试）' }

$composeV1 = $false
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    if (Get-Command docker-compose -ErrorAction SilentlyContinue) {
        $composeV1 = $true
        Write-Warn '在用 Compose v1（docker-compose），建议升级到 v2'
    }
    else {
        Stop-WithError '未找到 docker compose。Docker Desktop 自带它；Linux 上装 compose 插件。'
    }
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ComposeArgs)
    if ($composeV1) { & docker-compose -f $composeFile @ComposeArgs }
    else { & docker compose -f $composeFile @ComposeArgs }
    if ($LASTEXITCODE -ne 0) { throw "docker compose $($ComposeArgs -join ' ') 失败（退出码 $LASTEXITCODE）" }
}

# ── 端口：从 compose 里读，不写死 ─────────────────────────
# 用户可能自己改过映射；照 compose 说的做，才不会「起在 7864 却去探 7865」。
$hostBind = '127.0.0.1'
$hostPort = '7864'
$portLine = Select-String -Path $composeFile -Pattern '^\s*-\s*"?([0-9a-zA-Z\.\-]+:)?(\d+):7864"?\s*$' | Select-Object -First 1
if ($portLine) {
    $spec = ($portLine.Line.Trim() -replace '^-\s*', '') -replace '"', ''
    $parts = $spec.Split(':')
    if ($parts.Count -eq 3) { $hostBind = $parts[0]; $hostPort = $parts[1] }
    elseif ($parts.Count -eq 2) { $hostBind = '0.0.0.0'; $hostPort = $parts[0] }
}

# 这些子命令只转发，不该顺手重建镜像
if ($Action -in @('down', 'ps', 'logs', 'build', 'restart')) {
    Set-Location $root
    if ($Action -eq 'restart') { Invoke-Compose restart; Write-Info "已重启；面板：http://${hostBind}:${hostPort}" }
    else { Invoke-Compose $Action }
    exit 0
}

if ($Action -eq 'password') {
    # 首启密码只在第一次生成时打印一行，之后就没有了
    Set-Location $root
    $lines = & { if ($composeV1) { docker-compose -f $composeFile logs --tail 3000 } else { docker compose -f $composeFile logs --tail 3000 } } |
    Select-String -Pattern '密码|password' -CaseSensitive:$false
    if ($lines) { $lines | ForEach-Object { $_.Line } }
    else {
        Write-Warn '日志里没找到密码行（可能已经初始化过：密码只在首启打印一次）'
        Write-Info '忘了密码：进「设置 → 用户」重置，或直接改 data/users.json 后重启'
    }
    exit 0
}

# ── 1. .env ─────────────────────────────────────────────
Write-Step '1/5 环境配置（.env）'
Set-Location $root
$envPath = Join-Path $root '.env'
$envExample = Join-Path $root '.env.example'
if (Test-Path $envPath) { Write-Ok '.env 已存在，保留不动' }
elseif (Test-Path $envExample) {
    Copy-Item $envExample $envPath
    Write-Ok '已从 .env.example 生成 .env（出口代理 / 镜像加速等都在里面改）'
}
else { Write-Warn '既没有 .env 也没有 .env.example，按 compose 默认值继续' }

# `.env.example` 写的是 WB2API_BASE=http://127.0.0.1:7863 —— 那是「管理端跑在
# 宿主上」的老形态；compose 部署下它在容器里等于连自己。
Update-Wb2ApiBase -Path $envPath -Target $wb2apiTarget

New-Item -ItemType Directory -Force -Path (Join-Path $root 'data') | Out-Null

# ── 2. 上游 workbuddy2api ───────────────────────────────
Write-Step '2/5 上游 workbuddy2api'

$expectedUpstream = [System.IO.Path]::GetFullPath((Join-Path $root '..\workbuddy2api'))
if ($UpstreamDir -ne $expectedUpstream) {
    Write-Warn "TF_UPSTREAM_DIR=$UpstreamDir 与 compose 的挂载（..\workbuddy2api）不一致"
    Write-Warn '管理端会读不到上游 config.json / auths。请把 compose 的挂载改成同一路径，或用目录联接：'
    Write-Warn "  cmd /c mklink /J `"$expectedUpstream`" `"$UpstreamDir`""
}

function Start-Upstream {
    param([string]$Dir)

    $composeInUpstream = @('docker-compose.yml', 'compose.yml', 'compose.yaml') |
    ForEach-Object { Join-Path $Dir $_ } |
    Where-Object { Test-Path $_ } |
    Select-Object -First 1
    if (-not $composeInUpstream) { Stop-WithError "$Dir 里没有 docker-compose.yml —— 确认这是上游仓库根目录" }

    # 端口收敛：上游默认对所有网卡暴露 7863，管理端只需要宿主这一跳。
    # 也兼容已经收敛成 127.0.0.1 的旧部署（那种在 Linux 上正是连不上的原因）。
    $want = '"{0}:{1}:{1}"' -f $upstreamBind, $upstreamPort
    $text = [System.IO.File]::ReadAllText($composeInUpstream)
    if ($text.Contains($want)) {
        Write-Ok "上游端口已绑定 ${upstreamBind}:$upstreamPort（安全基线）"
    }
    else {
        $pattern = '(?m)^(\s*-\s*)"?(\d{1,3}(?:\.\d{1,3}){3}:)?' +
        [regex]::Escape($upstreamPort) + ':' + [regex]::Escape($upstreamPort) + '"?\s*$'
        $text = [regex]::Replace($text, $pattern, ('$1' + $want))
        [System.IO.File]::WriteAllText($composeInUpstream, $text, (New-Object System.Text.UTF8Encoding($false)))
        Write-Ok "上游端口已收敛为 ${upstreamBind}:$upstreamPort"
    }

    # config.json 含 api_key 与账号状态：只生成，不覆盖
    $cfg = Join-Path $Dir 'config.json'
    $cfgExample = Join-Path $Dir 'config.example.json'
    if (-not (Test-Path $cfg)) {
        if (Test-Path $cfgExample) {
            Copy-Item $cfgExample $cfg
            $py = Get-Command $Python -ErrorAction SilentlyContinue
            if ($py) {
                $script = 'import json,secrets,sys;p=sys.argv[1];c=json.load(open(p,encoding="utf-8"));c["api_key"]=secrets.token_hex(16);json.dump(c,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)'
                & $Python -c $script $cfg
                if ($LASTEXITCODE -eq 0) { Write-Ok '已生成上游 config.json（api_key 随机）' }
                else { Write-Warn '生成 api_key 失败，config.json 里仍是模板值，请手工改' }
            }
            else { Write-Warn '没有 python，config.json 已从模板复制但 api_key 仍是模板值 —— 请手工改掉' }
        }
        else { Write-Warn '上游没有 config.example.json，跳过配置生成' }
    }
    else { Write-Ok '上游 config.json 已存在，保留不动（含 api_key 与账号）' }

    New-Item -ItemType Directory -Force -Path (Join-Path $Dir 'auths'), (Join-Path $Dir 'data') | Out-Null
    # Windows 下的 bind mount 不需要 chown（Docker Desktop 的共享卷语义不同），这里不做处理

    Write-Info '启动上游容器（首次会构建镜像，需要几分钟）'
    if ($composeV1) { & docker-compose -f $composeInUpstream up -d } else { & docker compose -f $composeInUpstream up -d }
    if ($LASTEXITCODE -ne 0) { Stop-WithError "上游启动失败：cd $Dir; docker compose logs" }

    for ($i = 1; $i -le 90; $i++) {
        try {
            Invoke-WebRequest -Uri "http://${upstreamBind}:$upstreamPort/healthz" -TimeoutSec 3 -UseBasicParsing | Out-Null
            Write-Ok "上游已就绪（http://${upstreamBind}:$upstreamPort）"
            return
        }
        catch {
            if ($i -eq 90) { Write-Warn "上游 180 秒内没就绪；先继续装管理端，稍后看：cd $Dir; docker compose logs" }
            Start-Sleep -Seconds 2
        }
    }
}

$upstreamCompose = Join-Path $UpstreamDir 'docker-compose.yml'
$bundledUpstream = Join-Path $root 'upstream'

if ($SkipUpstream -or $env:TF_SKIP_UPSTREAM -eq '1') {
    Write-Info '跳过上游：管理端会显示上游不可达，直到你把它起起来'
}
elseif ((Test-Path (Join-Path $UpstreamDir 'config.json')) -or (Test-Path $upstreamCompose)) {
    Write-Ok "复用已有上游部署：$UpstreamDir"
    Start-Upstream -Dir $UpstreamDir
}
elseif (Test-Path $bundledUpstream) {
    Write-Info "使用仓库内置的上游源码：$bundledUpstream"
    New-Item -ItemType Directory -Force -Path $UpstreamDir | Out-Null
    Copy-Item (Join-Path $bundledUpstream '*') $UpstreamDir -Recurse -Force
    Start-Upstream -Dir $UpstreamDir
}
elseif ($UpstreamSrc) {
    if (-not (Test-Path $UpstreamSrc)) { Stop-WithError "TF_UPSTREAM_SRC 指向的路径不存在：$UpstreamSrc" }
    New-Item -ItemType Directory -Force -Path $UpstreamDir | Out-Null
    if ($UpstreamSrc -like '*.tar.gz' -or $UpstreamSrc -like '*.tgz') {
        & tar -xzf $UpstreamSrc -C $UpstreamDir --strip-components=1
        if ($LASTEXITCODE -ne 0) { Stop-WithError "解压失败：$UpstreamSrc（Windows 10+ 自带 tar，确认它在 PATH 里）" }
    }
    else { Copy-Item (Join-Path $UpstreamSrc '*') $UpstreamDir -Recurse -Force }
    Start-Upstream -Dir $UpstreamDir
}
else {
    # 源码仓库与发布包都自带 upstream\，走到这里说明树不完整。
    Stop-WithError "仓库里没有 upstream\ 目录（上游源码随仓库/发布包分发，不应缺失）。检查源码是否完整，或用 TF_UPSTREAM_SRC 指定一份。"
}

# ── 3. data\ ────────────────────────────────────────────
Write-Step '3/5 数据目录'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'data') | Out-Null
Write-Ok 'data\ 已就绪（Windows 下不需要改属主）'

# ── 4. 构建并启动管理端 ─────────────────────────────────
Write-Step '4/5 构建并启动管理端'
if ($NoBuild -or $env:TF_NO_BUILD -eq '1') {
    Write-Info '跳过构建，直接用已有镜像'
    Invoke-Compose up -d
}
else {
    Write-Info '构建镜像（首次几分钟；前端在构建期编译）'
    Invoke-Compose build
    Invoke-Compose up -d
}

# ── 5. 等待就绪 ─────────────────────────────────────────
Write-Step '5/5 等待服务就绪'
$probeBind = if ($hostBind -eq '0.0.0.0') { '127.0.0.1' } else { $hostBind }
$url = "http://${probeBind}:${hostPort}"
$ready = $false
for ($i = 1; $i -le $HealthTimeout; $i++) {
    try {
        Invoke-WebRequest -Uri "$url/api/healthz" -TimeoutSec 3 -UseBasicParsing | Out-Null
        $ready = $true
        break
    }
    catch {
        # 面板应答了就算起来（可能是 503：上游没起来）。只有连不上才继续等。
        if ($_.Exception.Response) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
}
if ($ready) { Write-Ok '管理端已就绪' }
else {
    Write-Warn "$HealthTimeout 秒内没等到 $url/api/healthz"
    Write-Warn '看日志：deploy\deploy.ps1 -Action logs'
}

Invoke-Compose ps

$bindNote = ''
if ($hostBind -eq '127.0.0.1') {
    $bindNote = @"
 注意： 只监听本机回环。远程访问请走反向代理（HTTPS），
        或自行把 docker-compose.yml 的端口映射改成 0.0.0.0:$hostPort。
"@
}

Write-Host @"

──────────────────────────────────────────────
 面板： $url
$bindNote
 上游： http://${upstreamBind}:$upstreamPort（仅宿主 / 容器网桥可达）
 首次登录：初始管理员密码在容器日志里（只在首启生成一次）：
   deploy\deploy.ps1 -Action password
 之后：在「设置 → 用户」改掉它。

 下游接入：base_url = $url/v1
   · workbuddy 模型：模型名照旧（如 claude-sonnet-4-5）
   · trae 模型：模型名前缀 trae/（多平台页能看到可用清单）

 平台凭据、数据库、日志都在 data\（已 gitignore）。
──────────────────────────────────────────────
"@
