#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二维码掩码显影工具  QR Mask Reveal / Rebuild
=================================================================
解析模式：高清 QR 图片 -> payload、mask 掩码编号(0~7)、纠错等级、版本
生成模式：给定「时间戳」-> 套用扫描件模板生成同族二维码（掩码/纠错/固定段照抄）
=================================================================

【核心校验点 · 读代码请先看这里】
1) 生成侧：qrcode 库「原生」支持指定掩码，关键参数就是 mask_pattern。
       qrcode.QRCode(version=..., error_correction=..., box_size=...,
                     border=..., mask_pattern=mask)   # mask: 0~7
   一旦显式传入 mask_pattern，库就不会再按 penalty 规则自动挑掩码，
   而是把你指定的掩码直接写进格式信息(Format Information, 15bit BCH)。
   因此：version + 纠错等级 + 载荷 + mask_pattern 四项一致，产出的模块
   矩阵就与原图逐模块相同 —— 这就是「复现同款 QR 对照验证规律」的地基。

2) 解析侧：zxing-cpp 读 mask_pattern。
   Result.extra 中直接带 ['DataMask']（即掩码 0~7）、['Version']、
   ['ECLevel']、['UEC']：
       r = zxingcpp.read_barcode(img)
       mask = int(r.extra["DataMask"])
   该值等于格式信息里那 3bit 掩码解码后的结果，可直接回填给
   qrcode 的 mask_pattern 参数。
   兼容兜底：个别旧版 zxing-cpp 不暴露 extra，则用本程序内置的几何法
   ——按 ZXing 的读取顺序从模块矩阵的格式信息区取 15bit，做 BCH(15,5)
   最小汉明距离纠错，反解出 (纠错等级, 掩码)。两条路径语义完全一致。

3) 闭环自证：[3] 生成完会立刻调用 [1] 的解析器回读刚生成的图片，
   比对 掩码 / 纠错等级 / 版本 / 载荷，四项全中即证明「读出来的掩码
   参数可以原样回填给生成器复现」。

4) 先后规律（实测结论）：掩码只由 纠错等级+掩码编号 决定，与生成先后无关；
   真正能区分顺序的是载荷里的 Unix 时间戳段。所以 [3] 只让你填时间，
   固定段、掩码、纠错、版本全部从扫描件里学来，直接套用。

依赖：zxing-cpp、pillow、qrcode（打包另需 pyinstaller）
"""

import os
import re
import sys
import time
import traceback
import subprocess
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 依赖导入
_DEP_MISSING = []

try:
    from PIL import Image, ImageOps
except Exception:                                   # pragma: no cover
    Image = None
    _DEP_MISSING.append("pillow")

try:
    import zxingcpp
except Exception:                                   # pragma: no cover
    zxingcpp = None
    _DEP_MISSING.append("zxing-cpp")

try:
    import qrcode
    from qrcode.constants import (
        ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H,
    )
except Exception:                                   # pragma: no cover
    qrcode = None
    _DEP_MISSING.append("qrcode")

# ---------------------------------------------------------------- 常量表
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp",
              ".tif", ".tiff", ".ppm")

EC_CHOICES = ("L", "M", "Q", "H")

LEVEL_TO_EC_CONST = {
    "L": ERROR_CORRECT_L if qrcode else None,
    "M": ERROR_CORRECT_M if qrcode else None,
    "Q": ERROR_CORRECT_Q if qrcode else None,
    "H": ERROR_CORRECT_H if qrcode else None,
}

# 格式信息的 2bit 纠错等级编码 -> 字母（QR 规范：01=L 00=M 11=Q 10=H）
EC_BITS_TO_LEVEL = {0b01: "L", 0b00: "M", 0b11: "Q", 0b10: "H"}

FORMAT_XOR_MASK = 0x5412      # 格式信息整体异或的掩码
FORMAT_POLY = 0x537           # BCH(15,5) 生成多项式

# 7x7 定位图案（1=黑）标准形状，几何兜底解析时用于校验网格对齐
FINDER_PATTERN = (
    (1, 1, 1, 1, 1, 1, 1),
    (1, 0, 0, 0, 0, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 0, 0, 0, 0, 1),
    (1, 1, 1, 1, 1, 1, 1),
)


# ================================================================ 通用小工具
def safe_console():
    """
    中文输出兜底：
    · 交互式控制台（真窗口）：Python 在 Windows 上走 UTF-8 控制台 API，原样输出；
    · 被重定向/管道（非 tty）：编码常是 GBK 或 ASCII，容易出乱码，
      这里统一改成 UTF-8，方便把日志落盘给别人看。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def natural_key(name):
    """文件名自然排序：qr_2.png 排在 qr_10.png 前。"""
    import re
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", name)]


def short_text(text, limit=58):
    """超长载荷截断显示，避免刷屏。"""
    if text is None:
        return ""
    one = " ".join(str(text).split())
    if len(one) <= limit:
        return one
    return one[:limit] + "…(%d)" % len(one)


def short_name(name, width=30):
    """长文件名截断时保留结尾后缀（(1)/(2).jpg 这类区分号在尾部）。"""
    if len(name) <= width:
        return name
    tail = min(10, width - 6)
    return name[:width - tail - 1] + "…" + name[-tail:]


def list_images(folder):
    """列出目录下全部图片，按自然顺序排序。"""
    names = []
    for n in os.listdir(folder):
        if os.path.splitext(n)[1].lower() in IMAGE_EXTS:
            names.append(n)
    names.sort(key=natural_key)
    return names


# ================================================================ 数据结构
class QRInfo:
    """一次解析的全部结果。"""

    __slots__ = ("path", "payload", "mask", "ec_level", "version",
                 "mask_source", "confidence", "extra")

    def __init__(self):
        self.path = ""
        self.payload = ""
        self.mask = None
        self.ec_level = None
        self.version = None
        self.mask_source = "unknown"
        self.confidence = "n/a"
        self.extra = {}

    @property
    def size(self):
        if self.version is None:
            return None
        return self.version * 4 + 17


