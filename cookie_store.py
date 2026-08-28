"""
从本机已安装的 Chrome / Edge 用户配置里提取 Google 登录 cookie（只读，不改原文件）。

原理：Chrome 把 cookie 存在 <User Data>/<Profile>/Network/Cookies 的 SQLite 里，
值用 AES-256-GCM 加密，主密钥在 Local State 的 os_crypt.encrypted_key 中、由 Windows
DPAPI 保护。这里复制一份 db（绕过 Chrome 的文件锁），解密后取回注入自动化浏览器。

新版 Chrome 的 v20 条目走 App-Bound 加密，浏览器外无法解密；遇到时跳过并计数，
若关键登录 cookie 恰好被它挡住，只能退化为在窗口里手动登录一次。
"""
import base64
import ctypes
import ctypes.wintypes
import glob
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

COOKIE_1PSID = "__Secure-1PSID"
COOKIE_1PSIDTS = "__Secure-1PSIDTS"

# 注入 Google 会话通常需要的核心 cookie 集
SEED_NAMES = [
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-1PSIDCC",
    "__Secure-3PSID", "__Secure-3PSIDTS", "__Secure-3PAPISID",
    "NID", "__Secure-ENID",
]

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
BROWSER_DIRS = {
    "chrome": LOCALAPPDATA / r"Google\Chrome\User Data",
    "edge": LOCALAPPDATA / r"Microsoft\Edge\User Data",
}

skipped_v20 = 0  # 解密过程中遇到的 App-Bound(v20) 条目数


def _dpapi_unprotect(data: bytes) -> bytes | None:
    if not hasattr(_dpapi_unprotect, "_localfree"):
        ctypes.windll.kernel32.LocalFree.restype = ctypes.wintypes.HLOCAL
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.wintypes.HLOCAL]
        _dpapi_unprotect._localfree = True

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.wintypes.LPVOID),  # 必须是裸指针：c_char_p 取值会被复制导致释放错误地址
        ]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.wintypes.LPVOID))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        return None
    addr = blob_out.pbData
    size = blob_out.cbData
    result = ctypes.string_at(addr, size)
    try:
        ctypes.windll.kernel32.LocalFree(ctypes.wintypes.HLOCAL(addr))
    except Exception:
        pass
    return result


def _load_master_key(user_data_dir: Path) -> bytes | None:
    local_state = user_data_dir / "Local State"
    if not local_state.exists():
        return None
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        encrypted = base64.b64decode(data["os_crypt"]["encrypted_key"])
        if encrypted[:5] != b"DPAPI":
            return None
        return _dpapi_unprotect(encrypted[5:])
    except Exception:
        return None


def _decrypt_value(raw: bytes, key: bytes) -> str | None:
    """v10/v11 = AES-GCM(nonce=val[3:15], ct+tag)；v20 = App-Bound，无法离线解密。"""
    global skipped_v20
    try:
        version = raw[:3]
        if version in (b"v10", b"v11"):
            from Crypto.Cipher import AES

            nonce, ciphertext, tag = raw[3:15], raw[15:-16], raw[-16:]
            plain = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ciphertext, tag)
            return plain.decode("utf-8")
        if version == b"v20":
            skipped_v20 += 1
            return None
    except Exception:
        pass
    return None


def _profile_dirs(user_data_dir: Path) -> list[Path]:
    """Default 排最前，其余 Profile* 按修改时间新到旧。"""
    result = []
    default = user_data_dir / "Default"
    if (default / "Network" / "Cookies").exists():
        result.append(default)
    profiles = [Path(p) for p in glob.glob(str(user_data_dir / "Profile *"))]
    profiles.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in profiles:
        if (p / "Network" / "Cookies").exists():
            result.append(p)
    return result


def _copy_db(cookies_db: Path) -> Path | None:
    tmp = Path(tempfile.gettempdir()) / f"gemini2a_cookies_{os.getpid()}_{time.time_ns()}.db"
    for _ in range(4):  # Chrome 运行时文件偶发被短暂锁住，重试几次
        try:
            shutil.copy2(cookies_db, tmp)
            return tmp
        except OSError:
            try:
                with open(cookies_db, "rb") as src, open(tmp, "wb") as dst:
                    dst.write(src.read())
                return tmp
            except OSError:
                time.sleep(0.35)
    return None


def collect_seed_cookies(preferred_browser: str | None = None) -> dict:
    """
    返回 {cookie名: 值}，键里不含 '_source'；来源见单独的 source 字段。
    找不到任何可用的 __Secure-1PSID 时返回空 dict。
    """
    global skipped_v20
    skipped_v20 = 0

    browsers = (
        {preferred_browser: BROWSER_DIRS[preferred_browser]}
        if preferred_browser in BROWSER_DIRS
        else BROWSER_DIRS
    )
    for name, user_data_dir in browsers.items():
        key = _load_master_key(user_data_dir)
        if not key:
            continue
        placeholders = f"({','.join('?' * len(SEED_NAMES))})"
        for profile in _profile_dirs(user_data_dir):
            cookies_db = profile / "Network" / "Cookies"
            tmp = _copy_db(cookies_db)
            if not tmp:
                continue
            found: dict[str, str] = {}
            try:
                conn = sqlite3.connect(tmp)
                rows = conn.execute(
                    "SELECT host_key, name, encrypted_value FROM cookies "
                    f"WHERE host_key LIKE '%google.com' AND name IN {placeholders}",
                    SEED_NAMES,
                ).fetchall()
                conn.close()
            except Exception:
                rows = []
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass

            for _host, cname, enc in rows:
                value = _decrypt_value(enc, key)
                # 真实 1PSID 是 g.a000... 的长串，明显占位/损坏则不要
                if not value or (cname == COOKIE_1PSID and ("." in value and len(value) < 60)):
                    continue
                found[cname] = value

            if found.get(COOKIE_1PSID):
                found["_source"] = f"{name}:{profile.name}"
                if skipped_v20:
                    print(f"[gemini2a] 跳过了 {skipped_v20} 个 App-Bound(v20) 加密条目")
                return found
    return {}
