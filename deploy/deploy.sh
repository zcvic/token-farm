#!/usr/bin/env bash
# ============================================================
# token-farm 一键部署（Docker Compose）
#
# 一条命令起全套：管理端容器 + 兄弟目录里的上游 workbuddy2api。
# 重复执行是安全的：已有 .env / config.json / 账号与数据一律保留。
#
# 用法：
#   bash deploy/deploy.sh              # 构建并启动（默认）
#   bash deploy/deploy.sh build        # 只构建镜像
#   bash deploy/deploy.sh restart      # 重启容器（不改代码）
#   bash deploy/deploy.sh ps           # 运行状态
#   bash deploy/deploy.sh logs         # 跟日志（Ctrl+C 退出）
#   bash deploy/deploy.sh password     # 从日志里捞首启管理员密码
#   bash deploy/deploy.sh down         # 停止并移除容器（data/ 保留）
#
# 可覆盖的环境变量：
#   TF_UPSTREAM_DIR   上游目录，默认 <仓库>/../workbuddy2api
#   TF_UPSTREAM_SRC   上游源码来源（目录 / .tar.gz）；留空则用已有目录或 git clone
#   TF_UPSTREAM_REPO  上游 git 地址，默认 Sliverkiss/workbuddy2api
#   TF_SKIP_UPSTREAM  1 = 只起管理端（上游自己另想办法）
#   TF_UPSTREAM_BIND  上游端口绑哪个宿主地址；默认 Linux 取 docker0 网关
#                     （如 172.17.0.1），其它平台 127.0.0.1
#   TF_NO_BUILD       1 = 跳过 build，直接用已有镜像
#   TF_HEALTH_TIMEOUT 等待就绪的秒数，默认 180
#   TF_PYTHON         用于生成密钥 / 改 config.json 的 python，默认 python3
#
# 为什么上游要单独起：管理端**不替代** workbuddy2api，它只是上游的可视化 + 分发层。
# 账号轮询、令牌刷新、并发与熔断都在上游进程里，所以两边必须一起跑。
# ============================================================
set -euo pipefail

# ── 输出 helpers（与本目录 install.sh 同一套风格）────────────
info() { printf '\033[1;34m[·]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m══ %s\033[0m\n' "$*"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-up}"
COMPOSE_FILE="${ROOT}/docker-compose.yml"
UPSTREAM_DIR="${TF_UPSTREAM_DIR:-${ROOT}/../workbuddy2api}"
UPSTREAM_REPO="${TF_UPSTREAM_REPO:-https://github.com/Sliverkiss/workbuddy2api.git}"
UPSTREAM_PORT="${UPSTREAM_PORT:-7863}"
HEALTH_TIMEOUT="${TF_HEALTH_TIMEOUT:-180}"
PY="${TF_PYTHON:-python3}"

case "$ACTION" in
  up|build|down|restart|ps|logs|password|help|-h|--help) ;;
  *) die "未知子命令：${ACTION}（可用：up / build / down / restart / ps / logs / password）" ;;
esac
if [ "$ACTION" = help ] || [ "$ACTION" = -h ] || [ "$ACTION" = --help ]; then
  sed -n '/^# 用法：/,/^# 为什么上游要单独起/p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

[ -f "$COMPOSE_FILE" ] || die "找不到 ${COMPOSE_FILE}：请在本仓库根目录下运行"

# ── 0. 预检 ───────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "未找到 docker。装一个再来：https://docs.docker.com/engine/install/"
docker info >/dev/null 2>&1 || die "docker 守护进程没在跑（或当前用户没有权限）。先启动它，再执行本脚本。"

# v2 是常态；v1（docker-compose）只作兜底，新版 compose 语法它未必认，所以会提示
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
  warn "在用 Compose v1（docker-compose），建议升级到 v2（docker compose）"
else
  die "未找到 docker compose。安装：https://docs.docker.com/compose/install/"
fi

# 健康检查与日志抓取要 curl；没有就退化成「不轮询，只给地址」
HAVE_CURL=0
command -v curl >/dev/null 2>&1 && HAVE_CURL=1

