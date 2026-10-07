"""禁止 pytest 收集本目录

spike/setup.py 是独立的 cx_Freeze 构建脚本，在导入时会调用 setup()
（依赖不在项目依赖里），被 pytest 收集会报错。本目录只是验证脚手架。
"""
collect_ignore = ['setup.py', 'pywebview_spike.py']