# ================================================================ 掩码解析
def mask_from_zxing_extra(result):
    """
    核心校验点 2：zxing-cpp 直接给掩码。
    Result.extra['DataMask'] 就是格式信息解出的 3bit 掩码（0~7）。
    同时顺手取 Version / ECLevel，供生成模式回填。
    """
    extra = getattr(result, "extra", None)
    info = {}
    if isinstance(extra, dict):
        for key in ("DataMask", "Version", "ECLevel", "UEC"):
            if key in extra:
                info[key] = extra[key]
    # 属性直读的兼容写法（不同版本绑定字段名略有差异）
    for attr in ("mask_pattern", "maskPattern"):
        value = getattr(result, attr, None)
        if isinstance(value, int) and 0 <= value <= 7 and "DataMask" not in info:
            info["DataMask"] = value

    mask = info.get("DataMask")
    try:
        mask = int(mask) if mask is not None else None
    except (TypeError, ValueError):
        mask = None
    if isinstance(mask, int) and not 0 <= mask <= 7:
        mask = None

    version = info.get("Version")
    try:
        version = int(version) if version is not None else None
    except (TypeError, ValueError):
        version = None

    return mask, version, info


def _build_format_table():
    """
    生成 32 个合法格式信息码字 -> (纠错等级, 掩码) 的对照表。
    码字 = (data5 << 10 | BCH余数) ^ 0x5412，data5 = ec_bits(2bit) << 3 | mask(3bit)
    """
    table = {}
    for data5 in range(32):
        value = data5 << 10
        rem = value
        for shift in range(4, -1, -1):
            if rem & (1 << (shift + 10)):
                rem ^= FORMAT_POLY << shift
        codeword = ((data5 << 10) | rem) ^ FORMAT_XOR_MASK
        table[codeword] = (EC_BITS_TO_LEVEL[(data5 >> 3) & 0b11], data5 & 0b111)
    return table


FORMAT_TABLE = _build_format_table()


def decode_format_bits(bits15):
    """15bit -> (纠错等级, 掩码)；允许 <=3bit 误差（规范纠错能力）。"""
    best = None
    for codeword, payload in FORMAT_TABLE.items():
        dist = bin(codeword ^ bits15).count("1")
        if dist <= 3 and (best is None or dist < best[0]):
            best = (dist, payload)
    if best is None:
        return None
    return best[1]


