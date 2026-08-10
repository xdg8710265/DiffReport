"""变更影响分析模块：优先 jcci AST 分析，失败则降级为文件分类分析"""

import json
import os
import re
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ImpactResult:
    """影响分析结果"""
    success: bool = False
    error_message: str = ""
    changed_classes: list = field(default_factory=list)
    affected_classes: list = field(default_factory=list)
    affected_methods: dict = field(default_factory=dict)
    raw_output: str = ""
    # 降级分析字段
    fallback: bool = False
    java_files_by_category: dict = field(default_factory=dict)
    test_suggestions: list = field(default_factory=list)


class ImpactAnalyzer:
    """Java 代码变更影响分析器"""

    @staticmethod
    def analyze_branches(
        repo_path: str,
        old_branch: str,
        new_branch: str,
    ) -> ImpactResult:
        """
        分析两个分支之间的 Java 代码变更影响。

        优先使用 jcci AST 分析，失败时降级为基于文件路径的启发式分析。
        """
        repo = Path(repo_path)

        # 尝试 jcci
        result = ImpactAnalyzer._try_jcci(repo_path, old_branch, new_branch)
        if result.success and result.affected_classes:
            return result

        # jcci 失败或结果为空，降级为启发式分析
        print("   ⚠️ jcci 分析受限，启用降级分析模式...")
        fallback = ImpactAnalyzer._fallback_analysis(str(repo), old_branch, new_branch)
        fallback.success = True
        fallback.fallback = True
        return fallback

    @staticmethod
    def _try_jcci(repo_path: str, old_branch: str, new_branch: str) -> ImpactResult:
        """尝试使用 jcci 进行 AST 级别分析"""
        result = ImpactResult()

        try:
            import jcci
            from jcci import jcci as jcci_module
        except ImportError:
            result.error_message = "jcci 未安装"
            return result

        repo = Path(repo_path)
        if not (repo / ".git").exists():
            result.error_message = "不是有效的 Git 仓库"
            return result

        try:
            from git import Repo
            git_repo = Repo(str(repo))
            project_git_url = git_repo.remotes.origin.url
        except Exception:
            result.error_message = "无法获取 Git remote URL"
            return result

        # 切换到临时目录
        original_cwd = os.getcwd()
        tmp_workdir = tempfile.mkdtemp(prefix="jcci_")
        os.chdir(tmp_workdir)

        try:
            logging.getLogger().setLevel(logging.WARNING)

            print(f"   jcci 分析中: {old_branch} → {new_branch}")

            # 调用 jcci analyze_branches
            jcci_module.analyze_branches(
                project_git_url=project_git_url,
                branch_name_first=old_branch,
                branch_name_second=new_branch,
                request_user="diff-scanner",
            )

            # 查找 .cci 输出文件
            project_name = project_git_url.split("/")[-1].split(".git")[0]
            cci_files = list(Path(tmp_workdir).rglob("*.cci"))

            if cci_files:
                with open(str(cci_files[0]), "r", encoding="utf-8") as f:
                    cci_data = json.load(f)
                parsed = ImpactAnalyzer._parse_cci_flare(cci_data)
                result.success = True
                result.changed_classes = parsed.get("changed_classes", [])
                result.affected_classes = parsed.get("affected_classes", [])
                result.affected_methods = parsed.get("affected_methods", {})
                result.raw_output = json.dumps(cci_data, indent=2, ensure_ascii=False)[:5000]
            else:
                result.error_message = "jcci 未生成 .cci 输出文件"

        except Exception as e:
            result.error_message = f"jcci 异常: {str(e)[:300]}"
            import traceback
            result.raw_output = traceback.format_exc()[:2000]
        finally:
            os.chdir(original_cwd)

        return result

    @staticmethod
    def _fallback_analysis(repo_path: str, old_branch: str, new_branch: str) -> ImpactResult:
        """
        降级分析：基于文件路径和命名规范做启发式影响判断

        不依赖 jcci，直接在 git diff 基础上做智能分类。
        """
        result = ImpactResult()

        try:
            from git import Repo
            repo = Repo(repo_path)
            changed = repo.git.diff(
                f"{old_branch}...{new_branch}", name_only=True, diff_filter="AM"
            )
            changed_files = [
                f for f in changed.strip().split("\n") if f and f.endswith(".java")
            ]
        except Exception as e:
            result.error_message = f"获取变更文件失败: {e}"
            return result

        if not changed_files:
            result.error_message = "没有 Java 文件变更"
            return result

        # 按目录/包名分类
        categories = {
            "controller": [],
            "service": [],
            "service_impl": [],
            "mapper": [],
            "dao": [],
            "repository": [],
            "entity": [],
            "dto": [],
            "vo": [],
            "util": [],
            "config": [],
            "enums": [],
            "constant": [],
            "other": [],
        }

        for f in changed_files:
            f_lower = f.lower()
            categorized = False
            for cat in categories:
                if cat == "other":
                    continue
                if f"/{cat}/" in f_lower or f"/{cat}s/" in f_lower:
                    # service_impl 特殊处理
                    if cat == "service" and ("/impl/" in f_lower or f_lower.endswith("impl.java")):
                        categories["service_impl"].append(f)
                    else:
                        categories[cat].append(f)
                    categorized = True
                    break
            if not categorized:
                # 按文件名后缀匹配
                for cat in ["controller", "service", "mapper", "dao", "entity", "dto", "vo", "util", "config"]:
                    if f_lower.endswith(f"{cat}.java"):
                        categories[cat].append(f)
                        categorized = True
                        break
            if not categorized:
                # Spring 注解扫描：简单判断
                categories["other"].append(f)

        # 移除空分类
        result.java_files_by_category = {
            k: v for k, v in categories.items() if v
        }
        result.changed_classes = changed_files

        # 生成测试建议
        suggestions = ImpactAnalyzer._generate_test_suggestions(categories)
        result.test_suggestions = suggestions

        # 提取受影响的关键模块
        affected = set()
        affected_methods = {}

        # Controller 层的变更 -> 影响集成测试
        for f in categories.get("controller", []):
            module = ImpactAnalyzer._extract_module(f)
            affected.add(f"{module} (API/集成测试)")
            affected_methods[f] = ["所有 @RequestMapping 方法需回归"]

        # Service 层的变更 -> 影响单元测试 + 上层调用者
        for f in categories.get("service", []) + categories.get("service_impl", []):
            module = ImpactAnalyzer._extract_module(f)
            affected.add(f"{module} (单元测试)")
            # 尝试关联 controller
            service_name = os.path.basename(f).replace("Impl.java", "").replace(".java", "")
            for cf in categories.get("controller", []):
                if service_name.lower().replace("service", "") in cf.lower():
                    affected.add(f"{ImpactAnalyzer._extract_module(cf)} (集成测试)")
                    affected_methods.setdefault(cf, []).append(f"调用 {service_name} 的接口")

        # DAO/Mapper 变更 -> 影响数据层测试
        for f in categories.get("mapper", []) + categories.get("dao", []) + categories.get("repository", []):
            module = ImpactAnalyzer._extract_module(f)
            affected.add(f"{module} (数据层测试)")

        # Entity/DTO/VO 变更 -> 影响序列化/反序列化测试
        for f in categories.get("entity", []) + categories.get("dto", []) + categories.get("vo", []):
            module = ImpactAnalyzer._extract_module(f)
            affected.add(f"{module} (序列化测试)")

        result.affected_classes = sorted(list(affected))
        result.affected_methods = affected_methods

        return result

    @staticmethod
    def _extract_module(file_path: str) -> str:
        """从文件路径提取模块名"""
        parts = file_path.replace("\\", "/").split("/")
        # 找到 com/xxx 之后的模块路径
        for i, part in enumerate(parts):
            if part in ("com", "cn", "org"):
                if i + 1 < len(parts):
                    return "/".join(parts[i + 1:-1])
        # 回退：取最后两级目录
        if len(parts) >= 3:
            return "/".join(parts[-3:-1])
        return "/".join(parts[:-1])

    @staticmethod
    def _generate_test_suggestions(categories: dict) -> list:
        """根据文件分类生成测试建议"""
        suggestions = []

        if categories.get("controller"):
            suggestions.append(
                f"🔴 API 集成测试: {len(categories['controller'])} 个 Controller 变更，需全量回归接口测试"
            )

        if categories.get("service") or categories.get("service_impl"):
            count = len(categories.get("service", [])) + len(categories.get("service_impl", []))
            suggestions.append(
                f"🟡 Service 单元测试: {count} 个 Service 变更，关注业务逻辑和事务边界"
            )

        if categories.get("mapper") or categories.get("dao") or categories.get("repository"):
            count = (
                len(categories.get("mapper", []))
                + len(categories.get("dao", []))
                + len(categories.get("repository", []))
            )
            suggestions.append(
                f"🟡 数据层测试: {count} 个 DAO/Mapper 变更，关注 SQL 正确性和数据一致性"
            )

        if categories.get("entity") or categories.get("dto") or categories.get("vo"):
            count = (
                len(categories.get("entity", []))
                + len(categories.get("dto", []))
                + len(categories.get("vo", []))
            )
            suggestions.append(
                f"🟢 数据传输对象: {count} 个 Entity/DTO/VO 变更，关注序列化和字段映射"
            )

        if categories.get("util"):
            suggestions.append(
                f"🟡 工具类变更: {len(categories['util'])} 个，影响范围可能较广，建议全量回归"
            )

        if categories.get("config"):
            suggestions.append(
                f"🔴 配置变更: {len(categories['config'])} 个，可能影响应用启动和行为，需重点验证"
            )

        if categories.get("enums") or categories.get("constant"):
            count = len(categories.get("enums", [])) + len(categories.get("constant", []))
            suggestions.append(
                f"🟢 枚举/常量变更: {count} 个，关注所有引用处的兼容性"
            )

        return suggestions

    @staticmethod
    def _parse_cci_flare(flare_data: dict) -> dict:
        """解析 jcci 输出的 flare 格式 JSON"""
        parsed = {
            "changed_classes": [],
            "affected_classes": [],
            "affected_methods": {},
        }

        children = flare_data.get("children", [])
        for child in children:
            name = child.get("name", "")
            if name == "Impact_Apis":
                continue

            if name not in parsed["changed_classes"]:
                parsed["changed_classes"].append(name)

            for method_node in child.get("children", []):
                for impact_node in method_node.get("children", []):
                    impact_name = impact_node.get("name", "")
                    if impact_name.startswith("impacted."):
                        impact_target = impact_name[len("impacted."):]
                        parts = impact_target.rsplit(".", 1)
                        if len(parts) == 2:
                            impacted_class, impacted_method = parts
                        else:
                            impacted_class = impact_target
                            impacted_method = "(字段/声明)"

                        if impacted_class not in parsed["affected_classes"]:
                            parsed["affected_classes"].append(impacted_class)
                        parsed["affected_methods"].setdefault(impacted_class, [])
                        if impacted_method not in parsed["affected_methods"][impacted_class]:
                            parsed["affected_methods"][impacted_class].append(impacted_method)

        return parsed
