"""a_bogus 签名（抖音 web 端，cus 变体）。

SM3 + 魔改 RC4 + 环境指纹 + 魔改 Base64，纯算法零依赖。
"""

from __future__ import annotations

import math
import random
import time
from typing import Callable, Optional, Sequence, Union

__all__ = [
    "SM3",
    "rc4_encrypt",
    "result_encrypt",
    "sign",
    "sign_datail",
    "sign_reply",
    "sign_abogus",
    "gen_ms_token",
    "gen_verify_fp",
]


def _now_ms() -> int:
    """JS Date.now() 等价物。"""
    return int(time.time() * 1000)


def rc4_encrypt(plaintext: str, key: str) -> str:
    """JS rc4_encrypt：对字符串按 charCode 逐字节 RC4。输入均为 Latin-1 范围字符串。"""
    s = list(range(256))
    j = 0
    key_len = len(key)
    for i in range(256):
        j = (j + s[i] + ord(key[i % key_len])) % 256
        s[i], s[j] = s[j], s[i]

    i = j = 0
    cipher = []
    for ch in plaintext:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        t = (s[i] + s[j]) % 256
        cipher.append(chr(s[t] ^ ord(ch)))
    return "".join(cipher)


# ===================== SM3 =====================

_MASK32 = 0xFFFFFFFF


def _le(e: int, r: int) -> int:
    """JS: (e << (r %= 32) | e >>> 32 - r) >>> 0（>>> 的移位量同样按 32 取模）。"""
    r %= 32
    return ((e << r) | (e >> ((32 - r) % 32))) & _MASK32


def _de(e: int) -> int:
    if 0 <= e < 16:
        return 2043430169
    if 16 <= e < 64:
        return 2055708042
    raise ValueError("invalid j for constant Tj")


def _pe(e: int, r: int, t: int, n: int) -> int:
    if 0 <= e < 16:
        return (r ^ t ^ n) & _MASK32
    if 16 <= e < 64:
        return ((r & t) | (r & n) | (t & n)) & _MASK32
    raise ValueError("invalid j for bool function FF")


def _he(e: int, r: int, t: int, n: int) -> int:
    if 0 <= e < 16:
        return (r ^ t ^ n) & _MASK32
    if 16 <= e < 64:
        return ((r & t) | (~r & n)) & _MASK32
    raise ValueError("invalid j for bool function GG")


