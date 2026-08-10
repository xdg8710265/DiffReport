"""差异渲染模块：调用 diff2html CLI 生成 HTML，支持按文件拆分（懒加载）"""

import subprocess
import re
import sys
import tempfile
import os
from pathlib import Path
from typing import Dict, Tuple


class DiffRenderer:
    """生成自包含的 diff HTML 片段，支持按文件拆分用于懒加载"""

    @staticmethod
    def render_html(diff_text: str, diff_style: str = "side-by-side") -> str:
        """
        调用 diff2html CLI 生成完整 HTML（去除 CDN 外链和外围标签），
        返回可嵌入模板的自包含 HTML 片段。
        """
        if not diff_text.strip():
            return ""

        full_html = DiffRenderer._run_diff2html(diff_text, diff_style)
        if not full_html:
            return DiffRenderer._fallback_html(diff_text)

        return DiffRenderer._clean_html(full_html)

    @staticmethod
    def render_lazy(
        diff_text: str, diff_style: str = "side-by-side"
    ) -> Tuple[str, Dict[str, str]]:
        """
        生成用于懒加载的 diff 数据。

        Returns:
            (styles_and_scripts, {filepath: file_diff_html})
            - styles_and_scripts: 所有 <style> + <script> 标签（全局共享）
            - file_diffs: 按文件路径索引的 diff HTML 片段
        """
        if not diff_text.strip():
            return "", {}

        full_html = DiffRenderer._run_diff2html(diff_text, diff_style)
        if not full_html:
            return "", {}

        cleaned = DiffRenderer._clean_html(full_html)

        # 提取全局 <style> 和 <script>（所有文件共享）
        global_parts = []
        file_diffs = {}

        # 提取 style 块
        style_pattern = re.compile(r"(<style[^>]*>.*?</style>)", re.DOTALL | re.IGNORECASE)
        for m in style_pattern.finditer(cleaned):
            global_parts.append(m.group(1))

        # 提取 script 块（diff2html UI JS）
        script_pattern = re.compile(r"(<script[^>]*>.*?</script>)", re.DOTALL | re.IGNORECASE)
        for m in script_pattern.finditer(cleaned):
            global_parts.append(m.group(1))

        styles_and_scripts = "\n".join(global_parts)

        # 提取每个文件的 d2h-file-wrapper（使用 div 深度计数，正确匹配嵌套结构）
        wrapper_start_pattern = re.compile(
            r'<div[^>]*id="d2h-\d+"[^>]*class="[^"]*d2h-file-wrapper[^"]*"[^>]*>',
            re.DOTALL,
        )
        for m in wrapper_start_pattern.finditer(cleaned):
            start_pos = m.start()
            # 从 wrapper 开始位置计算 div 嵌套深度
            depth = 1
            pos = m.end()
            # 扫描后续字符，计数 <div 和 </div> 直到 depth=0
            div_open = re.compile(r'<div\b', re.IGNORECASE)
            div_close = re.compile(r'</div>', re.IGNORECASE)
            while depth > 0 and pos < len(cleaned):
                # 查找下一个 <div 或 </div>
                next_open = div_open.search(cleaned, pos)
                next_close = div_close.search(cleaned, pos)
                
                if next_close is None:
                    break  # 没有更多闭合标签，不应该出现
                    
                # 确定哪个先出现
                if next_open and next_open.start() < next_close.start():
                    depth += 1
                    pos = next_open.end()
                else:
                    depth -= 1
                    pos = next_close.end()
            
            wrapper_html = cleaned[start_pos:pos]
            
            # 提取文件名
            name_match = re.search(r'class="d2h-file-name"[^>]*>([^<]+)<', wrapper_html)
            if name_match:
                filepath = name_match.group(1)
                file_diffs[filepath] = wrapper_html

        styles_and_scripts = "\n".join(global_parts)

        return styles_and_scripts, file_diffs

    @staticmethod
    def _run_diff2html(diff_text: str, diff_style: str) -> str:
        """调用 diff2html CLI，返回生成的原始 HTML"""
        tmp_dir = tempfile.mkdtemp(prefix="diff2html_")
        diff_file = os.path.join(tmp_dir, "input.diff")
        html_file = os.path.join(tmp_dir, "output.html")

        try:
            with open(diff_file, "w", encoding="utf-8") as f:
                f.write(diff_text)

            style_flag = "side" if diff_style == "side-by-side" else "line"
            cmd_parts = [
                "diff2html",
                "--style", style_flag,
                "--format", "html",
                "--input", "file",
                "--file", html_file,
                "--hc",
                "--matching", "lines",
                "--diffStyle", "word",
                "--",
                diff_file,
            ]

            if sys.platform == "win32":
                cmd = " ".join(f'"{p}"' if " " in p else p for p in cmd_parts)
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=True)
            else:
                result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=120)

            if result.returncode != 0:
                print(f"⚠️ diff2html 执行失败: {result.stderr[:300]}")
                return ""

            if not os.path.exists(html_file):
                print("⚠️ diff2html 未生成输出文件")
                return ""

            with open(html_file, "r", encoding="utf-8") as f:
                return f.read()

        except FileNotFoundError:
            print("⚠️ diff2html CLI 未安装，使用降级渲染")
            return ""
        except subprocess.TimeoutExpired:
            print("⚠️ diff2html 超时，使用降级渲染")
            return ""
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _clean_html(full_html: str) -> str:
        """清洗 diff2html 输出：去除 CDN 外链和外围标签"""
        # 去掉 CDN <link> 标签
        cleaned = re.sub(
            r'<link\s+[^>]*href\s*=\s*["\']https?://[^"\']*["\'][^>]*/?>',
            "",
            full_html,
            flags=re.IGNORECASE,
        )
        # 去掉外围标签
        cleaned = re.sub(r'<!doctype[^>]*>', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'</?html[^>]*>', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'</?head[^>]*>', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'</?body[^>]*>', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'<title[^>]*>.*?</title>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r'<meta[^>]*/?>', '', cleaned, flags=re.IGNORECASE)

        # 去掉 diff2html 的自动初始化脚本（会尝试找不存在的 #diff 元素导致报错）
        cleaned = re.sub(
            r'<script[^>]*>\s*document\.addEventListener\([^<]*?getElementById\([\'"]diff[\'"]\)[^<]*?</script>',
            '',
            cleaned,
            flags=re.DOTALL,
        )

        return cleaned.strip()

    @staticmethod
    def _fallback_html(diff_text: str) -> str:
        """纯文本降级渲染"""
        lines = diff_text.split("\n")
        html_parts = [
            '<style>',
            '.d2h-wrapper{font-family:Consolas,monospace;font-size:13px;line-height:1.5;}',
            '.d2h-file-header{background:#f7f7f7;padding:8px 16px;font-weight:600;border-bottom:1px solid #d8d8d8;}',
            '.d2h-ins{background:#e6ffed;}',
            '.d2h-ins .d2h-code-linenumber{background:#cdffd8;}',
            '.d2h-del{background:#ffeef0;}',
            '.d2h-del .d2h-code-linenumber{background:#ffdce0;}',
            '.d2h-cntx{background:#fff;}',
            '.d2h-code-linenumber{color:rgba(0,0,0,0.3);padding:0 8px;text-align:right;user-select:none;}',
            '.d2h-hunk-header{background:#f1f8ff;color:#0366d6;padding:4px 16px;}',
            '.d2h-diff-table{width:100%;border-collapse:collapse;}',
            '.d2h-file-wrapper{border:1px solid #ddd;margin-bottom:1em;border-radius:3px;}',
            '.d2h-code-line{padding:0 8px;white-space:pre-wrap;word-break:break-all;}',
            '</style>',
            '<div class="d2h-wrapper">',
        ]
        current_file = None
        for line in lines:
            if line.startswith("diff --git"):
                if current_file:
                    html_parts.append("</tbody></table></div>")
                parts = line.split(" ")
                current_file = parts[3][2:] if len(parts) >= 4 else line[12:]
                html_parts.append(
                    f'<div class="d2h-file-wrapper">'
                    f'<div class="d2h-file-header">{_esc(current_file)}</div>'
                    f'<table class="d2h-diff-table"><tbody>'
                )
            elif line.startswith("---") or line.startswith("+++"):
                continue
            elif line.startswith("@@"):
                html_parts.append(
                    f'<tr><td colspan="3" class="d2h-hunk-header">{_esc(line)}</td></tr>'
                )
            elif current_file:
                if line.startswith("+"):
                    cls, prefix = "d2h-ins", "+"
                elif line.startswith("-"):
                    cls, prefix = "d2h-del", "-"
                else:
                    cls, prefix = "d2h-cntx", " "
                content = _esc(line[1:]) if len(line) > 1 else ""
                html_parts.append(
                    f'<tr class="{cls}">'
                    f'<td class="d2h-code-linenumber">{prefix}</td>'
                    f'<td class="d2h-code-line">{content}</td>'
                    f'</tr>'
                )
        if current_file:
            html_parts.append("</tbody></table></div>")
        html_parts.append("</div>")
        return "\n".join(html_parts)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
