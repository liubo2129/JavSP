"""P0.5 阶段验证 pywebview + cx_Freeze 的最小工程。

这里的文件是**独立的验证脚手架**，不属于 javsp 包，也不参与打包与测试：

* ``pywebview_spike.py``  —— 一个最小窗口：验证 window.start() 能在 .app 内
  拉起 WKWebView、后台线程能经 evaluate_js 推送、关窗能杀掉子进程组
* ``index.html``          —— 上面那个窗口的页面
* ``setup.py``            —— 它自己的 cx_Freeze 配置（注意不是仓库根目录那个）

维护说明：
* 这些结论已固化在上层代码里（``javsp/gui.py`` 的对话框与关窗清理、
  仓库 ``setup.py`` 的 pyobjc 打包配置、CI 的冻结产物冒烟测试），
  所以本目录主要用于**复现与回归**，改动上方代码前可以先跑一遍。
* 构建方式：``cd spike && ../.venv/bin/python setup.py bdist_mac``
  产物在 ``spike/build/``，已在 .gitignore 中忽略。
"""
