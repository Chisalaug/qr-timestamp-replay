#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二维码实时放映 / 实验模拟器（Web 图形界面 · 本地服务）
=================================================================
仅供学习与实验：
  · 上传一张或多张同族二维码，程序从图片里「动态学习」模板——
    固定业务段、时间戳段、掩码、纠错等级、版本全部以解析结果为准，
    不写死；换一批图，模板随之改变。
  · 按样本时间戳推算相邻刷新间隔（不一致时列出差值，间隔可手动改）。
  · 实时模式：以基准时间戳为锚点，按系统当前时间推算当前 / 下一张
    应有的时间戳，定时刷新显示，模拟原系统的实效性刷新。

本地起一个标准库 HTTP 服务，自动打开默认浏览器；无需前端框架。
打包入口：build_web.py（PyInstaller）。

依赖：zxing-cpp、pillow、qrcode；打包另需 pyinstaller。
"""

import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime

# 复用命令行版本里已经验证过的解析 / 模板 / 生成逻辑
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import main as core  # noqa: E402

# ================================================================ 运行状态
STATE = {
    "loaded": False,
    "error": "",
    "samples": [],            # [{name, payload, ts, mask, ec, version, time}]
    "template": {             # 内置示例占位；导入样本后被解析结果覆盖
        "head": ["EXAMPLE"], "tail": ["DEMO"], "mask": 3, "ec": "M", "version": 3,
    },
    "intervals": [],          # 样本相邻时间戳差（秒）
    "interval": 120,          # 推算用固定刷新间隔（秒，可手动改）
    "base_ts": None,          # 最早一张样本的时间戳
    "mode": "period",         # period=按周期对齐锚点；now=直接用系统当前时间
    "box_size": 10,
    "auto_refresh": True,
}


# ================================================================ 模板学习
def _timestamp_segments(payload):
    """返回 (段列表, 时间戳段下标；找不到返回 -1)。"""
    segs = core.split_payload(payload)
    kp = -1
    for i, seg in enumerate(segs):
        if core._is_timestamp(seg):
            kp = i
            break
    return segs, kp


def _learn_from_infos(infos):
    """
    从解析结果学模板：固定段 / 时间戳段位置 / 掩码 / 纠错 / 版本。
    多张优先用 analyze_payload_order 找变化段；单张直接定位时间戳段。
    """
    if not infos:
        return None

    # 多张：优先复用命令行版的分段 + 时间戳识别逻辑
    tmpl = core.learn_ts_template(infos)
    if tmpl is not None:
        return tmpl

    # 单张兜底：在载荷里找时间戳段
    info0 = infos[0]
    segs, kp = _timestamp_segments(info0.payload)
    if kp < 0:
        # 载荷里没有明显时间戳段：把整段视为固定段，时间戳追加到末尾
        head, tail = segs, []
    else:
        head, tail = segs[:kp], segs[kp + 1:]
    return {
        "head": head,
        "tail": tail,
        "mask": info0.mask,
        "ec": (info0.ec_level or "M").upper(),
        "version": info0.version,
    }


def _extract_samples(infos):
    """从解析结果整理样本列表，附带换算时间与相邻间隔。"""
    rows = []
    for info in infos:
        segs, kp = _timestamp_segments(info.payload)
        ts = None
        if kp >= 0:
            try:
                ts = int(segs[kp])
            except (TypeError, ValueError):
                ts = None
        rows.append({
            "name": os.path.basename(info.path),
            "payload": info.payload,
            "ts": ts,
            "mask": info.mask,
            "ec": info.ec_level or "?",
            "version": info.version or "?",
            "time": core.fmt_ts(ts) if ts else "—",
        })

    rows.sort(key=lambda r: (r["ts"] is None, r["ts"]))
    intervals = []
    for i in range(1, len(rows)):
        a, b = rows[i - 1]["ts"], rows[i]["ts"]
        if a is not None and b is not None:
            intervals.append(b - a)
    return rows, intervals


def _guess_interval(intervals):
    """间隔候选：有重复差值取众数；否则取最小差值。样本不足返回默认 120。"""
    if not intervals:
        return 120
    if len(set(intervals)) < len(intervals):
        # 众数
        return max(set(intervals), key=intervals.count)
    return min(intervals)


def apply_samples(infos):
    """解析成功后，把模板 / 样本 / 间隔写入全局状态。"""
    tmpl = _learn_from_infos(infos)
    rows, intervals = _extract_samples(infos)

    if tmpl is not None:
        STATE["template"].update({
            "head": tmpl.get("head") or [],
            "tail": tmpl.get("tail") or [],
            "mask": tmpl.get("mask"),
            "ec": tmpl.get("ec") or "M",
            "version": tmpl.get("version"),
        })

    STATE["samples"] = rows
    STATE["intervals"] = intervals
    timed = [r["ts"] for r in rows if r["ts"] is not None]
    STATE["base_ts"] = min(timed) if timed else None
    guessed = _guess_interval(intervals)
    # 只在没有手动固定过间隔时采用推算值；简单起见每次刷新样本都重算
    STATE["interval"] = guessed
    STATE["loaded"] = bool(rows)
    STATE["error"] = ""


# ================================================================ 实时推算
def compute_live():
    """
    依据当前系统时间，推算当前应显示的时间戳与下一张。
    返回 dict：cur_ts / next_ts / countdown / payload / 序号等。
    """
    now = int(time.time())
    t = STATE["template"]
    interval = max(1, int(STATE["interval"] or 120))

    if STATE["mode"] == "now" or STATE["base_ts"] is None:
        cur_ts = now
        next_ts = None
        countdown = interval
        n = None
    else:
        anchor = STATE["base_ts"]
        n = (now - anchor) // interval if now >= anchor else 0
        cur_ts = anchor + n * interval
        next_ts = cur_ts + interval
        countdown = max(0, next_ts - now)

    payload = core.build_ts_payload(cur_ts, t)
    return {
        "now": now,
        "cur_ts": cur_ts,
        "next_ts": next_ts,
        "countdown": countdown,
        "index": n,
        "payload": payload,
        "cur_time": core.fmt_ts(cur_ts),
        "now_time": core.fmt_ts(now),
    }


# ================================================================ 内存渲染
def render_qr_png(payload, mask, ec_level, version, box_size=10, border=4):
    """在内存里渲染二维码 PNG，返回 (字节, 实际版本)。"""
    if core.qrcode is None:
        raise RuntimeError("缺少依赖：qrcode")
    mask = int(mask) if mask is not None else 3
    ec_level = (ec_level or "M").upper()
    if ec_level not in core.EC_CHOICES:
        ec_level = "M"

    def _build(ver):
        code = core.qrcode.QRCode(
            version=ver,
            error_correction=core.LEVEL_TO_EC_CONST[ec_level],
            box_size=box_size,
            border=border,
            mask_pattern=mask,          # 显式指定掩码，关闭自动挑选
        )
        code.add_data(payload)
        code.make(fit=(ver is None))
        img = code.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), code.version

    try:
        return _build(version)
    except Exception:
        # 指定版本放不下 -> 自动挑最小版本
        return _build(None)


# ================================================================ HTTP 服务
def _send_json(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


def _parse_files(payload, tmp_dir):
    """
    解析上传载荷：
      {"files": [{"name": 文件名, "data": "data:image/png;base64,...."}],
       "folder": "本地目录路径"}
    返回 (infos, errors)。
    """
    infos, errors = [], []

    # 1) 浏览器多选上传的图片（base64）
    for item in payload.get("files") or []:
        name = item.get("name") or "upload.png"
        data = item.get("data") or ""
        if "," in data[:64]:
            data = data.split(",", 1)[1]
        try:
            blob = base64.b64decode(data)
            fd, path = tempfile.mkstemp(suffix=os.path.splitext(name)[1] or ".png",
                                        dir=tmp_dir)
            os.close(fd)
            with open(path, "wb") as fp:
                fp.write(blob)
            infos.append(core.parse_qr_image(path))
        except Exception as exc:
            errors.append("%s：%s" % (name, exc))

    # 2) 本地目录扫描（打包 exe 本地运行时，直接读磁盘）
    folder = (payload.get("folder") or "").strip().strip('"').strip("'")
    if folder and os.path.isdir(folder):
        for name in core.list_images(folder):
            full = os.path.join(folder, name)
            try:
                infos.append(core.parse_qr_image(full))
            except Exception as exc:
                errors.append("%s：%s" % (name, exc))
    elif folder:
        errors.append("目录不存在：%s" % folder)

    return infos, errors


def handle_state(handler):
    live = compute_live()
    out = {
        "loaded": STATE["loaded"],
        "error": STATE["error"],
        "samples": STATE["samples"],
        "template": STATE["template"],
        "intervals": STATE["intervals"],
        "interval": STATE["interval"],
        "base_ts": STATE["base_ts"],
        "mode": STATE["mode"],
        "box_size": STATE["box_size"],
        "auto_refresh": STATE["auto_refresh"],
        "live": live,
        "template_text": core.template_text(STATE["template"]),
    }
    _send_json(handler, out)


def handle_analyze(handler, tmp_dir):
    payload = _read_json(handler)
    infos, errors = _parse_files(payload, tmp_dir)
    if infos:
        apply_samples(infos)
        if errors:
            STATE["error"] = "部分图片解析失败：" + "；".join(errors[:3])
    else:
        STATE["loaded"] = False
        STATE["error"] = "未识别到有效二维码：" + "；".join(errors[:3])
    handle_state(handler)


def handle_settings(handler):
    data = _read_json(handler)
    if "interval" in data:
        try:
            STATE["interval"] = max(1, int(float(data["interval"])))
        except (TypeError, ValueError):
            pass
    if data.get("mode") in ("period", "now"):
        STATE["mode"] = data["mode"]
    if "box_size" in data:
        try:
            STATE["box_size"] = max(2, int(float(data["box_size"])))
        except (TypeError, ValueError):
            pass
    if "auto_refresh" in data:
        STATE["auto_refresh"] = bool(data["auto_refresh"])
    # 允许手动覆盖固定段 / 掩码 / 纠错（高级微调）
    t = STATE["template"]
    if isinstance(data.get("head"), list):
        t["head"] = [str(x) for x in data["head"] if str(x) != ""]
    if isinstance(data.get("tail"), list):
        t["tail"] = [str(x) for x in data["tail"] if str(x) != ""]
    if data.get("mask") is not None:
        try:
            t["mask"] = int(data["mask"])
        except (TypeError, ValueError):
            pass
    if data.get("ec") in core.EC_CHOICES:
        t["ec"] = data["ec"]
    handle_state(handler)


def handle_qr(handler, query):
    from urllib.parse import parse_qs
    qs = parse_qs(query)
    def _first(key, default=None):
        vals = qs.get(key)
        return vals[0] if vals else default

    ts = _first("ts")
    try:
        ts = int(ts) if ts is not None else compute_live()["cur_ts"]
    except (TypeError, ValueError):
        ts = compute_live()["cur_ts"]

    t = STATE["template"]
    payload = core.build_ts_payload(ts, t)
    try:
        blob, real_version = render_qr_png(
            payload, t.get("mask"), t.get("ec"), t.get("version"),
            box_size=STATE["box_size"],
        )
    except Exception as exc:
        _send_json(handler, {"error": str(exc)}, status=500)
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "image/png")
    handler.send_header("Content-Length", str(len(blob)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(blob)


def make_handler(tmp_dir):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 静默控制台日志

        def do_GET(self):
            path = self.path.split("?", 1)
            route, query = (path + [""])[:2]
            if route in ("/", "/index.html"):
                body = PAGE_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/state":
                handle_state(self)
            elif route == "/api/qr":
                handle_qr(self, query)
            else:
                _send_json(self, {"error": "not found"}, status=404)

        def do_POST(self):
            route = self.path.split("?", 1)[0]
            if route == "/api/analyze":
                handle_analyze(self, tmp_dir)
            elif route == "/api/settings":
                handle_settings(self)
            else:
                _send_json(self, {"error": "not found"}, status=404)

    return Handler


def pick_port(start=8765):
    """端口被占用就顺延。"""
    from http.server import HTTPServer
    for port in range(start, start + 20):
        try:
            return HTTPServer(("127.0.0.1", port), None).server_address[1]
        except OSError:
            continue
    return start


def ensure_dependencies():
    """缺依赖时给出引导：打印安装命令，可选择自动 pip 安装后重启进程。"""
    if not getattr(core, "_DEP_MISSING", None):
        return True
    print("⚠ 缺少依赖：%s" % "、".join(core._DEP_MISSING))
    req = os.path.join(HERE, "requirements.txt")
    if os.path.isfile(req):
        print("  请先执行：pip install -r requirements.txt")
    else:
        print("  请先执行：pip install zxing-cpp pillow qrcode")
    ans = input("是否现在自动安装？[Y/n]: ").strip().lower()
    if ans in ("n", "no", "否"):
        return False
    cmd = [sys.executable, "-m", "pip", "install"]
    cmd += ["-r", req] if os.path.isfile(req) else ["zxing-cpp", "pillow", "qrcode"]
    try:
        rc = subprocess.call(cmd)
    except Exception as exc:
        print("调用 pip 失败：%s" % exc)
        return False
    if rc != 0:
        print("安装未成功，可换国内镜像：")
        print("  pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return False
    print("安装完成，正在重启程序…")
    try:
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
    except Exception as exc:
        print("自动重启失败（%s），请手动重新运行。" % exc)
    return False


def main():
    from http.server import HTTPServer

    safe_console()
    if not ensure_dependencies():
        return
    tmp_dir = tempfile.mkdtemp(prefix="qrmask_web_")

    # 启动时自动扫描可能存在的样本目录（脚本目录 / exe 同级 / 当前目录）
    auto_candidates = [
        os.path.join(HERE, "图片"),
        os.path.join(os.path.dirname(sys.executable), "图片"),
        os.path.join(os.getcwd(), "图片"),
    ]
    for auto_dir in auto_candidates:
        if os.path.isdir(auto_dir):
            try:
                infos = []
                for name in core.list_images(auto_dir):
                    try:
                        infos.append(core.parse_qr_image(os.path.join(auto_dir, name)))
                    except Exception:
                        pass
                if infos:
                    apply_samples(infos)
                    break
            except Exception:
                pass

    port = pick_port()
    server = HTTPServer(("127.0.0.1", port), make_handler(tmp_dir))
    url = "http://127.0.0.1:%d/" % port

    print("=" * 60)
    print(" 二维码实时放映 · 实验模拟器（Web 本地服务）")
    print(" 地址：%s" % url)
    print(" 关闭本窗口即退出程序。仅供学习实验使用。")
    print("=" * 60)

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


def safe_console():
    """非 tty 输出兜底 UTF-8，避免日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


