"""重新扫描已整理目录 + 覆盖整理的单元测试

用户场景：目录里已经整理过一批影片（有 nfo），但当时的元数据是错的。
默认 ``scanner.skip_nfo_dir`` 会把这类文件夹整棵跳过，导致无法更正。
这里固定两条能力：

  1. ``scan_movies(include_scraped=True)`` 能重新扫出已整理的影片
  2. 覆盖整理时旧目录被移进 ``.replaced/`` 备份，而不是与新结果并存
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from javsp.config import Cfg
from javsp.datatype import Movie
from javsp.file import REPLACED_DIR_NAME, backup_existing_dir, scan_movies


@pytest.fixture(scope='module', autouse=True)
def _init_config():
    Cfg()


def _make_video(path, size=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    # minimum_size 默认 232MiB，测试里不做真实大文件，改由配置放宽
    path.write_bytes(b'x')
    return path


@pytest.fixture
def big_files_only(monkeypatch):
    """把最小体积阈值降到 0，避免测试里造 200MB 文件"""
    cfg = Cfg()
    original = cfg.scanner
    object.__setattr__(cfg, 'scanner', original.model_copy(update={'minimum_size': 0}))
    try:
        yield
    finally:
        object.__setattr__(cfg, 'scanner', original)


# ------------------------- 扫描已整理目录 ------------------------- #

def test_scan_skips_scraped_dir_by_default(tmp_path, big_files_only):
    """默认行为不变：有 nfo 的文件夹整棵跳过（这是既有语义，不能悄悄改）"""
    organized = tmp_path / 'organized' / 'ABP-647'
    organized.mkdir(parents=True)
    (organized / 'ABP-647.mp4').write_bytes(b'x')
    (organized / 'movie.nfo').write_text('<movie/>')

    clean = tmp_path / 'clean'
    clean.mkdir()
    (clean / 'SSIS-001.mp4').write_bytes(b'x')

    ids = [m.dvdid for m in scan_movies(str(tmp_path))]
    assert ids == ['SSIS-001']


def test_scan_includes_scraped_dir_when_asked(tmp_path, big_files_only):
    """include_scraped=True 时必须把已整理的影片重新扫出来"""
    organized = tmp_path / 'organized' / 'ABP-647'
    organized.mkdir(parents=True)
    (organized / 'ABP-647.mp4').write_bytes(b'x')
    (organized / 'movie.nfo').write_text('<movie/>')

    ids = [m.dvdid for m in scan_movies(str(tmp_path), include_scraped=True)]
    assert 'ABP-647' in ids


def test_scan_does_not_crash_when_ignored_dir_has_nfo(tmp_path, big_files_only):
    """忽略目录里带 nfo 时不得崩溃

    改造前同一个目录名会被 ``dirnames.remove()`` 调用两次
    （先命中忽略规则、再命中 nfo 判断），抛 ValueError 让整轮扫描失败。
    """
    ignored = tmp_path / '#整理完成'
    ignored.mkdir()
    (ignored / 'ABP-647.mp4').write_bytes(b'x')
    (ignored / 'movie.nfo').write_text('<movie/>')

    # 不抛异常即通过
    assert scan_movies(str(tmp_path), include_scraped=True) == []


# --------------------------- 旧目录备份 --------------------------- #

def test_backup_moves_dir_under_replaced(tmp_path):
    old = tmp_path / '女优A' / '[ABP-647] 旧标题'
    old.mkdir(parents=True)
    (old / 'movie.nfo').write_text('old')

    dest = backup_existing_dir(str(old), anchor=str(tmp_path))

    assert dest is not None
    assert os.path.dirname(dest) == os.path.join(str(tmp_path), REPLACED_DIR_NAME)
    # 旧目录已不在原位，内容被完整搬进备份
    assert not old.exists()
    assert os.path.isfile(os.path.join(dest, 'movie.nfo'))


def test_backup_returns_none_for_missing_dir(tmp_path):
    assert backup_existing_dir(str(tmp_path / '不存在'), anchor=str(tmp_path)) is None


def test_repeated_backup_does_not_overwrite_earlier_one(tmp_path):
    """同一部影片被反复更新时，旧备份不能被新的覆盖掉"""
    for i in range(2):
        old = tmp_path / '[ABP-647] 旧标题'
        old.mkdir()
        (old / 'movie.nfo').write_text(f'v{i}')
        dest = backup_existing_dir(str(old), anchor=str(tmp_path))
        assert dest is not None

    replaced = tmp_path / REPLACED_DIR_NAME
    backups = list(replaced.iterdir())
    assert len(backups) == 2, backups


def test_backup_anchor_for_dir_mode_points_at_output_root():
    """按目录模式：备份固定落在 #整理完成/.replaced/"""
    from javsp.__main__ import _backup_anchor

    movie = Movie('ABP-647')
    movie.files = ['/x/ABP-647.mp4']
    anchor = _backup_anchor(movie)
    assert anchor is not None
    assert os.path.basename(anchor) == '#整理完成'


