"""交互式问答输入模块"""

import os
import sys
from pathlib import Path

from .cli_input import ScanConfig

# questionary 是可选依赖，降级为 input()
try:
    import questionary
    HAS_QUESTIONARY = True
except ImportError:
    HAS_QUESTIONARY = False


def run_interactive() -> ScanConfig:
    """交互式问答获取扫描配置"""
    config = ScanConfig()

    print("\n" + "=" * 50)
    print("  代码对比扫描工具 — 交互式配置")
    print("=" * 50 + "\n")

    # 步骤 1: 仓库
    repo_input = _ask(
        "请输入仓库路径或 GitLab URL",
        default=os.getcwd(),
        validate=lambda x: True if x.strip() else "仓库路径不能为空",
    )
    config.repo_path = repo_input

    # 步骤 2: 旧版本
    config.old_ref = _ask(
        "请输入旧版本 (commit SHA / 分支名 / tag)",
        default="main",
    )

    # 步骤 3: 新版本
    config.new_ref = _ask(
        "请输入新版本 (commit SHA / 分支名 / tag)",
        default="HEAD",
    )

    # 步骤 4: 输出目录
    config.output_dir = _ask(
        "报告输出目录",
        default="./reports",
    )

    # 步骤 5: 影响分析
    enable_impact = _confirm("是否启用 Java 变更影响分析 (jcci)?", default=False)
    config.impact_analysis = enable_impact

    # 步骤 6: 保留临时仓库
    config.keep_temp = _confirm("是否保留临时 clone 的仓库?", default=False)

    return config


def _ask(prompt: str, default: str = "", validate=None) -> str:
    """封装问答，兼容 questionary 和 input()"""
    if HAS_QUESTIONARY:
        result = questionary.text(
            prompt + ":",
            default=default,
            validate=validate,
        ).ask()
        return result if result is not None else default

    # 降级方案
    if default:
        value = input(f"{prompt} [{default}]: ").strip()
        return value if value else default
    else:
        value = input(f"{prompt}: ").strip()
        while not value:
            print("⚠️ 输入不能为空，请重新输入")
            value = input(f"{prompt}: ").strip()
        return value


def _confirm(prompt: str, default: bool = True) -> bool:
    """封装确认，兼容 questionary 和 input()"""
    if HAS_QUESTIONARY:
        return questionary.confirm(prompt + "?", default=default).ask()

    # 降级方案
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {hint}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")
