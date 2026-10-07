"""P0.5 spike 的 cx_Freeze 打包配置。

目的不是产出可发布的 App，而是验证 pyobjc/pywebview 能否被可靠地打进 .app，
以及静态资源能否经 resource_path() 解析到。
"""
import os
import sys
from cx_Freeze import setup, Executable

proj_root = os.path.abspath(os.path.dirname(__file__))

build_exe = {
    'include_files': [
        (f'{proj_root}/index.html', 'spike/index.html'),
    ],
    'excludes': ['unittest', 'pytest'],
    # pywebview 的后端与 pyobjc framework 需显式列出：cx_Freeze 的自动探测
    # 对 pyobjc 的动态导入不可靠（这是本 spike 要验证的重点之一）
    'packages': [
        'webview',
        'objc',
        'Foundation',
        'AppKit',
        'WebKit',
        'Quartz',
        'Security',
        'UniformTypeIdentifiers',
    ],
}

executable = Executable(
    'pywebview_spike.py',
    target_name='JavSPSpike',
)

options = {'build_exe': build_exe}
if sys.platform == 'darwin':
    options['bdist_mac'] = {'bundle_name': 'JavSPSpike'}

setup(name='JavSPSpike', options=options, executables=[executable])
