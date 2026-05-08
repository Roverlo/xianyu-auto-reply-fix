#!/bin/bash
# 闲鱼自动回复系统停止脚本

echo "正在停止闲鱼自动回复系统..."

if pgrep -f "Start.py" > /dev/null; then
    pkill -f "Start.py"
    sleep 2

    if pgrep -f "Start.py" > /dev/null; then
        echo "正在强制停止..."
        pkill -9 -f "Start.py"
    fi

    # 清理 lite 扫码登录可能残留的 node 子进程，避免 EPIPE 噪音
    pkill -f "utils/gen_tfstk.js" 2>/dev/null

    echo "已停止"
else
    echo "程序未在运行"
fi
