# -*- coding: utf-8 -*-
"""
CookieCloud 客户端
==================

多站点支持的前提是「每个站点都要有有效登录态」。本模块把 CookieCloud 作为
唯一的 Cookie 来源，站点适配器不再各自维护 Cookie，新增站点只要:

    1. 在浏览器里保持该站登录（CookieCloud 插件自动上传）
    2. 在 sites 注册表里加一条适配器
    3. 适配器声明自己的 `domain`，本模块负责按域取 Cookie

加密格式
--------
CookieCloud 客户端（浏览器插件）用 crypto-js 加密，密文形如
``Salted__`` + 8 字节 salt + AES 密文。密钥派生是 crypto-js 的 OpenSSL 兼容
方式，容易踩的坑是 **passphrase 不是密码明文**：

    passphrase = md5(f"{uuid}-{password}")[:16]        # 16 个 ASCII 字符
    key, iv    = EVP_BytesToKey(MD5, passphrase, salt, key_len=32, iv_len=16)
    plaintext  = AES-256-CBC(key, iv).decrypt(ciphertext) 后 PKCS7 去填充

新版插件还有 ``aes-128-cbc-fixed`` 变体（响应体带 ``iv`` 字段），本模块按
顺序自动尝试，全部失败才报错，因此新旧客户端都能兼容。
"""

import base64
import hashlib
import json
import threading
import time

import requests

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    _HAS_CRYPTO = "pycryptodome"
except ImportError:  # pragma: no cover - 依赖缺失时给出明确指引
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        _HAS_CRYPTO = "cryptography"
    except ImportError:
        _HAS_CRYPTO = None


class CookieCloudError(RuntimeError):
    pass


def _aes_cbc_decrypt(key, iv, ciphertext):
    """AES-CBC 解密 + PKCS7 去填充。"""
    if _HAS_CRYPTO == "pycryptodome":
        return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext), 16)
    if _HAS_CRYPTO == "cryptography":
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        data = dec.update(ciphertext) + dec.finalize()
        pad = data[-1] if data else 0
        if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
            data = data[:-pad]
        return data
    raise CookieCloudError(
        "缺少 AES 依赖，请安装 pycryptodome：pip install pycryptodome")


def _evp_bytes_to_key(passphrase, salt, key_len=32, iv_len=16):
    """OpenSSL/EvpKDF 兼容派生（crypto-js 使用的实现方式）。"""
    out, prev = b"", b""
    while len(out) < key_len + iv_len:
        prev = hashlib.md5(prev + passphrase + salt).digest()
        out += prev
    return out[:key_len], out[key_len:key_len + iv_len]


def decrypt_payload(uuid, password, encrypted_b64, iv_b64="", crypto_type=""):
    """
    解密 CookieCloud 密文，返回明文 dict。多格式自动尝试。
    """
    if not encrypted_b64:
        raise CookieCloudError(
            "未获取到加密数据。请确认浏览器插件已上传、UUID 与密码正确。")

    blob = base64.b64decode(encrypted_b64)
    passphrase = hashlib.md5(
        ("%s-%s" % (uuid, password)).encode("utf-8")).hexdigest()[:16].encode("utf-8")

    def crypto_js(klen):
        if blob[:8] != b"Salted__":
            raise ValueError("非 crypto-js 格式")
        salt, ct = blob[8:16], blob[16:]
        k, iv = _evp_bytes_to_key(passphrase, salt, klen, 16)
        return _aes_cbc_decrypt(k, iv, ct)

    def fixed_iv():
        iv = base64.b64decode(iv_b64) if iv_b64 else b"\x00" * 16
        return _aes_cbc_decrypt(passphrase, iv, blob)

    if blob[:8] == b"Salted__":
        candidates = [("crypto-js/AES-256-CBC", lambda: crypto_js(32)),
                      ("crypto-js/AES-128-CBC", lambda: crypto_js(16)),
                      ("aes-128-cbc-fixed", fixed_iv)]
    else:
        candidates = [("aes-128-cbc-fixed", fixed_iv),
                      ("crypto-js/AES-256-CBC", lambda: crypto_js(32))]

    last_err, plain, used = None, None, ""
    for name, fn in candidates:
        try:
            plain = fn()
            used = name
            break
        except Exception as e:  # noqa: BLE001 - 逐个格式试，失败即换下一个
            last_err = e

    if plain is None:
        raise CookieCloudError(
            "解密失败（已尝试全部格式）。请检查 UUID 与密码是否与插件一致。"
            "最后一次错误：%s: %s" % (type(last_err).__name__, last_err))

    try:
        data = json.loads(plain.decode("utf-8"))
    except Exception as e:
        raise CookieCloudError("解密结果不是合法 JSON：%s" % e)

    return data, used