# ── 端口：从 compose 里读，而不是写死 ──────────────────────
# 用户很可能自己改过端口映射；脚本照着 compose 说的做，才不会「起在 7864 却去
# 探测 7865」。三种写法都要认：127.0.0.1:7864:7864 / 7870:7864 / 7864:7864。
HOST_BIND="127.0.0.1"
HOST_PORT="7864"
PORT_SPEC="$(grep -m1 -E '^[[:space:]]*-[[:space:]]*"?([0-9a-zA-Z._-]+:)?([0-9]+:)?7864(/[a-z]+)?"?[[:space:]]*$' "$COMPOSE_FILE" \
  | sed -E 's/^[[:space:]]*-[[:space:]]*//; s/"//g; s#/[a-z]+$##' || true)"
if [ -n "$PORT_SPEC" ]; then
  IFS=: read -r _p1 _p2 _p3 <<<"$PORT_SPEC"
  if [ -n "${_p3:-}" ]; then
    HOST_BIND="$_p1"
    HOST_PORT="$_p2"
  else
    HOST_BIND="0.0.0.0"
    HOST_PORT="$_p1"
  fi
fi

# 探测面板地址时别去连 0.0.0.0（有些 libcurl 会直接拒）
PROBE_BIND="$HOST_BIND"
if [ "$PROBE_BIND" = "0.0.0.0" ]; then
  PROBE_BIND="127.0.0.1"
fi

# ── 上游端口绑哪个宿主地址 ────────────────────────────────
# 管理端**跑在容器里**，容器内的 127.0.0.1 是它自己 —— 上游只绑回环的话，
# 面板起来后会一直显示「上游不可用」。两种平台的解法不同：
#   · Docker Desktop（Windows / macOS）：host.docker.internal 被转发到宿主回环，
#     绑 127.0.0.1 就行（compose 里也配了 extra_hosts）。
#   · Linux：没有这个转发，必须绑宿主网桥地址（docker0 网关，通常 172.17.0.1），
#     容器从默认网络才够得着，而且它不是公网地址。
# 想换地址（例如只绑内网网卡）就设 TF_UPSTREAM_BIND。
UPSTREAM_BIND="${TF_UPSTREAM_BIND:-}"
if [ -z "$UPSTREAM_BIND" ]; then
  if [ "$(uname -s 2>/dev/null)" = "Linux" ]; then
    UPSTREAM_BIND="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
    UPSTREAM_BIND="${UPSTREAM_BIND:-172.17.0.1}"
  else
    UPSTREAM_BIND="127.0.0.1"
  fi
fi
# 面板容器要连的地址。Linux 直接写绑定的那个 IP；Docker Desktop 走
# host.docker.internal（它才是被转发到宿主回环的名字）。用户显式指定了一个
# 非回环 IP 时，两个平台都直接用它。
WB2API_HOST=""
if [ "$(uname -s 2>/dev/null)" = "Linux" ]; then
  WB2API_HOST="$UPSTREAM_BIND"
else
  case "$UPSTREAM_BIND" in
    127.*|localhost) WB2API_HOST="" ;;
    *) WB2API_HOST="$UPSTREAM_BIND" ;;
  esac
fi
WB2API_TARGET="http://${WB2API_HOST:-host.docker.internal}:${UPSTREAM_PORT}"

# .env 里若把上游写成 127.0.0.1 / localhost，容器里那份配置是连自己的，必然失败。
# 只改这种明确的回环值：服务名（同网络）、host.docker.internal、自定义 IP 都是
# 用户有意为之，保持原样。
normalize_wb2api_base() {
  local envf="$1" cur
  [ -f "$envf" ] || return 0
  cur="$(grep -E '^[[:space:]]*WB2API_BASE=' "$envf" | tail -1 | sed -E 's/^[[:space:]]*WB2API_BASE=//; s/^"(.*)"[[:space:]]*$/\1/; s/[[:space:]]+$//')"
  [ -n "$cur" ] || return 0
  case "$cur" in
    *127.0.0.1*|*localhost*|*'[::1]'*|*'//0.0.0.0'*) ;;
    *) return 0 ;;
  esac
  sed -E "s#^([[:space:]]*WB2API_BASE=).*#\1${WB2API_TARGET}#" "$envf" > "${envf}.tmp" \
    && mv "${envf}.tmp" "$envf"
  ok "WB2API_BASE 由 ${cur} 改为 ${WB2API_TARGET}（容器里的回环地址连不到上游）"
}

compose_mgr() { $COMPOSE -f "$COMPOSE_FILE" "$@"; }

