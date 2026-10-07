"""与操作nfo文件相关的功能"""
import os
from lxml import etree
from lxml.etree import tostring
from lxml.builder import E


from javsp.datatype import MovieInfo
from javsp.config import Cfg


def write_nfo(info: MovieInfo, nfo_file):
    """将存储了影片信息的'info'写入到nfo文件中"""
    # NFO spec: https://kodi.wiki/view/NFO_files/Movies
    nfo = E.movie()
    dic = info.get_info_dic()

    if info.nfo_title:
        nfo.append(E.title(info.nfo_title))
    else:
        nfo.append(E.title(info.title))

    # 仅在标题被处理过时'ori_title'字段才会有值
    if info.ori_title:
        nfo.append(E.originaltitle(info.ori_title))

    # Kodi的文档中评分支持多个来源，但经测试，添加了多个评分时Kodi也只显示了第一个评分
    if info.score:
        nfo.append(E.rating(info.score))

    # 目前没有合适的字段用于outline（一行简短的介绍），力求不在nfo中写入冗余的信息，因此不添加outline标签
    # 而且无论是Kodi还是Jellyfin中都没有找到实际显示outline的位置；tagline倒是都有发现

    if info.plot:
        nfo.append(E.plot(info.plot))

    # 目前没有合适的字段用于tagline（一行简短的介绍）

    # 并不是每个数据源都有影片的时长信息（例如airav）
    if info.duration:
        nfo.append(E.runtime(info.duration))

    # thumb字段可以用来为不同的aspect强制指定图片文件名
    # 例如可以将'NoPoster.jpg'指定给'ABC-123.mp4'，而不必按照poster文件名的常规命名规则来
    # 但是Emby不支持此特性，Jellyfin的文档和社区都比较弱，没找到相关说明，推测多半也不支持

    # fanart通常也是通过给fanart图片命名来匹配
    nfo.append(E.mpaa('NC-17'))     # 分级

    # 将DVD ID和CID写入到uniqueid字段
    if info.dvdid:
        nfo.append(E.uniqueid(info.dvdid, type='num', default='true'))
    if info.cid:
        nfo.append(E.uniqueid(info.cid, type='cid'))

    # 选择要写入的genre数据源字段：将[]作为后备结果，以确保genre结果为None时后续不会抛出异常
    for genre_item in (info.genre_norm, info.genre, []):
        if genre_item:
            break

    genre = genre_item.copy()
    # 添加自定义分类
    for genre_new in Cfg().summarizer.nfo.custom_genres_fields:
        genre.append(genre_new.format(**dic))
    # 分类去重
    genre = list(set(genre))
    # 写入genre分类：优先使用genre_norm。在Jellyfin上，只有genre可以直接跳转，tag不可以
    # 也同时写入tag。TODO: 还没有研究tag和genre在Kodi上的区别
    for i in genre:
        nfo.append(E.genre(i))

    tags = []
    # 添加自定义tag
    for tag_new in Cfg().summarizer.nfo.custom_tags_fields:
            tags.append(tag_new.format(**dic))
    # 去重
    tags = list(set(tags))
    # 写入tag
    for i in tags:
        nfo.append(E.tag(i))

    # Kodi上的country字段没说必须使用国家的代码（比如JP），所以目前暂定直接使用国家名
    nfo.append(E.country('日本'))

    if info.serial:
        # 部分影片有系列。set字段支持overview作为介绍，但是目前没发现有地方可以获取到系列的介绍
        nfo.append(E.set(E.name(info.serial)))

    if info.director:
        nfo.append(E.director(info.director))

    # 发行日期。文档中关于'year'字段的说明: Do not use. Use <premiered> instead
    if info.publish_date:
        nfo.append(E.premiered(info.publish_date))

    # 原文是 Production studio: 因此这里写入的是影片制作商
    if info.producer:
        nfo.append(E.studio(info.producer))

    # trailer 预告片
    if info.preview_video:
        nfo.append(E.trailer(info.preview_video))

    # TODO: fileinfo 字段，看起来可以给定字幕语言和类型，留待开发

    # 写入演员名。Kodi支持用thumb显示演员头像，如果能获取到演员头像也一并写入
    if info.actress:
        for i in info.actress:
            if (info.actress_pics) and (i in info.actress_pics):
                nfo.append(E.actor(E.name(i), E.thumb(info.actress_pics[i])))
            else:
                nfo.append(E.actor(E.name(i)))

    with open(nfo_file, 'wt', encoding='utf-8') as f:
        f.write(tostring(nfo, encoding='unicode', pretty_print=True,
                         doctype='<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'))


def read_nfo(nfo_file: str) -> dict:
    """读取 nfo 文件并返回结构化数据（write_nfo 的逆操作）

    供界面展示"整理后的影片信息"用。只提取界面需要的字段，多值字段统一
    返回列表，缺失字段返回 None / []，调用方无需再做存在性判断。
    """
    parser = etree.XMLParser(recover=True, resolve_entities=False)
    tree = etree.parse(nfo_file, parser=parser)
    root = tree.getroot()

    def one(tag: str) -> str | None:
        el = root.find(tag)
        if el is None or el.text is None:
            return None
        text = el.text.strip()
        return text or None

    def many(tag: str) -> list:
        # 用 xpath 而非 findall：lxml 的 ElementPath 不支持 'tag/text()' 写法
        return [t.strip() for t in root.xpath(f'{tag}/text()') if t and t.strip()]

    # uniqueid 用 type 区分 DVD ID 与 CID
    uniqueids = {}
    for el in root.findall('uniqueid'):
        kind = el.get('type') or 'other'
        if el.text:
            uniqueids[kind] = el.text.strip()

    # actor 可能带 thumb（女优头像链接）
    actresses = []
    for el in root.findall('actor'):
        name_el = el.find('name')
        if name_el is None or not name_el.text:
            continue
        thumb_el = el.find('thumb')
        actresses.append({
            'name': name_el.text.strip(),
            'thumb': (thumb_el.text or '').strip() if thumb_el is not None else None,
        })

    set_el = root.find('set/name')

    def int_or_none(value):
        try:
            return int(str(value).split('.')[0])
        except (TypeError, ValueError):
            return None

    return {
        'title': one('title'),
        'original_title': one('originaltitle'),
        'plot': one('plot'),
        'rating': one('rating'),
        'runtime': int_or_none(one('runtime')),
        'mpaa': one('mpaa'),
        'premiered': one('premiered'),
        'studio': one('studio'),
        'director': one('director'),
        'country': one('country'),
        'trailer': one('trailer'),
        'series': set_el.text.strip() if set_el is not None and set_el.text else None,
        'dvdid': uniqueids.get('num'),
        'cid': uniqueids.get('cid'),
        'genres': many('genre'),
        'tags': many('tag'),
        'actresses': actresses,
        'nfo_file': os.path.abspath(nfo_file),
        'nfo_mtime': os.path.getmtime(nfo_file),
    }


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    info = MovieInfo(from_file=os.path.join('unittest', 'data', 'IPX-177 (javbus).json'))
    write_nfo(info)
