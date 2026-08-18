#!/usr/bin/env python3
"""
扫描服务 — HTTP 接口，多线程处理请求，扫描不阻塞。

用法:
  python scan_server.py              # 默认端口 8899
  python scan_server.py --port 9000  # 自定义端口
  python scan_server.py --repo E:/my-project  # 指定默认仓库

接口:
  GET  /                    → 对比入口页面
  POST /scan                → 触发扫描（后台异步），返回 task_id
  GET  /scan-status?task_id=... → 查询扫描进度
  GET  /branches?repo=...   → 列出仓库分支
  GET  /config              → 获取/设置配置
  GET  /status              → 服务状态
"""

import os
import sys
import json
import re
import subprocess
import urllib.parse
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# 确保当前目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner.git_ops import GitOperator

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_config.json")
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_history.json")

# lark-cli 路径（服务器进程 PATH 可能不包含 npm 全局目录）
_LARK_CLI = os.path.expandvars(r"%APPDATA%\npm\lark-cli.cmd")
if not os.path.exists(_LARK_CLI):
    _LARK_CLI = "lark-cli"  # fallback
_scan_tasks: dict = {}
_tasks_lock = threading.Lock()


def load_config() -> dict:
    """加载配置，兼容旧格式（单 repo_path → 转为 repos 列表）"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return {}
    else:
        cfg = {}

    # 兼容旧格式: { "repo_path": "..." } → { "repos": [...] }
    if "repo_path" in cfg and "repos" not in cfg:
        old_path = cfg.pop("repo_path")
        name = os.path.basename(old_path.rstrip("/\\"))
        cfg["repos"] = [{"name": name, "path": old_path}]
        if "default" not in cfg:
            cfg["default"] = name
        save_config(cfg)
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_scan_history() -> list:
    """加载扫描历史记录（最新在前）"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_scan_history(history: list):
    """保存扫描历史记录（最多保留 50 条）"""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[:50], f, ensure_ascii=False, indent=2)


