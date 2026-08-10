"""报告构建模块：将 diff HTML + 影响分析组装成最终报告"""

import json
import re
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .git_ops import DiffStats
from .impact_analyzer import ImpactResult


class ReportBuilder:
    """使用 Jinja2 模板生成最终 HTML 报告（支持懒加载）"""

    def __init__(self):
        template_dir = Path(__file__).parent.parent / "templates"
        self._env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(["html"]),
        )
        self._template = self._env.get_template("report.html")

    def build(
        self,
        repo_name: str,
        old_ref: str,
        new_ref: str,
        old_info: dict,
        new_info: dict,
        diff_stats: DiffStats,
        diff_html: str,
        impact_result: Optional[ImpactResult],
        file_diffs: Optional[Dict[str, str]] = None,
        filtered_files: Optional[list] = None,
        output_dir: str = "./reports",
        repo_path: str = "",
    ) -> str:
        """
        组装最终 HTML 报告

        Args:
            repo_name: 仓库名
            old_ref: 旧版本引用
            new_ref: 新版本引用
            old_info: 旧版本 commit 信息
            new_info: 新版本 commit 信息
            diff_stats: 原始 diff 统计
            diff_html: 全局样式和脚本（或完整 diff HTML）
            impact_result: jcci 影响分析结果（可选）
            file_diffs: 按文件路径索引的 diff HTML 片段（懒加载模式）
            filtered_files: 过滤后的文件列表（懒加载模式，用于显示）
            output_dir: 输出目录
        """
        # 准备模板上下文
        ctx = {
            "repo_name": repo_name,
            "repo_path": repo_path,
            "old_ref": old_ref,
            "new_ref": new_ref,
            "old_info": old_info,
            "new_info": new_info,
            "diff_stats": diff_stats,
            "diff_html": diff_html,
            "impact": impact_result,
            "net_change": diff_stats.insertions - diff_stats.deletions,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 懒加载模式
        if file_diffs is not None:
            # JSON 序列化文件 diffs（转为 JS 安全的字符串）
            file_diffs_json = json.dumps(file_diffs, ensure_ascii=False)
            ctx["file_diffs_json"] = file_diffs_json
            ctx["file_count"] = len(file_diffs)
            ctx["is_lazy"] = True

            # 使用过滤后的文件列表展示
            if filtered_files is not None:
                display_files = filtered_files
            else:
                display_files = list(file_diffs.keys())

            # 按父目录分组（倒数第二个路径名）
            grouped = {}  # parent_dir -> [files]
            for f in display_files:
                # 处理 Git 重命名标记 {old → new}
                clean = f
                if "{" in clean and "}" in clean:
                    clean = re.sub(r'\{[^}]* → ', '', clean)
                    clean = clean.replace('}', '')
                
                parts = clean.rsplit("/", 1)
                if len(parts) == 2:
                    parent = parts[0].rsplit("/", 1)[-1] if "/" in parts[0] else parts[0]
                else:
                    parent = ""  # 根目录文件
                grouped.setdefault(parent, []).append(f)

            # 排序：根目录 → 其他按字母
            root_files = grouped.pop("", [])
            sorted_groups = []
            if root_files:
                sorted_groups.append({"group": "📂 根目录", "files": sorted(root_files), "count": len(root_files)})
            for parent in sorted(grouped.keys()):
                sorted_groups.append({"group": f"📁 {parent}", "files": sorted(grouped[parent]), "count": len(grouped[parent])})

            ctx["file_groups"] = sorted_groups
            ctx["display_files"] = display_files  # 保留用于旧版兼容

            filtered_note = f"（已过滤 SQL 等非代码文件，原始 {diff_stats.files_changed} → {len(display_files)}）"
            ctx["filtered_note"] = filtered_note
        else:
            ctx["is_lazy"] = False
            ctx["file_count"] = 0
            ctx["display_files"] = diff_stats.changed_files
            ctx["filtered_note"] = ""

        # 渲染
        html = self._template.render(**ctx)

        # 写入文件
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"diff_report_{timestamp}.html"
        output_path = os.path.join(output_dir, filename)
        Path(output_path).write_text(html, encoding="utf-8")

        return output_path