def _otsu_threshold(gray):
    """Otsu 全局阈值，二值化用。"""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if total == 0:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    weight_b = 0
    sum_b = 0.0
    best_t, best_var = 128, -1.0
    for t in range(256):
        weight_b += hist[t]
        if weight_b == 0:
            continue
        weight_f = total - weight_b
        if weight_f == 0:
            break
        sum_b += t * hist[t]
        mean_b = sum_b / weight_b
        mean_f = (sum_all - sum_b) / weight_f
        var = weight_b * weight_f * (mean_b - mean_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def _binarize(gray, threshold, invert=False):
    """返回模式 L 的黑白图：黑像素=255，白像素=0。"""
    return gray.point(lambda v: 255 if ((v <= threshold) != bool(invert)) else 0)


def _prepare_binary(gray):
    """
    Otsu 阈值 + 退化保护。
    纯黑白截图（只有 0/255 两个灰度）会让 Otsu 退化成 0，
    此时改用 (最暗+最亮)/2 做中点阈值，保证二值化仍然正确。
    """
    low, high = gray.getextrema()
    if high - low < 20:                 # 几乎纯色图，没有可用对比
        return None, None
    threshold = _otsu_threshold(gray)
    if threshold <= low or threshold >= high:
        threshold = (low + high) // 2
    return _binarize(gray, threshold), threshold


def _sample_matrix(bin_img, box, n):
    """把符号区域按 n x n 网格重采样成模块矩阵（True=黑模块）。"""
    left, top, right, bottom = box
    cell = bin_img.crop((left, top, right, bottom)).resize((n, n), Image.BOX)
    data = cell.tobytes()          # 模式 L，直接拿 0~255 字节，比 getdata 快且无弃用告警
    matrix = []
    for row in range(n):
        base = row * n
        matrix.append([data[base + col] >= 128 for col in range(n)])
    return matrix


def _sample_matrix_inset(bin_img, box, n, inset):
    left, top, right, bottom = box
    sub = (left + inset, top + inset, right - inset, bottom - inset)
    if sub[2] - sub[0] < n or sub[3] - sub[1] < n:
        return None
    return _sample_matrix(bin_img, sub, n)


def _finder_score(matrix, n):
    """三个角上 7x7 定位图案匹配度，用于确认网格/版本猜对了。"""
    hit = total = 0
    for origin_r, origin_c in ((0, 0), (0, n - 7), (n - 7, 0)):
        for r in range(7):
            for c in range(7):
                total += 1
                if matrix[origin_r + r][origin_c + c] == bool(FINDER_PATTERN[r][c]):
                    hit += 1
    return hit / total if total else 0.0


def _timing_score(matrix, n):
    """水平定时图案（第 6 行，x=8..n-9 黑白交替，偶数位为黑）匹配度。"""
    hit = total = 0
    for c in range(8, n - 8):
        total += 1
        if matrix[6][c] == (c % 2 == 0):
            hit += 1
    return hit / total if total else 0.0


def _read_format_bits(matrix, n):
    """按 ZXing 读取顺序取两份 15bit 格式信息（matrix[row][col]）。"""
    def bit(row, col):
        if 0 <= row < n and 0 <= col < n:
            return 1 if matrix[row][col] else 0
        return 0

    bits1 = 0
    for col in range(6):
        bits1 = (bits1 << 1) | bit(8, col)
    bits1 = (bits1 << 1) | bit(8, 7)
    bits1 = (bits1 << 1) | bit(8, 8)
    bits1 = (bits1 << 1) | bit(7, 8)
    for row in range(5, -1, -1):
        bits1 = (bits1 << 1) | bit(row, 8)

    bits2 = 0
    for row in range(n - 1, n - 8, -1):
        bits2 = (bits2 << 1) | bit(row, 8)
    for col in range(n - 8, n):
        bits2 = (bits2 << 1) | bit(8, col)

    return bits1, bits2


def mask_via_geometry(gray, expect_ec=None):
    """
    兼容兜底路径：zxing-cpp 若不暴露 extra，则自己从图像重建模块矩阵，
    读格式信息区 15bit -> BCH 纠错 -> (纠错等级, 掩码)。
    expect_ec 传入 zxing 已识别出的纠错等级，用来交叉验证网格是否采样正确。
    只对干净、正方的 QR 图有效（截图 / 生成的图完全够用）。
    """
    bin_img, _threshold = _prepare_binary(gray)
    if bin_img is None:
        return None
    fallback = None
    for invert in (False, True):
        work = bin_img if not invert else Image.eval(bin_img, lambda v: 255 - v)
        box = work.getbbox()
        if not box:
            continue
        width = box[2] - box[0]
        height = box[3] - box[1]
        if width < 42 or height < 42:
            continue
        if max(width, height) / min(width, height) > 1.35:
            continue

        side = min(width, height)
        # 按「模块尺寸是否接近整数」排序，真版本通常排在最前，命中即提前退出
        versions = sorted(
            range(1, 41),
            key=lambda v: abs(side / (v * 4 + 17) - round(side / (v * 4 + 17)))
        )
        for version in versions:
            n = version * 4 + 17
            module = side / n
            if module < 2.0 or module > 64:
                continue
            for inset in (0, 1, 2, 3):
                matrix = _sample_matrix_inset(work, box, n, inset)
                if matrix is None:
                    continue
                fs = _finder_score(matrix, n)
                if fs < 0.92 or _timing_score(matrix, n) < 0.80:
                    continue
                for bits in _read_format_bits(matrix, n):
                    decoded = decode_format_bits(bits)
                    if decoded is None:
                        continue
                    ec_level, mask = decoded
                    result = {
                        "mask": mask,
                        "ec_level": ec_level,
                        "version": version,
                        "source": "format-info(BCH/几何兜底)",
                        "confidence": "medium" if not expect_ec else "low",
                    }
                    # 纠错等级与 zxing 的结果对上，说明网格采样是对的，直接采用
                    if expect_ec and ec_level == expect_ec:
                        result["confidence"] = "medium"
                        return result
                    if fallback is None:
                        fallback = result
    return fallback


# ================================================================ 解析模式
def _read_barcode_ladder(image):
    """
    扫描件常有纸张阴影、噪点、灰度不均，zxing-cpp 默认二值化会漏读。
    这里按「强度逐级加码」重试：原始 → 降噪 → 换二值化器 → 灰度/对比增强。
    返回 (Barcode, 识别方式标签)；全部失败返回 (None, None)。
    """
    gray = ImageOps.grayscale(image)
    variants = [
        ("默认", image, {}),
        ("降噪", image, {"try_denoise": True}),
        ("降噪+全局直方图", image, {"try_denoise": True,
                                    "binarizer": zxingcpp.Binarizer.GlobalHistogram}),
        ("降噪+固定阈值", image, {"try_denoise": True,
                                  "binarizer": zxingcpp.Binarizer.FixedThreshold}),
        ("灰度+降噪", gray, {"try_denoise": True}),
        ("灰度+直方图+降噪", gray, {"try_denoise": True,
                                    "binarizer": zxingcpp.Binarizer.GlobalHistogram}),
        ("灰度+固定阈值+降噪", gray, {"try_denoise": True,
                                      "binarizer": zxingcpp.Binarizer.FixedThreshold}),
    ]
    try:
        enhanced = ImageOps.autocontrast(gray)
        variants += [
            ("对比增强+降噪", enhanced, {"try_denoise": True}),
            ("对比增强+直方图+降噪", enhanced, {"try_denoise": True,
                                                "binarizer": zxingcpp.Binarizer.GlobalHistogram}),
        ]
    except Exception:
        pass

    for label, img, kw in variants:
        try:
            result = zxingcpp.read_barcode(
                img, return_errors=True,
                try_rotate=True, try_invert=True, try_downscale=True, **kw)
        except Exception:
            continue
        # return_errors 模式下可能带回校验存疑的码：text 非空就先收下，置信度另标
        if result is not None and (result.valid or (result.text and result.text.strip())):
            return result, label
    return None, None


def parse_qr_image(path):
    """
    解析单张图片：payload / mask(0~7) / 纠错等级 / 版本。
    先走 zxing-cpp 多级预处理直读，读不到掩码再用几何法兜底。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError("文件不存在：%s" % path)
    if Image is None or zxingcpp is None:
        raise RuntimeError("缺少依赖：" + "、".join(_DEP_MISSING))

    image = Image.open(path)
    image.load()
    image = ImageOps.exif_transpose(image)
    if max(image.size) > 3000:          # 超大图先缩，只为稳定识别，不影响格式信息
        image = image.copy()
        image.thumbnail((3000, 3000), Image.LANCZOS)

    result, method = _read_barcode_ladder(image)
    if result is None:
        raise ValueError(
            "未能识别到二维码（已尝试降噪/多种二值化/灰度/对比增强），"
            "请检查图片清晰度、是否含完整静区，以及是否严重倾斜或模糊"
        )

    info = QRInfo()
    info.path = path
    info.payload = result.text or ""
    info.ec_level = (result.ec_level or "").upper() or None
    info.extra["decode_method"] = method

    mask, version, extra = mask_from_zxing_extra(result)
    info.extra.update(extra)
    if version is not None:
        info.version = version

    if not result.valid:
        info.confidence = "low(校验存疑)"

    if mask is not None:
        info.mask = mask
        info.mask_source = "zxing-cpp extra['DataMask']"
        if info.confidence == "n/a":
            info.confidence = "high"
    else:
        # 兼容兜底
        geo = mask_via_geometry(ImageOps.exif_transpose(image).convert("L"),
                               expect_ec=info.ec_level)
        if geo is None:
            raise ValueError(
                "二维码已识别，但掩码(Mask)解析失败：当前 zxing-cpp 未暴露 extra，"
                "几何兜底也未成功（图片可能是旋转/变形/低清）"
            )
        info.mask = geo["mask"]
        info.mask_source = geo["source"]
        info.confidence = geo["confidence"]
        if info.version is None:
            info.version = geo["version"]
        info.ec_level = info.ec_level or geo["ec_level"]

    return info


def verify_image_matrix(path, payload, ec_level, mask, version=None):
    """
    生成侧闭环校验（辅助）：把图片重采样成模块矩阵，与 qrcode 现场生成的
    同参数矩阵逐模块比对，返回 (一致率, 差异模块数)。
    仅在几何法可用时返回，纯核对用，不参与判定主流程。
    """
    try:
        gray = ImageOps.exif_transpose(Image.open(path)).convert("L")
        if max(gray.size) > 3000:
            gray = gray.copy()
            gray.thumbnail((3000, 3000), Image.LANCZOS)
        bin_img, _ = _prepare_binary(gray)
        if bin_img is None:
            return None
        box = bin_img.getbbox()
        if not box:
            return None
        v = version
        if v is None:
            return None
        n = v * 4 + 17
        matrix = _sample_matrix(bin_img, box, n)
        ref = qrcode.QRCode(version=v, error_correction=LEVEL_TO_EC_CONST[ec_level],
                            box_size=1, border=0, mask_pattern=mask)
        ref.add_data(payload)
        ref.make(fit=False)
        expected = ref.get_matrix()
        diff = 0
        for r in range(n):
            for c in range(n):
                if bool(expected[r][c]) != matrix[r][c]:
                    diff += 1
        return 1.0 - diff / (n * n), diff
    except Exception:
        return None


def print_info(info):
    """统一格式打印单张解析结果。"""
    print("-" * 66)
    print("文件        : %s" % os.path.basename(info.path))
    print("载荷 payload: %s" % short_text(info.payload))
    print("载荷长度    : %d 字符" % len(info.payload))
    segs = split_payload(info.payload)
    if len(segs) > 1:
        print("载荷分段    : %s" % " | ".join(segs))
    print("纠错等级    : %s" % (info.ec_level or "未知"))
    print("掩码编号    : %s" % (info.mask if info.mask is not None else "未知"))
    print("规格版本    : %s" % ("version %d (%d×%d)" % (info.version, info.size, info.size)
                                if info.version else "未知"))
    print("掩码来源    : %s (置信度 %s)" % (info.mask_source, info.confidence))
    print("-" * 66)


# ================================================================ 生成模式
def generate_qr(payload, mask, ec_level, out_path,
                version=None, box_size=10, border=4):
    """
    核心校验点 1：用 qrcode 原生 mask_pattern 生成指定掩码的二维码。
    version 传 None 则按载荷自动挑最小版本；指定版本时要求载荷放得下。
    """
    if qrcode is None:
        raise RuntimeError("缺少依赖：qrcode")
    if mask not in range(8):
        raise ValueError("掩码必须是 0~7 的整数")
    if ec_level not in EC_CHOICES:
        raise ValueError("纠错等级只能是 L / M / Q / H")

    code = qrcode.QRCode(
        version=version,
        error_correction=LEVEL_TO_EC_CONST[ec_level],
        box_size=box_size,
        border=border,
        mask_pattern=mask,          # ← 关键：显式指定掩码，关闭自动挑选
    )
    code.add_data(payload)
    code.make(fit=(version is None))

    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    image = code.make_image(fill_color="black", back_color="white")
    image.save(out_path)
    return code.version, out_path


def save_qr_safely(payload, mask, ec_level, out_path,
                   version=None, box_size=10, border=4):
    """
    落地写盘的两层保险：
    1) 版本溢出 -> 自动改回 fit 选版本；
    2) PermissionError（打包后的 exe 在某些系统策略下只能写程序所在目录）
       -> 依次回退到「当前目录」「用户主目录」，同名文件，并把实际路径回显。
    """
    def _once(path):
        try:
            return generate_qr(payload, mask, ec_level, path,
                               version=version, box_size=box_size, border=border)
        except Exception as exc:
            low = str(exc).lower()
            if version and ("capacity" in low or "overflow" in low or "too big" in low):
                return generate_qr(payload, mask, ec_level, path,
                                   version=None, box_size=box_size, border=border)
            raise

    tried = [out_path]
    name = os.path.basename(out_path) or "qr.png"
    for folder in (os.getcwd(), os.path.expanduser("~")):
        alt = os.path.join(folder, name)
        if os.path.normcase(os.path.abspath(alt)) != os.path.normcase(os.path.abspath(out_path)):
            tried.append(alt)

    last_err = None
    for path in tried:
        try:
            rv, saved = _once(path)
            if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(out_path)):
                print("  ⚠ 目标路径不可写，已改存到：%s" % saved)
            return rv, saved
        except PermissionError as exc:
            last_err = exc
            print("  · 写入被系统拒绝：%s" % path)
        except Exception as exc:
            raise
    raise last_err if last_err else PermissionError("写入失败")


# ------------------------------------------------ 载荷分段与先后顺序
_PAYLOAD_SPLIT = re.compile(r"[|／/\-_,;，；\s]+")


def split_payload(text):
    """按常见分隔符把载荷拆成段（| / - , ; 空格 等）。"""
    if not text:
        return []
    return [p for p in _PAYLOAD_SPLIT.split(str(text).strip()) if p]


def _parse_number(value):
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return None


def _is_timestamp(value):
    """10~13 位、落在 Unix 秒/毫秒量级范围的数字，按时间戳处理。"""
    n = _parse_number(value)
    return (isinstance(n, int) and 10 <= len(str(value).strip()) <= 13
            and 1_000_000_000 <= n <= 9_999_999_999)


def _format_key(value):
    if _is_timestamp(value):
        try:
            return datetime.fromtimestamp(int(value), TZ_CST).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(value)
    return str(value)


def analyze_payload_order(ok_list):
    """
    分析一组同主题二维码：
    - 拆出每张载荷的分段；
    - 找出发生变化的段，优先挑「时间戳段」，其次纯数字编号段，最后文本段；
    - 按该段升序给出先后顺序（order 是 ok_list 的下标序列）。
    """
    segments = [split_payload(i.payload) for i in ok_list]
    max_len = max((len(s) for s in segments), default=0)
    varying, constant = [], []
    for pos in range(max_len):
        values = [s[pos] if pos < len(s) else "" for s in segments]
        (varying if len(set(values)) > 1 else constant).append(pos)

    key_pos, key_kind = None, None
    if varying:
        for pos in varying:                      # 优先：像时间戳的变化段
            vals = [segments[i][pos] if pos < len(segments[i]) else ""
                    for i in range(len(segments))]
            if vals and all(_is_timestamp(v) for v in vals if v):
                key_pos, key_kind = pos, "timestamp"
                break
        if key_pos is None:                      # 其次：纯数字编号段
            for pos in varying:
                vals = [segments[i][pos] if pos < len(segments[i]) else ""
                        for i in range(len(segments))]
                if vals and all(_parse_number(v) is not None for v in vals if v):
                    key_pos, key_kind = pos, "number"
                    break
        if key_pos is None:                      # 最后：文本段，按字典序
            key_pos, key_kind = varying[0], "text"

    order = list(range(len(segments)))
    if key_pos is not None:
        def sort_key(idx):
            v = segments[idx][key_pos] if key_pos < len(segments[idx]) else ""
            n = _parse_number(v)
            return (0, n) if n is not None else (1, v)
        order = sorted(range(len(segments)), key=sort_key)

    identical = len(set(i.payload for i in ok_list)) <= 1
    return {"segments": segments, "key_pos": key_pos, "key_kind": key_kind,
            "order": order, "varying": varying, "constant": constant,
            "identical": identical}


# ================================================================ 时间戳模板
# 扫描件实测载荷：20311|1795664165|2024411082
#   第 1 段 = 固定业务码；第 2 段 = Unix 秒级时间戳；第 3 段 = 固定尾码
# 生成侧只替换中间那段时间戳，固定段/掩码/纠错/版本全部照抄扫描件，
# 就能产出与扫描件同族、可按时间排序的二维码。
TZ_CST = timezone(timedelta(hours=8))        # 扫描件时间基准固定东八区

# 内置示例占位：仅用于无样本时演示拼装形态，真实业务段以解析结果为准。
DEFAULT_TS_TEMPLATE = {
    "head": ["EXAMPLE"],
    "tail": ["DEMO"],
    "mask": 3,
    "ec": "M",
    "version": 3,
}
_TS_TEMPLATE = dict(DEFAULT_TS_TEMPLATE)


def fmt_ts(ts):
    """Unix 秒 -> 东八区可读时间。"""
    try:
        return datetime.fromtimestamp(int(ts), TZ_CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def template_text(tmpl=None):
    """模板的人类可读形态，方便在菜单里回显。"""
    t = tmpl or _TS_TEMPLATE
    parts = [str(x) for x in (t.get("head") or [])] + ["<时间戳>"] \
            + [str(x) for x in (t.get("tail") or [])]
    return "|".join(parts)


def build_ts_payload(ts, tmpl=None):
    """把时间戳塞进模板，拼出最终载荷。"""
    t = tmpl or _TS_TEMPLATE
    parts = [str(x) for x in (t.get("head") or [])] + [str(int(ts))] \
            + [str(x) for x in (t.get("tail") or [])]
    return "|".join(parts)


def _dt_ts(dt):
    """datetime -> Unix 秒；naive 时间按东八区解释，避免受系统时区影响。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CST)
    return int(dt.timestamp())