class SM3:
    """SM3 实现（reg/chunk/size + write/sum/_compress/_fill）。"""

    def __init__(self) -> None:
        self.reg: list[int] = [0] * 8
        self.chunk: list[int] = []
        self.size: int = 0
        self.reset()

    def reset(self) -> None:
        self.reg = [
            1937774191,
            1226093241,
            388252375,
            3666478592,
            2842636476,
            372324522,
            3817729613,
            2969243214,
        ]
        self.chunk = []
        self.size = 0

    def write(self, data: Union[str, Sequence[int]]) -> None:
        # JS: 字符串走 encodeURIComponent 百分号编码（即 UTF-8 字节序列）
        if isinstance(data, str):
            a = list(data.encode("utf-8"))
        else:
            a = list(data)
        self.size += len(a)
        f = 64 - len(self.chunk)
        if len(a) < f:
            self.chunk = self.chunk + a
        else:
            self.chunk = self.chunk + a[:f]
            while len(self.chunk) >= 64:
                self._compress(self.chunk)
                if f < len(a):
                    self.chunk = a[f : min(f + 64, len(a))]
                else:
                    self.chunk = []
                f += 64

    def sum(self, data: Union[str, Sequence[int], None] = None, fmt: Optional[str] = None):
        if data:
            self.reset()
            self.write(data)
        self._fill()
        for f in range(0, len(self.chunk), 64):
            self._compress(self.chunk[f : f + 64])
        if fmt == "hex":
            # JS 原版此处调用了未定义的 se()（补零），会抛 ReferenceError；
            # 按意图实现为 8 位补零十六进制。
            result: Union[str, list[int]] = "".join(f"{r:08x}" for r in self.reg)
        else:
            arr = [0] * 32
            for f in range(8):
                c = self.reg[f]
                arr[4 * f + 3] = c & 255
                c >>= 8
                arr[4 * f + 2] = c & 255
                c >>= 8
                arr[4 * f + 1] = c & 255
                c >>= 8
                arr[4 * f] = c & 255
            result = arr
        self.reset()
        return result

    def _compress(self, t: Sequence[int]) -> None:
        if len(t) < 64:
            # JS 原版这里是数组与数字比较（恒 false），永不触发；Python 版保持容错不抛错。
            return
        # 消息扩展
        w = [0] * 132
        for i in range(16):
            w[i] = (
                (t[4 * i] << 24) | (t[4 * i + 1] << 16) | (t[4 * i + 2] << 8) | t[4 * i + 3]
            ) & _MASK32
        for n in range(16, 68):
            a = w[n - 16] ^ w[n - 9] ^ _le(w[n - 3], 15)
            a = a ^ _le(a, 15) ^ _le(a, 23)
            w[n] = (a ^ _le(w[n - 13], 7) ^ w[n - 6]) & _MASK32
        for n in range(64):
            w[n + 68] = (w[n] ^ w[n + 4]) & _MASK32

        reg = self.reg[:]
        for c in range(64):
            o = _le(reg[0], 12) + reg[4] + _le(_de(c), c)
            o = _le(o & _MASK32, 7)
            s = (o ^ _le(reg[0], 12)) & _MASK32
            u = _pe(c, reg[0], reg[1], reg[2])
            u = (u + reg[3] + s + w[c + 68]) & _MASK32
            b = _he(c, reg[4], reg[5], reg[6])
            b = (b + reg[7] + o + w[c]) & _MASK32
            reg = [
                u,
                reg[0],
                _le(reg[1], 9),
                reg[2],
                (b ^ _le(b, 9) ^ _le(b, 17)) & _MASK32,
                reg[4],
                _le(reg[5], 19),
                reg[6],
            ]
        for l in range(8):
            self.reg[l] = (self.reg[l] ^ reg[l]) & _MASK32

    def _fill(self) -> None:
        a = 8 * self.size
        self.chunk.append(128)
        # JS: var f = this.chunk.push(128) % 64（push 返回新长度）
        f = len(self.chunk) % 64
        if 64 - f < 8:
            f -= 64
        while f < 56:
            self.chunk.append(0)
            f += 1
        c = a // 4294967296  # Math.floor(a / 2**32)
        c &= _MASK32  # JS >>> 按 uint32 截断
        for i in range(4):
            self.chunk.append((c >> (8 * (3 - i))) & 255)
        a32 = a & _MASK32
        for i in range(4):
            self.chunk.append((a32 >> (8 * (3 - i))) & 255)


# ===================== 魔改 Base64 =====================

_S_OBJ = {
    "s0": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
    "s1": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
    "s2": "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
    "s3": "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
    "s4": "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
}


def _get_long_int(round_: int, long_str: str) -> int:
    # JS charCodeAt 越界返回 NaN，位运算时 NaN 按 0 处理；此处显式补 0
    r = round_ * 3

    def code(i: int) -> int:
        return ord(long_str[i]) if i < len(long_str) else 0

    return (code(r) << 16) | (code(r + 1) << 8) | code(r + 2)


def result_encrypt(long_str: str, num: Optional[str] = None) -> str:
    table = _S_OBJ[num]  # type: ignore[index]
    result: list[str] = []
    lound = 0
    long_int = _get_long_int(lound, long_str)
    # JS: for (i = 0; i < long_str.length / 3 * 4; i++)，界为非整数时向上取整
    count = math.ceil(len(long_str) / 3 * 4)
    for i in range(count):
        if i // 4 != lound:
            lound += 1
            long_int = _get_long_int(lound, long_str)
        key = i % 4
        if key == 0:
            result.append(table[(long_int & 16515072) >> 18])
        elif key == 1:
            result.append(table[(long_int & 258048) >> 12])
        elif key == 2:
            result.append(table[(long_int & 4032) >> 6])
        else:
            result.append(table[long_int & 63])
    return "".join(result)