class CookieCloudClient:
    """带 TTL 缓存的 CookieCloud 读取器（线程安全）。"""

    def __init__(self, url="", uuid="", password="", timeout=20, cache_ttl=300):
        self.configure(url, uuid, password, timeout, cache_ttl)

    def configure(self, url, uuid, password, timeout=20, cache_ttl=300):
        with _cache_lock:
            self.url = (url or "").rstrip("/")
            self.uuid = uuid or ""
            self.password = password or ""
            self.timeout = int(timeout or 20)
            self.cache_ttl = int(cache_ttl or 300)
            self._data = None
            self._fetched_at = 0.0
            self._meta = {}

    # ------------------------------------------------------------ 拉取

    def fetch(self, force=False):
        """拉取并解密，返回 cookie_data（{domain: [cookie, ...]}）。"""
        with _cache_lock:
            fresh = (self._data is not None
                     and (time.time() - self._fetched_at) < self.cache_ttl)
            if fresh and not force:
                return self._data

        if not self.url or not self.uuid or not self.password:
            raise CookieCloudError("CookieCloud 未配置（缺少 URL / UUID / 密码）")

        req_url = "%s/get/%s" % (self.url, self.uuid)
        started = time.time()
        try:
            resp = requests.get(req_url, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json() or {}
        except Exception as e:
            raise CookieCloudError("请求 CookieCloud 失败：%s: %s" % (type(e).__name__, e))

        encrypted = payload.get("encrypted") or payload.get("data") or ""
        data, used = decrypt_payload(
            self.uuid, self.password, encrypted,
            iv_b64=payload.get("iv") or "",
            crypto_type=payload.get("crypto_type") or "")

        cookie_data = data.get("cookie_data") or {}
        if not isinstance(cookie_data, dict):
            raise CookieCloudError("cookie_data 结构异常，请确认 CookieCloud 版本")

        with _cache_lock:
            self._data = cookie_data
            self._fetched_at = time.time()
            self._meta = {
                "format": used,
                "domains": len(cookie_data),
                "cookies": sum(len(v) for v in cookie_data.values()
                               if isinstance(v, list)),
                "update_time": data.get("update_time") or payload.get("update_time"),
                "elapsed_ms": int((time.time() - started) * 1000),
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            return self._data

    def meta(self):
        with _cache_lock:
            return dict(getattr(self, "_meta", {}) or {})

    def age(self):
        with _cache_lock:
            if not self._fetched_at:
                return None
            return int(time.time() - self._fetched_at)

    # ------------------------------------------------------------ 取 Cookie

    def match_domains(self, domain):
        """匹配主域及其全部子域，按域名字符串长度升序（主域在前，子域后覆盖）。"""
        data = self.fetch()
        domain = (domain or "").lower().lstrip(".")
        matched = []
        for k, v in data.items():
            if not isinstance(v, list):
                continue
            kk = str(k).lower().lstrip(".")
            if kk == domain or kk.endswith("." + domain):
                matched.append((kk, v))
        matched.sort(key=lambda x: len(x[0]))
        return matched

    def cookie_string(self, domain, only_names=None, skip_expired=True):
        """
        拼成 `name=value; name=value`。返回 (cookie_str, count, expired_names)。
        未匹配到时 cookie_str 为 None。
        """
        allowed = {n.lower() for n in (only_names or [])}
        merged = {}
        expired = []
        now = time.time()

        for _sub, items in self.match_domains(domain):
            for c in items:
                if not isinstance(c, dict):
                    continue
                name = str(c.get("name", "")).strip()
                value = c.get("value")
                if not name or value is None:
                    continue
                if allowed and name.lower() not in allowed:
                    continue
                if skip_expired:
                    exp = c.get("expirationDate")
                    if exp:
                        try:
                            exp = float(exp)
                            if exp > 1e12:
                                exp /= 1000
                            if 0 < exp < now:
                                expired.append(name)
                                continue
                        except (TypeError, ValueError):
                            pass
                merged[name] = str(value)

        if not merged:
            return None, 0, expired
        cookie_str = "; ".join("%s=%s" % (k, merged[k]) for k in sorted(merged))
        return cookie_str, len(merged), expired

    def domains(self):
        """已同步的域名清单（含 Cookie 数量），用于「设置」页展示。"""
        data = self.fetch()
        out = []
        for k, v in data.items():
            if isinstance(v, list):
                out.append({"domain": str(k), "count": len(v)})
        return sorted(out, key=lambda x: x["domain"])


_cache_lock = threading.RLock()
client = CookieCloudClient()
