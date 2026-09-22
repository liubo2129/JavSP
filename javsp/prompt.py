import sys

from javsp.config import Cfg
from javsp.ui import ask_macos_text, show_error_and_exit, stdin_is_tty


def prompt(message: str, what: str) -> str:
    """读取用户输入。

    终端环境下使用 input()；macOS 上从 Finder 启动时使用原生输入框。
    """
    if Cfg().other.interactive:
        if stdin_is_tty():
            return input(message)
        if sys.platform == 'darwin':
            return ask_macos_text(message, title='JavSP')
        show_error_and_exit(
            f'JavSP 需要交互输入（{what}），但当前没有可用的终端或图形界面。')
    show_error_and_exit(f'缺少{what}')
