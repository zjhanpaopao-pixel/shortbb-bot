#!/usr/bin/env bash
# ShortBB 模拟交易执行器 —— 云服务器上一键更新
# 作用：拉取最新代码 + 重启容器（代码在挂载卷里，restart 即重载，无需重建镜像）
# 用法：./update.sh
set -e
cd "$(dirname "$0")"

echo ">>> [1/2] 拉取最新代码"
git pull

echo ">>> [2/2] 重启执行器容器"
docker compose -f docker-compose.shortbb.yml restart shortbb

echo ">>> 完成。查看实时日志："
echo "    docker compose -f docker-compose.shortbb.yml logs -f"
echo ">>> 提示：如果只改了 .env（密钥/DRY_RUN等环境变量），请用 'up -d' 而不是本脚本："
echo "    docker compose -f docker-compose.shortbb.yml up -d"