_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y.%m.%d",
)


def parse_time_input(text, base_ts=None):
    """
    把用户输入解析成 Unix 秒级时间戳，返回 (ts, 说明)。解析不了抛 ValueError。
    支持：
      · 空                    -> base_ts（没有则当前时间）
      · +90 / -120 / +5m / -1h -> 在 base_ts（没有则现在）上加减
      · 1795664165            -> 10 位 Unix 秒
      · 1795664165000         -> 13 位 Unix 毫秒，自动截到秒
      · 2026-11-26 11:36:05 / 2026/11/26 11:36 / 2026.11.26
      · 20261126113605 / 202611261136 / 20261126
      · 11:36:05 / 113605     -> 今天该时刻
    """
    raw = (text or "").strip().replace("：", ":")
    now_ts = int(time.time())
    if not raw:
        if base_ts:
            return int(base_ts), "沿用基准时间"
        return now_ts, "当前时间"

    # 相对偏移
    m = re.fullmatch(r"([+-])\s*(\d+)\s*([smhSMH]?)", raw)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        num = int(m.group(2)) * {"s": 1, "m": 60, "h": 3600}[(m.group(3) or "s").lower()]
        base = int(base_ts) if base_ts else now_ts
        return base + sign * num, "相对基准 %s%d 秒" % (m.group(1), num)

    # 带分隔符的常规写法优先，保证提示里回显的格式名准确
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return _dt_ts(dt), fmt

    digits = re.sub(r"[\s:：/.\-]", "", raw)
    if digits.isdigit():
        n = int(digits)
        if len(digits) == 13:
            return n // 1000, "Unix 毫秒→秒"
        if len(digits) == 10 and n >= 1_000_000_000:
            return n, "Unix 秒"
        if len(digits) == 14:
            return _dt_ts(datetime.strptime(digits, "%Y%m%d%H%M%S")), "YYYYMMDDHHMMSS"
        if len(digits) == 12:
            return _dt_ts(datetime.strptime(digits, "%Y%m%d%H%M")), "YYYYMMDDHHMM"
        if len(digits) == 8:
            return _dt_ts(datetime.strptime(digits, "%Y%m%d")), "YYYYMMDD（零点）"
        if len(digits) in (4, 6):
            today = datetime.now(TZ_CST)
            hh, mm, ss = int(digits[:2]), int(digits[2:4]), int(digits[4:6] or 0)
            if hh > 23 or mm > 59 or ss > 59:
                raise ValueError("时分秒超出范围")
            return _dt_ts(today.replace(hour=hh, minute=mm, second=ss,
                                        microsecond=0)), "今天 %s" % digits

    for fmt in ("%H:%M:%S", "%H:%M"):     # 只给时分（秒），补今天的日期
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        today = datetime.now(TZ_CST)
        dt = dt.replace(year=today.year, month=today.month, day=today.day)
        return _dt_ts(dt), fmt

    raise ValueError("无法识别的时间写法：%s" % raw)


