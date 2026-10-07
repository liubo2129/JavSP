"""javsp/settings.py 的单元测试

设置文件是用户可见状态，容错性要求高：读坏了要回落默认、写失败不能留半个文件、
未认识的站点必须被忽略。这些行为都用测试固定下来。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from javsp.config import CrawlerID
from javsp.settings import (
    DEFAULT_SELECTION, GROUP_KEYS, ORPHAN_CRAWLERS, all_crawlers_by_group,
    default_enabled_by_group, load_crawler_selection, load_settings,
    merge_into_config, normalize_selection, save_crawler_selection,
    save_settings, settings_path, to_enabled,
)


@pytest.fixture
def cfg_root(tmp_path, monkeypatch):
    """每个用例用独立的设置文件"""
    target = tmp_path / 'settings.json'
    monkeypatch.setenv('JAVSP_SETTINGS_FILE', str(target))
    return target


# ------------------------------ 输入规范化 ------------------------------ #

@pytest.mark.parametrize('spec, expected', [
    (['javdb', 'avsox'], ['javdb', 'avsox']),
    ({'javdb': True, 'avsox': False}, ['javdb']),
    ({'avsox': True, 'javdb': True}, ['avsox', 'javdb']),
    (None, []),
    ([], []),
    ('javdb', []),                      # 非列表/字典一律视为空
    (['javdb', 'javdb'], ['javdb']),    # 去重
    (['javdb', '不存在的站点'], ['javdb']),  # 未知站点被丢弃
])
def test_to_enabled_normalizes(spec, expected):
    assert to_enabled(spec) == expected


# -------------------------------- 读写 -------------------------------- #

def test_load_settings_missing_file_returns_empty(cfg_root):
    assert not cfg_root.exists()
    assert load_settings() == {}


def test_save_then_load_round_trip(cfg_root):
    save_settings({'foo': 'bar', 'crawler_selection': {'normal': ['javdb']}})
    assert load_settings() == {
        'foo': 'bar', 'crawler_selection': {'normal': ['javdb']}, 'version': 1}


def test_load_settings_tolerates_broken_json(cfg_root):
    cfg_root.parent.mkdir(parents=True, exist_ok=True)
    cfg_root.write_text('{ 这不是合法 JSON', encoding='utf-8')
    # 关键：不能抛异常，否则整个界面会起不来
    assert load_settings() == {}


def test_load_settings_tolerates_non_object(cfg_root):
    cfg_root.parent.mkdir(parents=True, exist_ok=True)
    cfg_root.write_text('[1, 2, 3]', encoding='utf-8')
    assert load_settings() == {}


def test_save_is_atomic_no_leftover_temp_files(cfg_root):
    save_settings({'a': 1})
    save_settings({'a': 2})
    leftovers = [p.name for p in cfg_root.parent.iterdir() if p.name.startswith('.settings-')]
    assert leftovers == []
    assert load_settings()['a'] == 2


def test_settings_path_honours_env_override(cfg_root):
    assert settings_path() == cfg_root


# ---------------------------- 抓取器选择 ---------------------------- #

def test_load_crawler_selection_empty_when_unset(cfg_root):
    assert load_crawler_selection() == {}


def test_save_and_load_crawler_selection(cfg_root):
    save_crawler_selection({'normal': ['javdb', 'avsox'], 'fc2': ['fc2']})
    assert load_crawler_selection() == {'normal': ['javdb', 'avsox'], 'fc2': ['fc2']}


def test_save_crawler_selection_does_not_clobber_other_keys(cfg_root):
    save_settings({'crawler_selection': {'normal': ['javdb']}, 'other_setting': 42})
    save_crawler_selection({'normal': ['avsox']})
    data = load_settings()
    assert data['other_setting'] == 42
    assert data['crawler_selection']['normal'] == ['avsox']


def test_save_crawler_selection_drops_empty_and_unknown_groups(cfg_root):
    save_crawler_selection({
        'normal': ['javdb'],
        'fc2': [],                  # 空组不写入，便于回落到默认
        'bogus_group': ['javdb'],   # 未知组直接忽略
    })
    saved = load_crawler_selection()
    assert saved == {'normal': ['javdb']}


def test_normalize_selection_always_returns_all_groups():
    out = normalize_selection({'normal': ['javdb']})
    assert set(out) == set(GROUP_KEYS)
    assert out['normal'] == ['javdb']
    assert out['cid'] == []


# --------------------------- 合并进配置 --------------------------- #

def _fresh_selection(mapping):
    """构造一个真实的 CrawlerSelect（frozen pydantic 模型）

    不用假对象：merge_into_config 依赖 model_copy，且真实模型的 frozen
    语义正是这里要覆盖的行为。
    """
    from javsp.config import CrawlerSelect

    return CrawlerSelect(
        normal=[CrawlerID(n) for n in mapping.get('normal', [])],
        fc2=[CrawlerID(n) for n in mapping.get('fc2', [])],
        cid=[CrawlerID(n) for n in mapping.get('cid', [])],
        getchu=[CrawlerID(n) for n in mapping.get('getchu', [])],
        gyutto=[CrawlerID(n) for n in mapping.get('gyutto', [])],
    )


def _names(values):
    return [c.value for c in values]


@pytest.fixture
def singleton_cfg():
    """真实 Cfg 单例；merge_into_config 会改写它，用完恢复原状

    这些用例刻意用真实配置模型而不是假对象：confz 的模型是 frozen 的，
    用假对象测不出 model_copy 路径。
    """
    from javsp.config import Cfg

    cfg = Cfg()
    original_crawler = cfg.crawler
    yield cfg
    object.__setattr__(cfg, 'crawler', original_crawler)


def test_merge_into_config_puts_user_order_first(singleton_cfg):
    from javsp.config import Cfg

    # 当前 config 与出厂基线一致（含 jav321 等）
    sel = _fresh_selection({'normal': DEFAULT_SELECTION['normal']})
    object.__setattr__(Cfg(), 'crawler', Cfg().crawler.model_copy(update={'selection': sel}))

    merge_into_config(Cfg().crawler.selection, {'normal': ['javdb', 'avsox']})
    # 用户列表就是最终名单：只有他们选的，且按他们的顺序
    assert _names(Cfg().crawler.selection.normal) == ['javdb', 'avsox']


def test_merge_into_config_actually_disables_sites(singleton_cfg):
    """核心语义：用户没选的站点必须真的消失

    这是本功能存在的意义——早先的实现把整个出厂列表又追加回去，
    界面上的开关等于无效。
    """
    from javsp.config import Cfg

    sel = _fresh_selection({'normal': DEFAULT_SELECTION['normal']})
    object.__setattr__(Cfg(), 'crawler', Cfg().crawler.model_copy(update={'selection': sel}))

    # 只留 jav321：其余 7 个都必须被移除
    merge_into_config(Cfg().crawler.selection, {'normal': ['jav321']})
    assert _names(Cfg().crawler.selection.normal) == ['jav321']


def test_merge_into_config_keeps_untouched_groups(singleton_cfg):
    from javsp.config import Cfg

    sel = _fresh_selection({'normal': ['javdb'], 'fc2': ['fc2'],
                            'cid': ['fanza'], 'getchu': ['dl_getchu'],
                            'gyutto': ['gyutto']})
    object.__setattr__(Cfg(), 'crawler', Cfg().crawler.model_copy(update={'selection': sel}))

    merge_into_config(Cfg().crawler.selection, {'normal': ['javdb']})
    # 用户没提到的组保持原样
    assert _names(Cfg().crawler.selection.cid) == ['fanza']


def test_merge_into_config_can_enable_orphan_crawler(singleton_cfg):
    """孤儿抓取器出厂不在列表里，用户启用后必须真的被加入"""
    from javsp.config import Cfg

    sel = _fresh_selection({'normal': DEFAULT_SELECTION['normal'],
                            'fc2': DEFAULT_SELECTION['fc2'],
                            'cid': DEFAULT_SELECTION['cid'],
                            'getchu': DEFAULT_SELECTION['getchu'],
                            'gyutto': DEFAULT_SELECTION['gyutto']})
    object.__setattr__(Cfg(), 'crawler', Cfg().crawler.model_copy(update={'selection': sel}))

    merge_into_config(Cfg().crawler.selection, {'normal': ['njav', 'javdb']})
    # njav 是孤儿，用户显式启用后必须出现，且保持用户给的顺序
    assert _names(Cfg().crawler.selection.normal) == ['njav', 'javdb']


def test_merge_into_config_ignores_empty_group(singleton_cfg):
    from javsp.config import Cfg

    sel = _fresh_selection({'normal': ['javdb'], 'fc2': ['fc2'],
                            'cid': ['fanza'], 'getchu': ['dl_getchu'],
                            'gyutto': ['gyutto']})
    object.__setattr__(Cfg(), 'crawler', Cfg().crawler.model_copy(update={'selection': sel}))

    merge_into_config(Cfg().crawler.selection, {'normal': []})
    assert _names(Cfg().crawler.selection.normal) == ['javdb']


def test_merge_into_config_appends_factory_new_site(singleton_cfg):
    """升级场景：出厂基线新增了站点，而配置还是旧的 -> 自动补上，不能丢

    判据是"出厂基线有、当前配置没有"。这种站点此前不存在，
    用户不可能主动禁用。
    """
    from javsp.config import Cfg

    # 模拟旧配置：normal 里少了出厂基线的 prestige
    old = [n for n in DEFAULT_SELECTION['normal'] if n != 'prestige']
    sel = _fresh_selection({'normal': old})
    object.__setattr__(Cfg(), 'crawler', Cfg().crawler.model_copy(update={'selection': sel}))

    merge_into_config(Cfg().crawler.selection, {'normal': ['jav321']})
    names = _names(Cfg().crawler.selection.normal)
    assert names[0] == 'jav321'
    assert 'prestige' in names, '出厂新增的站点被静默丢弃了'


# --------------------------- 端到端：设置生效 --------------------------- #

def test_settings_reach_cfg_via_worker_helper(cfg_root, monkeypatch):
    """集成：保存的设置能被 worker 的 _apply_user_settings 应用到 Cfg()

    这是本功能的关键链路（设置界面 -> settings.json -> worker -> 实际抓取），
    单独测各层都通过、链路断了的情况最容易漏掉。
    """
    from javsp.config import Cfg
    from javsp.worker import _apply_user_settings
    from javsp.events import CollectorSink

    monkeypatch.setenv('JAVSP_SETTINGS_FILE', str(cfg_root))
    save_crawler_selection({'normal': ['jav321']})

    cfg = Cfg()
    original = cfg.crawler
    try:
        sink = CollectorSink()
        _apply_user_settings(sink)
        normal = [c.value for c in Cfg().crawler.selection.normal]
        # 用户列表就是最终名单：其余站点必须真的被禁用
        assert normal == ['jav321'], normal
        # 上报了事件，便于界面/日志确认设置已生效
        assert 'settings.applied' in sink.kinds()
    finally:
        object.__setattr__(cfg, 'crawler', original)


def test_no_settings_file_leaves_config_untouched(cfg_root, monkeypatch):
    """没有设置文件时不该改动任何东西（出厂行为必须与从前一致）"""
    from javsp.config import Cfg
    from javsp.worker import _apply_user_settings
    from javsp.events import CollectorSink

    monkeypatch.setenv('JAVSP_SETTINGS_FILE', str(cfg_root))
    cfg = Cfg()
    original = cfg.crawler
    before = [c.value for c in cfg.crawler.selection.normal]
    try:
        sink = CollectorSink()
        _apply_user_settings(sink)
        assert [c.value for c in Cfg().crawler.selection.normal] == before
        assert sink.kinds() == []
    finally:
        object.__setattr__(cfg, 'crawler', original)

def test_all_crawlers_by_group_covers_every_crawler():
    """界面要能列出全部站点，一个都不能漏

    注意同一个站点**允许**出现在多个分组（avsox / javdb 同时支持普通番号与
    FC2），所以这里只断言覆盖性，不断言唯一性。
    """
    by_group = all_crawlers_by_group()
    flat = [name for names in by_group.values() for name in names]
    assert set(flat) == {c.value for c in CrawlerID}


def test_all_crawlers_by_group_has_no_duplicates_within_a_group():
    for group, names in all_crawlers_by_group().items():
        assert len(names) == len(set(names)), f'{group} 组内出现重复站点'


def test_cross_group_crawlers_are_the_expected_ones():
    """跨分组的站点是刻意安排的，固定下来避免无意中扩大范围"""
    counts = {}
    for names in all_crawlers_by_group().values():
        for n in names:
            counts[n] = counts.get(n, 0) + 1
    assert {n for n, c in counts.items() if c > 1} == {'avsox', 'javdb'}


def test_default_enabled_excludes_orphans():
    enabled = default_enabled_by_group()
    flat = {n for names in enabled.values() for n in names}
    assert flat.isdisjoint(ORPHAN_CRAWLERS), '孤儿抓取器必须默认关闭'


def test_default_selection_only_uses_known_crawlers():
    known = {c.value for c in CrawlerID}
    for group, names in DEFAULT_SELECTION.items():
        assert group in GROUP_KEYS
        assert set(names) <= known


def test_orphan_groups_are_valid():
    assert set(ORPHAN_CRAWLERS.values()) <= set(GROUP_KEYS)
