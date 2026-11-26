#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二维码实时放映 · 实验模拟器 —— Web 版一键打包脚本（PyInstaller）
=================================================================
用法：
    python build_web.py                # 默认：单文件打包 dist/QRReplay.exe
    python build_web.py --onedir       # 目录模式（启动更快，中文路径可直接双击）
    python build_web.py --icon app.ico # 指定图标
    python build_web.py --check       # 只检查环境与依赖
    python build_web.py --no-install   # 缺依赖时不自动 pip install

产物：
    dist/QRReplay.exe        单文件版（双击起本地 Web 服务并自动打开浏览器）
    dist/QRReplay/           目录版

⚠ 已知坑（与命令行版一致）：PyInstaller 单文件 exe 放在含中文/非 ASCII
  路径下双击会报 Could not create temporary directory。解决：
    1) 把 exe 复制到纯英文路径（C:\\QRReplay\\、桌面）再双击；
    2) 用 --onedir 目录版，就地可运行。
"""

import os
import sys
import shutil
import subprocess
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "QRReplay"
ENTRY_FILE = "app_web.py"
REQ_FILE = "requirements.txt"
PROBE_PORT = 8765          # app_web.py 默认从该端口起，自检就探它

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
    req = os.path.join(HERE, REQ_FILE)
    cmd = [sys.executable, "-m", "pip", "install"]
    if os.path.isfile(req):
        cmd += ["-r", req]
    else:
        cmd += ["--upgrade"] + packages
    try:
        subprocess.run(cmd, check=True, cwd=HERE)
    except subprocess.CalledProcessError:
        fail("依赖安装失败，可换国内镜像：\n"
             "  pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple")


def clean_old(onefile=True):
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
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        # 本地服务需要保留控制台窗口看日志；界面在浏览器里
        "--hidden-import", "main",
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
    return any(ord(ch) > 127 for ch in text)


def self_check(onefile=True):
    """启动自检：跑起来后探测本地首页 200，证明服务能起来。"""
    if onefile:
        target = os.path.join(HERE, "dist", "%s.exe" % APP_NAME)
    else:
        target = os.path.join(HERE, "dist", APP_NAME, "%s.exe" % APP_NAME)
    if not os.path.isfile(target):
        return False

    work_dir = tempfile.mkdtemp(prefix="qrreplay_check_")
    proc = None
    try:
        if onefile:
            probe = shutil.copy2(target, os.path.join(work_dir, "%s.exe" % APP_NAME))
        else:
            probe = os.path.join(work_dir, APP_NAME)
            shutil.copytree(os.path.dirname(target), probe)
            probe = os.path.join(probe, "%s.exe" % APP_NAME)

        proc = subprocess.Popen([probe], cwd=work_dir,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding="utf-8", errors="replace")
        ok = False
        deadline = time.time() + 20
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/" % PROBE_PORT, timeout=2) as resp:
                    if resp.status == 200:
                        ok = True
                        break
            except Exception:
                time.sleep(0.8)
        if not ok and proc.poll() is not None:
            out = (proc.stdout.read() or "")[:200] if proc.stdout else ""
            info("启动自检未通过（进程退出）：%s" % out)
        return ok
    except Exception as exc:
        info("启动自检异常：%s" % exc)
        return False
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
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
            print("⚠ 当前目录含中文/非 ASCII：单文件版在此路径双击可能无法启动。")
            print("  用法一：复制到纯英文路径（C:\\QRReplay\\）再双击；")
            print("  用法二：python build_web.py --onedir 生成目录版就地运行。")
        else:
            print("使用：双击运行，浏览器会自动打开本地页面。")
        if self_check(onefile=True):
            info("启动自检通过：本地服务可访问")
    else:
        target = os.path.join(HERE, "dist", APP_NAME)
        print("\n产物目录：%s" % target)
        print("入口：%s" % os.path.join(target, "%s.exe" % APP_NAME))
        print("提示：目录模式不解压临时目录，中文路径下也能直接双击。")
        if self_check(onefile=False):
            info("启动自检通过：本地服务可访问")


def main(argv):
    onefile = "--onedir" not in argv
    icon = None
    for index, item in enumerate(argv):
        if item == "--icon" and index + 1 < len(argv):
            icon = argv[index + 1]
    only_check = "--check" in argv
    auto_install = "--no-install" not in argv

    print("=" * 60)
    print(" %s 打包脚本（PyInstaller · Web 本地服务）" % APP_NAME)
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
