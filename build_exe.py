#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二维码掩码显影工具 —— 一键打包脚本（PyInstaller 单文件）
=================================================================
用法：
    python build_exe.py                # 默认：单文件打包 dist/QRMaskTool.exe
    python build_exe.py --onedir       # 改成目录模式（启动更快，方便排查）
    python build_exe.py --icon app.ico # 指定图标
    python build_exe.py --check        # 只检查环境与依赖，不执行打包
    python build_exe.py --no-install   # 缺依赖时不自动 pip install

产物：dist/QRMaskTool.exe（单文件、双击即用、无需本机 Python）

⚠ 已知坑（本脚本会自动检测）：PyInstaller 单文件 exe 放在「含中文/非 ASCII
  字符」的路径下（如 D:\\dsh项目\\二维码显影\\），启动时会报
      LOADER: Could not create temporary directory!
  这是 bootloader 自解压临时目录时的路径编码限制。解决办法两条：
      1) 把 exe 复制到纯英文路径（如 C:\\QRMaskTool\\、桌面）再双击；
      2) 改用目录模式：python build_exe.py --onedir，产物就地可运行。
  脚本打包完会自动把 exe 复制到临时英文路径做一次启动自检。
"""

import os
import sys
import shutil
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "QRMaskTool"
ENTRY_FILE = "main.py"
REQ_FILE = "requirements.txt"

# 运行时真正需要的四个依赖（module, pip 包名）
DEPENDENCIES = (
    ("zxingcpp", "zxing-cpp"),
    ("PIL", "pillow"),
    ("qrcode", "qrcode"),
    ("PyInstaller", "pyinstaller"),
)


def info(msg):
    print("[build] %s" % msg)


def fail(msg, code=1):
    print("[build] ✘ %s" % msg)
    sys.exit(code)


def check_python():
    if sys.version_info < (3, 8):
        fail("需要 Python 3.8 及以上，当前 %s" % sys.version.split()[0])
    info("Python %s  (%s)" % (sys.version.split()[0], sys.executable))


def missing_deps():
    """逐个 import 试探，返回缺失的 pip 包名列表。"""
    missing = []
    for module, package in DEPENDENCIES:
        try:
            __import__(module)
        except Exception:
            missing.append(package)
    return missing


def install_deps(packages):
    if not packages:
        return
    info("安装缺失依赖：%s" % "、".join(packages))
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + packages
    if os.path.isfile(os.path.join(HERE, REQ_FILE)):
        cmd = [sys.executable, "-m", "pip", "install", "-r",
               os.path.join(HERE, REQ_FILE)]
    try:
        subprocess.run(cmd, check=True, cwd=HERE)
    except subprocess.CalledProcessError:
        fail("依赖安装失败，请检查网络或换用国内镜像源：\n"
             "  pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple")


def clean_old(onefile=True):
    """
    只清理「本次打包要产出的那份」，不动 dist 里的另一份：
    这样单文件版和目录版可以并存于 dist/，互不覆盖。
    """
    targets = [
        os.path.join(HERE, "build"),
        os.path.join(HERE, "%s.spec" % APP_NAME),
        os.path.join(HERE, "dist", "%s.exe" % APP_NAME) if onefile
        else os.path.join(HERE, "dist", APP_NAME),
    ]
    for path in targets:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            info("已清理目录 %s" % os.path.basename(path))
        elif os.path.isfile(path):
            try:
                os.remove(path)
                info("已清理文件 %s" % os.path.basename(path))
            except OSError:
                pass


def run_pyinstaller(onefile=True, icon=None):
    entry = os.path.join(HERE, ENTRY_FILE)
    if not os.path.isfile(entry):
        fail("找不到入口文件 %s" % entry)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        # 控制台交互程序：必须保留控制台窗口，不能加 --noconsole
        "--hidden-import", "zxingcpp",
        "--hidden-import", "qrcode",
        "--hidden-import", "qrcode.image.pil",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageOps",
        "--collect-submodules", "qrcode",
        "--exclude-module", "tkinter",
        "--exclude-module", "pytest",
        "--exclude-module", "numpy",
        "--log-level", "WARN",
    ]
    cmd.append("--onefile" if onefile else "--onedir")
    if icon and os.path.isfile(icon):
        cmd += ["--icon", os.path.abspath(icon)]
    cmd.append(entry)

    info("执行：%s" % " ".join(cmd))
    started = time.time()
    try:
        subprocess.run(cmd, check=True, cwd=HERE)
    except subprocess.CalledProcessError as exc:
        fail("PyInstaller 打包失败（退出码 %s），请查看上方日志" % exc.returncode)
    info("打包完成，耗时 %.1f 秒" % (time.time() - started))


def has_non_ascii(text):
    """判断路径里是否含有中文等非 ASCII 字符。"""
    return any(ord(ch) > 127 for ch in text)


def self_check(onefile=True):
    """
    启动自检：把 exe 复制到纯英文的临时目录跑一次「[4] 退出」，
    既验证打包产物是否可用，也顺带绕开中文路径的 bootloader 坑。
    """
    if onefile:
        target = os.path.join(HERE, "dist", "%s.exe" % APP_NAME)
    else:
        target = os.path.join(HERE, "dist", APP_NAME, "%s.exe" % APP_NAME)
    if not os.path.isfile(target):
        return False

    work_dir = tempfile.mkdtemp(prefix="qrmask_check_")
    try:
        if onefile:
            probe = shutil.copy2(target, os.path.join(work_dir, "%s.exe" % APP_NAME))
        else:
            probe = os.path.join(work_dir, APP_NAME)
            shutil.copytree(os.path.dirname(target), probe)
            probe = os.path.join(probe, "%s.exe" % APP_NAME)
        proc = subprocess.run([probe], input="4\n", text=True, timeout=90,
                              capture_output=True, encoding="utf-8", errors="replace")
        ok = proc.returncode == 0 and "二维码掩码显影工具" in (proc.stdout or "")
        if not ok:
            info("启动自检未通过（退出码 %s）：%s"
                 % (proc.returncode, (proc.stderr or proc.stdout or "")[:200]))
        return ok
    except Exception as exc:
        info("启动自检异常：%s" % exc)
        return False
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def report(onefile=True):
    if onefile:
        target = os.path.join(HERE, "dist", "%s.exe" % APP_NAME)
        if not os.path.isfile(target):
            fail("未找到产物 %s" % target)
        size_mb = os.path.getsize(target) / 1024 / 1024
        print("\n" + "=" * 60)
        print("产物路径：%s" % target)
        print("产物大小：%.1f MB" % size_mb)
        print("=" * 60)
        if has_non_ascii(HERE):
            print("⚠ 当前目录含中文/非 ASCII 字符：PyInstaller 单文件版在这种路径下")
            print("  双击会报 Could not create temporary directory（bootloader 限制）。")
            print("  用法一：把 QRMaskTool.exe 复制到纯英文路径（C:\\QRMaskTool\\、桌面）再双击；")
            print("  用法二：python build_exe.py --onedir 生成目录版，产物就地可运行。")
        else:
            print("使用：双击运行即可；也可在 cmd 里执行该 exe 进入交互菜单。")
        print("提示：单文件版首次启动会自解压，杀软偶有误报，加白名单即可。")

        if self_check(onefile=True):
            info("启动自检通过：exe 在英文路径下可正常进入菜单")
    else:
        target = os.path.join(HERE, "dist", APP_NAME)
        print("\n产物目录：%s" % target)
        print("入口：%s" % os.path.join(target, "%s.exe" % APP_NAME))
        print("提示：目录模式不会解压到临时目录，放在中文路径下也能直接双击。")
        if self_check(onefile=False):
            info("启动自检通过：目录版 exe 可正常进入菜单")


def main(argv):
    onefile = "--onedir" not in argv
    icon = None
    for index, item in enumerate(argv):
        if item == "--icon" and index + 1 < len(argv):
            icon = argv[index + 1]
    only_check = "--check" in argv
    auto_install = "--no-install" not in argv

    print("=" * 60)
    print(" %s 打包脚本（PyInstaller 单文件）" % APP_NAME)
    print("=" * 60)

    check_python()
    missing = missing_deps()
    if missing:
        info("缺失依赖：%s" % "、".join(missing))
        if only_check:
            fail("环境未就绪（--check 模式不安装）")
        if auto_install:
            install_deps(missing)
            if missing_deps():
                fail("仍有依赖缺失，请手动 pip install -r requirements.txt")
        else:
            fail("缺少依赖且已禁用自动安装（--no-install）")
    else:
        info("依赖齐全：zxing-cpp / pillow / qrcode / pyinstaller")

    if only_check:
        info("环境检查通过（--check 模式，不执行打包）")
        return

    if onefile and has_non_ascii(HERE):
        info("检测到打包目录含中文字符，单文件版会在该路径下无法启动（见文首说明）")

    clean_old(onefile=onefile)
    run_pyinstaller(onefile=onefile, icon=icon)
    report(onefile=onefile)


if __name__ == "__main__":
    main(sys.argv[1:])
