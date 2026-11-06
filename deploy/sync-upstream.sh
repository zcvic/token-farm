#!/usr/bin/env bash
# ============================================================
# 参考源对照（只读，不合并）
#
# 上游 Go 源码（workbuddy2api）已经在本仓库的 upstream/ 里维护，日常直接改，
# 不再从外部仓库 fetch / merge。从参考仓同步能力是「挑提交、导出补丁、再应用」
# 的人工流程，见 docs/upstream-sync.md。
#
# 本脚本只剩一件事：对照 Trae 适配层的协议事实来源（trae2api-hub）。
# 两边没有共享历史，merge / rebase 都不成立；只能定期看它改了什么协议，
# 人工判断要不要跟着改。
#
# 自己的提交推 `origin`。
#
# 用法：
#   bash deploy/sync-upstream.sh --refs         # 看参考源改了什么（不合并）
#   bash deploy/sync-upstream.sh --refs --mark  # 核对完，把当前版本记为基线
#
# 可覆盖的环境变量：
#   TF_TRAE_REF_REMOTE  Trae 参考源远端名，默认 trae-ref
#   TF_TRAE_REF_BRANCH  参考源分支，默认 main
#
# 退出码：0 对照完成；非 0 为命令失败。
# ============================================================
set -euo pipefail

info() { printf '\033[1;34m[·]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m══ %s\033[0m\n' "$*"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REFS_ONLY=0
MARK_REF=0

for arg in "$@"; do
  case "$arg" in
    --refs) REFS_ONLY=1 ;;
    --mark) MARK_REF=1 ;;
    -h|--help) sed -n '/^# 用法：/,/^# 退出码/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "未知参数：$arg（--refs / --mark / --help）" ;;
  esac
done
if [ "$MARK_REF" -eq 1 ] && [ "$REFS_ONLY" -ne 1 ]; then
  die "--mark 要跟 --refs 一起用（它标记的是 Trae 参考源的基线）"
fi
if [ "$REFS_ONLY" -ne 1 ]; then
  info "本脚本只做 Trae 参考源的对照（--refs）。"
  info "上游 Go 源码在 upstream/ 里维护；从参考仓挑能力见 docs/upstream-sync.md。"
  info "用法：bash deploy/sync-upstream.sh --refs [--mark]"
  exit 0
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

  # --no-tags：参考仓库的 tag 名（v1.x）与本地 tag 撞车，fetch 会因
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

refs_report
