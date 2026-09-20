"""a_bogus 签名（抖音 imdesktop.douyin.com，dhzx 变体）。

签名用在 imdesktop passport 接口（/passport/web/get_qrcode/ 、
/passport/web/check_qrconnect/ 等），与 web 端 cus 变体算法不同。

算法组成：
  - 双重 SM3（params 与 data 都做 getArr(getArrStr(...))）
  - RC4 变体（abArr256 / uaArr256 + garble）
  - 自定义 base64（shortStr = s4，含 = 填充与 JS 一致）

复用本仓库 utils/abogus.py 的 SM3 / rc4_encrypt。
"""

from __future__ import annotations

import random
import time
from typing import Callable, Optional

from .abogus import SM3

# 固定环境串（抖音 imdesktop 客户端默认值）
_FIXED_ENV = "784|943|1707|1019|1707|1019|1707|1067|MacIntel"

# base64 编码表（与 web 端 s4 相同）
_SHORT_STR = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"

# 1721836800000 = 2024-07-25 00:00:00 UTC 的毫秒数；1209600000 = 14 天
_BASE_TIME_MS = 1721836800000
_PERIOD_MS = 1209600000


def _codes_of(s: str) -> list[int]:
    """UTF-8 字节数组（与 JS `Array.from(Buffer.from(s, 'utf8'))` 等价）。"""
    return list(s.encode("utf-8"))


def _hex_to_arr(hex_str: str) -> list[int]:
    return list(bytes.fromhex(hex_str))


def _get_arr(sm3_input) -> list[int]:
    """SM3 输入（字符串或字节数组）→ 32 字节数组。

    与 JS `hexToArr(sm3(input))` 等价：sm3 接受字符串（UTF-8 编码）或字节数组。
    Python SM3().sum 已经支持两种入参，所以此处只需 SM3 → hex → bytes。
    """
    return _hex_to_arr(SM3().sum(sm3_input, fmt="hex"))


def _get_arr_str(s: str) -> list[int]:
    """等价 JS `getArr(codesOf(s))`：SM3 → hex → bytes（1 层 SM3）。

    这就是 JS 里 getArrStr 的实现：
        getArrStr(s) = getArr(codesOf(s)) = hexToArr(sm3(codesOf(s)))
    第二层 SM3 在调用方做（`getArr(getArrStr(s))`），不要在这里加。
    """
    return _get_arr(_codes_of(s))


def _ab_arr256() -> list[int]:
    """JS switch 两分支最终都是这串的 RC4 初始置换（lm=211）。"""
    nums = list(range(255, -1, -1))
    prev = 0
    lm = 211
    for i in range(256):
        prev = (prev * nums[i] + prev + lm) % 256
        nums[i], nums[prev] = nums[prev], nums[i]
    return nums


def _ua_arr256(ua_salt: int) -> list[int]:
    """UA 用的 RC4 初始置换（lm = [0, 1, ua_salt]）。"""
    nums = list(range(255, -1, -1))
    prev = 0
    lm = [0, 1, ua_salt]
    for i in range(256):
        prev = (prev * nums[i] + prev + lm[i % 3]) % 256
        nums[i], nums[prev] = nums[prev], nums[i]
    return nums


def _garble(arr256: list[int], input_bytes: list[int]) -> list[int]:
    """RC4 流变体（修改 arr256 自身，调用方按需复制）。"""
    n4 = 0
    ans: list[int] = []
    for i in range(len(input_bytes)):
        n2 = (i + 1) % 256
        n4 = (n4 + arr256[n2]) % 256
        old = arr256[n2]
        arr256[n2] = arr256[n4]
        arr256[n4] = old
        n7 = (arr256[n2] + old) % 256
        ans.append(input_bytes[i] ^ arr256[n7])
    return ans


def _encryption_ua(ss: list[int]) -> list[int]:
    """自定义 base64（与 web 端 s3 等价表），返回码点数组。"""
    str_table = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"
    out: list[int] = []
    j = 0
    for i in range(0, len(ss), 3):
        if i + 3 <= len(ss):
            number = ((ss[i] & 255) << 16) | ((ss[i + 1] & 255) << 8) | (ss[i + 2] & 255)
            out.extend([
                ord(str_table[(number & 16515072) >> 18]),
                ord(str_table[(number & 258048) >> 12]),
                ord(str_table[(number & 4032) >> 6]),
                ord(str_table[number & 63]),
            ])
        if i + 3 > len(ss):
            if len(ss) - j == 2:
                b = (ss[j + 1] << 8) | (ss[j] << 16)
                out.extend([
                    ord(str_table[(b & 16515072) >> 18]),
                    ord(str_table[(b & 258048) >> 12]),
                    ord(str_table[(b & 4032) >> 6]),
                    61,  # '='
                ])
            elif len(ss) - j == 1:
                b = ss[j] << 16
                out.extend([
                    ord(str_table[(b & 16515072) >> 18]),
                    ord(str_table[(b & 258048) >> 12]),
                    61,  # '='
                    61,  # '='
                ])
        j += 3
    return out


