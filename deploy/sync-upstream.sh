#!/usr/bin/env bash
# ============================================================
# 同步参考项目
#
# 本仓库有两个参考源，性质完全不同：
#
#   upstream    = workbuddy-manager，本仓库的**派生来源**。有共享提交历史，
#                 可以 fetch + merge，脚本默认做的就是这件事。
#   trae-ref    = trae2api-hub，Trae 适配层的**协议事实来源**（只读参考）。
#                 我们按它的协议重写了原生实现，两边没有共享历史，**不能 merge**；
#                 只能定期看它改了什么协议，人工判断要不要跟着改。用 --refs。
#
# 自己的提交推 `origin`。
#
# 用法：
#   bash deploy/sync-upstream.sh            # fetch + 合并 + 跑单测
#   bash deploy/sync-upstream.sh --check    # 只看有什么新提交，不动代码
#   bash deploy/sync-upstream.sh --no-test  # 只合并，不跑测试
#   bash deploy/sync-upstream.sh --refs     # 只看 Trae 参考源改了什么（不合并）
#   bash deploy/sync-upstream.sh --refs --mark   # 核对完，把当前版本记为基线
#
# 可覆盖的环境变量：
#   TF_UPSTREAM_REMOTE  远端名，默认 upstream
#   TF_UPSTREAM_BRANCH  上游分支，默认自动探测（通常 main）
#   TF_TRAE_REF_REMOTE  Trae 参考源远端名，默认 trae-ref
#   TF_TRAE_REF_BRANCH  Trae 参考源分支，默认 main
#   TF_PYTHON           跑测试用的 python，默认依次找 .venv / venv / python3
#
# 退出码：0 已是最新或合并成功；2 需要人工处理（冲突 / 工作区不干净）；
#         其它非 0 为命令失败。
# ============================================================
set -euo pipefail

info() { printf '\033[1;34m[·]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m══ %s\033[0m\n' "$*"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${TF_UPSTREAM_REMOTE:-upstream}"
BRANCH="${TF_UPSTREAM_BRANCH:-}"
DEFAULT_REMOTE="https://github.com/ithtelab/workbuddy-manager.git"
CHECK_ONLY=0
RUN_TESTS=1
REFS_ONLY=0
MARK_REF=0

for arg in "$@"; do
  case "$arg" in
    --check|-c) CHECK_ONLY=1 ;;
    --no-test) RUN_TESTS=0 ;;
    --refs) REFS_ONLY=1 ;;
    --mark) MARK_REF=1 ;;
    -h|--help) sed -n '/^# 用法：/,/^# 退出码/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "未知参数：$arg（--check / --no-test / --refs / --mark / --help）" ;;
  esac
done
if [ "$MARK_REF" -eq 1 ] && [ "$REFS_ONLY" -ne 1 ]; then
  die "--mark 要跟 --refs 一起用（它标记的是 Trae 参考源的基线，不是上游合并）"
fi

cd "$ROOT"
git rev-parse --git-dir >/dev/null 2>&1 || die "$ROOT 不是 git 仓库"

# ── Trae 参考源（只读，不合并）───────────────────────────
# 参考仓库承载「Trae 服务端怎么说话」的事实；本仓库按这些事实重写了原生实现，
# 两边没有共享历史，merge / rebase 都不成立 —— 只能定期对照，人工判断。
TRAE_REF_REMOTE="${TF_TRAE_REF_REMOTE:-trae-ref}"
TRAE_REF_REPO="${TF_TRAE_REF_REPO:-https://github.com/shuishuipingan/trae2api-hub.git}"
TRAE_REF_BRANCH="${TF_TRAE_REF_BRANCH:-main}"
TRAE_REF_LOCK="${ROOT}/deploy/trae-ref.txt"
# 只关心这几类文件：协议事实（端点 / 请求头 / 签名 / 设备指纹）、模型目录、
# 定价、变更日志。参考仓库其余部分是它自己的看板与任务编排，改多少都与本仓库无关。
TRAE_REF_PATHS=(
  trae_client.py
  trae_fingerprint.py
  trae_accounts.py
  trae_sign.py
  trae_catalog_cn.json
  trae_catalog_intl.json
  pricing/trae_pricing.json
  CHANGELOG.md
)

