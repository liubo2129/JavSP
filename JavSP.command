#!/bin/bash
# JavSP macOS 终端启动器
#
# 双击本文件会通过 Terminal.app 启动 JavSP.app，并保留终端输出。
# 请把本文件放在 JavSP.app 同一目录；从源码构建时也会自动查找 build/JavSP.app。
#
# 默认走命令行刮削（保留终端输出，便于排查）。
# 若要打开桌面界面，用环境变量或直接传参数：
#     JAVSP_GUI=1 ./JavSP.command
#     ./JavSP.command --gui

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

APP=""
for candidate in \
    "$SCRIPT_DIR/JavSP.app" \
    "$SCRIPT_DIR/build/JavSP.app"; do
    if [[ -x "$candidate/Contents/MacOS/JavSP" ]]; then
        APP="$candidate"
        break
    fi
done

if [[ -z "$APP" ]]; then
    /usr/bin/osascript -e \
        'display alert "JavSP" message "未找到 JavSP.app。请将本文件放在 JavSP.app 同一目录。" as critical' \
        >/dev/null 2>&1
    exit 1
fi

export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

# JAVSP_GUI=1 或显式 --gui 时进入桌面界面
if [[ "${JAVSP_GUI:-0}" == "1" && "$#" -eq 0 ]]; then
    exec "$APP/Contents/MacOS/JavSP" --gui
fi

exec "$APP/Contents/MacOS/JavSP" "$@"
