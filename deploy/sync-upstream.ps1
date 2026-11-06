# ============================================================
# 参考源对照（只读，不合并）—— Windows / PowerShell
#
# 上游 Go 源码（workbuddy2api）已经在本仓库的 upstream\ 里维护，日常直接改，
# 不再从外部仓库 fetch / merge。从参考仓同步能力是「挑提交、导出补丁、再应用」
# 的人工流程，见 docs\upstream-sync.md。
#
# 本脚本只剩一件事：对照 Trae 适配层的协议事实来源（trae2api-hub）。
# 两边没有共享历史，merge / rebase 都不成立；只能定期看它改了什么协议，
# 人工判断要不要跟着改。
#
# 自己的提交推 `origin`。
#
# 用法（在仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File deploy\sync-upstream.ps1 -Refs
#   powershell -ExecutionPolicy Bypass -File deploy\sync-upstream.ps1 -Refs -Mark
#
# 可覆盖的环境变量：TF_TRAE_REF_REMOTE / TF_TRAE_REF_BRANCH
# 退出码：0 对照完成；非 0 为命令失败。
# ============================================================
param(
    [switch]$Refs,
    [switch]$Mark
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Info { param([string]$Message) Write-Host "[·] $Message" -ForegroundColor Blue }
function Write-Ok { param([string]$Message) Write-Host "[√] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[!] $Message" -ForegroundColor Yellow }
function Write-Step { param([string]$Message) Write-Host "`n══ $Message" -ForegroundColor Cyan }
function Stop-WithError {
    param([string]$Message, [int]$Code = 1)
    Write-Host "[×] $Message" -ForegroundColor Red
    exit $Code
}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# git 的输出按行取回；调用处自己判断 $LASTEXITCODE
function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    return (& git @GitArgs)
}

& git rev-parse --git-dir *> $null
if ($LASTEXITCODE -ne 0) { Stop-WithError "$root 不是 git 仓库" }

# ── Trae 参考源（只读，不合并）───────────────────────────
# 参考仓库承载「Trae 服务端怎么说话」的事实；本仓库按这些事实重写了原生实现，
# 两边没有共享历史，merge / rebase 都不成立 —— 只能定期对照，人工判断。
$traeRefRemote = if ($env:TF_TRAE_REF_REMOTE) { $env:TF_TRAE_REF_REMOTE } else { 'trae-ref' }
$traeRefRepo = if ($env:TF_TRAE_REF_REPO) { $env:TF_TRAE_REF_REPO } else { 'https://github.com/shuishuipingan/trae2api-hub.git' }
$traeRefBranch = if ($env:TF_TRAE_REF_BRANCH) { $env:TF_TRAE_REF_BRANCH } else { 'main' }
$traeRefLock = Join-Path $root 'deploy\trae-ref.txt'
# 只关心这几类文件：协议事实（端点 / 请求头 / 签名 / 设备指纹）、模型目录、
# 定价、变更日志。参考仓库其余部分是它自己的看板与任务编排，与本仓库无关。
$traeRefPaths = @(
    'trae_client.py'
    'trae_fingerprint.py'
    'trae_accounts.py'
    'trae_sign.py'
    'trae_catalog_cn.json'
    'trae_catalog_intl.json'
    'pricing/trae_pricing.json'
    'CHANGELOG.md'
)

if ($Mark -and -not $Refs) {
    Stop-WithError '-Mark 要跟 -Refs 一起用（它标记的是 Trae 参考源的基线）'
}

if (-not $Refs) {
    Write-Info '本脚本只做 Trae 参考源的对照（-Refs）。'
    Write-Info '上游 Go 源码在 upstream\ 里维护；从参考仓挑能力见 docs\upstream-sync.md。'
    Write-Info '用法：powershell -ExecutionPolicy Bypass -File deploy\sync-upstream.ps1 -Refs [-Mark]'
    exit 0
}

Write-Step 'Trae 参考源（只读，不合并）'