refs_report() {
  step "Trae 参考源（只读，不合并）"

  if ! git remote get-url "$TRAE_REF_REMOTE" >/dev/null 2>&1; then
    info "本地没有远端 $TRAE_REF_REMOTE，按协议参考项目补上：$TRAE_REF_REPO"
    git remote add "$TRAE_REF_REMOTE" "$TRAE_REF_REPO"
  fi
  ok "参考仓库：$TRAE_REF_REMOTE → $(git remote get-url "$TRAE_REF_REMOTE")"

  # --no-tags：参考仓库的 tag 名（v1.x）与 upstream 撞车，fetch 会因
  # 「would clobber existing tag」直接失败；这里也根本不需要它的 tag。
  git fetch "$TRAE_REF_REMOTE" --prune --no-tags >/dev/null 2>&1 \
    || die "git fetch $TRAE_REF_REMOTE 失败（网络 / 代理？）"
  local ref="${TRAE_REF_REMOTE}/${TRAE_REF_BRANCH}"
  git rev-parse --verify "$ref" >/dev/null 2>&1 || die "远端没有分支 $ref"

  local head_sha base_sha head_short base_short
  head_sha="$(git rev-parse "$ref")"
  head_short="$(printf '%s' "$head_sha" | cut -c1-12)"

  base_sha=""
  if [ -f "$TRAE_REF_LOCK" ]; then
    base_sha="$(grep -vE '^[[:space:]]*(#|$)' "$TRAE_REF_LOCK" | head -1 | tr -d '[:space:]')"
  fi
  base_short="$(printf '%s' "$base_sha" | cut -c1-12)"

  if [ "$MARK_REF" -eq 1 ]; then
    {
      printf '# 已人工核对过的 trae2api-hub 提交 —— 本仓库 Trae 适配层的协议事实来源。\n'
      printf '# 推进时机：跑过 --refs 看过 diff，确认本仓库不受影响（或已跟着改完）之后。\n'
      printf '# 记录方式：sync-upstream 的 --refs --mark（Windows 用 -Refs -Mark）\n'
      printf '%s\n' "$head_sha"
    } > "$TRAE_REF_LOCK"
    ok "基线已记为 ${head_short}"
    return 0
  fi

  if [ -z "$base_sha" ]; then
    warn "还没有基线（deploy/trae-ref.txt 缺失或只有注释）。"
    info "参考仓库最近 15 条提交："
    git --no-pager log --oneline -15 "$ref" || true
    echo
    info "核对完用 --refs --mark 记录基线，之后每次只看增量。"
    return 0
  fi

  if ! git cat-file -e "${base_sha}^{commit}" 2>/dev/null; then
    warn "基线 ${base_short} 不在本地对象里（参考仓库可能改写/压缩过历史）"
    info "参考仓库当前：${head_short}"
    warn "确认无误后用 --refs --mark 重设基线"
    return 0
  fi

  ok "基线 ${base_short} → 参考仓库当前 ${head_short}"
  if [ "$base_sha" = "$head_sha" ]; then
    ok "参考源没有新提交，本仓库无需跟进"
    return 0
  fi

  local n
  n="$(git rev-list --count "${base_sha}..${head_sha}")"
  info "参考仓库新增 ${n} 条提交："
  git --no-pager log --oneline --no-merges "${base_sha}..${head_sha}" | head -30 || true
  if [ "$n" -gt 30 ]; then
    info "… 其余 $((n - 30)) 条略"
  fi

  echo
  local touched
  touched="$(git --no-pager diff --name-only "${base_sha}..${head_sha}" -- "${TRAE_REF_PATHS[@]}" || true)"
  if [ -z "$touched" ]; then
    ok "没碰到协议事实文件（只有看板 / 编排变化），本仓库无需跟进"
  else
    warn "这些协议事实文件有改动，需要人工判断："
    git --no-pager diff --stat "${base_sha}..${head_sha}" -- "${TRAE_REF_PATHS[@]}"
    echo
    info "看具体改动：git --no-pager diff ${base_short}..${head_short} -- ${TRAE_REF_PATHS[*]}"
  fi

  echo
  info "核对完（已跟改 / 确认不受影响）把基线推进："
  info "  bash deploy/sync-upstream.sh --refs --mark"
}

if [ "$REFS_ONLY" -eq 1 ]; then
  refs_report
  exit 0
fi

# ── 远端 ─────────────────────────────────────────────────
if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  info "本地没有远端 $REMOTE，按参考项目地址补上：$DEFAULT_REMOTE"
  git remote add "$REMOTE" "$DEFAULT_REMOTE"
fi
UPSTREAM_URL="$(git remote get-url "$REMOTE")"
ok "参考项目：$REMOTE → $UPSTREAM_URL"

CURRENT="$(git rev-parse --abbrev-ref HEAD)"
[ "$CURRENT" = "HEAD" ] && die "当前是游离 HEAD，先切到分支（例如 git switch token-farm）"
ok "当前分支：$CURRENT"

