"""获取各个网站的免代理地址"""
import re
import sys

from javsp.web.base import Request, is_usable, get_html


# 可用于查询JavBus当前地址的站点：永久域名 + 已知的防屏蔽地址
JAVBUS_URL_SEEDS = [
    'https://www.javbus.com',
    'https://www.buscdn.casa',
    'https://www.dmmbus.casa',
    'https://www.cdnbus.casa',
    'https://www.busdmm.casa',
    'https://www.busjav.casa',
    'https://www.javbus.casa',
    'https://www.seedmm.casa',
]

# 可用于查询JavDB当前地址的站点
JAVDB_URL_SEEDS = [
    'https://javdb.com',
    'https://javdb580.com',
]


def get_proxy_free_url(site_name: str, prefer_url=None) -> str:
    """获取指定网站的免代理地址
    Args:
        site_name (str): 站点名称
        prefer_url (str, optional): 优先测试此url是否可用
    Returns:
        str: 指定站点的免代理地址（失败时为空字符串）
    """
    if prefer_url and is_usable(prefer_url, timeout=5):
        return prefer_url
    # 当prefer_url不可用时，尝试自动获取指定网站的免代理地址
    site_name = site_name.lower()
    func_name = f'_get_{site_name}_urls'
    get_funcs = [i for i in dir(sys.modules[__name__]) if i.startswith('_get_')]
    if func_name in get_funcs:
        get_urls = getattr(sys.modules[__name__], func_name)
        try:
            urls = get_urls()
            return _choose_one(urls)
        except:
            return ''
    else:
        raise Exception("Dont't know how to get proxy-free url for " + site_name)


def _choose_one(urls) -> str:
    for url in urls:
        # 不能只看能否连通：域名被停放时也会返回 200
        if is_usable(url, timeout=5):
            return url
    return ''


def _get_avsox_urls() -> list:
    html = get_html('https://tellme.pw/avsox')
    urls = html.xpath('//h4/strong/a/@href')
    return urls


def _get_javbus_urls() -> list:
    """从JavBus首页的公告栏获取当前的防屏蔽地址

    JavBus的地址会定期更换，旧地址（如javbus.one）可能已经无法访问。每个可用的
    JavBus站点首页都会公布防屏蔽地址，因此逐个尝试这些站点，取第一个能解析出结果的。
    """
    # JavBus会根据Accept-Language判断访客地区，非中文会被重定向到年龄验证页
    request = Request()
    request.headers['Accept-Language'] = 'zh-CN,zh;q=0.9,zh-TW;q=0.8'
    for seed in JAVBUS_URL_SEEDS:
        try:
            html = request.get_html(seed)
        except Exception:
            continue
        urls = html.xpath("//strong[contains(text(), '防屏蔽地址')]/following-sibling::a[1]/@href")
        if urls:
            return urls
    return []


def _get_javdb_urls() -> list:
    """从JavDB网页顶部的公告栏获取最新域名

    公告栏形如：``官方App: <a>app.javdb580.com</a>, 最新域名: <a>javdb580.com</a>,
    永久域名: <a>javdb.com</a>``。原地址发布页 jav524.app 已经停止服务，改为直接
    从站内公告读取。
    """
    request = Request(use_scraper=True)
    request.headers['Accept-Language'] = 'zh-CN,zh;q=0.9'
    for seed in JAVDB_URL_SEEDS:
        try:
            html = request.get_html(seed)
        except Exception:
            continue
        # 只要「最新域名」，排除官方App的域名
        urls = html.xpath("//nav[contains(@class, 'sub-header')]"
                          "//a[contains(@href, 'javdb') and not(contains(@href, 'app.'))]/@href")
        if urls:
            return urls
    return []


def _get_javlib_urls() -> list:
    """从JavLibrary的GitHub主页简介获取当前域名"""
    html = get_html('https://github.com/javlibcom')
    bios = html.xpath("//div[contains(@class, 'user-profile-bio')]")
    if not bios:
        return []
    match = re.search(r'[\w\.]+', bios[0].text_content(), re.A)
    if match:
        domain = f'https://www.{match.group(0)}.com'
        return [domain]
    return []


if __name__ == "__main__":
    print('javdb:\t', _get_javdb_urls())
    print('javlib:\t', _get_javlib_urls())