# 子命令直接转发，不再走安装流程（这些操作不该顺手重建镜像）
case "$ACTION" in
  down|ps|logs|build)
    cd "$ROOT"
    compose_mgr "$ACTION"
    exit 0
    ;;
  restart)
    cd "$ROOT"
    compose_mgr restart
    info "已重启；面板：http://${HOST_BIND}:${HOST_PORT}"
    exit 0
    ;;
  password)
    # 首启密码只出现在日志里一行，且只在**第一次**生成时打印。找不到就明说。
    cd "$ROOT"
    if compose_mgr logs --no-log-prefix workbuddy-manager 2>/dev/null | grep -iE '密码|password' | head -20; then
      :
    else
      warn "日志里没有找到密码行。可能已经初始化过（密码在首次启动时只打印一次）"
      info "忘了密码：进「设置 → 用户」重置管理员密码，或用 docker compose exec 进容器操作"
    fi
    exit 0
    ;;
esac

# ── 1. .env ──────────────────────────────────────────────
# compose 只会把 environment: 里列出的变量传进容器，但 WB_HTTP_PROXY 这类是用
# ${} 从 .env 取的。所以 .env 必须有；内容照抄模板，用户之后按需改。
step "1/5 环境配置（.env）"
cd "$ROOT"
if [ -f .env ]; then
  ok ".env 已存在，保留不动"
elif [ -f .env.example ]; then
  cp .env.example .env
  ok "已从 .env.example 生成 .env（出口代理 / 镜像加速等都在里面改）"
else
  warn "既没有 .env 也没有 .env.example，按 compose 里的默认值继续"
fi

# `.env.example` 里写的是 WB2API_BASE=http://127.0.0.1:7863 —— 那是「管理端跑在
# 宿主上」的老形态。compose 部署下它在容器里等于连自己，必须改写；用户自己填的
# 服务名 / host.docker.internal 不动。
normalize_wb2api_base .env

# data/ 是唯一必须存在的宿主机目录：数据库、日志、平台凭据都在这
mkdir -p data
[ -f .gitignore ] && grep -q '^data/' .gitignore && ok "data/ 已被 .gitignore 忽略（凭据不会误提交）"

# ── 2. 上游 workbuddy2api ────────────────────────────────
step "2/5 上游 workbuddy2api"

# 上游目录里 compose 挂载写的是 ../workbuddy2api，改路径就必须改 compose；
# 这里只在用户显式指定了别的目录时**提醒**，不偷偷改。
if [ "$UPSTREAM_DIR" != "${ROOT}/../workbuddy2api" ]; then
  warn "TF_UPSTREAM_DIR=${UPSTREAM_DIR} 与 compose 的挂载（../workbuddy2api）不一致"
  warn "管理端会读不到上游 config.json / auths。请把 compose 第 126 行改成同一路径，或用软链接："
  warn "  ln -s '${UPSTREAM_DIR}' '${ROOT}/../workbuddy2api'"
fi

