"""按番号获取模式的单元测试

覆盖两个容易出错的点：
  1. 番号输入的分隔与去重（用户会粘贴各种格式）
  2. 按番号模式下的输出路径（movie.files 为空，既有代码依赖它取扩展名）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.worker import _build_by_id_movies, parse_movie_ids


@pytest.fixture(scope='module', autouse=True)
def _init_config():
    Cfg()


# ------------------------------ 番号解析 ------------------------------ #

@pytest.mark.parametrize('text, expected', [
    ('ABP-647', ['ABP-647']),
    ('ABP-647, SSIS-001', ['ABP-647', 'SSIS-001']),
    ('ABP-647，SSIS-001；ABC-123', ['ABP-647', 'SSIS-001', 'ABC-123']),
    ('ABP-647\nSSIS-001\tX', ['ABP-647', 'SSIS-001', 'X']),
    ('  ABP-647  ', ['ABP-647']),
    ('ABP-647、SSIS-001', ['ABP-647', 'SSIS-001']),
    ('ABP-647|SSIS-001', ['ABP-647', 'SSIS-001']),
])
def test_parse_movie_ids_separators(text, expected):
    assert parse_movie_ids(text) == expected


def test_parse_movie_ids_dedupes_case_insensitively():
    # 保留首次出现的写法，避免用户重复粘贴时反复抓取
    assert parse_movie_ids('abp-647 ABP-647 Abp-647') == ['abp-647']


def test_parse_movie_ids_empty_input():
    assert parse_movie_ids('') == []
    assert parse_movie_ids(None) == []
    assert parse_movie_ids('  ,  ;  ') == []


# --------------------------- 按番号构造 Movie --------------------------- #

def test_build_by_id_movies_has_no_files():
    movies = _build_by_id_movies(['ABP-647'])
    assert len(movies) == 1
    movie = movies[0]
    assert movie.dvdid == 'ABP-647'
    # 按番号获取没有输入文件，这是与按目录模式最大的区别
    assert movie.files == []
    assert movie.by_id_mode is True


def test_build_by_id_movies_multiple():
    movies = _build_by_id_movies(['ABP-647', 'SSIS-001'])
    assert [m.dvdid for m in movies] == ['ABP-647', 'SSIS-001']
    assert all(m.by_id_mode for m in movies)


# ------------------------------ 输出路径 ------------------------------ #

def _scraped_movie(avid: str) -> Movie:
    """构造一部"已抓取完成"的影片（info 就绪，files 为空）"""
    movie = Movie(avid)
    movie.files = []
    movie.by_id_mode = True
    movie.by_id_folder = '/tmp/javsp-byid-test'
    info = MovieInfo(from_file=os.path.join(
        os.path.dirname(__file__), 'data', 'ABP-647 (airav).json'))
    info.dvdid = avid
    info.actress = ['瀬名きらり']
    movie.info = info
    return movie


def test_generate_names_by_id_outputs_folder_named_after_id():
    from javsp.__main__ import generate_names

    movie = _scraped_movie('ABP-647')
    generate_names(movie)

    # 输出结构：<目标文件夹>/<番号>/
    assert movie.save_dir == '/tmp/javsp-byid-test/ABP-647'
    assert movie.basename == 'ABP-647'
    assert movie.nfo_file == '/tmp/javsp-byid-test/ABP-647/movie.nfo'
    assert movie.fanart_file.endswith('fanart.jpg')
    assert movie.poster_file.endswith('poster.jpg')
    # 元数据仍按配置生成标题
    assert movie.info.nfo_title.startswith('ABP-647')


def test_generate_names_by_id_survives_empty_files():
    """files 为空时不得因 max() 空序列而崩溃（这是既有代码的隐含假设）"""
    from javsp.__main__ import generate_names

    movie = _scraped_movie('ABP-647')
    assert movie.files == []
    generate_names(movie)          # 不抛异常即通过
    assert movie.save_dir


def test_generate_names_by_id_without_folder_is_safe():
    """未设置目标文件夹时不应崩溃，而是退回到番号命名"""
    from javsp.__main__ import generate_names

    movie = _scraped_movie('ABP-647')
    movie.by_id_folder = ''
    generate_names(movie)
    assert movie.save_dir
    assert movie.basename


def test_planned_steps_excludes_move_files_in_by_id_mode():
    """按番号模式没有源文件，不应出现 move_files 步骤"""
    from javsp.__main__ import _planned_steps

    by_id = _scraped_movie('ABP-647')
    steps = _planned_steps([by_id])
    assert 'move_files' not in steps
    assert 'crawl' in steps and 'write_nfo' in steps


def test_planned_steps_includes_move_files_in_normal_mode():
    from javsp.__main__ import _planned_steps

    normal = Movie('ABP-647')
    normal.files = ['/tmp/x/ABP-647.mp4']
    steps = _planned_steps([normal])
    assert 'move_files' in steps
