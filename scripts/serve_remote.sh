#!/usr/bin/env bash
# 一键起「远程批改」服务（2026-09-15）。
#
# 起两样东西：
#   1. uvicorn 绑 0.0.0.0:8787 —— 局域网可访问（本机免鉴权、远程需令牌）
#   2. cloudflared 快速隧道    —— 外网可访问（手机流量也行）
#
# 为什么要用脚本：这两进程隔天会掉；而且后台任务里 `cd A && cmd &` 的 cd
# 不作用于后续命令，手敲相对路径容易把日志写错地方。
#
# ⚠ 本环境踩到的两个坑（已规避，勿改回去）：
#   1. `curl -s -o /dev/null <url>`（不带 -w）会返回 exit 23「写错误」，
#      用它做存活检查会把「服务正常」误判成「没起」。故一律用 -w '%{http_code}' 取码判断。
#   2. 不要用 `> log` 覆盖**仍在运行**的进程的日志文件——老进程的 fd 还在，
#      继续按自己的偏移写，会把新旧内容交错成乱码。故每次启动用带时间戳的新日志。
#
# 用法：
#   bash scripts/serve_remote.sh            # 起了就复用，没起就起
#   bash scripts/serve_remote.sh --restart  # 先杀掉占用端口的进程再起（改完代码用这个）
set -u

PROJ="F:/agi/language-genome"
PY="F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe"
CF="/c/Program Files (x86)/cloudflared/cloudflared"
DBG="$PROJ/data/_dbg"
TOKEN_FILE="$PROJ/data/review_token.txt"
PORT=8787

mkdir -p "$DBG"
STAMP=$(date +%Y%m%d_%H%M%S)

# 取 HTTP 状态码；连不上返回 000。
# ⚠ 不要写成 `curl ... -w '%{http_code}' ... || echo 000`：
#   curl 的 -w **失败时本身就会打印 000**，再加 `|| echo 000` 会拼成 "000000"，
#   于是 `!= "000"` 恒为真 —— 脚本会一直谎报"服务已在运行"，然后什么都不做。
#   这是本轮踩到的第三个静默失败，同一类问题反复出现，故在此写死注释。
code() {
  local c
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-3}" "$1" 2>/dev/null)
  printf '%s' "${c:-000}"
}

# 端口是否真的在监听（不依赖 HTTP 响应，避免把"起了但报错"当成正常）
port_listening() {
  netstat -ano 2>/dev/null | grep -E "TCP.*:$PORT[[:space:]].*LISTENING" >/dev/null
}

# 杀掉占用 $PORT 的进程。
# ⚠ 两个已踩过的坑，勿改回去：
#   1. `pkill -f uvicorn` 在 Windows 上杀不掉 python.exe，**且静默失败**——
#      表现是"重启后仍是旧代码"，极难察觉（本轮就踩了）。
#   2. `taskkill //F //PID n` 会被 Git Bash 当字面参数，报"无效参数/选项 - '//F'"。
#      必须 `MSYS_NO_PATHCONV=1 taskkill /F /PID n`。
# 另：杀完必须**回查确认**，否则又是静默失败。
kill_port() {
  local pids
  pids=$(netstat -ano 2>/dev/null | grep -E "TCP.*:$PORT[[:space:]].*LISTENING" | awk '{print $NF}' | sort -u)
  for p in $pids; do
    [ -n "$p" ] && [ "$p" != "0" ] || continue
    if MSYS_NO_PATHCONV=1 taskkill /F /PID "$p" >/dev/null 2>&1; then
      echo "  已杀 PID $p"
    else
      echo "  ⚠ 杀 PID $p 失败"
    fi
  done
  sleep 2
  if netstat -ano 2>/dev/null | grep -E "TCP.*:$PORT[[:space:]].*LISTENING" >/dev/null; then
    echo "✗ 端口 :$PORT 仍被占用，重启未生效——别继续，先手动处理"
    exit 1
  fi
}

# ── 1) uvicorn ────────────────────────────────────────────────
if [ "${1:-}" = "--restart" ]; then
  echo "· 重启服务：先杀掉占用 :$PORT 的进程"
  kill_port
fi

