#!/usr/bin/env bash
# ============================================================
#  ShortBB 机器人 · 一键上云脚本（非技术用户专用）
#  功能：装 Docker → 填币安【测试网】密钥 → 后台启动机器人
#  机器人启动后 24 小时常驻，崩溃会自动重启。
# ============================================================
set -e

echo "=================================================="
echo "  ShortBB 机器人 一键上云"
echo "=================================================="

# 必须在仓库目录里运行
if [ ! -f docker-compose.shortbb.yml ]; then
  echo "错误：当前目录找不到 docker-compose.shortbb.yml"
  echo "请先 'cd shortbb-bot' 进入仓库目录，再运行 bash setup.sh"
  exit 1
fi

# 不是 root 就加 sudo
if [ "$EUID" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

# ---------- [1/4] 安装 Docker（已装则跳过）----------
if command -v docker >/dev/null 2>&1; then
  echo "[1/4] Docker 已安装，跳过。"
else
  echo "[1/4] 正在安装 Docker（约 1-2 分钟，请稍候）..."
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker 2>/dev/null || true
  # 确保 docker compose 插件可用（部分系统需单独装）
  if ! $SUDO docker compose version >/dev/null 2>&1; then
    echo "      docker compose 插件缺失，尝试安装..."
    $SUDO apt-get update >/dev/null 2>&1 && $SUDO apt-get install -y docker-compose-plugin >/dev/null 2>&1 \
      || $SUDO yum install -y docker-compose-plugin >/dev/null 2>&1 \
      || echo "      自动安装失败，请手动装 docker compose 插件后重跑本脚本。"
  fi
  echo "      Docker 安装完成。"
fi

# ---------- [2/4] 填写币安【测试网】密钥（只填测试网！）----------
if [ -f .env ]; then
  echo "[2/4] 检测到已有 .env，保留不覆盖。"
else
  echo "[2/4] 需要填写币安【测试网】密钥（注意：是测试网，不是真钱账户！）"
  echo "      来源：桌面文件『币安模拟API key.txt』，只取第 1、2 行（测试网）。"
  echo -n "      粘贴 测试网 API Key："; read -r BB_KEY
  echo -n "      粘贴 测试网 Secret："; read -rs BB_SECRET; echo
  cat > .env <<EOF
BINANCE_TESTNET_API_KEY=${BB_KEY}
BINANCE_TESTNET_SECRET=${BB_SECRET}
DRY_RUN=0
LOOP=1
EOF
  echo "      .env 已生成（密钥只存在这台服务器本地，不会上传到任何地方）。"
fi

# ---------- [3/4] 启动机器人 ----------
echo "[3/4] 后台启动机器人（docker compose up -d）..."
$SUDO docker compose -f docker-compose.shortbb.yml up -d

# ---------- [4/4] 清理：把 clone 时可能带上的令牌从 remote 抹掉 ----------
echo "[4/4] 清理仓库地址里的令牌..."
git remote set-url origin https://github.com/zjhanpaopao-pixel/shortbb-bot.git 2>/dev/null || true

echo "=================================================="
echo "  完成！机器人已在云服务器后台运行。"
echo "  看持仓：登录币安测试网官网（testnet.binancefuture.com）"
echo "  看日志：sudo docker compose -f docker-compose.shortbb.yml logs -f"
echo "  以后改代码后更新：./update.sh"
echo "=================================================="
