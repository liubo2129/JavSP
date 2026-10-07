"""javsp/nfo.py 的单元测试

focus 在 read_nfo：界面用它展示"整理后的影片信息"，字段解析错了会直接
显示成空白或错值，因此把字段映射固定下来。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from javsp.config import Cfg
from javsp.datatype import MovieInfo
from javsp.nfo import read_nfo, write_nfo


@pytest.fixture(scope='module', autouse=True)
def _init_config():
    Cfg()


@pytest.fixture
def sample_info():
    info = MovieInfo(from_file=os.path.join(
        os.path.dirname(__file__), 'data', 'ABP-647 (airav).json'))
    info.dvdid = 'ABP-647'
    info.nfo_title = 'ABP-647 テスト用タイトル'
    info.duration = '160'
    info.publish_date = '2017-10-06'
    info.producer = 'プレステージ'
    info.serial = 'テスト系列'
    info.director = 'テスト監督'
    info.score = '8.5'
    info.actress = ['瀬名きらり', '葵つかさ']
    info.actress_pics = {'瀬名きらり': 'https://example.com/sena.jpg'}
    return info


@pytest.fixture
def nfo_file(tmp_path, sample_info):
    path = tmp_path / 'movie.nfo'
    write_nfo(sample_info, str(path))
    return str(path)


def test_read_nfo_round_trip_scalar_fields(nfo_file):
    data = read_nfo(nfo_file)
    assert data['title'] == 'ABP-647 テスト用タイトル'
    assert data['runtime'] == 160          # 字符串被转成 int
    assert data['premiered'] == '2017-10-06'
    assert data['studio'] == 'プレステージ'
    assert data['series'] == 'テスト系列'
    assert data['director'] == 'テスト監督'
    assert data['rating'] == '8.5'
    assert data['country'] == '日本'
    assert data['mpaa'] == 'NC-17'


def test_read_nfo_uniqueids_separated_by_type(nfo_file):
    data = read_nfo(nfo_file)
    assert data['dvdid'] == 'ABP-647'
    # 这条 fixture 没有 cid，缺失时必须是 None 而不是空字符串
    assert data['cid'] is None


def test_read_nfo_genres_and_tags_are_lists(nfo_file):
    data = read_nfo(nfo_file)
    assert isinstance(data['genres'], list) and data['genres']
    assert isinstance(data['tags'], list)
    assert '无码' in data['genres']


def test_read_nfo_actresses_include_thumb(nfo_file):
    data = read_nfo(nfo_file)
    names = [a['name'] for a in data['actresses']]
    assert '瀬名きらり' in names
    assert '葵つかさ' in names
    # 有头像的女优带 thumb，没有的为 None
    by_name = {a['name']: a['thumb'] for a in data['actresses']}
    assert by_name['瀬名きらり'] == 'https://example.com/sena.jpg'
    assert by_name['葵つかさ'] is None


def test_read_nfo_reports_file_metadata(nfo_file):
    data = read_nfo(nfo_file)
    assert data['nfo_file'] == os.path.abspath(nfo_file)
    assert isinstance(data['nfo_mtime'], float)


def test_read_nfo_plot_is_preserved(nfo_file):
    data = read_nfo(nfo_file)
    assert data['plot'] and len(data['plot']) > 20


def test_read_nfo_missing_field_returns_none(tmp_path):
    """缺字段时返回 None，避免界面显示 'None' 或抛异常"""
    path = tmp_path / 'minimal.nfo'
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<movie><title>只有标题</title></movie>',
        encoding='utf-8')
    data = read_nfo(str(path))
    assert data['title'] == '只有标题'
    assert data['plot'] is None
    assert data['series'] is None
    assert data['dvdid'] is None
    assert data['genres'] == []
    assert data['actresses'] == []


def test_read_nfo_tolerates_broken_xml(tmp_path):
    """nfo 损坏时不应抛异常（recover 模式），至少能读出可解析的部分"""
    path = tmp_path / 'broken.nfo'
    path.write_text('<movie><title>未闭合', encoding='utf-8')
    data = read_nfo(str(path))
    assert data['title'] == '未闭合'