def _gener_random(random_val: float, option: Sequence[int]) -> list[int]:
    # JS 位运算先把操作数 ToInt32（截断小数）
    r = int(random_val)
    return [
        ((r & 255) & 170) | (option[0] & 85),
        ((r & 255) & 85) | (option[0] & 170),
        (((r >> 8) & 255) & 170) | (option[1] & 85),
        (((r >> 8) & 255) & 85) | (option[1] & 170),
    ]


def _generate_rc4_bb_str(
    url_search_params: str,
    user_agent: str,
    window_env_str: str,
    suffix: str = "cus",
    arguments: Sequence[int] = (0, 1, 14),
    now: Optional[Callable[[], int]] = None,
) -> str:
    """RC4 字节流（generate_rc4_bb_str）。"""
    now_fn = now or _now_ms
    sm3 = SM3()
    start_time = now_fn()
    # 1: (url_search_params + suffix) 两次 sm3
    url_search_params_list = sm3.sum(sm3.sum(url_search_params + suffix))
    # 2: 后缀两次 sm3
    cus = sm3.sum(sm3.sum(suffix))
    # 3: UA 先 RC4（key 为 chr(0)+chr(1)+chr(arguments[2])，JS 0.00390625 经 fromCharCode 截断为 0）
    ua = sm3.sum(
        result_encrypt(
            rc4_encrypt(user_agent, chr(0) + chr(1) + chr(arguments[2])), "s3"
        )
    )
    end_time = now_fn()

    b: dict[int, int] = {}
    b[8] = 3  # 固定
    b[10] = end_time
    # b[15] 内联常量：aid=6383, pageId=6241
    page_id = 6241
    aid = 6383
    b[16] = start_time
    b[18] = 44  # 固定

    # 3次加密开始时间
    b[20] = (b[16] >> 24) & 255
    b[21] = (b[16] >> 16) & 255
    b[22] = (b[16] >> 8) & 255
    b[23] = b[16] & 255
    b[24] = int(b[16] / 256**4)  # JS: (b[16] / 2**32) >> 0
    b[25] = int(b[16] / 256**5)

    # Arguments [0, 1, 14, ...]
    b[26] = (arguments[0] >> 24) & 255
    b[27] = (arguments[0] >> 16) & 255
    b[28] = (arguments[0] >> 8) & 255
    b[29] = arguments[0] & 255

    b[30] = int(arguments[1] / 256) & 255  # JS: (Arguments[1] / 256) & 255
    b[31] = (arguments[1] % 256) & 255
    b[32] = (arguments[1] >> 24) & 255
    b[33] = (arguments[1] >> 16) & 255

    b[34] = (arguments[2] >> 24) & 255
    b[35] = (arguments[2] >> 16) & 255
    b[36] = (arguments[2] >> 8) & 255
    b[37] = arguments[2] & 255

    b[38] = url_search_params_list[21]
    b[39] = url_search_params_list[22]

    b[40] = cus[21]
    b[41] = cus[22]

    b[42] = ua[23]
    b[43] = ua[24]

    # 3次加密结束时间
    b[44] = (b[10] >> 24) & 255
    b[45] = (b[10] >> 16) & 255
    b[46] = (b[10] >> 8) & 255
    b[47] = b[10] & 255
    b[48] = b[8]
    b[49] = int(b[10] / 256**4)
    b[50] = int(b[10] / 256**5)

    # object 配置项
    b[51] = page_id
    b[52] = (page_id >> 24) & 255
    b[53] = (page_id >> 16) & 255
    b[54] = (page_id >> 8) & 255
    b[55] = page_id & 255

    b[56] = aid
    b[57] = aid & 255
    b[58] = (aid >> 8) & 255
    b[59] = (aid >> 16) & 255
    b[60] = (aid >> 24) & 255

    window_env_list = [ord(c) for c in window_env_str]
    b[64] = len(window_env_list)
    b[65] = b[64] & 255
    b[66] = (b[64] >> 8) & 255

    b[69] = 0
    b[70] = b[69] & 255
    b[71] = (b[69] >> 8) & 255

    b[72] = (
        b[18] ^ b[20] ^ b[26] ^ b[30] ^ b[38] ^ b[40] ^ b[42] ^ b[21] ^ b[27] ^ b[31]
        ^ b[35] ^ b[39] ^ b[41] ^ b[43] ^ b[22] ^ b[28] ^ b[32] ^ b[36] ^ b[23] ^ b[29]
        ^ b[33] ^ b[37] ^ b[44] ^ b[45] ^ b[46] ^ b[47] ^ b[48] ^ b[49] ^ b[50] ^ b[24]
        ^ b[25] ^ b[52] ^ b[53] ^ b[54] ^ b[55] ^ b[57] ^ b[58] ^ b[59] ^ b[60] ^ b[65]
        ^ b[66] ^ b[70] ^ b[71]
    )
    bb = [
        b[18], b[20], b[52], b[26], b[30], b[34], b[58], b[38], b[40], b[53], b[42],
        b[21], b[27], b[54], b[55], b[31], b[35], b[57], b[39], b[41], b[43], b[22],
        b[28], b[32], b[60], b[36], b[23], b[29], b[33], b[37], b[44], b[45], b[59],
        b[46], b[47], b[48], b[49], b[50], b[24], b[25], b[65], b[66], b[70], b[71],
    ]
    bb = bb + window_env_list + [b[72]]
    return rc4_encrypt("".join(chr(x) for x in bb), chr(121))