bootstrap_upstream() {
  local dir="$1"
  [ -f "${dir}/docker-compose.yml" ] || [ -f "${dir}/compose.yml" ] || [ -f "${dir}/compose.yaml" ] \
    || die "${dir} 里没有 docker-compose.yml —— 确认这是上游仓库根目录"

  # 端口收敛：上游 compose 默认 "7863:7863" 是对所有网卡暴露的。管理端只需要
  # 宿主这一跳，收窄到 UPSTREAM_BIND 不改变功能，却少一个公网入口。
  # 同时兼容已经收敛成 127.0.0.1 的旧部署（Linux 上那正是连不上的原因）。
  local want="\"${UPSTREAM_BIND}:${UPSTREAM_PORT}:${UPSTREAM_PORT}\""
  if grep -qF -- "$want" "${dir}/docker-compose.yml" 2>/dev/null; then
    ok "上游端口已绑定 ${UPSTREAM_BIND}:${UPSTREAM_PORT}（安全基线）"
  else
    # 用 [[:space:]] 而不是 \s：后者是 GNU 扩展，macOS 自带的 BSD sed 不认。
    # 主机部分允许缺省、0.0.0.0 或任意 IPv4 —— 都是「上一个脚本/人留下的」。
    sed -i.bak -E \
      "s#^([[:space:]]*-[[:space:]]*)\"?([0-9]{1,3}(\\.[0-9]{1,3}){3}:)?${UPSTREAM_PORT}:${UPSTREAM_PORT}\"?[[:space:]]*\$#\\1${want}#" \
      "${dir}/docker-compose.yml" && rm -f "${dir}/docker-compose.yml.bak"
    if grep -qF -- "$want" "${dir}/docker-compose.yml" 2>/dev/null; then
      ok "上游端口已收敛为 ${UPSTREAM_BIND}:${UPSTREAM_PORT}（不对公网暴露）"
    else
      warn "未能自动收敛上游端口，请手工把 compose 的端口映射改为 ${want}"
    fi
  fi

  # config.json 含 api_key 与账号池状态：**只生成，不覆盖**
  if [ ! -f "${dir}/config.json" ]; then
    if [ -f "${dir}/config.example.json" ]; then
      cp "${dir}/config.example.json" "${dir}/config.json"
      if command -v "$PY" >/dev/null 2>&1; then
        "$PY" - "${dir}/config.json" <<'PYEOF'
import json, secrets, sys
path = sys.argv[1]
cfg = json.load(open(path, encoding='utf-8'))
cfg['api_key'] = secrets.token_hex(16)
json.dump(cfg, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
PYEOF
        ok "已生成上游 config.json（api_key 随机）"
      else
        warn "没有 ${PY}，config.json 已从模板复制但 api_key 仍是模板值 —— 请手工改掉"
      fi
    else
      warn "上游没有 config.example.json，跳过配置生成；首次启动可能需要手工建 config.json"
    fi
  else
    ok "上游 config.json 已存在，保留不动（含 api_key 与账号）"
  fi

  mkdir -p "${dir}/auths" "${dir}/data"
  # 上游容器以 uid 10001 运行；bind mount 的属主由宿主机决定，镜像内的 chown 无效。
  # 不改属主的话，表现为「auths 里明明有文件、面板显示 0 个账号」。
  if [ "$(id -u)" -eq 0 ]; then
    chown -R 10001:10001 "${dir}/auths" "${dir}/data" 2>/dev/null || \
      warn "chown 10001 失败（网络文件系统？）—— 账号读写可能报权限错误"
  else
    info "非 root：跳过 chown。若面板显示 0 个账号，执行 sudo chown -R 10001:10001 '${dir}/auths'"
  fi

  info "启动上游容器（首次会构建镜像，需要几分钟）"
  ( cd "$dir" && $COMPOSE up -d ) || die "上游启动失败：cd ${dir} && $COMPOSE logs"

  if [ "$HAVE_CURL" -eq 1 ]; then
    for i in $(seq 1 90); do
      if curl -sf -m 3 "http://${UPSTREAM_BIND}:${UPSTREAM_PORT}/healthz" >/dev/null 2>&1; then
        ok "上游已就绪（http://${UPSTREAM_BIND}:${UPSTREAM_PORT}）"
        return 0
      fi
      [ "$i" -eq 90 ] && warn "上游 180 秒内没就绪；先继续装管理端，稍后看：cd ${dir} && $COMPOSE logs"
      sleep 2
    done
  fi
}

if [ "${TF_SKIP_UPSTREAM:-0}" = "1" ]; then
  info "TF_SKIP_UPSTREAM=1：跳过上游（管理端会显示上游不可达，直到你把它起起来）"
elif [ -f "${UPSTREAM_DIR}/config.json" ] || [ -f "${UPSTREAM_DIR}/docker-compose.yml" ]; then
  ok "复用已有上游部署：${UPSTREAM_DIR}"
  bootstrap_upstream "$UPSTREAM_DIR"
elif [ -d "${ROOT}/upstream" ]; then
  # 官方 Release 包的形态：上游源码随包分发
  info "使用仓库内置的上游源码：${ROOT}/upstream"
  mkdir -p "$UPSTREAM_DIR"
  cp -a "${ROOT}/upstream/." "${UPSTREAM_DIR}/"
  bootstrap_upstream "$UPSTREAM_DIR"
elif [ -n "${TF_UPSTREAM_SRC:-}" ]; then
  [ -e "$TF_UPSTREAM_SRC" ] || die "TF_UPSTREAM_SRC 指向的路径不存在：${TF_UPSTREAM_SRC}"
  info "使用指定源码：${TF_UPSTREAM_SRC}"
  mkdir -p "$UPSTREAM_DIR"
  case "$TF_UPSTREAM_SRC" in
    *.tar.gz|*.tgz) tar -xzf "$TF_UPSTREAM_SRC" -C "$UPSTREAM_DIR" --strip-components=1 ;;
    *) [ -d "$TF_UPSTREAM_SRC" ] || die "TF_UPSTREAM_SRC 既不是目录也不是 .tar.gz"; cp -a "$TF_UPSTREAM_SRC/." "$UPSTREAM_DIR/" ;;
  esac
  bootstrap_upstream "$UPSTREAM_DIR"