def learn_ts_template(ok_list):
    """
    从一批同主题二维码里学出模板：
    时间戳所在段之前的段 -> head，之后的段 -> tail，再带上掩码/纠错/版本。
    找不到时间戳段就返回 None（不覆盖现有模板）。
    """
    if not ok_list:
        return None
    info = analyze_payload_order(ok_list)
    if info["key_kind"] != "timestamp" or info["key_pos"] is None:
        return None
    kp = info["key_pos"]
    seg0 = info["segments"][0]
    return {
        "head": seg0[:kp],
        "tail": seg0[kp + 1:],
        "mask": ok_list[0].mask,
        "ec": (ok_list[0].ec_level or "M").upper(),
        "version": ok_list[0].version,
    }


# ================================================================ 交互菜单
def pause():
    try:
        input("\n按回车继续…")
    except (EOFError, KeyboardInterrupt):
        print()


def ask(prompt, default=""):
    raw = input(prompt).strip()
    return raw if raw else default


def do_single():
    """[1] 单张图片解析"""
    print("\n【1】单张图片解析 —— 请给出一张高清二维码图片的路径")
    path = ask("图片路径（可直接把文件拖进来）: ")
    if not path:
        print("已取消。")
        return None
    path = path.strip('"').strip("'")
    try:
        info = parse_qr_image(path)
    except Exception as exc:
        print("解析失败：%s" % exc)
        return None
    print_info(info)
    print("提示：进入【3】可直接用这张图的时间戳为基准，生成同族 QR。")
    return info


