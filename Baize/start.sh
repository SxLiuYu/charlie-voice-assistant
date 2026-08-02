#!/bin/bash
# 白泽 Baize 3.2 启动脚本
cd "$(dirname "$0")"
echo "🦌 启动白泽..."
node dist/cli/bootstrap.js start
