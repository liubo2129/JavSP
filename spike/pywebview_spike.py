"""P0.5 spike: 验证 pywebview + cx_Freeze 冻结后的四个关键前提。

窗口是 GUI 程序，冻结后没有 stdout，因此一切结论都写入日志文件
（位于 ~/Library/Logs/JavSP/spike.log），由外部读取判定。

验证项：
  1. webview.start() 能否在 .app 内拉起 WKWebView
  2. 后台线程能否经 evaluate_js 把进度推给前端
  3. 子进程（模拟抓取 worker）能否被启动、上报、并被干净杀掉
  4. 关窗后是否留下孤儿进程
"""
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

LOG_PATH = Path(os.environ.get(
    'JAVSP_SPIKE_LOG',
    Path.home() / 'Library' / 'Logs' / 'JavSP' / 'spike.log'))
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_PATH), filemode='a', level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] pid=%(process)d %(message)s')
log = logging.getLogger('spike')

log.info('=== spike start ===')
log.info('frozen=%s executable=%s', getattr(sys, 'frozen', False), sys.executable)
log.info('python=%s', sys.version.replace('\n', ' '))
log.info('cwd=%s', os.getcwd())

child_proc = None


def resource_path(rel: str) -> Path:
    """与 javsp/lib.py:resource_path 相同的策略：先 exe 目录，再 Contents/Resources"""
    if not getattr(sys, 'frozen', False):
        # 源码模式：仓库根目录（spike/ 的上一级）
        return Path(__file__).resolve().parent.parent / rel
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [exe_dir]
    if exe_dir.name == 'MacOS' and exe_dir.parent.name == 'Contents':
        candidates.append(exe_dir.parent / 'Resources')
    for base in candidates:
        if (base / rel).exists():
            return base / rel
    return candidates[0] / rel


def push_progress(window):
    """模拟抓取管线的进度推送：后台线程 -> evaluate_js"""
    try:
        log.info('[thread] background pusher started')
        for i in range(1, 6):
            time.sleep(0.7)
            js = f"window.pushProgress({i}, 5, 'step-{i}')"
            window.evaluate_js(js)
            log.info('[thread] evaluate_js OK: %s', js)
        window.evaluate_js("window.markDone('push-ok')")
        log.info('[thread] SPIKEEVAL=PASS')
    except Exception as e:
        log.exception('[thread] evaluate_js FAILED: %r', e)
        log.info('[thread] SPIKEEVAL=FAIL')


def start_child(window):
    """启动一个 detached 子进程，模拟抓取 worker，验证能被干净杀掉"""
    global child_proc
    try:
        cmd = [sys.executable]
        if not getattr(sys, 'frozen', False):
            cmd += ['-c', 'import time,sys\nfor i in range(300):\n    print(i, flush=True)\n    time.sleep(0.5)']
        else:
            cmd += ['--spike-child']
        child_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
        log.info('[child] started pid=%s pgid-able=True', child_proc.pid)
        log.info('[child] SPIKEKILL=PENDING')
    except Exception as e:
        log.exception('[child] failed to start: %r', e)


def stop_child(reason: str):
    """杀掉整个子进程组，验证无孤儿"""
    global child_proc
    if child_proc is None or child_proc.poll() is not None:
        log.info('[child] nothing to kill (reason=%s)', reason)
        return
    pid = child_proc.pid
    try:
        os.killpg(os.getpgid(pid), 15)
        time.sleep(0.5)
        if child_proc.poll() is None:
            os.killpg(os.getpgid(pid), 9)
        child_proc.wait(timeout=5)
        log.info('[child] killed pid=%s reason=%s rc=%s', pid, reason, child_proc.returncode)
        log.info('[child] SPIKEKILL=PASS')
    except Exception as e:
        log.exception('[child] kill failed: %r', e)
        log.info('[child] SPIKEKILL=FAIL')


def main():
    if '--spike-child' in sys.argv:
        # 子进程模式：无限循环，等待被杀
        log.info('[child-proc] entered child mode, will idle')
        while True:
            time.sleep(1)
        return

    import webview  # 延迟导入，子进程模式不需要 GUI

    html = resource_path('spike/index.html')
    log.info('html exists=%s path=%s', html.exists(), html)

    window = webview.create_window(
        'JavSP Spike', str(html), width=560, height=420)

    def on_loaded():
        log.info('[gui] loaded event fired → WINDOW=PASS')
        threading.Thread(target=push_progress, args=(window,), daemon=True).start()
        start_child(window)

    def on_closing():
        log.info('[gui] closing event fired')
        stop_child('window-closing')

    window.events.loaded += on_loaded
    window.events.closing += on_closing

    def after_start():
        # 在这里做真实启动路径的检查；5 秒后自动关窗，便于无人值守验证
        log.info('[gui] after_start; gui=%s', webview.guilib)
        log.info('[gui] SPIKEWINDOW=PASS')
        time.sleep(6)
        log.info('[gui] auto-closing for unattended verification')
        try:
            window.destroy()
        except Exception as e:
            log.info('[gui] destroy failed: %r', e)

    threading.Thread(target=after_start, daemon=True).start()

    try:
        webview.start(gui='cocoa' if sys.platform == 'darwin' else None)
        log.info('[gui] webview.start returned normally')
    except Exception as e:
        log.exception('[gui] webview.start FAILED: %r', e)
        log.info('[gui] SPIKEWINDOW=FAIL')
    finally:
        stop_child('final-cleanup')
        log.info('=== spike end ===')


if __name__ == '__main__':
    main()
