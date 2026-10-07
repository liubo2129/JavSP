"""父进程侧的事件消费验证：启动 worker 子进程并实时解析 NDJSON 事件流。

用法::

    python tools/consume_events.py /path/to/videos [--raw]

这也是 GUI 进程要做的原型：spawn worker -> 按行读 stdout -> 解析成 Event。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ_ROOT))

from javsp.events import Event, read_events  # noqa: E402


def build_command(root: str) -> list[str]:
    """构造 worker 启动命令

    复用项目既有的 confz CLI 注入方式（``--o<key>``），而不是新增参数解析，
    这样 worker 无需自己的参数处理逻辑。
    """
    return [
        sys.executable, '-m', 'javsp.worker',
        '--oscanner.input_directory', root,
        '--oscanner.manual', 'false',
        '--osummarizer.move_files', 'false',
        '--osummarizer.extra_fanarts.enabled', 'false',
        '--oother.check_update', 'false',
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('root', help='要扫描的影片目录')
    parser.add_argument('--raw', action='store_true', help='额外打印原始 NDJSON 行')
    args = parser.parse_args()

    env = os.environ.copy()
    # 必须设置：否则 __main__ 的 _should_run_in_background() 会再派生一层子进程
    env['JAVSP_BACKGROUND_WORKER'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONPATH'] = str(PROJ_ROOT)

    cmd = build_command(os.path.abspath(args.root))
    print('[*] spawn:', ' '.join(cmd), file=sys.stderr)
    t0 = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace',
        env=env, cwd=str(PROJ_ROOT),
    )

    # stderr 单独一个线程消费，避免管道写满导致 worker 阻塞
    stderr_lines: list[str] = []

    def drain():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    threading.Thread(target=drain, daemon=True).start()

    # 逐行解析：这就是 GUI 侧要做的实时消费
    counts: dict[str, int] = {}
    assert proc.stdout is not None
    for event in read_events(proc.stdout):
        counts[event.kind] = counts.get(event.kind, 0) + 1
        p = event.payload
        if event.kind == 'crawler.succeeded':
            detail = f"{p.get('crawler')} OK title={(p.get('title') or '')[:34]}"
        elif event.kind == 'crawler.retry':
            detail = f"{p.get('crawler')} retry {p.get('attempt')}/{p.get('total')}"
        elif event.kind == 'crawler.failed':
            detail = f"{p.get('crawler')} FAIL {p.get('error')}"
        elif event.kind in ('movie.started', 'movie.finished', 'movie.failed'):
            detail = f"{p.get('movie_id')} [{p.get('index')}/{p.get('total')}]" + (
                f" step={p.get('step')} error={p.get('error')}" if event.kind == 'movie.failed' else '')
        elif event.kind == 'movie.step':
            detail = f"{p.get('step')} ({p.get('step_index')}/{p.get('step_total')})"
        elif event.kind == 'scan.finished':
            detail = f"识别到 {p.get('movie_count')} 部: {p.get('movies')}"
        elif event.kind == 'run.finished':
            detail = f"完成 {p.get('finished_count')}/{p.get('movie_count')}"
        else:
            detail = str(p)[:100]
        print(f"[{time.time()-t0:6.2f}s] {event.kind:22} {detail}", flush=True)
        if args.raw:
            print('        raw:', event.to_json()[:160], file=sys.stderr)

    rc = proc.wait(timeout=60)
    elapsed = time.time() - t0
    print(f"\n[*] worker 退出码={rc} 用时={elapsed:.1f}s", file=sys.stderr)
    print('[*] 事件统计:', dict(sorted(counts.items())), file=sys.stderr)
    if rc != 0 and stderr_lines:
        print('[*] worker stderr 尾部:', file=sys.stderr)
        for line in stderr_lines[-8:]:
            print('    ' + line.rstrip(), file=sys.stderr)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