step "获取上游"
git fetch "$REMOTE" --prune --tags
# 上游默认分支：优先跟随远端的 HEAD 指向，探测不到再退回 main
if [ -z "$BRANCH" ]; then
  BRANCH="$(git symbolic-ref -q --short "refs/remotes/${REMOTE}/HEAD" 2>/dev/null | sed "s#^${REMOTE}/##" || true)"
  [ -n "$BRANCH" ] || BRANCH="main"
fi
UPSTREAM_REF="${REMOTE}/${BRANCH}"
git rev-parse --verify "$UPSTREAM_REF" >/dev/null 2>&1 || die "远端没有分支 $UPSTREAM_REF"
ok "对照分支：$UPSTREAM_REF"

# ── 差异概览 ─────────────────────────────────────────────
INCOMING="$(git rev-list --count "HEAD..${UPSTREAM_REF}" 2>/dev/null || echo 0)"
BEHIND_OURS="$(git rev-list --count "${UPSTREAM_REF}..HEAD" 2>/dev/null || echo 0)"

step "差异"
info "本地有、上游没有的提交：${BEHIND_OURS} 条（本项目的改动）"
info "上游有、本地没有的提交：${INCOMING} 条"

if [ "$INCOMING" -eq 0 ]; then
  ok "已是最新，无需同步"
  exit 0
fi

echo
git --no-pager log --oneline --no-merges "HEAD..${UPSTREAM_REF}" | head -40
if [ "$INCOMING" -gt 40 ]; then
  echo "… 其余 $((INCOMING - 40)) 条略"
fi

echo
info "涉及文件（按改动次数排序，前 25）："
git --no-pager diff --stat "HEAD...${UPSTREAM_REF}" | tail -n +1 | head -25 || true

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo
  info "只做检查，未改动工作区。要合并就运行：bash deploy/sync-upstream.sh"
  exit 0
fi

# ── 工作区必须干净 ───────────────────────────────────────
step "合并"
if [ -n "$(git status --porcelain)" ]; then
  git status --short | head -20
  die "工作区不干净。先提交（git commit）或暂存（git stash），再同步 ——
   脚本不替你决定哪些改动该进哪个提交。"
fi

if git merge-base --is-ancestor HEAD "$UPSTREAM_REF"; then
  info "可以快进（本地没有分叉）"
  git merge --ff-only "$UPSTREAM_REF"
else
  info "本地有分叉（你的多平台改动），做三方合并"
  if ! git merge --no-edit "$UPSTREAM_REF"; then
    echo
    warn "合并冲突，以下文件需要手工解决："
    git --no-pager diff --name-only --diff-filter=U || true
    cat <<'EOF'

 建议顺序：
   1) 先看「上游为什么改这里」：git log --oneline -5 MERGE_HEAD -- <文件>
   2) 保留本仓库语义（trae/ 分流、平台路由、导航计数），再把上游的改动并进去
   3) git add <文件> && git commit
   4) 重跑：bash deploy/sync-upstream.sh --no-test 会提示「已是最新」
   5) 放弃这次合并：git merge --abort

 常见冲突点见 TOKEN-FARM.md 的「冲突主要会出现在哪」一节。
EOF
    exit 2
  fi
fi
ok "已合并 $UPSTREAM_REF"

# ── 测试 ─────────────────────────────────────────────────
if [ "$RUN_TESTS" -eq 0 ]; then
  warn "按参数跳过测试（--no-test）；合并结果未验证"
else
  step "跑单测"
  PY="${TF_PYTHON:-}"
  if [ -z "$PY" ]; then
    for cand in "$ROOT/.venv/bin/python" "$ROOT/venv/bin/python"; do
      [ -x "$cand" ] && PY="$cand" && break
    done
  fi
  [ -n "$PY" ] || PY="$(command -v python3 || true)"
  [ -n "$PY" ] || die "找不到 python。用 TF_PYTHON=/path/to/python 指定解释器。"

  info "解释器：$PY"
  if "$PY" -X utf8 -m unittest discover -s server/tests -t . -q; then
    ok "单测通过"
  else
    warn "单测未通过 —— 合并本身可能是对的，但先别推上去"
    exit 3
  fi
fi

cat <<EOF

──────────────────────────────────────────────
 同步完成。下一步：
   git log --oneline -3           # 确认合并结果
   git push origin ${CURRENT}       # 推到自己的仓库
   bash deploy/deploy.sh          # 重新构建并部署
──────────────────────────────────────────────
EOF
