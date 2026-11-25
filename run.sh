#!/usr/bin/env bash
# FoolAgent 启动脚本（Linux / macOS）
#   ./run.sh            # 全套检查
#   ./run.sh check      # 只做环境自检
#   ./run.sh test       # 只跑单元测试
#   ./run.sh live       # 只跑真实 API 连通性
#   ./run.sh agent      # 端到端
#   ./run.sh agent "你的任务"
#   ./run.sh chat       # 交互式对话
#   ./run.sh web        # 浏览器对话框（实时显示处理步骤）
set -euo pipefail

# 有 .venv 就用它，避免装进虚拟环境的依赖找不到
cd "$(dirname "$0")"
PY=python3
if [ -x .venv/bin/python3 ]; then
    PY=.venv/bin/python3
fi
exec "$PY" run.py "$@"
