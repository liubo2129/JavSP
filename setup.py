"""cx_Freeze 打包配置。

产出物只包含**一个**可执行文件，它承担三个角色（角色派发见
``javsp/__main__.py`` 顶部）：

    JavSP                 命令行刮削（默认）
    JavSP --gui           桌面窗口
    JavSP --javsp-worker  GUI 拉起的抓取子进程

两个容易踩的点（P0.5 spike 与 P4 实测结论）：

1. ``javsp/webui/`` 是静态前端资源，必须显式列入 ``include_files``。
   ``javsp/lib.py:resource_path()`` 只能解析 exe 目录或 ``Contents/Resources``，
   无法解析冻结后 ``lib/`` 内的模块旁文件。
2. pyobjc 的模块名与 pip 包名**不一致**：pip 包是 ``pyobjc-framework-WebKit``，
   但模块名是 ``WebKit``。用包名写进 ``packages`` 会直接
   ``ImportError: No module named 'pyobjc_framework_WebKit'``。
"""
import os
import sys
from typing import List, Tuple

from cx_Freeze import Executable, setup

# https://github.com/marcelotduarte/cx_Freeze/issues/1288
base = None

proj_root = os.path.abspath(os.path.dirname(__file__))


include_files: List[Tuple[str, str]] = [
    (f'{proj_root}/config.yml', 'config.yml'),
    (f'{proj_root}/data', 'data'),
    (f'{proj_root}/image', 'image'),
    # 桌面界面的前端资源（零构建：HTML/CSS/JS 直接随包分发）。
    # 目标目录用顶层 'webui' 而非 'javsp/webui'：冻结后 javsp 会被打包成
    # lib/javsp.zip，cx_Freeze 会先创建 javsp 目录导致 "Not a directory" 冲突。
    # 运行时由 GuiServer 依次尝试两种布局。
    (f'{proj_root}/javsp/webui', 'webui'),
]

includes = []

for file in os.listdir('javsp/web'):
    name, ext = os.path.splitext(file)
    if ext == '.py':
        includes.append('javsp.web.' + name)

packages = [
    'pendulum',  # pydantic_extra_types depends on pendulum
    # pywebview 及其 macOS 后端在函数内部静态 import 平台模块，
    # 因此整个包一起带上（函数内的 import 也能被 cx_Freeze 追踪）
    'webview',
]

if sys.platform == 'darwin':
    # pyobjc：这里必须写**模块名**而不是 pip 包名，见模块 docstring
    packages += [
        'objc',
        'Foundation',
        'AppKit',
        'WebKit',
        'Quartz',
        'Security',
        'UniformTypeIdentifiers',
    ]

build_exe = {
    'include_files': include_files,
    'includes': includes,
    'excludes': ['unittest', 'pytest'],
    'packages': packages,
}

if sys.platform == 'win32':
    icon = './image/JavSP.ico'
elif sys.platform == 'darwin':
    icon = './image/JavSP.icns'
else:
    icon = None


def _detect_version() -> str:
    """推断版本号

    优先用已安装的发行版元数据；从源码直接构建时没有元数据（包未安装），
    此时退回 pyproject.toml 的占位版本，避免构建失败。
    """
    try:
        import importlib.metadata as meta
        return meta.version('javsp')
    except Exception:  # noqa: BLE001 - 任何异常都不应阻断打包
        pass
    try:
        import tomllib
        with open(f'{proj_root}/pyproject.toml', 'rb') as f:
            data = tomllib.load(f)
        return str(data.get('tool', {}).get('poetry', {}).get('version') or '0.0.0')
    except Exception:  # noqa: BLE001
        return '0.0.0'


_version = _detect_version()

javsp = Executable(
    './javsp/__main__.py',
    target_name='JavSP',
    base=base,
    icon=icon,
)

options = {'build_exe': build_exe}
if sys.platform == 'darwin':
    # macOS 使用 bdist_mac 生成 JavSP.app；应用图标由 iconfile 指定。
    # 注意：cx_Freeze 的选项名是 plist_items（list[tuple[str, str]]），
    # 不是 info_plist —— 后者不存在，会被当成未识别选项。
    options['bdist_mac'] = {
        'iconfile': './image/JavSP.icns',
        'bundle_name': 'JavSP',
        'plist_items': [
            ('NSHighResolutionCapable', True),
            ('CFBundleIdentifier', 'com.javsp.app'),
            ('CFBundleShortVersionString', _version),
            ('CFBundleVersion', _version),
            # 目录选择首选 pywebview 原生面板（不依赖自动化权限）。
            # 这里仍声明 AppleEvents 用途，作为 osascript 兜底路径的提示文案：
            # 缺少该键时 macOS 会直接拒绝自动化请求，且不给用户任何提示。
            ('NSAppleEventsUsageDescription',
             'JavSP 需要控制「访达」以打开文件夹选择窗口。'),
        ],
    }

setup(
    name='JavSP',
    options=options,
    executables=[javsp],
)
