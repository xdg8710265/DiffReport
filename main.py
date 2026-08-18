#!/usr/bin/env python3
"""
代码对比扫描工具 — 主入口

支持 4 种输入模式:
  1. 命令行参数:  python main.py --repo /path/to/project --old abc --new def
  2. 交互式问答:  python main.py --interactive
  3. 配置文件:    python main.py --config scan.yaml
  4. 自动 clone:  python main.py --gitlab-url <URL> --auto
"""

import os
import sys

# 确保当前目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner.cli_input import parse_args, ScanConfig
from scanner.interactive import run_interactive
from scanner.config_loader import load_config, generate_example_config
from scanner.git_ops import GitOperator
from scanner.diff_render import DiffRenderer
from scanner.impact_analyzer import ImpactAnalyzer
from scanner.report_builder import ReportBuilder


def main():
    """主流程"""
    config = _resolve_config()
    output_path = run_scan(
        repo_path=config.repo_path,
        old_ref=config.old_ref,
        new_ref=config.new_ref,
        impact=config.impact_analysis,
        output_dir=config.output_dir,
        keep_temp=config.keep_temp,
        exclude=config.exclude,
    )
    print(f"\n✅ 报告已生成: {output_path}")
    print(f"   在浏览器中打开: file:///{os.path.abspath(output_path).replace(os.sep, '/')}")
    print("\n🎉 扫描完成！")


def run_scan(repo_path: str, old_ref: str, new_ref: str,
             impact: bool = True, output_dir: str = "./reports",
             keep_temp: bool = False, exclude: list = None,
             context_lines: int = 10) -> str:
    """执行代码对比扫描，返回生成的报告文件路径。可供命令行或 HTTP 服务调用。
    
    Args:
        context_lines: 上下文行数，默认 10（快速），-1 表示全部上下文（慢但完整）
    """
    print("🚀 代码对比扫描工具 v1.0")
    print("─" * 40)

    # 验证必填参数
    if not repo_path or not old_ref or not new_ref:
        raise ValueError("缺少必要参数: repo_path, old_ref, new_ref")

    print(f"\n📋 扫描配置:")
    print(f"   仓库:     {repo_path}")
    print(f"   旧版本:   {old_ref}")
    print(f"   新版本:   {new_ref}")
    print(f"   输出目录: {output_dir}")
    print(f"   影响分析: {'开启' if impact else '关闭'}")
    print(f"   上下文:   {'全部' if context_lines < 0 else f'{context_lines} 行'}")
    print()

    # ── 步骤 1: 定位仓库 ──
    git_op = GitOperator()
    try:
        resolved_path = git_op.resolve_repo(repo_path)
    except Exception as e:
        raise RuntimeError(f"仓库定位失败: {e}")

    # ── 步骤 2: fetch 最新 ──
    git_op.fetch_all()

    # ── 步骤 3: diff 统计 ──
    print("\n📊 正在计算变更统计...")
    try:
        diff_stats = git_op.get_diff_stat(old_ref, new_ref)
    except Exception as e:
        raise RuntimeError(f"获取 diff 统计失败: {e}")

    # 排除 SQL 等非代码文件
    if exclude is not None:
        exclude_exts = set(exclude)
    else:
        exclude_exts = {".sql"}
    if exclude_exts:
        print(f"   排除文件类型: {exclude_exts}")

    if diff_stats.files_changed == 0:
        print("ℹ️ 两版本之间没有代码变更")
    else:
        print(f"   变更文件: {diff_stats.files_changed} 个")
        print(f"   新增行:   +{diff_stats.insertions}")
        print(f"   删除行:   -{diff_stats.deletions}")
        print(f"   净变化:   {'+' if diff_stats.insertions >= diff_stats.deletions else ''}"
              f"{diff_stats.insertions - diff_stats.deletions}")

    # ── 步骤 4: diff 可视化（过滤 + 懒加载） ──
    print("\n🎨 正在生成差异可视化...")
    diff_text = git_op.get_diff_text(old_ref, new_ref, exclude_exts=exclude_exts,
                                     context_lines=context_lines)

    diff_styles = ""
    file_diffs = {}
    filtered_files = []

    if diff_text.strip():
        renderer = DiffRenderer()
        diff_styles, file_diffs = renderer.render_lazy(diff_text, diff_style="side-by-side")
        filtered_files = list(file_diffs.keys())
        print(f"   渲染文件: {len(filtered_files)} 个（已过滤 SQL 等）")
    else:
        print("   (无差异内容)")

    # ── 步骤 5: 影响分析 (可选) ──
    impact_result = None
    if impact:
        print("\n🔍 正在进行 Java 变更影响分析...")
        impact_result = ImpactAnalyzer.analyze_branches(
            str(resolved_path),
            old_ref,
            new_ref,
        )
        if impact_result.success:
            affected_count = len(impact_result.affected_classes)
            print(f"   ✅ 影响分析完成: {affected_count} 个下游类受影响")
        else:
            print(f"   ⚠️ {impact_result.error_message[:200]}")
    else:
        print("\n⏭️ 跳过影响分析（使用 --impact 启用）")

    # ── 步骤 6: 生成报告 ──
    print("\n📝 正在生成报告...")
    builder = ReportBuilder()
    old_info = git_op.get_commit_info(old_ref)
    new_info = git_op.get_commit_info(new_ref)
    repo_name = git_op.get_repo_name()

    output_path = builder.build(
        repo_name=repo_name,
        repo_path=repo_path,
        old_ref=old_ref,
        new_ref=new_ref,
        old_info=old_info,
        new_info=new_info,
        diff_stats=diff_stats,
        diff_html=diff_styles,
        file_diffs=file_diffs,
        filtered_files=filtered_files,
        impact_result=impact_result,
        output_dir=output_dir,
    )

    # ── 步骤 7: 清理旧报告（保留最新 10 条） ──
    _cleanup_old_reports(output_dir, keep=10)

    # ── 步骤 8: 清理临时仓库 ──
    if not keep_temp:
        git_op.cleanup()
    else:
        if git_op._temp_dir:
            print(f"📁 临时仓库已保留: {git_op._temp_dir}")

    return output_path