def _get_arr2() -> list[int]:
    return _codes_of(_FIXED_ENV)


def _get_last3_num(dt1: int) -> list[int]:
    return _codes_of(str((dt1 + 3) & 255) + ",")


def _b8(v: int, k: int) -> int:
    """Go 风格位移：k 可超过 32，用除法取字节。"""
    return int(v / 2 ** k) & 255


def _get_last_num2(arr1: list[int], arr: list[int]) -> int:
    indices = [
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
        20, 21, 22, 23, 25, 26, 27, 29, 30, 31, 33, 34, 35, 36, 37, 38, 39, 40,
        41, 42, 43, 44, 45, 46, 47, 48, 50, 51, 53, 54,
    ]
    x = arr1[0] ^ arr1[1] ^ arr1[2] ^ arr1[3] ^ arr1[4] ^ arr1[5] ^ arr1[6] ^ arr1[7]
    for i in indices:
        x ^= arr[i]
    return x


def _get_num_list(rand: Callable[[], float], arr0: list[int], arr_ar: list[int]) -> list[int]:
    num_list: list[int] = []
    n = len(arr_ar)
    for i in range(0, n, 3):
        if i + 2 >= n:
            if i + 1 >= n:
                num_list.append(arr_ar[i])
            else:
                num_list.extend([arr_ar[i], arr_ar[i + 1]])
        else:
            random_byte = int(rand() * 1000) & 255
            num_list.extend([
                (random_byte & 145) | (arr_ar[i] & 110),
                (random_byte & 66) | (arr_ar[i + 1] & 189),
                (random_byte & 44) | (arr_ar[i + 2] & 211),
                ((arr_ar[i] & 145) | (arr_ar[i + 1] & 66)) | (arr_ar[i + 2] & 44),
            ])
    return [*arr0, *num_list]


def _top_header(rand: Callable[[], float]) -> list[int]:
    num1 = int(rand() * 65535) & 255
    num2 = int(rand() * 40)
    return [
        (num1 & 170) | (3 & 85), (num1 & 85) | (3 & 170),
        (num2 & 170) | (82 & 85), (num2 & 85) | (82 & 170),
    ]


def _random_garbled_list(rand: Callable[[], float]) -> list[int]:
    r1 = int(rand() * 65535)
    num1a = r1 & 255
    num2a = (r1 >> 8) & 255
    num1b = int(rand() * 240)
    num2b = (int(rand() * 255) & 77) | 2 | 16 | 32 | 128
    return [
        (num1a & 170) | (1 & 85), (num1a & 85) | (1 & 170),
        (num2a & 170) | (0 & 85), (num2a & 85) | (0 & 170),
        (num1b & 170) | (1 & 85), (num1b & 85) | (1 & 170),
        (num2b & 170) | (0 & 85), (num2b & 85) | (0 & 170),
    ]


def _get_arr29(
    rand: Callable[[], float],
    now: int,
    dt1: int,
    dt2: int,
    params: str,
    data: str,
    user_agent: str,
) -> list[int]:
    par_arr = _get_arr(_get_arr_str(params))  # 双重 SM3
    data_arr = _get_arr(_get_arr_str(data))
    ua_salt = 0
    browser_arr = _get_arr(_encryption_ua(_garble(_ua_arr256(ua_salt), _codes_of(user_agent))))
    num = _get_arr2()
    date_time3 = int((now - _BASE_TIME_MS) / _PERIOD_MS)
    arr0 = _random_garbled_list(rand)

    arr = [0] * 55
    arr[0] = 41
    arr[1] = date_time3
    arr[2] = 5
    arr[3] = (int(dt1 - dt2) + 3) & 255
    arr[4] = _b8(dt1, 0)
    arr[5] = _b8(dt1, 8)
    arr[6] = _b8(dt1, 16)
    arr[7] = _b8(dt1, 24)
    arr[8] = _b8(dt1, 32)
    arr[9] = _b8(dt1, 40)
    arr[10] = 1
    arr[11] = 0
    arr[12] = 1
    arr[13] = 0
    arr[14] = 1
    arr[15] = 0
    arr[16] = 0
    arr[17] = 0
    arr[18] = ua_salt & 255
    arr[19] = (ua_salt >> 8) & 255
    arr[20] = (ua_salt >> 16) & 255
    arr[21] = (ua_salt >> 24) & 255
    arr[22] = par_arr[9]
    arr[23] = par_arr[18]
    arr[24] = 3
    arr[25] = par_arr[3]
    arr[26] = data_arr[10]
    arr[27] = data_arr[19]
    arr[28] = 4
    arr[29] = data_arr[4]
    arr[30] = browser_arr[11]
    arr[31] = browser_arr[21]
    arr[32] = 5
    arr[33] = browser_arr[5]
    arr[34] = _b8(dt2, 0)
    arr[35] = _b8(dt2, 8)
    arr[36] = _b8(dt2, 16)
    arr[37] = _b8(dt2, 24)
    arr[38] = _b8(dt2, 32)
    arr[39] = _b8(dt2, 40)
    arr[40] = 3
    arr32 = 6241
    arr[41] = arr32 & 255
    arr[42] = (arr32 >> 8) & 255
    arr[43] = (arr32 >> 16) & 255
    arr[44] = (arr32 >> 24) & 255
    arr36 = 6383
    arr[45] = arr36 & 255
    arr[46] = (arr36 >> 8) & 255
    arr[47] = (arr36 >> 16) & 255
    arr[48] = (arr36 >> 24) & 255
    last_num_one = _get_last3_num(dt1)
    arr[49] = len(num)
    arr[50] = len(num) & 255
    arr[51] = (len(num) >> 8) & 255
    arr[52] = len(last_num_one)
    arr[53] = len(last_num_one) & 255
    arr[54] = (len(last_num_one) >> 8) & 255
    last_num = _get_last_num2(arr0, arr)

    order = [
        9, 18, 30, 35, 47, 4, 44, 19, 10, 23,
        12, 40, 25, 42, 3, 22, 38, 21, 5, 45,
        1, 29, 6, 43, 33, 14, 36, 37, 2, 46,
        15, 48, 31, 26, 16, 13, 8, 41, 27, 17,
        39, 20, 11, 0, 34, 7, 50, 51, 53, 54,
    ]
    arr2 = [arr[o] for o in order]
    new_arr2 = [*arr2, *num, *last_num_one, last_num]
    return _get_num_list(rand, arr0, new_arr2)