if [ "$(code "http://127.0.0.1:$PORT/" 3)" != "000" ]; then
  echo "✓ 服务已在运行（:$PORT）"
else
  LOG="$DBG/uvicorn_$STAMP.log"
  echo "· 启动 uvicorn（0.0.0.0:$PORT），日志 $LOG"
  # `< /dev/null` 不能省：否则后台进程会继承调用方的 stdin/stdout 管道，
  # 使 `bash serve_remote.sh | tail` 这类管道永远等不到 EOF 而挂住。
  ( cd "$PROJ" && nohup "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" \
      < /dev/null > "$LOG" 2>&1 & )
  for _ in $(seq 1 20); do
    sleep 1
    [ "$(code "http://127.0.0.1:$PORT/" 2)" != "000" ] && break
  done
fi

# 回查：端口必须真的在听，否则报错退出（防"谎报在运行"）
if ! port_listening; then
  echo "✗ 端口 :$PORT 没有在监听 —— 启动失败"
  tail -20 "$LOG" 2>/dev/null
  exit 1
fi
echo "✓ 本机可访问"

# ── 2) 隧道 ───────────────────────────────────────────────────
LOG="$DBG/cf_tunnel_$STAMP.log"
URL=""
# 先看有没有活着的隧道
for f in $(ls -t "$DBG"/cf_tunnel*.log 2>/dev/null); do
  u=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$f" 2>/dev/null | tail -1)
  if [ -n "$u" ] && [ "$(code "$u/" 8)" != "000" ]; then URL="$u"; break; fi
done

if [ -n "$URL" ]; then
  echo "✓ 隧道已在运行"
else
  echo "· 启动 cloudflared 隧道…"
  pkill -f "cloudflared.*tunnel" 2>/dev/null
  sleep 1
  ( nohup "$CF" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate \
      < /dev/null > "$LOG" 2>&1 & )
  for _ in $(seq 1 25); do
    sleep 3
    URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$LOG" 2>/dev/null | tail -1)
    [ -n "$URL" ] && break
  done
fi

TOKEN=""
[ -f "$TOKEN_FILE" ] && TOKEN=$(tr -d '\r\n' < "$TOKEN_FILE")

# 取本机局域网 IP。不用 `hostname -I`（Windows Git Bash 下无输出），
# 也不用 `ipconfig`（输出是 GBK，被 grep 当二进制）。用 Python 探出口网卡地址，
# 只看路由不发包。拿不到就退回 ipconfig（转码后再解析）。
LANIP=$("$PY" -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80)); print(s.getsockname()[0])
except Exception: pass
finally: s.close()
" 2>/dev/null | tr -d '\r\n')
if [ -z "$LANIP" ]; then
  LANIP=$(ipconfig 2>/dev/null | iconv -f GBK -t UTF-8 2>/dev/null \
          | grep -i "IPv4" | grep -oE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" \
          | grep -v "^127\." | head -1)
fi
[ -z "$LANIP" ] && LANIP="<本机局域网IP>"

echo ""
echo "──────────────────────────────────────────────"
echo " 本机   http://127.0.0.1:$PORT/"
echo " 局域网 http://$LANIP:$PORT/"
if [ -n "$URL" ]; then echo " 外网   $URL/"; else echo " 外网   未取到（看 $LOG）"; fi
echo ""
if [ -n "$TOKEN" ]; then
  # 军师 P0 退回：令牌不许明文进终端/日志（会话记录、截图都会带走它）。
  # 带令牌链接只写入 0600 文件，终端只提示位置。
  LINK_FILE="$PROJ/data/review_token_link.txt"
  umask 077
  {
    echo "# 手机直接开这个（点一次换 cookie，之后免输）；生成时间 $(date '+%F %T')"
    if [ -n "$URL" ]; then echo "$URL/?t=$TOKEN"; else echo "(本次未取到外网 URL，局域网) http://$LANIP:$PORT/?t=$TOKEN"; fi
  } > "$LINK_FILE"
  chmod 600 "$LINK_FILE"
  echo " 令牌   已写入 $TOKEN_FILE（不回显）"
  echo " 带令牌链接 → $LINK_FILE （chmod 600，自行查看，勿转发）"
else
  echo " 令牌文件缺失：$TOKEN_FILE"
fi
echo "──────────────────────────────────────────────"
echo " 提示：快速隧道的 URL 每次重启都会变；令牌固定不变。"
echo " 改完代码要重启：bash scripts/serve_remote.sh --restart"
echo " 停隧道：MSYS_NO_PATHCONV=1 taskkill /F /PID \$(netstat -ano | grep cloudflared 查到的PID)"
