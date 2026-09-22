import os
import sys
from typing import List, Tuple
from cx_Freeze import setup, Executable

# https://github.com/marcelotduarte/cx_Freeze/issues/1288
base = None

proj_root = os.path.abspath(os.path.dirname(__file__))


include_files: List[Tuple[str, str]] = [
    (f'{proj_root}/config.yml', 'config.yml'),
    (f'{proj_root}/data', 'data'),
    (f'{proj_root}/image', 'image')
]

includes = []

for file in os.listdir('javsp/web'):
    name, ext = os.path.splitext(file)
    if ext == '.py':
        includes.append('javsp.web.' + name)

packages = [ 
    'pendulum' # pydantic_extra_types depends on pendulum
]

build_exe = {
    'include_files': include_files,
    'includes': includes,
    'excludes': ['unittest'],
    'packages': packages,
}

if sys.platform == 'win32':
    icon = './image/JavSP.ico'
elif sys.platform == 'darwin':
    icon = './image/JavSP.icns'
else:
    icon = None

javsp = Executable(
    './javsp/__main__.py', 
    target_name='JavSP', 
    base=base,
    icon=icon,
)

options = {'build_exe': build_exe}
if sys.platform == 'darwin':
    # macOS 使用 bdist_mac 生成 JavSP.app；应用图标由 iconfile 指定
    options['bdist_mac'] = {
        'iconfile': './image/JavSP.icns',
        'bundle_name': 'JavSP',
    }

setup(
    name='JavSP',
    options=options,
    executables=[javsp]
)

