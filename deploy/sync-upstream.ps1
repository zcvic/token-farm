# ============================================================
# 同步参考项目 —— Windows / PowerShell
#
# 本仓库有两个参考源，性质完全不同：
#
#   upstream  = workbuddy-manager，本仓库的**派生来源**。有共享提交历史，
#               可以 fetch + merge，脚本默认做的就是这件事。
#   trae-ref  = trae2api-hub，Trae 适配层的**协议事实来源**（只读参考）。
#               我们按它的协议重写了原生实现，两边没有共享历史，**不能 merge**；
#               只能定期看它改了什么，人工判断要不要跟着改。用 -Refs。
#
# 自己的提交推 `origin`。
#
# 用法（在仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File deploy\sync-upstream.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\sync-upstream.ps1 -Check
#   powershell -ExecutionPolicy Bypass -File deploy\sync-upstream.ps1 -NoTest
#   powershell -ExecutionPolicy Bypass -File deploy\sync-upstream.ps1 -Refs
#   powershell -ExecutionPolicy Bypass -File deploy\sync-upstream.ps1 -Refs -Mark
#
# 可覆盖的环境变量：TF_UPSTREAM_REMOTE / TF_UPSTREAM_BRANCH /
#                   TF_TRAE_REF_REMOTE / TF_TRAE_REF_BRANCH / TF_PYTHON
# 退出码：0 已最新或合并成功；2 需要人工处理；3 测试未通过。
# ============================================================
param(
    [switch]$Check,
    [switch]$NoTest,
    [switch]$Refs,
    [switch]$Mark,
    [string]$Remote = $(if ($env:TF_UPSTREAM_REMOTE) { $env:TF_UPSTREAM_REMOTE } else { 'upstream' }),
    [string]$Branch = $env:TF_UPSTREAM_BRANCH,
    [string]$Python = $env:TF_PYTHON
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
$defaultRemote = 'https://github.com/ithtelab/workbuddy-manager.git'
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
    Stop-WithError '-Mark 要跟 -Refs 一起用（它标记的是 Trae 参考源的基线，不是上游合并）'
}

if ($Refs) {
    Write-Step 'Trae 参考源（只读，不合并）'

    & git remote get-url $traeRefRemote *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Info "本地没有远端 $traeRefRemote，按协议参考项目补上：$traeRefRepo"
        & git remote add $traeRefRemote $traeRefRepo
        if ($LASTEXITCODE -ne 0) { Stop-WithError "添加远端失败：git remote add $traeRefRemote $traeRefRepo" }
    }
    $traeRefUrl = (Invoke-Git remote get-url $traeRefRemote) -join ''
    Write-Ok "参考仓库：$traeRefRemote → $traeRefUrl"

    # --no-tags：参考仓库的 tag 名（v1.x）与 upstream 撞车，fetch 会因
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
    exit 0
}

# ── 远端 ────────────────────────────────────────────────
& git remote get-url $Remote *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Info "本地没有远端 $Remote，按参考项目地址补上：$defaultRemote"
    & git remote add $Remote $defaultRemote
    if ($LASTEXITCODE -ne 0) { Stop-WithError "添加远端失败：git remote add $Remote $defaultRemote" }
}
$upstreamUrl = (Invoke-Git remote get-url $Remote) -join ''
Write-Ok "参考项目：$Remote → $upstreamUrl"

$current = (Invoke-Git rev-parse --abbrev-ref HEAD) -join ''
if ($current -eq 'HEAD') { Stop-WithError '当前是游离 HEAD，先切到分支（例如 git switch token-farm）' }
Write-Ok "当前分支：$current"

Write-Step '获取上游'
& git fetch $Remote --prune --tags
if ($LASTEXITCODE -ne 0) { Stop-WithError "git fetch $Remote 失败（网络 / 代理？）" }

if (-not $Branch) {
    $head = Invoke-Git symbolic-ref -q --short "refs/remotes/$Remote/HEAD" 2>$null
    if ($LASTEXITCODE -eq 0 -and $head) { $Branch = ($head -join '').Replace("$Remote/", '') }
    else { $Branch = 'main' }
}
$upstreamRef = "$Remote/$Branch"
& git rev-parse --verify $upstreamRef *> $null
if ($LASTEXITCODE -ne 0) { Stop-WithError "远端没有分支 $upstreamRef" }
Write-Ok "对照分支：$upstreamRef"