def test_backup_anchor_for_by_id_mode_points_at_target_folder():
    from javsp.__main__ import _backup_anchor

    movie = Movie('ABP-647')
    movie.by_id_mode = True
    movie.by_id_folder = '/tmp/target'
    assert _backup_anchor(movie) == '/tmp/target'


# --------------------------- 备份清理 --------------------------- #

def _make_backup_tree(root):
    """造两处备份：按番号模式与按目录模式各一"""
    for rel in (REPLACED_DIR_NAME + '/ABP-647',
                os.path.join('#整理完成', REPLACED_DIR_NAME, '[ABP-647] 旧标题')):
        d = os.path.join(root, rel)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'movie.nfo'), 'w', encoding='utf-8') as f:
            f.write('x' * 1000)
        with open(os.path.join(d, 'poster.jpg'), 'wb') as f:
            f.write(b'y' * 2048)


def test_backup_usage_counts_both_locations(tmp_path):
    from javsp.server import GuiServer

    _make_backup_tree(str(tmp_path))
    usage = GuiServer().backup_usage(str(tmp_path))

    assert usage['ok'] is True
    assert usage['files'] == 4            # 两处 × 各 2 个文件
    assert usage['bytes'] == 2 * (1000 + 2048)
    assert len(usage['roots']) == 2


def test_clean_backups_removes_them_and_reports_freed_space(tmp_path):
    from javsp.server import GuiServer

    _make_backup_tree(str(tmp_path))
    result = GuiServer().clean_backups(str(tmp_path))

    assert result['ok'] is True
    assert len(result['removed']) == 2
    assert result['bytes'] == 2 * (1000 + 2048)
    assert not (tmp_path / REPLACED_DIR_NAME).exists()
    assert not (tmp_path / '#整理完成' / REPLACED_DIR_NAME).exists()


def test_clean_backups_is_idempotent(tmp_path):
    """再点一次"清理"不该报错，只是没东西可删"""
    from javsp.server import GuiServer

    server = GuiServer()
    _make_backup_tree(str(tmp_path))
    server.clean_backups(str(tmp_path))

    again = server.clean_backups(str(tmp_path))
    assert again['ok'] is True
    assert again['removed'] == []
    assert again['message'] == '没有需要清理的备份'


def test_clean_backups_refuses_without_directory():
    from javsp.server import GuiServer

    server = GuiServer()
    result = server.clean_backups('')
    assert result['ok'] is False
    assert result['error'] == 'no_directory'


def test_clean_backups_never_touches_normal_movie_dirs(tmp_path):
    """只认 .replaced，真实影片目录必须原样保留"""
    from javsp.server import GuiServer

    real = tmp_path / '#整理完成' / '瀬名きらり' / '[ABP-647] 正片'
    real.mkdir(parents=True)
    (real / 'ABP-647.mp4').write_bytes(b'video')
    (real / 'movie.nfo').write_text('ok')

    GuiServer().clean_backups(str(tmp_path))

    assert (real / 'ABP-647.mp4').exists()
    assert (real / 'movie.nfo').exists()


def test_backup_usage_ignores_symlinks(tmp_path):
    """符号链接不计入体积，避免绕出目录或重复计数"""
    from javsp.server import GuiServer

    backup = tmp_path / REPLACED_DIR_NAME
    backup.mkdir()
    (backup / 'real.nfo').write_text('x' * 100)
    try:
        os.symlink(str(backup / 'real.nfo'), str(backup / 'link.nfo'))
    except (OSError, NotImplementedError):
        pytest.skip('当前环境不支持创建符号链接')

    usage = GuiServer().backup_usage(str(tmp_path))
    assert usage['files'] == 1
    assert usage['bytes'] == 100


def test_human_size_formatting():
    from javsp.server import _human_size

    assert _human_size(0) == '0 B'
    assert _human_size(1024) == '1.0 KB'
    assert _human_size(5 * 1024 ** 3) == '5.0 GB'
