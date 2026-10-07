"""用户级设置的读写。

为什么不直接改 config.yml
------------------------

``config.yml`` 带了大量中文注释，它同时充当"默认值"和"文档"。用 PyYAML
回写会把这些注释全部丢掉（``ruamel.yaml`` 能保留注释，但项目没有这个依赖，
为一个开关引入它不划算）。因此 GUI 里的设置单独存成 JSON：

* ``config.yml``   —— 出厂默认值 + 文档，只读
* ``settings.json`` —— 用户显式改过的部分，缺失的键回落到默认值

这样也带来两个好处：升级时新增的配置项不会被旧设置覆盖；删掉这个文件即可
恢复到出厂状态。

存放位置跟随 ``config.py:get_macos_user_config()`` 的约定（macOS 放
``~/Library/Application Support/JavSP/``），方便用户集中找到。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from javsp.config import CrawlerID

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = 'settings.json'
SETTINGS_VERSION = 1

# 抓取器分组：用 data_src 作为键，与 Cfg().crawler.selection 一一对应
GROUP_KEYS = ('normal', 'fc2', 'cid', 'getchu', 'gyutto')

GROUP_LABELS = {
    'normal': '普通番号',
    'fc2': 'FC2',
    'cid': 'CID（DMM 内容 ID）',
    'getchu': 'Getchu',
    'gyutto': 'Gyutto',
}

# 各分组的默认站点与顺序。取自 config.yml 的出厂配置；
# 未被 config.yml 引用的站点（avwiki / njav / arzon / arzon_iv / fc2fan）
# 一并列出但默认关闭，让用户有办法启用它们。
DEFAULT_SELECTION: Dict[str, List[str]] = {
    'normal': ['airav', 'avsox', 'javbus', 'javdb', 'javlib', 'jav321',
               'mgstage', 'prestige'],
    'fc2': ['fc2', 'avsox', 'javdb', 'javmenu', 'fc2ppvdb'],
    'cid': ['fanza'],
    'getchu': ['dl_getchu'],
    'gyutto': ['gyutto'],
}

# 从未被 config.yml 引用、因而默认关闭的站点 -> 归入哪个分组
ORPHAN_CRAWLERS: Dict[str, str] = {
    'njav': 'normal',
    'avwiki': 'normal',
    'arzon': 'normal',
    'arzon_iv': 'normal',
    'fc2fan': 'fc2',
}


def settings_dir() -> Path:
    """设置文件所在目录（跟随 config.py 的用户配置约定）"""
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'JavSP'
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA')
        return Path(base) / 'JavSP' if base else Path.home() / 'AppData' / 'Roaming' / 'JavSP'
    return Path(os.environ.get('XDG_CONFIG_HOME') or (Path.home() / '.config')) / 'JavSP'


def settings_path() -> Path:
    """设置文件路径

    可用 ``JAVSP_SETTINGS_FILE`` 覆盖，便于测试与多环境隔离
    （GUI 拉起 worker 时会显式传这个变量，保证父子进程读写同一份设置）。
    """
    override = os.environ.get('JAVSP_SETTINGS_FILE')
    if override:
        return Path(override).expanduser()
    return settings_dir() / SETTINGS_FILENAME


def _all_crawlers() -> List[str]:
    return [c.value for c in CrawlerID]


def to_enabled(spec: Any) -> List[str]:
    """把任意输入规范成"启用的站点 id 列表"

    容忍三种写法，便于接口与手工编辑：

    * ``['javdb', 'avsox']``          直接给列表
    * ``{'javdb': True, 'avsox': False}``  给开关字典
    * ``None``                         视为空
    未认识的站点会被丢弃。
    """
    known = set(_all_crawlers())
    if spec is None:
        return []
    if isinstance(spec, dict):
        candidates = [k for k, v in spec.items() if v]
    elif isinstance(spec, (list, tuple, set)):
        candidates = list(spec)
    else:
        return []
    out: List[str] = []
    for item in candidates:
        name = str(item)
        if name in known and name not in out:
            out.append(name)
    return out


def load_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取用户设置；文件缺失或损坏时返回空设置（不抛异常）"""
    target = Path(path) if path else settings_path()
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        logger.warning('设置文件无法解析，将使用默认值: %s (%s)', target, e)
        return {}
    if not isinstance(data, dict):
        logger.warning('设置文件内容不是对象，将使用默认值: %s', target)
        return {}
    return data