def _generate_random_str(rand: Callable[[], float]) -> str:
    out: list[int] = []
    out += _gener_random(rand() * 10000, [3, 45])
    out += _gener_random(rand() * 10000, [1, 0])
    out += _gener_random(rand() * 10000, [1, 5])
    return "".join(chr(c) for c in out)


def sign(
    url_search_params: str,
    user_agent: str,
    sign_args: Sequence[int] = (0, 1, 14),
    *,
    _now: Optional[Callable[[], int]] = None,
    _rand: Optional[Callable[[], float]] = None,
) -> str:
    """对 query string 计算 a_bogus。_now/_rand 仅供测试注入（等价 JS Date.now/Math.random）。"""
    rand = _rand or random.random
    result_str = _generate_random_str(rand) + _generate_rc4_bb_str(
        url_search_params,
        user_agent,
        "1536|747|1536|834|0|30|0|0|1536|834|1536|864|1525|747|24|24|Win32",
        "cus",
        sign_args,
        now=_now,
    )
    return result_encrypt(result_str, "s4") + "="


def sign_datail(params: str, user_agent: str) -> str:
    return sign(params, user_agent, (0, 1, 14))


def sign_reply(params: str, user_agent: str) -> str:
    return sign(params, user_agent, (0, 1, 8))


# ===================== abogus.js 封装层 =====================

_MSTOKEN_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789=_-"
_VERIFY_FP_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gen_ms_token(length: int = 107) -> str:
    """生成随机伪 msToken（服务端只做格式校验，随机串即可通过）。"""
    return "".join(random.choice(_MSTOKEN_CHARS) for _ in range(length))


def gen_verify_fp() -> str:
    """生成合法格式的 verifyFp / fp（verify_xxxx 前缀随机串）。"""
    seg = lambda n: "".join(random.choice(_VERIFY_FP_CHARS) for _ in range(n))  # noqa: E731
    return f"verify_{seg(8)}_{seg(4)}_{seg(4)}_{seg(4)}_{seg(12)}"


def sign_abogus(query_string: str, user_agent: str) -> str:
    """对 query string 计算 a_bogus 签名（通用 GET 接口），返回签名（含结尾 =）。"""
    return sign_datail(query_string, user_agent)
