"""解析Chromium系浏览器Cookies的相关函数"""
import os
import sys
import json
import base64
import sqlite3
import logging
import subprocess
import tempfile
from glob import glob
from shutil import copyfile
from datetime import datetime

__all__ = ['get_browsers_cookies']


from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from Crypto.Cipher import AES

logger = logging.getLogger(__name__)


class Decrypter():
    def __init__(self, key):
        self.key = key
    def decrypt(self, encrypted_value):
        nonce = encrypted_value[3:3+12]
        ciphertext = encrypted_value[3+12:-16]
        tag = encrypted_value[-16:]
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag).decode('utf-8')
        return plaintext


def _get_browser_roots():
    """返回当前平台上各 Chromium 系浏览器的用户数据目录和密钥来源。

    返回值的第二项在 Windows 上为 None（使用 DPAPI），在 macOS 上为
    Keychain 中的 service 名称，在 Linux 上为 None（使用 Local State）。
    """
    if sys.platform == 'win32':
        base = os.getenv('LOCALAPPDATA') or os.path.expanduser('~/AppData/Local')
        return {
            'Chrome':        (os.path.join(base, 'Google', 'Chrome', 'User Data'), None),
            'Chrome Beta':   (os.path.join(base, 'Google', 'Chrome Beta', 'User Data'), None),
            'Chrome Canary': (os.path.join(base, 'Google', 'Chrome SxS', 'User Data'), None),
            'Chromium':      (os.path.join(base, 'Google', 'Chromium', 'User Data'), None),
            'Edge':          (os.path.join(base, 'Microsoft', 'Edge', 'User Data'), None),
            'Vivaldi':       (os.path.join(base, 'Vivaldi', 'User Data'), None),
        }
    if sys.platform == 'darwin':
        app_support = os.path.expanduser('~/Library/Application Support')
        return {
            'Chrome':        (os.path.join(app_support, 'Google', 'Chrome'), 'Chrome Safe Storage'),
            'Chrome Beta':   (os.path.join(app_support, 'Google', 'Chrome Beta'), 'Chrome Safe Storage'),
            'Chrome Canary': (os.path.join(app_support, 'Google', 'Chrome Canary'), 'Chrome Safe Storage'),
            'Chromium':      (os.path.join(app_support, 'Chromium'), 'Chromium Safe Storage'),
            'Edge':          (os.path.join(app_support, 'Microsoft Edge'), 'Microsoft Edge Safe Storage'),
            'Vivaldi':       (os.path.join(app_support, 'Vivaldi'), 'Vivaldi Safe Storage'),
        }
    config_home = os.getenv('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return {
        'Chrome':  (os.path.join(config_home, 'google-chrome'), None),
        'Chromium': (os.path.join(config_home, 'chromium'), None),
        'Edge':     (os.path.join(config_home, 'microsoft-edge'), None),
        'Vivaldi':  (os.path.join(config_home, 'vivaldi'), None),
    }


def get_browsers_cookies():
    """获取系统上的所有Chromium系浏览器的JavDB的Cookies"""
    # 不予支持: Opera, 360安全&极速, 搜狗使用非标的用户目录或数据格式; QQ浏览器屏蔽站点
    all_browser_cookies = []
    exceptions = []
    for brw, (user_dir, key_service) in _get_browser_roots().items():
        if not user_dir or not os.path.isdir(user_dir):
            continue
        cookies_files = glob(os.path.join(user_dir, '*', 'Cookies'))
        cookies_files += glob(os.path.join(user_dir, '*', 'Network', 'Cookies'))
        if not cookies_files:
            continue
        local_state = os.path.join(user_dir, 'Local State')
        # macOS 的 key 存在 Keychain 中，不依赖 Local State 文件
        if sys.platform != 'darwin' and not os.path.exists(local_state):
            continue
        try:
            if sys.platform == 'win32':
                key = decrypt_key_win(local_state)
            elif sys.platform == 'darwin':
                key = decrypt_key_mac(key_service)
            else:
                key = decrypt_key_linux(local_state)
        except Exception as e:
            exceptions.append(e)
            logger.debug(f"无法获取浏览器 {brw} 的Cookies密钥({e})", exc_info=True)
            continue
        decrypter = Decrypter(key)
        for file in cookies_files:
            file = os.path.normpath(file)
            try:
                profile = brw + ": " + os.path.relpath(file, user_dir).split(os.sep)[0]
            except ValueError:
                profile = brw
            try:
                records = get_cookies(file, decrypter)
                if records:
                    # 将records转换为便于使用的格式
                    for site, cookies in records.items():
                        entry = {'profile': profile, 'site': site, 'cookies': cookies}
                        all_browser_cookies.append(entry)
            except Exception as e:
                exceptions.append(e)
                logger.debug(f"无法解析Cookies文件({e}): {file}", exc_info=True)
    if len(all_browser_cookies) == 0 and len(exceptions) > 0:
        raise exceptions[0]
    return all_browser_cookies