def _get_garbled_string(
    rand: Callable[[], float],
    now: int,
    params: str,
    data: str,
    user_agent: str,
) -> list[int]:
    t1 = now
    t2 = t1 - int(rand() * 10)
    arr29 = _get_arr29(rand, now, t1, t2, params, data, user_agent)
    a = _top_header(rand)
    b = _garble(_ab_arr256(), arr29)
    return [*a, *b]


def _custom_base64_encode(garbled: list[int]) -> str:
    """与 JS getABogus 完全一致的 base64 编码（含末尾 = 填充）。

    不能复用 utils.abogus.result_encrypt：它对末尾非整 3 倍数会用幻影 0 字节
    输出 4 个字符，而 JS 用 = 截断，长度与内容都不一样。
    """
    sb: list[str] = []
    n = len(garbled)
    i = 0
    j = 0
    while i <= n:
        if i + 3 <= n:
            base_num = garbled[i + 2] | (garbled[i + 1] << 8) | (garbled[i] << 16)
            sb.append(_SHORT_STR[(base_num & 16515072) >> 18])
            sb.append(_SHORT_STR[(base_num & 258048) >> 12])
            sb.append(_SHORT_STR[(base_num & 4032) >> 6])
            sb.append(_SHORT_STR[base_num & 63])
        if i + 3 > n:
            rem = n - j
            if rem == 2:
                base_num = (garbled[j + 1] << 8) | (garbled[j] << 16)
                sb.append(_SHORT_STR[(base_num & 16515072) >> 18])
                sb.append(_SHORT_STR[(base_num & 258048) >> 12])
                sb.append(_SHORT_STR[(base_num & 4032) >> 6])
                sb.append("=")
            elif rem == 1:
                base_num = garbled[j] << 16
                sb.append(_SHORT_STR[(base_num & 16515072) >> 18])
                sb.append(_SHORT_STR[(base_num & 258048) >> 12])
                sb.append("==")
        i += 3
        j += 3
    return "".join(sb)


def sign_a_bogus(
    query_string: str,
    body_str: str,
    user_agent: str,
    now_ms: Optional[int] = None,
    *,
    _rand: Optional[Callable[[], float]] = None,
) -> str:
    """imdesktop 端 a_bogus 签名（dhzx 变体）。

    Args:
        query_string: 已编码的 query 串（不含前导 ?）
        body_str: 已编码的 POST body（GET 时传空串）
        user_agent: User-Agent
        now_ms: 当前毫秒时间戳（默认 time.time()*1000）
        _rand: 测试注入随机源（默认 random.random）
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rand = _rand if _rand is not None else random.random
    # JS: const params = url.slice(url.indexOf('?') + 1) + 'dhzx'
    # query_string 不含 ?，indexOf 返回 -1，slice(0) = 整串
    q_index = query_string.find("?")
    params = (query_string[q_index + 1:] if q_index >= 0 else query_string) + "dhzx"
    data = str(body_str) + "dhzx"
    garbled = _get_garbled_string(rand, now, params, data, user_agent)
    return _custom_base64_encode(garbled)