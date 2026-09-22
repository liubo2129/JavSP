"""无终端环境下用于提示和退出的辅助函数"""
import json
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def stream_is_tty(stream):
    """判断 stream 是否连接到了可交互终端"""
    try:
        return stream is not None and stream.isatty()
    except (AttributeError, ValueError):
        return False


def stdin_is_tty():
    return stream_is_tty(sys.stdin)


def stderr_is_tty():
    return stream_is_tty(sys.stderr)


def _applescript_string(s):
    """把普通字符串转成 AppleScript 双引号字符串"""
    return json.dumps(s, ensure_ascii=False)


def show_macos_alert(message, title='JavSP'):
    """在 macOS 上显示原生错误提示框"""
    if sys.platform != 'darwin':
        return False
    script = (
        f'display alert {_applescript_string(title)} '
        f'message {_applescript_string(message)} as critical'
    )
    try:
        subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, timeout=15, check=False,
            text=True, encoding='utf-8', errors='replace')
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def ask_macos_text(message, title='JavSP', default=''):
    """用 macOS 原生输入框读取一行文本；取消时返回空字符串"""
    if sys.platform != 'darwin':
        return default
    script = (
        f'display dialog {_applescript_string(message)} '
        f'default answer {_applescript_string(default)} '
        f'with title {_applescript_string(title)} '
        'buttons {"取消", "确定"} default button "确定" cancel button "取消"'
    )
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, timeout=300, check=False,
            text=True, encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return default
    if result.returncode != 0:
        return ''
    marker = 'text returned:'
    stdout = result.stdout.strip()
    if marker in stdout:
        return stdout.split(marker, 1)[1].strip()
    return ''


def show_macos_notification(message, title='JavSP'):
    """在 macOS 通知中心显示一条通知"""
    if sys.platform != 'darwin':
        return False
    script = (
        f'display notification {_applescript_string(message)} '
        f'with title {_applescript_string(title)}'
    )
    try:
        subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, timeout=10, check=False,
            text=True, encoding='utf-8', errors='replace')
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def show_error_and_exit(message, title='JavSP'):
    """记录错误，必要时显示原生提示框，然后退出"""
    logger.error(message)
    if sys.stderr is not None:
        try:
            print(message, file=sys.stderr)
        except (OSError, ValueError):
            pass
    if sys.platform == 'darwin' and not stderr_is_tty():
        show_macos_alert(message, title)
    sys.exit(1)
