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
import urllib.parse
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# 确保当前目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner.git_ops import GitOperator

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_config.json")

# ── 扫描任务追踪 ──
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
        _update_task(task_id, "done",
                     report_url=f"http://localhost:{port}/reports/{filename}",
                     report_file=output_path,
                     message=f"扫描完成: {filename}")
    except Exception as e:
        _update_task(task_id, "error", message=str(e))


def _update_task(task_id: str, status: str, **kwargs):
    with _tasks_lock:
        task = _scan_tasks.get(task_id, {})
        task["status"] = status
        task.update(kwargs)
        _scan_tasks[task_id] = task


# ── HTML 页面 ──

ROOT_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>代码对比扫描</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f0f2f5; color: #24292e; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.container { max-width: 700px; width: 100%; margin: 40px 20px; }
.header { text-align: center; margin-bottom: 30px; }
.header h1 { font-size: 28px; color: #24292e; margin-bottom: 6px; }
.header p { color: #586069; font-size: 14px; }
.card { background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.08); padding: 32px; }
.form-group { margin-bottom: 20px; }
.form-group label { display: block; font-size: 13px; font-weight: 600; color: #586069; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
.form-group input, .form-group select { width: 100%; padding: 12px 14px; border: 2px solid #e1e4e8; border-radius: 8px; font-size: 15px; font-family: 'SFMono-Regular', Consolas, monospace; background: #fafbfc; transition: border-color 0.2s, box-shadow 0.2s; }
.form-group input:focus, .form-group select:focus { outline: none; border-color: #0366d6; box-shadow: 0 0 0 3px rgba(3,102,214,.12); background: #fff; }
.form-group .hint { font-size: 12px; color: #959da5; margin-top: 4px; }
.row { display: flex; gap: 12px; }
.row .form-group { flex: 1; }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 12px 28px; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; transition: all 0.15s; }
.btn-primary { background: linear-gradient(135deg, #0366d6, #0256b9); color: #fff; width: 100%; }
.btn-primary:hover { background: linear-gradient(135deg, #0256b9, #0146a0); }
.btn-primary:disabled { background: #959da5; cursor: not-allowed; }
.btn-sm { padding: 10px 14px; font-size: 13px; border: 2px solid #e1e4e8; background: #f6f8fa; border-radius: 8px; cursor: pointer; }
.btn-sm:hover { background: #e1e4e8; }
.status { margin-top: 16px; padding: 12px 16px; border-radius: 8px; font-size: 14px; display: none; }
.status.info { display: block; background: #f0f7ff; color: #0366d6; border: 1px solid #c8e1ff; }
.status.success { display: block; background: #dcffe4; color: #28a745; border: 1px solid #bef5cb; }
.status.error { display: block; background: #ffeef0; color: #cb2431; border: 1px solid #ffdce0; }
.status a { color: inherit; font-weight: 600; }
.progress-bar { margin-top: 12px; height: 6px; background: #e1e4e8; border-radius: 3px; overflow: hidden; }
.progress-bar .fill { height: 100%; background: linear-gradient(90deg, #0366d6, #28a745); border-radius: 3px; animation: progress 2s ease-in-out infinite; width: 30%; }
@keyframes progress { 0% { width: 10%; } 50% { width: 70%; } 100% { width: 10%; } }
.reports-section { margin-top: 24px; }
.reports-section h3 { font-size: 16px; color: #586069; margin-bottom: 12px; }
.report-item { display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: #f6f8fa; border-radius: 8px; margin-bottom: 8px; font-size: 14px; transition: background 0.15s; }
.report-item:hover { background: #e8ecf0; }
.report-item a { color: #0366d6; text-decoration: none; font-weight: 500; flex: 1; word-break: break-all; }
.report-item .time { color: #959da5; font-size: 12px; white-space: nowrap; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🚀 代码对比扫描</h1>
    <p>选择对比分支，一键生成差异报告</p>
  </div>

  <div class="card">
    <div class="form-group">
      <label>📦 仓库</label>
      <div style="display:flex;gap:8px;">
        <select id="repo-select" style="flex:1;" onchange="onRepoChange()">
          <option value="">⏳ 发现仓库中...</option>
        </select>
        <button class="btn-sm" onclick="discoverRepos()" title="重新扫描仓库">🔍</button>
      </div>
      <input type="text" id="repo" placeholder="或手动输入仓库路径" style="margin-top:8px;display:none;">
      <div class="hint">下拉选择已发现的仓库，或手动输入路径</div>
    </div>

    <div class="row">
      <div class="form-group">
        <label>🔖 基线版本 (旧)</label>
        <input type="text" id="old-ref" value="master" placeholder="master">
      </div>
      <div class="form-group">
        <label>🆕 对比版本 (新)</label>
        <div style="display:flex;gap:8px;">
          <select id="new-ref" style="flex:1;">
            <option value="">— 选择分支 —</option>
          </select>
          <button class="btn-sm" onclick="loadBranches()" title="刷新分支列表">🔄</button>
        </div>
      </div>
    </div>

    <button class="btn btn-primary" id="scan-btn" onclick="startScan()">
      <span id="btn-text">🔍 开始对比（快速模式，约 10-30 秒）</span>
      <span id="btn-wait" style="display:none;">⏳ 等待扫描完成...</span>
    </button>

    <div style="display:flex;gap:24px;margin-top:16px;font-size:13px;color:#586069;">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
        <input type="checkbox" id="full-context" onchange="updateBtnText()">
        完整文件上下文（较慢，约 1-3 分钟）
      </label>
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
        <input type="checkbox" id="enable-impact" checked>
        Java 影响分析
      </label>
    </div>

    <div id="status" class="status"></div>
    <div id="progress" class="progress-bar" style="display:none;"><div class="fill"></div></div>
  </div>

  <div class="reports-section" id="reports-section" style="display:none;">
    <h3>📄 历史报告</h3>
    <div id="report-list"></div>
  </div>
</div>

<script>
var POLL_INTERVAL = 3000;

function getRepoPath() {
  var sel = document.getElementById('repo-select');
  var input = document.getElementById('repo');
  // 优先取手动输入，为空时取下拉选中值
  if (input.style.display !== 'none' && input.value.trim()) {
    return input.value.trim();
  }
  return sel.value || '';
}

function onRepoChange() {
  var sel = document.getElementById('repo-select');
  var input = document.getElementById('repo');
  if (sel.value === '__manual__') {
    input.style.display = 'block';
    input.focus();
    input.value = '';
    sel.value = '__manual__';
  } else if (sel.value && sel.value !== '__manual__') {
    input.style.display = 'none';
    input.value = '';
    loadBranches();
  }
}

function discoverRepos() {
  var sel = document.getElementById('repo-select');
  sel.innerHTML = '<option value="">⏳ 扫描仓库中...</option>';

  fetch('/repos')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var repos = data.repos || [];
      sel.innerHTML = '';
      if (repos.length === 0) {
        sel.innerHTML = '<option value="">— 未发现仓库 —</option>';
      } else {
        repos.forEach(function(r) {
          var label = r.name + '  (' + r.path + ')' + (r.source === 'discovered' ? ' 🔍' : '');
          sel.innerHTML += '<option value="' + r.path + '">' + label + '</option>';
        });
      }
      sel.innerHTML += '<option value="__manual__">✏️ 手动输入路径...</option>';
      // 自动选中默认仓库
      if (data.default_path) sel.value = data.default_path;
      if (sel.value) loadBranches();
    })
    .catch(function(err) {
      sel.innerHTML = '<option value="">— 加载失败 —</option>';
    });
}

function showStatus(msg, type) {
  var el = document.getElementById('status');
  el.className = 'status ' + (type || 'info');
  el.innerHTML = msg;
}

function setScanning(scanning) {
  document.getElementById('btn-text').style.display = scanning ? 'none' : 'inline';
  document.getElementById('btn-wait').style.display = scanning ? 'inline' : 'none';
  document.getElementById('scan-btn').disabled = scanning;
  document.getElementById('progress').style.display = scanning ? 'block' : 'none';
}

function loadBranches() {
  var repo = getRepoPath();
  if (!repo) { showStatus('请先选择或输入仓库路径', 'error'); return; }

  var sel = document.getElementById('new-ref');
  sel.innerHTML = '<option value="">⏳ 加载中...</option>';
  showStatus('正在获取分支列表...', 'info');

  fetch('/branches?repo=' + encodeURIComponent(repo))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.success) {
        sel.innerHTML = '<option value="">— 选择分支 (' + data.branches.length + ' 个) —</option>';
        (data.branches || []).forEach(function(b) {
          sel.innerHTML += '<option value="' + b + '">' + b + '</option>';
        });
        showStatus('✅ 已加载 ' + data.branches.length + ' 个分支', 'success');
        // 后台保存仓库路径
        fetch('/config', { method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: 'repo=' + encodeURIComponent(repo)
        }).catch(function(){});
      } else {
        sel.innerHTML = '<option value="">— 加载失败 —</option>';
        showStatus('❌ ' + data.error, 'error');
      }
    })
    .catch(function(err) {
      sel.innerHTML = '<option value="">— 网络错误 —</option>';
      showStatus('❌ 无法连接: ' + err.message, 'error');
    });
}

function updateBtnText() {
  var full = document.getElementById('full-context').checked;
  var btn = document.getElementById('btn-text');
  btn.textContent = full ? '🔍 开始对比（完整模式，约 1-3 分钟）' : '🔍 开始对比（快速模式，约 10-30 秒）';
}

function startScan() {
  var repo = getRepoPath();
  var old = document.getElementById('old-ref').value.trim() || 'master';
  var newRef = document.getElementById('new-ref').value;
  var full = document.getElementById('full-context').checked ? '1' : '0';
  var impact = document.getElementById('enable-impact').checked ? '1' : '0';

  if (!repo || !newRef) {
    showStatus('请填写仓库路径并选择对比分支', 'error');
    return;
  }

  setScanning(true);
  showStatus('正在启动扫描...', 'info');

  fetch('/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'repo=' + encodeURIComponent(repo)
      + '&old=' + encodeURIComponent(old)
      + '&new=' + encodeURIComponent(newRef)
      + '&impact=' + impact
      + '&full=' + full
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.task_id) {
        showStatus('⏳ 扫描进行中，请稍候（约 1-3 分钟）...', 'info');
        pollStatus(data.task_id);
      } else {
        setScanning(false);
        showStatus('❌ 启动失败: ' + (data.error || '未知错误'), 'error');
      }
    })
    .catch(function(err) {
      setScanning(false);
      showStatus('❌ 请求失败: ' + err.message, 'error');
    });
}

function pollStatus(taskId) {
  fetch('/scan-status?task_id=' + encodeURIComponent(taskId))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'done') {
        setScanning(false);
        showStatus('✅ 扫描完成！<a href="' + data.report_url + '" target="_blank">📄 打开报告</a>', 'success');
        loadReports();
      } else if (data.status === 'error') {
        setScanning(false);
        showStatus('❌ 扫描失败: ' + data.message, 'error');
      } else {
        // 仍在运行，继续轮询
        if (data.message) showStatus('⏳ ' + data.message, 'info');
        setTimeout(function() { pollStatus(taskId); }, POLL_INTERVAL);
      }
    })
    .catch(function(err) {
      // 网络错误，继续重试
      setTimeout(function() { pollStatus(taskId); }, POLL_INTERVAL);
    });
}

function loadReports() {
  fetch('/reports-list')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.reports && data.reports.length > 0) {
        document.getElementById('reports-section').style.display = 'block';
        var html = '';
        data.reports.forEach(function(r) {
          html += '<div class="report-item">'
            + '<span>📊</span>'
            + '<a href="/reports/' + r.filename + '" target="_blank">' + r.filename + '</a>'
            + '<span class="time">' + r.time + '</span>'
            + '</div>';
        });
        document.getElementById('report-list').innerHTML = html;
      }
    })
    .catch(function(){});
}

(function() {
  discoverRepos();
  loadReports();
})();
</script>
</body>
</html>"""


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
        elif path.startswith("/reports/"):
            self._serve_report(path)
        else:
            self._send_json({"error": "未知路径: " + path}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/scan":
            params = self._read_body_params()
            self._start_scan(params)
        elif path == "/config":
            params = self._read_body_params()
            self._handle_config_set(params)
        else:
            self._send_json({"error": "不支持的方法"}, 405)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── 根页面 ──
    def _serve_root(self):
        html = ROOT_HTML
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

    # ── 扫描（异步） ──
    def _start_scan(self, params):
        repo = params.get("repo", "")
        old = params.get("old", "master")
        new = params.get("new", "")
        impact = params.get("impact", "1") not in ("0", "false", "no")
        full_context = params.get("full", "0") not in ("0", "false", "no")

        if not repo or not new:
            self._send_json({"success": False, "error": "缺少参数: repo 和 new 为必填项"}, 400)
            return

        # 分支名补全 origin/
        if new and "/" not in new:
            new = "origin/" + new
        if old and "/" not in old and old != "master":
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
