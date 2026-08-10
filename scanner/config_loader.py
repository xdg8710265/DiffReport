"""YAML 配置文件加载模块"""

import os
from pathlib import Path
from typing import Optional

import yaml

from .cli_input import ScanConfig


def load_config(config_path: str) -> ScanConfig:
    """从 YAML 配置文件加载扫描配置"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError(f"配置文件为空: {config_path}")

    config = ScanConfig()

    # 仓库
    repo = data.get("repo", data.get("repository", ""))
    if not repo:
        raise ValueError("配置文件缺少 'repo' 字段")
    config.repo_path = repo

    # 如果是 GitLab URL
    if repo.startswith(("http://", "https://", "git@", "ssh://")):
        config.gitlab_url = repo

    # 对比 ref
    config.old_ref = str(data.get("old_ref", data.get("old", "main")))
    config.new_ref = str(data.get("new_ref", data.get("new", "HEAD")))

    # 输出目录
    config.output_dir = data.get("output", data.get("output_dir", "./reports"))

    # 影响分析
    config.impact_analysis = data.get("impact_analysis", data.get("impact", False))

    # 保留临时仓库
    config.keep_temp = data.get("keep_temp", data.get("keep_temporary", False))

    return config


def generate_example_config(output_path: Optional[str] = None) -> str:
    """生成配置文件示例"""
    example = """# 代码对比扫描工具 — 配置文件示例
# 使用: python main.py --config scan.yaml

# === 仓库配置 ===
# 本地仓库路径 或 GitLab URL
repo: /path/to/your/java/project

# === 对比版本 ===
# 旧版本（基准版本）: commit SHA / 分支名 / tag
old_ref: main
# 新版本（提测版本）: commit SHA / 分支名 / tag
new_ref: develop

# === 输出配置 ===
# 报告输出目录
output: ./reports

# === 分析配置 ===
# 是否启用 Java 变更影响分析 (jcci)
impact_analysis: true

# === 其他 ===
# 是否保留临时 clone 的仓库（默认自动清理）
keep_temp: false
"""
    if output_path:
        Path(output_path).write_text(example, encoding="utf-8")
        print(f"✅ 配置文件示例已生成: {output_path}")
    return example
