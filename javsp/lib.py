"""用来组织不需要依赖任何自定义类型的功能函数"""
import os
import re
import sys
from pathlib import Path


__all__ = ['re_escape', 'resource_path', 'strftime_to_minutes', 'detect_special_attr',
           'is_metadata_name', 'METADATA_BASENAMES']


# 各平台系统自动生成的目录级元数据文件（大小写不敏感）
METADATA_BASENAMES = frozenset({
    '.ds_store', 'thumbs.db', 'desktop.ini', 'ehthumbs.db',
})


def is_metadata_name(name: str) -> bool:
    """文件名是否是文件系统/系统软件产生的元数据文件，而非真实内容

    macOS 在**非 APFS 卷**（exFAT / FAT / SMB / NAS）上会为每个文件额外写一个
    AppleDouble 伴随文件，名字是 ``._<原名>``，内容以 ``Mac OS X`` 开头、
    通常恰好 4096 字节。它**保留了原扩展名**，因此只靠扩展名判断会把它当成
    真图片/真视频：

    * 剧照列表混进一堆"有路径但解码失败"的条目，界面显示为空白
      （前端 onerror 会移除 src，既不是破图也不是占位，就是一片空）
    * 扫描时被当成影片文件，虚增 video_count，甚至产出"无法识别番号"的
      假失败项

    ``.DS_Store`` / ``Thumbs.db`` / ``desktop.ini`` 同理，既不是影片也不是图片。

    该函数放在 lib.py 而不是 file.py：``file.py``（扫描）与 ``server.py``
    （图库/图片接口）都要用，而 server 依赖 file 会造成循环导入。
    """
    if not name:
        return True
    # AppleDouble：._foo.png。只看前缀，不看后缀——它刻意保留了原扩展名
    if name.startswith('._'):
        return True
    return name.lower() in METADATA_BASENAMES


_special_chars_map = {i: '\\' + chr(i) for i in b'()[]{}?*+|^$\\.'}
def re_escape(s: str) -> str:
    """用来对字符串进行转义，以将转义后的字符串用于构造正则表达式"""
    pattern = s.translate(_special_chars_map)
    return pattern


def resource_path(path: str) -> str:
    """获取一个随代码打包的文件在解压后的路径

    cx_Freeze 的 bdist_mac 会把资源放进 ``JavSP.app/Contents/Resources``，
    因此 macOS 下不能简单依赖当前工作目录。
    """
    if not getattr(sys, "frozen", False):
        path_joined = Path(__file__).parent.parent / path
        return str(path_joined)

    exe_dir = Path(sys.executable).resolve().parent
    candidates = [exe_dir]
    if sys.platform == 'darwin' and exe_dir.name == 'MacOS' and exe_dir.parent.name == 'Contents':
        candidates.append(exe_dir.parent / 'Resources')
    for base in candidates:
        candidate = base / path
        if candidate.exists():
            return str(candidate)
    return str(candidates[0] / path)


def strftime_to_minutes(s: str) -> int:
    """将HH:MM:SS或MM:SS的时长转换为分钟数返回

    Args:
        s (str): HH:MM:SS or MM:SS

    Returns:
        [int]: 取整后的分钟数
    """
    items = list(map(int, s.split(':')))
    if len(items) == 2:
        minutes = items[0] + round(items[1]/60)
    elif len(items) == 3:
        minutes = items[0] * 60 + items[1] + round(items[2]/60)
    else:
        raise ValueError(f"无法将字符串'{s}'转换为分钟")
    return minutes


_PATTERN = re.compile(r'(uncen(sor(ed)?)?([- _\s]*leak(ed)?)?|[无無][码碼](流出|破解))', flags=re.I)
def detect_special_attr(filepath: str, avid: str = None) -> str:
    """通过文件名检测影片是否有特殊属性（内嵌字幕、无码流出/破解）

    Returns:
        [str]: '', 'U', 'C', 'UC'
    """
    result = ''
    base = os.path.splitext(os.path.basename(filepath))[0].upper()
    # 尝试使用正则匹配
    match = _PATTERN.search(base)
    if match:
        result += 'U'
    # 尝试匹配-C/-U/-UC后缀的影片
    postfix = base.split('-')[-1]
    if postfix in ('U', 'C', 'UC'):
        result += postfix
    elif avid:
        pattern_str = re.sub(r'[_-]', '[_-]*', avid) + r'(UC|U|C)\b'
        match = re.search(pattern_str, base, flags=re.I)
        if match:
            result += match.group(1)
    # 最终格式化
    result = ''.join(sorted(set(result), reverse=True))
    return result


if __name__ == "__main__":
    print(detect_special_attr('ipx-177cd1.mp4', 'IPX-177'))
