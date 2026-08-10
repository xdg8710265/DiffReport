"""命令行参数解析模块"""

import argparse
import sys
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScanConfig:
    """扫描配置，所有输入模式汇总到此结构"""
    repo_path: str = ""
    old_ref: str = ""
    new_ref: str = ""
    output_dir: str = "./reports"
    impact_analysis: bool = False
    keep_temp: bool = False  # 是否保留临时 clone 的仓库
    gitlab_url: Optional[str] = None
    exclude: list = field(default_factory=list)  # 排除的文件后缀


def parse_args() -> ScanConfig:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        prog="code-diff-scanner",
        description="代码对比扫描工具 — 基于 diff2html + jcci 生成可视化对比报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 本地仓库 + 两个 commit SHA
  python main.py --repo /path/to/project --old abc1234 --new def5678

  # GitLab URL + 分支对比
  python main.py --gitlab-url https://gitlab.com/group/project.git --old main --new develop

  # 交互式模式
  python main.py --interactive

  # 配置文件模式
  python main.py --config scan.yaml

  # 带 Java 影响分析
  python main.py --repo /path/to/project --old v1.0 --new v2.0 --impact
        """,
    )
    # 输入模式（互斥组）
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--repo", "-r",
        help="本地仓库路径",
    )
    input_group.add_argument(
        "--gitlab-url", "-g",
        help="GitLab 仓库 URL（将自动 clone 到临时目录）",
    )
    input_group.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="交互式问答模式",
    )
    input_group.add_argument(
        "--config", "-c",
        help="YAML 配置文件路径",
    )

    # 对比参数
    parser.add_argument("--old", help="旧版本 (commit SHA / 分支 / tag)")
    parser.add_argument("--new", help="新版本 (commit SHA / 分支 / tag)")

    # 可选参数
    parser.add_argument("--output", "-o", default="./reports", help="报告输出目录 (默认: ./reports)")
    parser.add_argument("--impact", action="store_true", help="启用 Java 变更影响分析 (jcci)")
    parser.add_argument("--no-impact", action="store_true", help="禁用 Java 变更影响分析")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时 clone 的仓库（默认自动清理）")
    parser.add_argument("--auto", action="store_true", help="自动模式：clone 后交互式选择 ref 对比")
    parser.add_argument("--exclude", "-e", nargs="*", default=None, help="排除的文件后缀（默认: .sql），如: --exclude .sql .xml .properties")

    args = parser.parse_args()

    config = ScanConfig()
    config.output_dir = args.output
    config.keep_temp = args.keep_temp
    config.exclude = args.exclude  # None = use default, [] = user specified empty, ['.sql', '.xml'] = custom

    # 影响分析开关
    if args.no_impact:
        config.impact_analysis = False
    elif args.impact:
        config.impact_analysis = True

    # 确定输入模式
    if args.interactive:
        # 由 interactive.py 处理
        pass
    elif args.config:
        # 由 config_loader.py 处理
        from .config_loader import load_config
        config = load_config(args.config)
    elif args.repo:
        config.repo_path = args.repo
        config.old_ref = args.old or ""
        config.new_ref = args.new or ""
    elif args.gitlab_url:
        config.repo_path = args.gitlab_url  # 暂存 URL，由 main.py 处理 clone
        config.gitlab_url = args.gitlab_url
        config.old_ref = args.old or ""
        config.new_ref = args.new or ""
    else:
        # 默认进入交互式模式
        from .interactive import run_interactive
        return run_interactive()

    return config