else
  # 源码仓库的形态：本地没有上游，就按 GitHub 上那份拉一份。这**不是**官方推荐
  # 路径（官方 Release 带 upstream/），但「clone 下来一条命令跑起来」需要它。
  command -v git >/dev/null 2>&1 || die "本地没有上游，也没装 git，无法自动获取。请用官方 Release 包（内含 upstream/）或设 TF_UPSTREAM_SRC"
  info "本地没有上游，从 ${UPSTREAM_REPO} 克隆到 ${UPSTREAM_DIR}"
  if git clone --depth 1 "$UPSTREAM_REPO" "$UPSTREAM_DIR"; then
    bootstrap_upstream "$UPSTREAM_DIR"
  else
    warn "克隆失败（网络 / 代理？）。管理端仍会启动，但账号池不可用。"
    warn "可稍后手工补：git clone ${UPSTREAM_REPO} '${UPSTREAM_DIR}'，再重跑本脚本"
    rm -rf "$UPSTREAM_DIR" 2>/dev/null || true
  fi
fi

# ── 3. data/ 权限 ────────────────────────────────────────
step "3/5 数据目录权限"
mkdir -p "${ROOT}/data"
if [ "$(id -u)" -eq 0 ]; then
  # 容器以 uid 10001 运行；第一次 docker 自动建出来的 ./data 归 root，
  # 结果是启动时 sqlite 打不开数据库，报错里既没有路径也没有原因。
  chown -R 10001:10001 "${ROOT}/data" 2>/dev/null && ok "data/ 属主已设为 10001" || \
    warn "chown 失败（NFS / SMB？）—— 可改用命名卷，见 docker-compose.yml 的说明"
else
  info "非 root：跳过 chown。若启动报 sqlite 打不开，执行 sudo chown -R 10001:10001 '${ROOT}/data'"
fi

# ── 4. 构建并启动管理端 ──────────────────────────────────
step "4/5 构建并启动管理端"
cd "$ROOT"
if [ "${TF_NO_BUILD:-0}" = "1" ]; then
  info "TF_NO_BUILD=1：跳过构建，直接用已有镜像"
  compose_mgr up -d
else
  info "构建镜像（首次几分钟；前端在构建期编译）"
  compose_mgr build
  compose_mgr up -d
fi

# ── 5. 等待就绪 ──────────────────────────────────────────
step "5/5 等待服务就绪"
URL="http://${PROBE_BIND}:${HOST_PORT}"
if [ "$HAVE_CURL" -eq 0 ]; then
  warn "没有 curl，跳过健康检查"
else
  ready=0
  # /api/healthz 同时探管理端本身与上游连通性：不带 -f，让 503（上游没起来）
  # 也算「面板起来了」——否则只跳过上游的人会看到一条假的「没就绪」。
  for i in $(seq 1 "$HEALTH_TIMEOUT"); do
    if curl -sS -m 3 -o /dev/null "${URL}/api/healthz" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [ "$ready" -eq 1 ]; then
    ok "管理端已就绪"
  else
    warn "${HEALTH_TIMEOUT} 秒内没等到 ${URL}/api/healthz"
    warn "看日志：bash deploy/deploy.sh logs"
  fi
fi

compose_mgr ps || true

# ── 收尾提示 ─────────────────────────────────────────────
BIND_NOTE=""
if [ "$HOST_BIND" = "127.0.0.1" ]; then
  BIND_NOTE=" 注意： 只监听本机回环。远程访问请走反向代理（HTTPS），
        或自行把 docker-compose.yml 的端口映射改成 0.0.0.0:${HOST_PORT}。
"
fi

cat <<EOF

──────────────────────────────────────────────
 面板： ${URL}
${BIND_NOTE}
 上游： http://${UPSTREAM_BIND}:${UPSTREAM_PORT}（仅宿主 / 容器网桥可达）
 首次登录：初始管理员密码在容器日志里（只在首启生成一次）：
   bash deploy/deploy.sh password
 之后：在「设置 → 用户」改掉它。

 下游接入：base_url = ${URL}/v1
   · workbuddy 模型：模型名照旧（如 claude-sonnet-4-5）
   · trae 模型：模型名前缀 trae/（多平台页能看到可用清单）

 平台凭据、数据库、日志都在 data/（已 gitignore）。
──────────────────────────────────────────────
EOF