def _cleanup_old_reports(output_dir: str, keep: int = 10):
    """保留最新 N 份报告，删除更早的"""
    import glob
    pattern = os.path.join(output_dir, "diff_report_*.html")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if len(files) <= keep:
        return
    for old_file in files[keep:]:
        try:
            os.remove(old_file)
            print(f"🗑️  已清理旧报告: {os.path.basename(old_file)}")
        except OSError as e:
            print(f"⚠️ 清理失败: {old_file} — {e}")


def _resolve_config() -> ScanConfig:
    """解析输入配置，处理 4 种模式"""
    # 先尝试解析命令行参数
    args = sys.argv[1:]

    # --help
    if "--help" in args or "-h" in args:
        from scanner.cli_input import parse_args
        return parse_args()

    # --config
    for i, arg in enumerate(args):
        if arg in ("--config", "-c") and i + 1 < len(args):
            try:
                return load_config(args[i + 1])
            except Exception as e:
                print(f"❌ 配置文件加载失败: {e}")
                sys.exit(1)

    # --interactive
    if "--interactive" in args or "-i" in args:
        return run_interactive()

    # --gen-config
    if "--gen-config" in args:
        output = "./config.example.yaml"
        for i, arg in enumerate(args):
            if arg == "--gen-config" and i + 1 < len(args) and not args[i + 1].startswith("-"):
                output = args[i + 1]
        generate_example_config(output)
        print("✅ 配置文件示例已生成")
        sys.exit(0)

    # 命令行参数模式 / --auto 模式
    from scanner.cli_input import parse_args
    config = parse_args()

    # --auto: GitLab URL clone 后没有 old/new 则进入交互式
    if "--auto" in args and config.gitlab_url:
        if not config.old_ref or not config.new_ref:
            extra = run_interactive()
            config.old_ref = extra.old_ref
            config.new_ref = extra.new_ref
            config.output_dir = extra.output_dir
            config.impact_analysis = extra.impact_analysis

    # 如果还是缺参数，降级到交互式
    if not config.repo_path or not config.old_ref or not config.new_ref:
        print("ℹ️ 检测到参数不完整，切换到交互式模式\n")
        return run_interactive()

    return config


def _print_config_summary(config: ScanConfig):
    """打印配置摘要"""
    print(f"\n📋 扫描配置:")
    print(f"   仓库:     {config.repo_path}")
    print(f"   旧版本:   {config.old_ref}")
    print(f"   新版本:   {config.new_ref}")
    print(f"   输出目录: {config.output_dir}")
    print(f"   影响分析: {'开启' if config.impact_analysis else '关闭'}")
    print()


if __name__ == "__main__":
    main()