def do_batch():
    """[2] 批量解析 —— 汇总掩码列表与顺序清单"""
    print("\n【2】批量解析 —— 请给出存放二维码图片的文件夹")
    folder = ask("文件夹路径（直接回车用当前目录）: ", os.getcwd())
    folder = folder.strip('"').strip("'")
    if not os.path.isdir(folder):
        print("目录不存在：%s" % folder)
        return None

    names = list_images(folder)
    if not names:
        print("该目录下没有图片（支持 %s）。" % "、".join(e.strip(".") for e in IMAGE_EXTS))
        return None

    print("\n发现 %d 张图片，开始解析…" % len(names))
    ok_list, fail_list = [], []
    for index, name in enumerate(names, 1):
        full = os.path.join(folder, name)
        try:
            info = parse_qr_image(full)
            ok_list.append(info)
            print("[%d/%d] %-28s mask=%s  ec=%s  ver=%s  载荷=%s"
                  % (index, len(names), name, info.mask, info.ec_level or "?",
                     info.version if info.version else "?", short_text(info.payload, 30)))
        except Exception as exc:
            fail_list.append((name, str(exc)))
            print("[%d/%d] %-28s 解析失败：%s" % (index, len(names), name, exc))

    if not ok_list:
        print("\n没有一张解析成功。")
        return None

    print("\n" + "=" * 66)
    print("掩码汇总表（按文件名顺序）")
    print("=" * 66)
    print("%-4s %-30s %-6s %-4s %-4s" % ("#", "file", "mask", "ec", "ver"))
    for index, info in enumerate(ok_list, 1):
        print("%-4d %-30s %-6s %-4s %-4s"
              % (index, short_name(os.path.basename(info.path), 30), info.mask,
                 info.ec_level or "?", info.version if info.version else "?"))

    masks = [info.mask for info in ok_list]
    order_info = analyze_payload_order(ok_list)

    print("\n" + "-" * 66)
    print("一、掩码 / 纠错 / 版本汇总（掩码是可比对的特征之一）")
    print("-" * 66)
    print("掩码顺序清单  : %s" % " → ".join(str(m) for m in masks))
    print("去重后掩码集合: [%s]" % ", ".join(str(m) for m in sorted(set(m for m in masks if m is not None))))
    same_mask = len(set(masks)) == 1 and masks[0] is not None
    same_ec = len(set(i.ec_level for i in ok_list)) == 1
    if same_mask:
        print("掩码一致性    : 全部为 %s（掩码本身不构成区分先后的依据）" % masks[0])
    else:
        print("掩码一致性    : 不一致，取值如下：%s" % " → ".join(str(m) for m in masks))
    print("纠错等级一致  : %s" % ("是" if same_ec else "否"))

    # ---------------- 二、载荷分段与先后顺序（真正的排序依据）
    print("\n" + "=" * 66)
    print("二、载荷分段与先后顺序推断")
    print("=" * 66)

    if order_info["identical"]:
        print("  · 所有二维码载荷完全相同，内容层面无法区分先后；")
        print("    若掩码/纠错/版本也一致，只能靠图片外信息（拍摄顺序、编号等）判断。")
    else:
        segs = order_info["segments"]
        kp, kk = order_info["key_pos"], order_info["key_kind"]
        key_head = "关键段(第%d段)" % (kp + 1) if kp is not None else "关键段"
        print("%-4s %-28s %-20s %s" % ("#", "文件", key_head, "载荷"))
        for idx, info in enumerate(ok_list):
            key_val = segs[idx][kp] if kp is not None and kp < len(segs[idx]) else "-"
            key_show = _format_key(key_val) if key_val != "-" else "-"
            print("%-4d %-28s %-20s %s"
                  % (idx + 1, short_name(os.path.basename(info.path), 28), key_show,
                     short_text(info.payload, 34)))

        label = {"timestamp": "Unix 时间戳", "number": "数字编号", "text": "文本段"}[kk]
        n_seg = len(segs[0]) if segs else 0
        const_txt = "、".join("第%d段" % (p + 1) for p in order_info["constant"]) or "无"
        print("\n  排序依据：载荷第 %d 段（%s）；共 %d 段，保持不变的段：%s"
              % (kp + 1, label, n_seg, const_txt))
        order_word = {"timestamp": "时间从早到晚", "number": "从小到大",
                      "text": "字典序"}[kk]
        print("  先后顺序（%s）：" % order_word)
        for rank, idx in enumerate(order_info["order"], 1):
            info = ok_list[idx]
            key_val = segs[idx][kp] if kp < len(segs[idx]) else "-"
            print("    %d. %-26s  %s"
                  % (rank, short_name(os.path.basename(info.path), 26), _format_key(key_val)))
            print("        载荷：%s" % info.payload)

        if kk == "timestamp":
            print("  规律：第 %d 段是 Unix 时间戳，数值越大时间越晚，升序即生成先后顺序。" % (kp + 1))
        elif kk == "number":
            print("  规律：第 %d 段是数字编号，从小到大即为先后顺序。" % (kp + 1))
        else:
            print("  规律：第 %d 段是文本，当前按字典序排列，请结合编号含义人工确认方向。" % (kp + 1))

    # 掩码不构成先后规律（实测四张掩码完全相同），这里只留一行状态提示，不再算命式推断
    if not same_mask:
        print("\n  · 掩码序列不一致，但掩码只反映格式信息里的纠错+掩码组合，")
        print("    与生成先后无关；判序请以载荷中的时间戳/编号段为准。")

    # 学到模板：把「固定段 + 掩码 + 纠错 + 版本」记下来，供 [3] 直接套用生成
    tmpl = learn_ts_template(ok_list)
    if tmpl:
        _TS_TEMPLATE.update(tmpl)
        print("\n  · 已记录时间戳模板：%s" % template_text())
        print("    掩码=%s 纠错=%s 版本=%s —— 进入【3】即可按模板生成任意时间戳的同款 QR"
              % (_TS_TEMPLATE["mask"], _TS_TEMPLATE["ec"], _TS_TEMPLATE["version"]))

    if fail_list:
        print("\n失败清单")
        for name, reason in fail_list:
            print("  - %s : %s" % (name, reason))

    if ask("\n是否把解析与顺序清单另存为 txt？[y/N]: ", "n").lower().startswith("y"):
        out = os.path.join(folder, "qr_order_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
        try:
            segs = order_info["segments"]
            kp = order_info["key_pos"]
            with open(out, "w", encoding="utf-8") as fp:
                fp.write("二维码解析与先后顺序清单\n")
                fp.write("排序依据：%s\n" % ("载荷第%d段（%s）"
                       % (kp + 1, order_info["key_kind"]) if kp is not None else "无变化段"))
                fp.write("先后顺序,文件名,掩码,纠错等级,版本,关键段,载荷\n")
                for rank, idx in enumerate(order_info["order"], 1):
                    info = ok_list[idx]
                    key_val = segs[idx][kp] if kp is not None and kp < len(segs[idx]) else "-"
                    fp.write("%d,%s,%s,%s,%s,%s,%s\n"
                             % (rank, os.path.basename(info.path), info.mask,
                                info.ec_level or "", info.version or "",
                                _format_key(key_val),
                                info.payload.replace("\n", " ")))
            print("已保存：%s" % out)
        except Exception as exc:
            print("保存失败：%s" % exc)

    return masks


def do_generate(last=None):
    """[3] 按时间戳生成二维码：固定段/掩码/纠错/版本全部照抄模板，只换时间戳"""
    print("\n【3】按时间戳生成二维码 —— 套用扫描件模板，只替换时间戳")
    print("  模板载荷：%s" % template_text())
    print("  掩码=%s   纠错=%s   版本=%s"
          % (_TS_TEMPLATE.get("mask"), _TS_TEMPLATE.get("ec"),
             _TS_TEMPLATE.get("version") or "自动"))
    print("  时间写法：2026-11-26 11:36:05 / 1795664165 / 20261126113605 / 11:36:05")
    print("            相对量也行：+90（加 90 秒）、+5m、-1h；直接回车=基准时间")

    base_ts = None
    if last is not None and last.payload:
        for seg in split_payload(last.payload):
            if _is_timestamp(seg):
                base_ts = int(seg)
                break
        print("  基准时间（来自上次解析）：%s"
              % (fmt_ts(base_ts) if base_ts else "该载荷里没有时间戳"))

    ts = None
    while ts is None:
        hint = ("（回车=%s）" % fmt_ts(base_ts)) if base_ts else "（回车=现在）"
        raw = input("要生成的时间 %s: " % hint)
        try:
            ts, why = parse_time_input(raw, base_ts)
        except ValueError as exc:
            print("  %s" % exc)
            continue

    print("  → Unix 时间戳：%d" % ts)
    print("  → 换算时间  ：%s（UTC+8，依据 %s）" % (fmt_ts(ts), why))

    head = list(_TS_TEMPLATE.get("head") or [])
    tail = list(_TS_TEMPLATE.get("tail") or [])
    mask = int(_TS_TEMPLATE["mask"]) if _TS_TEMPLATE.get("mask") is not None else 3
    ec_level = str(_TS_TEMPLATE.get("ec") or "M").upper()
    version = _TS_TEMPLATE.get("version")
    payload = build_ts_payload(ts, {"head": head, "tail": tail})
    print("  → 载荷：%s" % payload)

    if ask("是否手动调整 掩码/纠错/固定段/版本？[y/N]: ", "n").lower().startswith("y"):
        custom = ask("整段自定义载荷（回车=继续用模板拼装）: ")
        if custom:
            payload = custom
        else:
            head_in = ask("时间戳之前的固定段（| 分隔，回车=保持）: ")
            if head_in:
                head = [p.strip() for p in head_in.split("|") if p.strip()]
            tail_in = ask("时间戳之后的固定段（| 分隔，回车=保持）: ")
            if tail_in:
                tail = [p.strip() for p in tail_in.split("|") if p.strip()]
            payload = build_ts_payload(ts, {"head": head, "tail": tail})
        while True:
            mt = ask("掩码 0~7（回车=%d）: " % mask, str(mask))
            try:
                m2 = int(mt)
            except ValueError:
                print("请输入 0~7 的整数。")
                continue
            if 0 <= m2 <= 7:
                mask = m2
                break
            print("范围错误，只能是 0~7。")
        while True:
            et = ask("纠错等级 L/M/Q/H（回车=%s）: " % ec_level, ec_level).upper()
            if et in EC_CHOICES:
                ec_level = et
                break
            print("只能是 L / M / Q / H。")
        vt = ask("固定版本 1~40（回车=%s）: " % (version or "自动"), str(version or ""))
        if vt:
            try:
                v2 = int(vt)
                if 1 <= v2 <= 40:
                    version = v2
                else:
                    print("版本范围 1~40，保持原值。")
            except ValueError:
                print("版本号无效，保持原值。")
        print("  → 最终载荷：%s" % payload)

    box_text = ask("每模块像素 box_size（回车=10）: ", "10")
    try:
        box_size = max(2, int(box_text))
    except ValueError:
        box_size = 10

    stamp = datetime.fromtimestamp(int(ts), TZ_CST).strftime("%Y%m%d_%H%M%S")
    default_name = "qr_ts_%s_mask%d.png" % (stamp, mask)
    out_path = ask("输出文件（回车=%s）: " % default_name,
                   os.path.join(os.getcwd(), default_name))
    out_path = out_path.strip('"').strip("'")
    if os.path.isdir(out_path):
        out_path = os.path.join(out_path, default_name)

    try:
        real_version, saved = save_qr_safely(payload, mask, ec_level, out_path,
                                             version=version, box_size=box_size)
    except Exception as exc:
        print("生成失败：%s" % exc)
        print("提示：换个输出目录（程序所在目录、桌面）再试，或确认当前用户对该目录有写权限。")
        return None

    size = real_version * 4 + 17
    print("\n已生成：%s" % saved)
    print("  时间戳=%d（%s）" % (ts, fmt_ts(ts)))
    print("  掩码=%d   纠错=%s   版本=%d (%d×%d)   模块像素=%d"
          % (mask, ec_level, real_version, size, size, box_size))
    print("  载荷=%s" % payload)

    # 闭环自证：回读刚生成的图片
    print("\n闭环回读校验（用解析器读回刚生成的图）")
    try:
        back = parse_qr_image(saved)
        checks = [
            ("掩码", back.mask == mask, "%s / 期望 %d" % (back.mask, mask)),
            ("纠错等级", (back.ec_level or "").upper() == ec_level,
             "%s / 期望 %s" % (back.ec_level, ec_level)),
            ("版本", back.version == real_version,
             "%s / 期望 %s" % (back.version, real_version)),
            ("载荷", back.payload.strip() == payload.strip(), "见上"),
        ]
        all_ok = all(item[1] for item in checks)
        for name, ok, detail in checks:
            print("  %s %-6s %s" % ("✔" if ok else "✘", name, detail))
        matrix = verify_image_matrix(saved, payload, ec_level, mask, real_version)
        if matrix:
            print("  模块矩阵比对：一致率 %.2f%%，差异 %d 个模块"
                  % (matrix[0] * 100, matrix[1]))
        print("  %s" % ("结论：时间戳模板回填成立，掩码/纠错/版本与扫描件同款。"
                       if all_ok else "结论：存在不一致项，请检查固定段或参数。"))
        print("  回读掩码来源：%s" % back.mask_source)
    except Exception as exc:
        print("  回读失败：%s" % exc)
    return saved


def print_banner():
    print("=" * 66)
    print(" 二维码掩码显影工具  QR Mask Reveal / Rebuild")
    print(" 解析：payload + 掩码(0~7) + 纠错等级     生成：按时间戳套模板出同款 QR")
    print("=" * 66)
    if _DEP_MISSING:
        print("⚠ 缺少依赖：%s" % "、".join(_DEP_MISSING))
        print("  请先执行：pip install -r requirements.txt\n")


def print_menu():
    print("""
[1] 单张图片解析：读取掩码、载荷、纠错等级
[2] 批量解析：读取文件夹内所有图片，推断先后顺序并学出时间戳模板
[3] 按时间戳生成二维码：只填时间，固定段/掩码/纠错照抄模板
[4] 退出""")


def install_deps_and_restart():
    """
    缺依赖时的自救入口：用「当前解释器」的 pip 装 requirements.txt。
    注意必须走 subprocess，直接在 input() 里敲 pip 命令是不会有任何效果的。
    装好后由 main() 负责重启进程，避免半残状态继续跑。
    """
    req = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    if not os.path.isfile(req):
        print("找不到 requirements.txt，请手动执行：")
        print("  pip install zxing-cpp pillow qrcode")
        return False
    cmd = [sys.executable, "-m", "pip", "install", "-r", req]
    print("执行：%s" % " ".join(cmd))
    try:
        code = subprocess.call(cmd)
    except Exception as exc:
        print("调用 pip 失败：%s" % exc)
        return False
    if code != 0:
        print("pip 返回 %s，安装未成功。可换镜像重试：\n"
              "  pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
              % code)
        return False
    return True


def main():
    safe_console()
    print_banner()
    if _DEP_MISSING:
        print("· 提示：依赖安装必须在命令行执行，直接在菜单里敲 pip 命令不会生效")
        if ask("是否现在自动安装缺失依赖？[y/N]: ", "y").lower().startswith("y"):
            if install_deps_and_restart():
                print("安装完成，正在重启程序…")
                try:
                    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
                except Exception as exc:
                    print("自动重启失败（%s），请手动重新运行本程序。" % exc)
                return
            print("未完成安装，先进入菜单（解析/生成会在调用时再报缺依赖）。")
    last_info = None            # 最近一次成功解析结果，供 [3] 取基准时间
    while True:
        print_menu()
        choice = ask("请选择 [1-4]: ")
        if choice in ("1",):
            result = do_single()
            if result is not None:
                last_info = result
        elif choice in ("2",):
            do_batch()
        elif choice in ("3",):
            do_generate(last_info)
        elif choice in ("4", "q", "quit", "exit"):
            print("再见。")
            return
        elif choice == "":
            continue
        else:
            print("无效选项，请输入 1~4。")
        pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
    except EOFError:
        print("\n已退出。")
    except Exception:
        print("\n程序异常：")
        traceback.print_exc()
        input("按回车关闭…")