def save_settings(settings: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """原子写入设置文件

    先写临时文件再 ``os.replace``：中途失败不会留下半截文件，
    避免下次启动读到损坏的设置。
    """
    target = Path(path) if path else settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(settings)
    payload.setdefault('version', SETTINGS_VERSION)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + '\n'

    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix='.settings-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # 任何失败都要清掉临时文件，别在用户目录留垃圾
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def load_crawler_selection() -> Dict[str, List[str]]:
    """读取用户保存的抓取器选择；未保存过的分组不出现在结果里"""
    raw = load_settings().get('crawler_selection')
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for group in GROUP_KEYS:
        if group in raw:
            enabled = to_enabled(raw[group])
            if enabled:
                out[group] = enabled
    return out


def load_saved_factory_baseline() -> Dict[str, List[str]]:
    """读取保存设置那一刻的出厂基线（用于识别出厂新增站点）"""
    raw = load_settings().get('crawler_selection_factory')
    if not isinstance(raw, dict):
        return {}
    return {group: to_enabled(raw.get(group)) for group in GROUP_KEYS if group in raw}


def save_crawler_selection(selection: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """保存抓取器选择（只写这一个键，其它设置保持不变）

    同时记录一份**出厂基线快照**：升级后靠它算出"哪些站点是出厂新增的"，
    从而只自动补上真正的新站点，而不会把用户主动禁用的站点又塞回来。
    """
    normalized = {
        group: to_enabled(selection.get(group))
        for group in GROUP_KEYS
        if to_enabled(selection.get(group))
    }
    settings = load_settings(path)
    settings['crawler_selection'] = normalized
    settings['crawler_selection_factory'] = default_enabled_by_group()
    return save_settings(settings, path)


def normalize_selection(selection: Dict[str, Any]) -> Dict[str, List[str]]:
    """把用户输入规范成完整的分组字典（供接口校验与回显）"""
    return {group: to_enabled(selection.get(group)) for group in GROUP_KEYS}


def merge_into_config(cfg_selection: Any,
                      enabled_by_group: Dict[str, Iterable[str]]) -> None:
    """把启用的站点写回 ``Cfg().crawler.selection``

    **用户保存的列表就是最终名单**，不能把用户主动禁用的站点再塞回来
    （否则界面上的开关形同虚设）。

    唯一例外是"出厂新增"——升级后出厂基线多了站点，而用户的设置文件里
    没有它（保存时它还不存在）。判据不靠猜，而是比对保存时记下的出厂
    基线快照（``crawler_selection_factory``）：

        出厂新增 = 当前出厂基线 - 保存时的出厂基线

    没有快照（老版本保存的设置）时退回用当前 config.yml 的列表当基线。

    顺序：用户保存的顺序在前，自动补上的出厂新增排在末尾
    （``selection`` 的顺序即数据优先级）。

    confz 的配置模型是 **frozen** 的（pydantic frozen=True），不能直接
    ``setattr``。这里自内向外重建：``CrawlerSelect.model_copy(update=...)``
    → ``Crawler.model_copy(update=...)`` → 用 ``object.__setattr__``
    把新的 crawler 挂回根 Cfg 单例。因为 worker 在
    ``import_crawlers()`` 之前一次性完成改写，运行期不会再变。

    Args:
        cfg_selection: ``Cfg().crawler.selection`` 对象
        enabled_by_group: ``{组: 启用的站点 id}``，仅包含用户显式保存过的组
    """
    from javsp.config import Cfg

    baseline = load_saved_factory_baseline()
    factory_now = default_enabled_by_group()

    updates: Dict[str, List[CrawlerID]] = {}
    for group in GROUP_KEYS:
        enabled = list(enabled_by_group.get(group) or [])
        if not enabled:
            # 用户没保存过这一组 -> 完整沿用 config.yml
            continue
        if group in baseline:
            known_then = set(baseline[group])
        else:
            # 没有快照：拿当前配置当近似基线
            try:
                known_then = {c.value for c in cfg_selection[group]}
            except Exception:  # noqa: BLE001
                known_then = set()
        additions = [n for n in factory_now.get(group, []) if n not in known_then]

        merged = list(enabled)
        for name in additions:
            if name not in merged:
                merged.append(name)
        updates[group] = [CrawlerID(name) for name in merged]

    if not updates:
        return

    new_selection = cfg_selection.model_copy(update=updates)
    cfg = Cfg()
    new_crawler = cfg.crawler.model_copy(update={'selection': new_selection})
    # 根对象也是 frozen 的，用 object.__setattr__ 绕过校验直接替换
    object.__setattr__(cfg, 'crawler', new_crawler)


def all_crawlers_by_group() -> Dict[str, List[str]]:
    """出厂状态下的完整站点分布（含孤儿项），供界面列出全部 19 个站点"""
    out: Dict[str, List[str]] = {g: list(DEFAULT_SELECTION.get(g, [])) for g in GROUP_KEYS}
    for name, group in ORPHAN_CRAWLERS.items():
        if name not in out[group]:
            out[group].append(name)
    return out


def default_enabled_by_group() -> Dict[str, List[str]]:
    """出厂默认启用状态（孤儿项默认关闭）"""
    return {g: list(v) for g, v in DEFAULT_SELECTION.items()}