def add_scan_record(repo_name: str, repo_path: str, old_ref: str, new_ref: str,
                    report_url: str, report_file: str, files_changed: int = 0,
                    insertions: int = 0, deletions: int = 0):
    """新增一条扫描记录"""
    history = load_scan_history()
    record = {
        "repo_name": repo_name,
        "repo_path": repo_path,
        "old_ref": old_ref,
        "new_ref": new_ref,
        "report_url": report_url,
        "report_file": os.path.basename(report_file) if report_file else "",
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    history.insert(0, record)
    save_scan_history(history)


def _is_git_repo(path: str) -> bool:
    """判断目录是否为 git 仓库"""
    norm = os.path.normpath(os.path.abspath(path))
    return os.path.isdir(norm) and os.path.isdir(os.path.join(norm, ".git"))


def discover_repos(search_roots: list = None) -> list:
    """
    自动发现所有 git 仓库。优先返回已注册的，再扫描发现新的。
    返回: [{"name": "...", "path": "...", "source": "registered"|"discovered"}]
    """
    cfg = load_config()
    registered = {r["path"]: r for r in cfg.get("repos", [])}

    # 先收集已注册的
    result = []
    seen_paths = set()
    for r in cfg.get("repos", []):
        path = os.path.normpath(os.path.abspath(r["path"]))
        if path not in seen_paths and _is_git_repo(path):
            result.append({"name": r.get("name", os.path.basename(path)), "path": path, "source": "registered"})
            seen_paths.add(path)

    # 扫描发现新的
    if search_roots is None:
        search_roots = [
            "E:/",
            os.path.join(os.path.dirname(__file__), ".."),
        ]
    for root in search_roots:
        root = os.path.normpath(os.path.abspath(root))
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                full = os.path.join(root, entry)
                if os.path.isdir(full) and _is_git_repo(full):
                    norm = os.path.normpath(full)
                    if norm not in seen_paths:
                        result.append({"name": entry, "path": norm, "source": "discovered"})
                        seen_paths.add(norm)
        except PermissionError:
            continue

    return result


def get_default_repo() -> str:
    """获取默认仓库路径"""
    cfg = load_config()
    default_name = cfg.get("default", "")
    repos = cfg.get("repos", [])
    for r in repos:
        if r.get("name") == default_name:
            return r["path"]
    # 返回第一个可用仓库
    if repos:
        return repos[0]["path"]
    # 兜底
    return os.environ.get("SCAN_REPO_PATH", "")


def run_scan_async(task_id: str, repo: str, old: str, new: str, impact: bool, port: int, full_context: bool):
    """后台线程执行扫描"""
    try:
        _update_task(task_id, "running", message="正在执行 git diff...")
        from main import run_scan

        output_path = run_scan(
            repo_path=repo,
            old_ref=old,
            new_ref=new,
            impact=impact,
            output_dir="./reports",
            keep_temp=False,
            context_lines=-1 if full_context else 10,
        )
        filename = os.path.basename(output_path)
        report_url = f"http://localhost:{port}/reports/{filename}"
        _update_task(task_id, "done",
                     report_url=report_url,
                     report_file=output_path,
                     message=f"扫描完成: {filename}")
        # 记录对比历史
        add_scan_record(
            repo_name=os.path.basename(repo.rstrip("/\\")),
            repo_path=repo,
            old_ref=old,
            new_ref=new,
            report_url=report_url,
            report_file=output_path,
        )
    except Exception as e:
        _update_task(task_id, "error", message=str(e))


def _update_task(task_id: str, status: str, **kwargs):
    with _tasks_lock:
        task = _scan_tasks.get(task_id, {})
        task["status"] = status
        task.update(kwargs)
        _scan_tasks[task_id] = task


# ── HTML 页面 ──

DASHBOARD_TEMPLATE = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")


def _load_dashboard():
    """加载仪表盘 HTML（带缓存）"""
    global _dashboard_cache, _dashboard_mtime
    mtime = os.path.getmtime(DASHBOARD_TEMPLATE)
    if not _dashboard_cache or mtime != _dashboard_mtime:
        with open(DASHBOARD_TEMPLATE, "r", encoding="utf-8") as f:
            _dashboard_cache = f.read()
        _dashboard_mtime = mtime
    return _dashboard_cache


_dashboard_cache = ""
_dashboard_mtime = 0.0


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP Server：每个请求在独立线程处理，扫描不阻塞其他请求"""
    allow_reuse_address = True
    daemon_threads = True


class ScanHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _send_html(self, html, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(html.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_report(self, path):
        safe_path = os.path.normpath(path.lstrip("/"))
        file_path = os.path.join(os.path.dirname(__file__), safe_path)
        if not file_path.startswith(os.path.dirname(__file__)):
            self._send_json({"error": "禁止访问"}, 403)
            return
        if not os.path.exists(file_path):
            self._send_json({"error": "文件不存在"}, 404)
            return
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            ct = "text/html; charset=utf-8" if file_path.endswith(".html") else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _parse_params(self):
        parsed = urllib.parse.urlparse(self.path)
        return dict(urllib.parse.parse_qsl(parsed.query))

    def _read_body_params(self):
        cl = int(self.headers.get("Content-Length", 0))
        return dict(urllib.parse.parse_qsl(self.rfile.read(cl).decode("utf-8")))

    def _read_json_body(self):
        cl = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(cl)
        # 尝试 UTF-8，失败则尝试 GBK
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        return json.loads(text)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._serve_root()
        elif path == "/repos":
            self._handle_repos()
        elif path == "/scan-status":
            self._handle_scan_status()
        elif path == "/branches":
            self._handle_branches()
        elif path == "/config":
            self._handle_config_get()
        elif path == "/status":
            self._send_json({"status": "ok", "message": "扫描服务运行中"})
        elif path == "/reports-list":
            self._handle_reports_list()
        elif path == "/scan-history":
            self._handle_scan_history()
        elif path == "/scan":
            # GET /scan?repo=...&old=...&new=...&impact=1
            params = self._parse_params()
            self._start_scan(params)
        elif path.startswith("/reports/"):
            self._serve_report(path)
        else:
            self._send_json({"error": "未知路径: " + path}, 404)

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/scan":
                params = self._read_body_params()
                self._start_scan(params)
            elif path == "/config":
                params = self._read_body_params()
                self._handle_config_set(params)
            elif path == "/send-to-lark":
                body = self._read_json_body()
                self._handle_send_to_lark(body)
            else:
                self._send_json({"error": "不支持的方法"}, 405)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json({"success": False, "error": str(e)}, 500)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── 根页面 ──
    def _serve_root(self):
        html = _load_dashboard()
        self._send_html(html)

    # ── 仓库列表 ──
    def _handle_repos(self):
        repos = discover_repos()
        default_path = get_default_repo()
        self._send_json({"repos": repos, "default_path": default_path})

    # ── 配置 ──
    def _handle_config_get(self):
        cfg = load_config()
        cfg["repos"] = discover_repos()
        cfg["default_path"] = get_default_repo()
        self._send_json(cfg)

    def _handle_config_set(self, params):
        cfg = load_config()
        if "repo" in params:
            repo_path = params["repo"]
            name = os.path.basename(repo_path.rstrip("/\\"))
            repos = cfg.get("repos", [])
            # 去重更新
            found = False
            for r in repos:
                if r["path"] == repo_path:
                    r["name"] = params.get("name", name)
                    found = True
                    break
            if not found:
                repos.append({"name": params.get("name", name), "path": repo_path})
            cfg["repos"] = repos
            if params.get("set_default") == "1":
                cfg["default"] = params.get("name", name)
        save_config(cfg)
        self._send_json({"success": True, "config": cfg})

    # ── 报告列表 ──
    def _handle_reports_list(self):
        reports_dir = os.path.join(os.path.dirname(__file__), "reports")
        reports = []
        if os.path.isdir(reports_dir):
            for f in sorted(os.listdir(reports_dir), reverse=True):
                if f.startswith("diff_report_") and f.endswith(".html"):
                    fpath = os.path.join(reports_dir, f)
                    from datetime import datetime
                    reports.append({
                        "filename": f,
                        "time": datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M"),
                        "size": os.path.getsize(fpath),
                    })
        self._send_json({"reports": reports[:20]})

    def _handle_scan_history(self):
        """返回最近 N 条扫描历史记录"""
        history = load_scan_history()
        self._send_json({"history": history[:10]})

    # ── 扫描（异步） ──
    def _start_scan(self, params):
        repo = params.get("repo", "")
        old = params.get("old", "master")
        new = params.get("new", "")
        impact = params.get("impact", "1") not in ("0", "false", "no")
        full_context = params.get("full", "1") not in ("0", "false", "no")

        if not repo or not new:
            self._send_json({"success": False, "error": "缺少参数: repo 和 new 为必填项"}, 400)
            return

        # 分支名补全 origin/（fetch 后 origin/* 是最新的远程代码）
        if new and "/" not in new:
            new = "origin/" + new
        if old and "/" not in old:
            old = "origin/" + old

        task_id = str(uuid.uuid4())
        port = self.server.server_address[1]

        with _tasks_lock:
            _scan_tasks[task_id] = {"status": "pending", "message": "排队中..."}

        print(f"\n📥 扫描任务 {task_id[:8]}: {repo}  {old} → {new}  完整上下文={full_context}")

        t = threading.Thread(target=run_scan_async,
                             args=(task_id, repo, old, new, impact, port, full_context),
                             daemon=True)
        t.start()

        self._send_json({"success": True, "task_id": task_id})

    # ── 扫描状态查询 ──
    def _handle_scan_status(self):
        params = self._parse_params()
        task_id = params.get("task_id", "")
        with _tasks_lock:
            task = _scan_tasks.get(task_id, {"status": "unknown", "message": "任务不存在"})
        self._send_json(task)

    # ── 分支列表 ──
    def _handle_branches(self):
        params = self._parse_params()
        repo = params.get("repo", "")
        if not repo:
            self._send_json({"success": False, "error": "缺少 repo 参数"}, 400)
            return
        try:
            git_op = GitOperator()
            git_op.resolve_repo(repo)
            branches = git_op.list_branches()
            git_op.cleanup()
            self._send_json({"success": True, "branches": branches})
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, 500)

    # ── 发送到飞书 ──
    def _handle_send_to_lark(self, body: dict):
        recipients_raw = body.get("recipients", "")
        summary = body.get("summary", {})
        report_url = body.get("report_url", "")
        report_file = body.get("report_file", "")

        if not recipients_raw:
            self._send_json({"success": False, "error": "请填写接收人"}, 400)
            return

        # 解析接收人列表（逗号/分号/换行分隔）
        recipients = re.split(r"[,;，；\n]+", recipients_raw.strip())
        recipients = [r.strip() for r in recipients if r.strip()]

        # 先解析所有用户 ID
        user_map = {}  # recipient -> open_id
        for recipient in recipients:
            try:
                user_id = _resolve_user_id(recipient)
                if user_id:
                    user_map[recipient] = user_id
                else:
                    user_map[recipient] = None
            except Exception as e:
                user_map[recipient] = None

        # 获取服务器端口（用于构造截图 URL）
        port = self.server.server_address[1]

        # 截取报告截图并上传到飞书（只做一次）
        image_key = ""
        screenshot_path = ""
        try:
            screenshot_path = _capture_report_screenshot(report_url)
            image_key = _upload_image_to_lark(screenshot_path)
        except Exception as e:
            print(f"⚠️ 截图/上传失败: {e}")
        finally:
            # 清理临时截图
            if screenshot_path and os.path.exists(screenshot_path):
                try:
                    os.remove(screenshot_path)
                except OSError:
                    pass

        # 发送消息
        results = []
        for recipient in recipients:
            user_id = user_map.get(recipient)
            if not user_id:
                results.append({"recipient": recipient, "status": "error", "message": "未找到该用户"})
                continue
            try:
                _send_report_message(user_id, summary, report_url, report_file,
                                     image_key=image_key)
                results.append({"recipient": recipient, "status": "ok", "message": "已发送"})
            except Exception as e:
                results.append({"recipient": recipient, "status": "error", "message": str(e)[:200]})

        ok_count = sum(1 for r in results if r["status"] == "ok")
        self._send_json({
            "success": ok_count > 0,
            "sent": ok_count,
            "total": len(results),
            "results": results,
        })


def _resolve_user_id(recipient: str) -> str:
    """解析接收人：如果是 open_id 直接返回，否则通过飞书搜索"""
    # 已经是 open_id
    if re.match(r"^ou_[a-z0-9]+$", recipient):
        return recipient

    # 通过 lark-cli 搜索用户
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    result = subprocess.run(
        [_LARK_CLI, "contact", "+search-user", "--query", recipient, "--as", "user", "--json"],
        capture_output=True, text=True, timeout=15, env=env
    )
    if result.returncode != 0:
        raise RuntimeError(f"搜索用户失败: {result.stderr[:200]}")
    data = json.loads(result.stdout)
    # lark-cli contact +search-user 返回 {"data": {"users": [...]}}
    users = data.get("data", {}).get("users", [])
    if not users:
        return ""  # 未找到
    return users[0].get("open_id", "")


def _resolve_report_path(report_file: str) -> str:
    """将前端传来的 report_file（如 /reports/xxx.html）解析为本地绝对路径"""
    safe_path = os.path.normpath(report_file.lstrip("/"))
    file_path = os.path.join(os.path.dirname(__file__), safe_path)
    if not file_path.startswith(os.path.dirname(__file__)):
        raise RuntimeError("非法文件路径")
    if not os.path.exists(file_path):
        raise RuntimeError(f"报告文件不存在: {report_file}")
    return file_path


def _capture_report_screenshot(report_url: str) -> str:
    """用 Playwright 截取报告页面截图，返回截图临时文件路径"""
    import tempfile
    from playwright.sync_api import sync_playwright

    screenshot_path = os.path.join(tempfile.gettempdir(),
                                   f"report_screenshot_{os.urandom(4).hex()}.png")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(report_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)  # 等待 diff2html 样式和文件列表渲染
            # 只截取报告概览区域（不包含可折叠的 diff 详情）
            page.screenshot(path=screenshot_path, full_page=True)
        finally:
            browser.close()

    return screenshot_path


def _upload_image_to_lark(image_path: str) -> str:
    """上传图片到飞书，返回 image_key（img_v3_xxx）"""
    image_dir = os.path.dirname(os.path.abspath(image_path))
    image_name = os.path.basename(image_path)
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    result = subprocess.run(
        [_LARK_CLI, "im", "images", "create",
         "--data", '{"image_type":"message"}',
         "--file", image_name,
         "--as", "user", "--json"],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=image_dir
    )
    if result.returncode != 0:
        raise RuntimeError(f"上传图片失败: {result.stderr[:300]}")
    data = json.loads(result.stdout)
    image_key = data.get("data", {}).get("image_key", "")
    if not image_key:
        raise RuntimeError(f"上传图片返回缺少 image_key: {json.dumps(data, ensure_ascii=False)[:300]}")
    return image_key


def _upload_to_drive(file_path: str) -> dict:
    """上传 HTML 报告到飞书云空间，返回 {"file_token": ..., "url": ...}"""
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    result = subprocess.run(
        [_LARK_CLI, "drive", "+upload", "--file", file_path, "--as", "user", "--json"],
        capture_output=True, text=True, timeout=180, env=env
    )
    if result.returncode != 0:
        raise RuntimeError(f"上传到云空间失败: {result.stderr[:300]}")
    data = json.loads(result.stdout)
    inner = data.get("data", {})
    file_token = inner.get("file_token", "")
    url = inner.get("url", "")
    if not file_token:
        raise RuntimeError(f"上传返回缺少 file_token: {json.dumps(data, ensure_ascii=False)[:500]}")
    return {"file_token": file_token, "url": url, "name": inner.get("name", "")}


def _share_file_to_users(file_token: str, user_ids: list) -> None:
    """将 Drive 文件分享给指定用户（view 权限），批量一次最多 10 人"""
    if not user_ids:
        return
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    batch_size = 10
    for i in range(0, len(user_ids), batch_size):
        batch = user_ids[i:i + batch_size]
        member_ids = ",".join(batch)
        result = subprocess.run(
            [_LARK_CLI, "drive", "+member-add",
             "--token", file_token,
             "--type", "file",
             "--member-id", member_ids,
             "--member-type", "openid",
             "--perm", "view",
             "--as", "user", "--json", "--yes"],
            capture_output=True, text=True, timeout=30, env=env
        )
        if result.returncode != 0:
            print(f"⚠️ 分享文件给部分用户失败: {result.stderr[:200]}")


def _send_report_message(user_id: str, summary: dict, report_url: str, report_file: str,
                          image_key: str = ""):
    """通过飞书发送报告摘要消息（优先发送截图，附带详情链接）"""
    repo_name = summary.get("repo_name", "unknown")
    old_ref = summary.get("old_ref", "")
    new_ref = summary.get("new_ref", "")
    generated_at = summary.get("generated_at", "")
    files_changed = summary.get("files_changed", 0)
    insertions = summary.get("insertions", 0)
    deletions = summary.get("deletions", 0)
    net = insertions - deletions
    report_name = os.path.basename(report_file) if report_file else "diff_report.html"

    # 影响分析摘要
    impact_lines = ""
    if summary.get("impact"):
        imp = summary["impact"]
        if imp.get("test_suggestions"):
            impact_lines = "\n📋 **测试建议**\n"
            for s in imp["test_suggestions"][:5]:
                impact_lines += f"- {s}\n"

    # 如果有截图，放在消息顶部
    if image_key:
        markdown = f"![报告截图]({image_key})\n\n"
    else:
        markdown = ""

    markdown += (
        f"📊 **代码对比报告**\n\n"
        f"**仓库**: {repo_name}\n"
        f"**对比**: `{old_ref}` → `{new_ref}`\n"
        f"**时间**: {generated_at}\n\n"
        f"📈 **变更概览**\n"
        f"- 变更文件: {files_changed} 个\n"
        f"- 新增: +{insertions} 行\n"
        f"- 删除: -{deletions} 行\n"
        f"- 净变化: {'+' if net >= 0 else ''}{net} 行\n"
        f"{impact_lines}"
        f"\n📄 报告文件: {report_name}\n"
    )
    if report_url:
        markdown += f"🔗 [查看详情]({report_url})"

    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    result = subprocess.run(
        [_LARK_CLI, "im", "+messages-send",
         "--user-id", user_id,
         "--markdown", markdown,
         "--as", "user", "--json"],
        capture_output=True, text=True, timeout=30, env=env
    )
    if result.returncode != 0:
        raise RuntimeError(f"发送失败: {result.stderr[:300]}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="代码对比扫描 HTTP 服务")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--repo", type=str, default="", help="添加默认仓库路径（可多次使用）")
    parser.add_argument("--scan-root", type=str, default="", help="额外扫描根目录发现仓库")
    args = parser.parse_args()

    if args.repo:
        cfg = load_config()
        name = os.path.basename(args.repo.rstrip("/\\"))
        repos = cfg.get("repos", [])
        if not any(r["path"] == args.repo for r in repos):
            repos.append({"name": name, "path": args.repo})
        cfg["repos"] = repos
        cfg["default"] = name
        save_config(cfg)
        print(f"📦 仓库已注册: {name} → {args.repo}")

    if args.scan_root:
        # 扫描指定根目录发现仓库并自动注册
        discovered = discover_repos(search_roots=[args.scan_root])
        cfg = load_config()
        repos = cfg.get("repos", [])
        existing_paths = {r["path"] for r in repos}
        for d in discovered:
            if d["path"] not in existing_paths:
                repos.append({"name": d["name"], "path": d["path"]})
                print(f"🔍 发现新仓库: {d['name']} → {d['path']}")
        cfg["repos"] = repos
        if not cfg.get("default") and repos:
            cfg["default"] = repos[0]["name"]
        save_config(cfg)

    repo_path = get_default_repo()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), ScanHandler)
    print(f"🚀 扫描服务已启动: http://localhost:{args.port}")
    if repo_path:
        print(f"📦 默认仓库: {repo_path}")
    else:
        print(f"📦 未检测到仓库，打开页面后会自动扫描 E:/ 下的 git 仓库")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 服务已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