# ── 差异概览 ────────────────────────────────────────────
$incoming = [int](((Invoke-Git rev-list --count "HEAD..$upstreamRef") -join '').Trim())
$ours = [int](((Invoke-Git rev-list --count "$upstreamRef..HEAD") -join '').Trim())

Write-Step '差异'
Write-Info "本地有、上游没有的提交：$ours 条（本项目的改动）"
Write-Info "上游有、本地没有的提交：$incoming 条"

if ($incoming -eq 0) {
    Write-Ok '已是最新，无需同步'
    exit 0
}

Write-Host ''
Invoke-Git log --oneline --no-merges "HEAD..$upstreamRef" | Select-Object -First 40 | ForEach-Object { Write-Host $_ }
if ($incoming -gt 40) { Write-Host "… 其余 $($incoming - 40) 条略" }

Write-Host ''
Write-Info '涉及文件（前 25 行）：'
Invoke-Git --no-pager diff --stat "HEAD...$upstreamRef" | Select-Object -First 25 | ForEach-Object { Write-Host $_ }

if ($Check) {
    Write-Host ''
    Write-Info '只做检查，未改动工作区。要合并就运行：deploy\sync-upstream.ps1'
    exit 0
}

# ── 工作区必须干净 ──────────────────────────────────────
Write-Step '合并'
$dirty = Invoke-Git status --porcelain
if ($dirty) {
    $dirty | Select-Object -First 20 | ForEach-Object { Write-Host $_ }
    Stop-WithError "工作区不干净。先提交（git commit）或暂存（git stash），再同步 ——
   脚本不替你决定哪些改动该进哪个提交。" 2
}

& git merge-base --is-ancestor HEAD $upstreamRef *> $null
$ffPossible = ($LASTEXITCODE -eq 0)

if ($ffPossible) {
    Write-Info '可以快进（本地没有分叉）'
    & git merge --ff-only $upstreamRef
    if ($LASTEXITCODE -ne 0) { Stop-WithError "快进失败：git merge --ff-only $upstreamRef" }
}
else {
    Write-Info '本地有分叉（你的多平台改动），做三方合并'
    & git merge --no-edit $upstreamRef
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Warn '合并冲突，以下文件需要手工解决：'
        Invoke-Git diff --name-only --diff-filter=U | ForEach-Object { Write-Host "   $_" }
        Write-Host @'

 建议顺序：
   1) 先看「上游为什么改这里」：git log --oneline -5 MERGE_HEAD -- <文件>
   2) 保留本仓库语义（trae/ 分流、平台路由、导航计数），再把上游的改动并进去
   3) git add <文件> && git commit
   4) 放弃这次合并：git merge --abort

 常见冲突点见 TOKEN-FARM.md 的「冲突主要会出现在哪」一节。
'@
        exit 2
    }
}
Write-Ok "已合并 $upstreamRef"

# ── 测试 ────────────────────────────────────────────────
if ($NoTest) {
    Write-Warn '按参数跳过测试（-NoTest）；合并结果未验证'
}
else {
    Write-Step '跑单测'
    if (-not $Python) {
        foreach ($candidate in @((Join-Path $root '.venv\Scripts\python.exe'), (Join-Path $root 'venv\Scripts\python.exe'))) {
            if (Test-Path $candidate) { $Python = $candidate; break }
        }
    }
    if (-not $Python) {
        $found = Get-Command python -ErrorAction SilentlyContinue
        if ($found) { $Python = $found.Source }
    }
    if (-not $Python) { Stop-WithError '找不到 python。用 TF_PYTHON=C:\path\to\python.exe 指定解释器。' }

    Write-Info "解释器：$Python"
    & $Python -X utf8 -m unittest discover -s server/tests -t . -q
    if ($LASTEXITCODE -ne 0) {
        Write-Warn '单测未通过 —— 合并本身可能是对的，但先别推上去'
        exit 3
    }
    Write-Ok '单测通过'
}

Write-Host @"

──────────────────────────────────────────────
 同步完成。下一步：
   git log --oneline -3              # 确认合并结果
   git push origin $current          # 推到自己的仓库
   deploy\deploy.ps1                 # 重新构建并部署
──────────────────────────────────────────────
"@