# ================================================================ 前端页面
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>二维码实时放映 · 实验模拟器</title>
<style>
  :root{--bg:#0f1420;--card:#1a2233;--line:#2b3550;--fg:#e6ecf5;--mut:#8b97b0;
        --acc:#4f8cff;--ok:#36c275;--warn:#f5a623;--bad:#ef5b6b;}
  *{box-sizing:border-box}
  body{margin:0;font-family:"Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--fg)}
  header{padding:16px 22px;border-bottom:1px solid var(--line)}
  header h1{margin:0 0 4px;font-size:18px}
  header .sub{color:var(--mut);font-size:12px}
  .wrap{display:grid;grid-template-columns:420px 1fr;gap:16px;padding:16px 22px}
  @media(max-width:900px){.wrap{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
  .card h2{margin:0 0 12px;font-size:14px;color:var(--acc)}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0}
  label{font-size:12px;color:var(--mut)}
  input,select,button{font:inherit}
  input[type=text],input[type=number],select{background:#0d1320;border:1px solid var(--line);color:var(--fg);
    border-radius:8px;padding:8px 10px;min-width:0}
  button{background:var(--acc);border:0;color:#fff;border-radius:8px;padding:9px 14px;cursor:pointer}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
  .filebtn{display:inline-block;border:1px dashed var(--line);border-radius:8px;padding:10px 12px;
    color:var(--mut);cursor:pointer}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
  th{color:var(--mut)}
  .qrbox{display:flex;flex-direction:column;align-items:center;gap:14px}
  .qrbox img{background:#fff;padding:14px;border-radius:12px;max-width:340px;width:100%}
  .meta{text-align:center}
  .meta .big{font-size:20px;font-weight:700;margin:4px 0}
  .meta .ts{color:var(--mut);font-size:13px}
  .pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;background:#0d1320;color:var(--mut)}
  .pill.ok{color:var(--ok)} .pill.warn{color:var(--warn)}
  .payload{font-family:ui-monospace,Consolas,monospace;font-size:13px;background:#0d1320;
    border:1px solid var(--line);border-radius:8px;padding:10px;word-break:break-all}
  .muted{color:var(--mut);font-size:12px}
  .err{color:var(--bad);font-size:12px;margin-top:8px}
  .kv{display:grid;grid-template-columns:90px 1fr;gap:6px 10px;font-size:12px}
  .kv b{color:var(--mut);font-weight:400}
</style>
</head>
<body>
<header>
  <h1>二维码实时放映 · 实验模拟器 <span class="pill">学习 / 实验用途</span></h1>
  <div class="sub">上传同族二维码自动学习模板（固定段 · 时间戳段 · 掩码 · 纠错 · 版本），按系统时间推算并定时刷新显示。</div>
</header>
<div class="wrap">
  <div class="card">
    <h2>1 · 导入样本二维码</h2>
    <div class="row">
      <label class="filebtn">选择图片（可多选）<input id="files" type="file" accept="image/*" multiple hidden></label>
      <label class="filebtn">选择文件夹<input id="dirpick" type="file" webkitdirectory hidden></label>
    </div>
    <div class="row">
      <input id="folder" type="text" placeholder="或填写本地目录路径，如 D:\项目\二维码显影\图片" style="flex:1">
      <button id="btnScan">扫描目录</button>
    </div>
    <div id="err" class="err"></div>

    <h2 style="margin-top:18px">2 · 样本与间隔</h2>
    <div id="sampleTable"></div>
    <div class="kv" style="margin-top:10px">
      <b>模板</b><span id="tpl">—</span>
      <b>掩码</b><span id="mask">—</span>
      <b>纠错/版本</b><span id="ecv">—</span>
      <b>相邻差值</b><span id="diffs">—</span>
    </div>

    <h2 style="margin-top:18px">3 · 放映设置</h2>
    <div class="row">
      <label>刷新间隔(秒)</label>
      <input id="interval" type="number" min="1" style="width:110px">
      <label>模式</label>
      <select id="mode">
        <option value="period">按周期对齐锚点</option>
        <option value="now">直接用系统当前时间</option>
      </select>
    </div>
    <div class="row">
      <label>模块像素</label>
      <input id="box" type="number" min="2" style="width:90px">
      <label><input id="auto" type="checkbox"> 自动定时刷新</label>
      <button id="btnApply" class="ghost">应用设置</button>
    </div>
    <div class="muted" style="margin-top:6px">提示：样本间隔不一致时，程序会列出各段差值，请按真实节奏手动修正间隔。</div>
  </div>

  <div class="card qrbox">
    <div class="meta">
      <span class="pill" id="statePill">未加载样本</span>
      <div class="big" id="curTime">—</div>
      <div class="ts" id="curTs">—</div>
    </div>
    <img id="qr" alt="二维码" style="display:none">
    <div class="payload" id="payload">—</div>
    <div class="row">
      <span class="muted">下一张：<span id="nextTs">—</span></span>
      <span class="muted">倒计时：<span id="countdown">—</span> 秒</span>
    </div>
  </div>
</div>

<script>
let state = null;

function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));}

async function loadState(){
  const r = await fetch("/api/state");
  state = await r.json();
  render();
}

function render(){
  if(!state) return;
  const err = document.getElementById("err");
  err.textContent = state.error || "";

  // 样本表
  const st = state.samples||[];
  let html = "<table><tr><th>文件</th><th>时间戳</th><th>时间</th><th>掩码</th><th>纠错/版本</th></tr>";
  st.forEach(s=>{html += `<tr><td>${esc(s.name)}</td><td>${esc(s.ts)}</td><td>${esc(s.time)}</td><td>${esc(s.mask)}</td><td>${esc(s.ec)}/${esc(s.version)}</td></tr>`;});
  html += "</table>";
  document.getElementById("sampleTable").innerHTML = html || '<div class="muted">暂无样本</div>';

  document.getElementById("tpl").textContent = state.template_text || "—";
  document.getElementById("mask").textContent = state.template.mask ?? "—";
  document.getElementById("ecv").textContent = `${state.template.ec||"?"} / ${state.template.version||"?"}`;
  document.getElementById("diffs").textContent = (state.intervals||[]).join(" 秒、") || "—";

  document.getElementById("interval").value = state.interval;
  document.getElementById("mode").value = state.mode;
  document.getElementById("box").value = state.box_size;
  document.getElementById("auto").checked = state.auto_refresh;

  const live = state.live||{};
  document.getElementById("curTime").textContent = live.cur_time || "—";
  document.getElementById("curTs").textContent = "Unix 时间戳：" + (live.cur_ts ?? "—");
  document.getElementById("payload").textContent = live.payload || "—";
  document.getElementById("nextTs").textContent = live.next_ts ? `${live.next_ts}（${state.live && state.live.next_ts ? new Date(state.live.next_ts*1000).toLocaleString() : "—"}）` : "—";
  document.getElementById("countdown").textContent = live.countdown ?? "—";

  const pill = document.getElementById("statePill");
  if(state.loaded){pill.textContent="已加载样本"; pill.className="pill ok";}
  else{pill.textContent="未加载样本"; pill.className="pill warn";}

  if(state.loaded){
    const img = document.getElementById("qr");
    img.style.display = "block";
    img.src = "/api/qr?ts=" + live.cur_ts + "&_=" + Date.now();
  } else {
    document.getElementById("qr").style.display = "none";
  }
}

async function analyze(files){
  const arr = [];
  for(const f of files){
    const b64 = await new Promise(res=>{const r=new FileReader();r.onload=()=>res(r.result);r.readAsDataURL(f);});
    arr.push({name:f.name, data:b64});
  }
  await fetch("/api/analyze",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({files:arr})});
  await loadState();
}

document.getElementById("files").addEventListener("change", e=>analyze(e.target.files));
document.getElementById("dirpick").addEventListener("change", e=>analyze(e.target.files));
document.getElementById("btnScan").addEventListener("click", async()=>{
  const folder = document.getElementById("folder").value.trim();
  await fetch("/api/analyze",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({folder})});
  await loadState();
});

document.getElementById("btnApply").addEventListener("click", async()=>{
  await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      interval: +document.getElementById("interval").value,
      mode: document.getElementById("mode").value,
      box_size: +document.getElementById("box").value,
      auto_refresh: document.getElementById("auto").checked,
    })});
  await loadState();
});

loadState();
setInterval(()=>{ if(state && state.auto_refresh) loadState(); }, 1000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出。")
    except Exception:
        import traceback
        traceback.print_exc()
        input("按回车关闭…")
