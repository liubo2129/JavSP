"""并发数设置的读写与校验

``crawler.max_workers`` 有两处入口：config.yml 与 GUI 设置文件。
GUI 的设置会覆盖配置文件，且必须被夹在合理范围内——这个数字直接决定
同时向各站点发多少请求，越界会让整批任务一起被限流。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from javsp.config import Cfg
from javsp.settings import (
    MAX_WORKERS_LIMIT, apply_saved_max_workers, load_max_workers,
    normalize_max_workers, save_max_workers,
)


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """把设置文件指到临时目录，绝不碰用户真实的 ~/Library/...

    同时负责还原 ``Cfg().crawler``：它是进程内单例，
    ``apply_saved_max_workers`` 改了就影响后续所有用例。
    """
    monkeypatch.setenv('JAVSP_SETTINGS_FILE', str(tmp_path / 'settings.json'))
    cfg = Cfg()
    original = cfg.crawler
    try:
        yield
    finally:
        object.__setattr__(cfg, 'crawler', original)


@pytest.mark.parametrize('value, expected', [
    (1, 1),
    (4, 4),
    (MAX_WORKERS_LIMIT, MAX_WORKERS_LIMIT),
    (0, 1),                       # 下界
    (-5, 1),                      # 负数
    (MAX_WORKERS_LIMIT + 10, MAX_WORKERS_LIMIT),   # 上界
    ('3', 3),                     # 前端传字符串
    ('abc', 1),                   # 非法输入回落到默认
    (None, 1),
    ('', 1),
])
def test_normalize_max_workers_clamps(value, expected):
    assert normalize_max_workers(value) == expected


def test_normalize_max_workers_keeps_custom_default():
    """非法输入应回落到调用方给的默认值，而不是硬编码的 1"""
    assert normalize_max_workers('abc', default=4) == 4


def test_load_max_workers_without_saved_setting_uses_default():
    assert load_max_workers(default=1) == 1


def test_save_and_load_round_trip():
    save_max_workers(3)
    assert load_max_workers() == 3


def test_save_max_workers_clamps_and_returns_effective_value():
    # 保存越界值时返回真正落盘的值，便于前端回显
    assert save_max_workers(999) == MAX_WORKERS_LIMIT
    assert load_max_workers() == MAX_WORKERS_LIMIT


def test_save_preserves_other_settings_keys():
    """并发数与站点开关存在同一个文件里，互不覆盖"""
    from javsp.settings import load_crawler_selection, save_crawler_selection

    save_crawler_selection({'normal': ['javdb', 'avsox']})
    save_max_workers(4)

    assert load_crawler_selection()['normal'] == ['javdb', 'avsox']
    assert load_max_workers() == 4

    # 反向再来一次，确认站点写入不会抹掉并发数
    save_crawler_selection({'normal': ['javbus']})
    assert load_max_workers() == 4
    assert load_crawler_selection()['normal'] == ['javbus']


def test_apply_saved_max_workers_overrides_config():
    save_max_workers(3)
    assert apply_saved_max_workers() == 3
    assert Cfg().crawler.max_workers == 3


def test_apply_saved_max_workers_without_setting_keeps_config_value():
    original = int(Cfg().crawler.max_workers)
    assert apply_saved_max_workers() == original
    assert Cfg().crawler.max_workers == original
