"""影片目录图片收集的单元测试

界面详情气泡靠 _collect_gallery 决定展示哪些图片、按什么顺序，
分类或排序错了会直接体现为"图片缺失/顺序错乱"，因此固定下来。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from PIL import Image

from javsp.server import _collect_gallery, _image_kind, _numeric_prefix


def _img(path: Path, size=(20, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, (60, 80, 120)).save(path)


def test_collect_gallery_classifies_poster_and_fanart(tmp_path):
    _img(tmp_path / 'poster.jpg')
    _img(tmp_path / 'fanart.jpg', (40, 20))
    g = _collect_gallery(tmp_path, 'ABP-647')
    assert Path(g['poster']).name == 'poster.jpg'
    assert Path(g['fanart']).name == 'fanart.jpg'
    assert g['stills'] == []


def test_collect_gallery_collects_extrafanart(tmp_path):
    for i in range(12):
        _img(tmp_path / 'extrafanart' / f'{i}.png')
    g = _collect_gallery(tmp_path, 'ABP-647')
    names = [Path(p).name for p in g['stills']]
    assert len(names) == 12
    # 关键：按数值排序，10 必须排在 9 之后（字典序会错成 "10" < "9"）
    assert names[0] == '0.png'
    assert names[9] == '9.png'
    assert names[10] == '10.png'
    assert names[11] == '11.png'


def test_collect_gallery_ignores_non_images(tmp_path):
    _img(tmp_path / 'poster.jpg')
    (tmp_path / 'movie.nfo').write_text('<movie/>', encoding='utf-8')
    (tmp_path / 'ABP-647.mp4').write_bytes(b'x')
    (tmp_path / 'notes.txt').write_text('hi', encoding='utf-8')
    g = _collect_gallery(tmp_path, 'ABP-647')
    all_paths = [g['poster']] + g['stills'] + g['other']
    assert all(p and Path(p).suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')
               for p in all_paths if p)
    assert len(g['other']) == 0


def test_collect_gallery_falls_back_to_fanart_when_no_poster(tmp_path):
    """没有竖版海报时用封面顶上，保证界面至少有一张主图"""
    _img(tmp_path / 'fanart.jpg', (40, 20))
    g = _collect_gallery(tmp_path, 'ABP-647')
    assert g['poster'] == g['fanart']


def test_collect_gallery_handles_missing_dir(tmp_path):
    g = _collect_gallery(tmp_path / 'not-exist', 'ABP-647')
    assert g == {'poster': None, 'fanart': None, 'stills': [], 'other': []}


def test_collect_gallery_accepts_stem_prefixed_images(tmp_path):
    """自定义命名（如 ABP-647-poster.jpg）也应被识别"""
    _img(tmp_path / 'ABP-647-poster.jpg')
    g = _collect_gallery(tmp_path, 'ABP-647')
    assert g['poster'] is not None


def test_collect_gallery_does_not_take_unrelated_images(tmp_path):
    """无关图片（既不叫 poster/fanart，也不以番号开头）不应混进来"""
    _img(tmp_path / 'poster.jpg')
    _img(tmp_path / 'screenshot-2024.png')
    g = _collect_gallery(tmp_path, 'ABP-647')
    assert 'screenshot' not in ' '.join(g['other'])


@pytest.mark.parametrize('name, expected', [
    ('poster.jpg', 'poster'),
    ('POSTER.PNG', 'poster'),
    ('fanart.jpg', 'fanart'),
    ('thumb.jpg', 'thumb'),
    ('landscape.jpg', 'landscape'),
    ('banner.jpg', 'banner'),
    ('random.jpg', None),
])
def test_image_kind(name, expected):
    assert _image_kind(Path(name)) == expected


@pytest.mark.parametrize('stem, expected', [
    ('0', 0), ('9', 9), ('10', 10), ('007', 7), ('abc', 1 << 30), ('', 1 << 30),
])
def test_numeric_prefix(stem, expected):
    assert _numeric_prefix(stem) == expected
