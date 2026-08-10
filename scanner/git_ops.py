"""Git 操作模块：clone / fetch / diff"""

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List

import git
from git import Repo, GitCommandError


# 默认排除的非代码文件后缀
DEFAULT_EXCLUDE_EXTS = {".sql"}  # DDL/DML 脚本


@dataclass
class DiffStats:
    """diff --stat 解析结果"""
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    changed_files: list = field(default_factory=list)


class GitOperator:
    """封装所有 Git 操作"""

    def __init__(self, repo_path: Optional[str] = None):
        self._repo: Optional[Repo] = None
        self._repo_path: Optional[str] = repo_path
        self._temp_dir: Optional[str] = None

    # ── 仓库定位 ──────────────────────────────────────

    def resolve_repo(self, repo_path: str) -> Path:
        """解析仓库路径：本地路径直接返回，远程 URL 则 clone 到临时目录"""
        if self._is_git_url(repo_path):
            return self._clone_temp(repo_path)
        path = Path(repo_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"仓库路径不存在: {path}")
        if not (path / ".git").exists():
            raise FileNotFoundError(f"不是有效的 Git 仓库: {path}")
        self._repo_path = str(path)
        return path

    @staticmethod
    def _is_git_url(s: str) -> bool:
        """判断是否是 git URL"""
        return bool(
            re.match(r"^(https?|git|ssh)://", s)
            or s.endswith(".git")
            or re.match(r"^git@", s)
        )

    def _clone_temp(self, url: str) -> Path:
        """克隆到临时目录"""
        self._temp_dir = tempfile.mkdtemp(prefix="diff_scan_")
        print(f"📥 正在克隆仓库到临时目录: {self._temp_dir}")
        Repo.clone_from(url, self._temp_dir, depth=1)
        print("✅ 克隆完成")
        self._repo_path = self._temp_dir
        return Path(self._temp_dir)

    # ── 仓库对象 ──────────────────────────────────────

    @property
    def repo(self) -> Repo:
        if self._repo is None:
            if not self._repo_path:
                raise RuntimeError("尚未设置仓库路径")
            self._repo = Repo(self._repo_path)
        return self._repo

    # ── fetch ─────────────────────────────────────────

    def fetch_all(self):
        """拉取所有远程分支和 tag"""
        print("🔄 正在拉取最新代码...")
        try:
            for remote in self.repo.remotes:
                remote.fetch(tags=True)
            print("✅ 拉取完成")
        except GitCommandError as e:
            print(f"⚠️ 拉取失败（可能网络问题）: {e}")

    def list_branches(self) -> list:
        """列出所有远程分支名（去掉 remotes/origin/ 前缀），按版本号倒序"""
        branches = []
        for ref in self.repo.refs:
            name = str(ref)
            if name.startswith("origin/") and not name.endswith("/HEAD"):
                branches.append(name.replace("origin/", ""))
        branches.sort(reverse=True)
        return branches

    # ── diff 核心 ─────────────────────────────────────

    def get_diff_text(self, old_ref: str, new_ref: str, 
                      exclude_exts: Optional[set] = None,
                      context_lines: int = 10) -> str:
        """获取两个 ref 之间的 diff 文本，可选择排除指定后缀的文件
        
        Args:
            context_lines: 上下文行数，默认 10（快速），-1 表示全部上下文（慢）
        """
        ctx = "-U99999" if context_lines < 0 else f"-U{context_lines}"
        try:
            diff_text = self.repo.git.diff(f"{old_ref}...{new_ref}", ctx)
        except GitCommandError:
            diff_text = self.repo.git.diff(old_ref, new_ref, ctx)

        if exclude_exts:
            diff_text = self._filter_diff_text(diff_text, exclude_exts)
        return diff_text

    @staticmethod
    def _filter_diff_text(diff_text: str, exclude_exts: set) -> str:
        """从 unified diff 文本中移除匹配排除后缀的文件块"""
        # diff 块以 "diff --git a/<path> b/<path>" 开头
        blocks = re.split(r'(?=^diff --git )', diff_text, flags=re.MULTILINE)
        kept = []
        for block in blocks:
            if not block.strip():
                continue
            # 提取文件路径
            m = re.match(r'^diff --git a/(.*?) b/(.*?)$', block, re.MULTILINE)
            if m:
                filepath = m.group(1)
                _, ext = os.path.splitext(filepath)
                if ext.lower() in exclude_exts:
                    continue
            kept.append(block)
        return "".join(kept)

    def get_diff_stat(self, old_ref: str, new_ref: str) -> DiffStats:
        """获取 diff --stat 统计信息"""
        try:
            stat_output = self.repo.git.diff(f"{old_ref}...{new_ref}", stat=True)
        except GitCommandError:
            stat_output = self.repo.git.diff(old_ref, new_ref, stat=True)

        return self._parse_stat(stat_output)

    @staticmethod
    def _parse_stat(stat_output: str) -> DiffStats:
        """解析 git diff --stat 输出"""
        lines = stat_output.strip().split("\n")
        stats = DiffStats()

        if not lines:
            return stats

        # 最后一行是汇总: "15 files changed, 342 insertions(+), 87 deletions(-)"
        summary = lines[-1]
        files_match = re.search(r"(\d+)\s+files?\s+changed", summary)
        ins_match = re.search(r"(\d+)\s+insertions?\(\+\)", summary)
        del_match = re.search(r"(\d+)\s+deletions?\(\-\)", summary)
        if files_match:
            stats.files_changed = int(files_match.group(1))
        if ins_match:
            stats.insertions = int(ins_match.group(1))
        if del_match:
            stats.deletions = int(del_match.group(1))

        # 解析每个文件
        for line in lines[:-1]:
            parts = line.strip().split("|")
            if len(parts) >= 2:
                file_path = parts[0].strip()
                stats.changed_files.append(file_path)

        return stats

    def get_changed_java_files(self, old_ref: str, new_ref: str) -> list:
        """获取变更的 Java 文件列表（只取修改的，不含删除的）"""
        try:
            changed = self.repo.git.diff(
                f"{old_ref}...{new_ref}", name_only=True, diff_filter="AM"
            )
        except GitCommandError:
            changed = self.repo.git.diff(
                old_ref, new_ref, name_only=True, diff_filter="AM"
            )
        files = [f for f in changed.strip().split("\n") if f.endswith(".java")]
        return files

    def get_diff_for_file(self, old_ref: str, new_ref: str, file_path: str) -> str:
        """获取单个文件的 diff"""
        try:
            return self.repo.git.diff(f"{old_ref}...{new_ref}", "--", file_path)
        except GitCommandError:
            return self.repo.git.diff(old_ref, new_ref, "--", file_path)

    # ── 仓库元信息 ────────────────────────────────────

    def get_repo_name(self) -> str:
        """获取仓库名"""
        remote_url = ""
        try:
            remote_url = self.repo.remotes.origin.url
        except Exception:
            pass
        # 从 URL 或路径提取仓库名
        name = os.path.basename(remote_url.rstrip("/"))
        if name.endswith(".git"):
            name = name[:-4]
        if not name:
            name = os.path.basename(str(self._repo_path or ""))
        return name or "unknown"

    def get_commit_info(self, ref: str) -> dict:
        """获取某个 ref 对应的 commit 信息"""
        try:
            commit = self.repo.commit(ref)
            return {
                "sha": commit.hexsha,
                "short_sha": commit.hexsha[:8],
                "message": commit.message.strip().split("\n")[0],
                "author": str(commit.author),
                "date": commit.committed_datetime.strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception:
            return {
                "sha": ref,
                "short_sha": ref[:8] if len(ref) >= 8 else ref,
                "message": "(无法获取)",
                "author": "-",
                "date": "-",
            }

    # ── 清理 ──────────────────────────────────────────

    def cleanup(self):
        """清理临时目录"""
        if self._temp_dir and os.path.exists(self._temp_dir):
            print(f"🗑️  正在清理临时目录: {self._temp_dir}")
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

    def __del__(self):
        self.cleanup()