& git remote get-url $traeRefRemote *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Info "本地没有远端 $traeRefRemote，按协议参考项目补上：$traeRefRepo"
    & git remote add $traeRefRemote $traeRefRepo
    if ($LASTEXITCODE -ne 0) { Stop-WithError "添加远端失败：git remote add $traeRefRemote $traeRefRepo" }
}
$traeRefUrl = (Invoke-Git remote get-url $traeRefRemote) -join ''
Write-Ok "参考仓库：$traeRefRemote → $traeRefUrl"

# --no-tags：参考仓库的 tag 名（v1.x）与本地 tag 撞车，fetch 会因
# 「would clobber existing tag」直接失败；这里也根本不需要它的 tag。
& git fetch $traeRefRemote --prune --no-tags *> $null
if ($LASTEXITCODE -ne 0) { Stop-WithError "git fetch $traeRefRemote 失败（网络 / 代理？）" }
$traeRef = "$traeRefRemote/$traeRefBranch"
& git rev-parse --verify $traeRef *> $null
if ($LASTEXITCODE -ne 0) { Stop-WithError "远端没有分支 $traeRef" }

$headSha = ((Invoke-Git rev-parse $traeRef) -join '').Trim()
$headShort = $headSha.Substring(0, [Math]::Min(12, $headSha.Length))

$baseSha = ''
if (Test-Path $traeRefLock) {
    $line = Get-Content $traeRefLock -Encoding utf8 |
    Where-Object { $_ -notmatch '^\s*(#|$)' } | Select-Object -First 1
    if ($line) { $baseSha = $line.Trim() }
}
$baseShort = if ($baseSha) { $baseSha.Substring(0, [Math]::Min(12, $baseSha.Length)) } else { '' }

if ($Mark) {
    $content = @(
        '# 已人工核对过的 trae2api-hub 提交 —— 本仓库 Trae 适配层的协议事实来源。'
        '# 推进时机：跑过 -Refs 看过 diff，确认本仓库不受影响（或已跟着改完）之后。'
        '# 记录方式：sync-upstream 的 --refs --mark（Windows 用 -Refs -Mark）'
        $headSha
    ) -join "`n"
    [System.IO.File]::WriteAllText($traeRefLock, $content + "`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok "基线已记为 $headShort"
    exit 0
}

if (-not $baseSha) {
    Write-Warn '还没有基线（deploy\trae-ref.txt 缺失或只有注释）'
    Write-Info '参考仓库最近 15 条提交：'
    Invoke-Git log --oneline -15 $traeRef | ForEach-Object { Write-Host $_ }
    Write-Host ''
    Write-Info '核对完用 -Refs -Mark 记录基线，之后每次只看增量'
    exit 0
}

& git cat-file -e "${baseSha}^{commit}" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Warn "基线 $baseShort 不在本地对象里（参考仓库可能改写 / 压缩过历史）"
    Write-Info "参考仓库当前：$headShort"
    Write-Warn '确认无误后用 -Refs -Mark 重设基线'
    exit 0
}

Write-Ok "基线 $baseShort → 参考仓库当前 $headShort"
if ($baseSha -eq $headSha) {
    Write-Ok '参考源没有新提交，本仓库无需跟进'
    exit 0
}

$range = "$baseSha..$headSha"
$count = [int](((Invoke-Git rev-list --count $range) -join '').Trim())
Write-Info "参考仓库新增 $count 条提交："
Invoke-Git log --oneline --no-merges $range | Select-Object -First 30 | ForEach-Object { Write-Host $_ }
if ($count -gt 30) { Write-Info "… 其余 $($count - 30) 条略" }

Write-Host ''
$touched = Invoke-Git diff --name-only $range -- @traeRefPaths
if (-not $touched) {
    Write-Ok '没碰到协议事实文件（只有看板 / 编排变化），本仓库无需跟进'
}
else {
    Write-Warn '这些协议事实文件有改动，需要人工判断：'
    Invoke-Git diff --stat $range -- @traeRefPaths | ForEach-Object { Write-Host $_ }
    Write-Host ''
    Write-Info "看具体改动：git --no-pager diff $baseShort..$headShort -- $($traeRefPaths -join ' ')"
}

Write-Host ''
Write-Info '核对完（已跟改 / 确认不受影响）把基线推进：'
Write-Info '  powershell -ExecutionPolicy Bypass -File deploy\sync-upstream.ps1 -Refs -Mark'