def convert_chrome_utc(chrome_utc):
    """将Chrome存储的UTC时间转换为UNIX的UTC时间格式"""
    # Chrome's cookies timestamp's epoch starts 1601-01-01T00:00:00Z
    second = int(chrome_utc / 1e6)
    if second > 0:  # 考虑chrome_utc为0的情况
        second = second - 11644473600
    unix_utc = datetime.fromtimestamp(second)
    return unix_utc

def decrypt_key_win(local_state):
    """从Local State文件中提取并解密出Cookies文件的密钥"""
    # Chrome 80+ 的Cookies解密方法参考自: https://stackoverflow.com/a/60423699/6415337
    import win32crypt
    with open(local_state, 'rt', encoding='utf-8') as file:
        encrypted_key = json.loads(file.read())['os_crypt']['encrypted_key']
    encrypted_key = base64.b64decode(encrypted_key)                                       # Base64 decoding
    encrypted_key = encrypted_key[5:]                                                     # Remove DPAPI
    decrypted_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]  # Decrypt key
    return decrypted_key


def decrypt_key_mac(service):
    """从 macOS Keychain 中提取 Chrome 系浏览器用于加密 Cookies 的密钥"""
    if not service:
        raise ValueError('未指定 Keychain service 名称')
    result = subprocess.run(
        ['security', 'find-generic-password', '-w', '-s', service],
        capture_output=True, timeout=30, check=False,
        text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        stderr = result.stderr.strip().replace('\n', ' ')
        raise RuntimeError(f"无法从Keychain读取'{service}': {stderr}")
    return bytes.fromhex(result.stdout.strip())


def decrypt_key_linux(local_state):
    """从Local State文件中提取并解密出Cookies文件的密钥，适用于Linux"""
    # 读取Local State文件中的密钥
    with open(local_state, 'rt', encoding='utf-8') as file:
        encrypted_key = json.loads(file.read())['os_crypt']['encrypted_key']
    encrypted_key = base64.b64decode(encrypted_key)
    encrypted_key = encrypted_key[5:]
    key = encrypted_key
    nonce = b' ' * 12
    aesgcm = AESGCM(key)
    decrypted_key = aesgcm.decrypt(nonce, encrypted_key, None)
    return decrypted_key


def get_cookies(cookies_file, decrypter, host_pattern='javdb%.com'):
    """从cookies_file文件中查找指定站点的所有Cookies"""
    # 复制Cookies文件到唯一的临时文件，避免直接操作原始Cookies文件以及多线程冲突
    fd, temp_cookie = tempfile.mkstemp(prefix='javsp_cookies_', suffix='.sqlite')
    os.close(fd)
    conn = None
    try:
        copyfile(cookies_file, temp_cookie)
        # 连接数据库进行查询
        conn = sqlite3.connect(temp_cookie)
        cursor = conn.cursor()
        cursor.execute(f'SELECT host_key, name, encrypted_value, expires_utc FROM cookies WHERE host_key LIKE "{host_pattern}"')
        # 将查询结果按照host_key进行组织
        now = datetime.now()
        records = {}
        for host_key, name, encrypted_value, expires_utc in cursor.fetchall():
            d = records.setdefault(host_key, {})
            # 只提取尚在有效期内的Cookies
            expires = convert_chrome_utc(expires_utc)
            if expires > now:
                d[name] = decrypter.decrypt(encrypted_value)
        # Cookies的核心字段是'_jdb_session'，因此如果records中缺失此字段（说明已过期），则对应的Cookies不再有效
        valid_records = {k: v for k, v in records.items() if '_jdb_session' in v}
        return valid_records
    finally:
        if conn is not None:
            conn.close()
        if os.path.exists(temp_cookie):
            os.remove(temp_cookie)


if __name__ == "__main__":
    all_cookies = get_browsers_cookies()
    for d in all_cookies:
        print('{:<20}{}'.format(d['profile'], d['site']))

