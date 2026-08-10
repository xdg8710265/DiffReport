# 代码对比扫描工具 (Code Diff Scanner)

基于 **diff2html + jcci** 的组合脚本工具，用于测试工程师对开发提测代码进行前后对比扫描，生成可视化 HTML 报告。

## 功能

- 🔍 **Git Diff 可视化** — 调用 diff2html 生成漂亮的侧栏对比 HTML 报告
- ⚠️ **Java 变更影响分析** — 集成 jcci 分析代码变更影响的下游类和方法
- 📊 **变更统计概览** — 变更文件数、新增/删除行数、净变化
- 🎯 **4 种输入模式** — 命令行 / 交互式 / 配置文件 / 自动 clone + 对比

## 快速开始

### 1. 安装依赖

```bash
# Python 依赖
pip install -r requirements.txt

# diff2html CLI（用于生成 HTML 差异报告）
npm install -g diff2html-cli

# jcci（可选，用于 Java 影响分析）
pip install jcci
```

### 2. 使用方式

#### 模式 1: 命令行参数

```bash
# 本地仓库 + commit SHA
python main.py --repo /path/to/project --old abc1234 --new def5678

# GitLab URL + 分支对比
python main.py --gitlab-url https://gitlab.com/group/project.git --old main --new develop

# 带 Java 影响分析
python main.py --repo /path/to/project --old v1.0 --new v2.0 --impact
```

#### 模式 2: 交互式问答

```bash
python main.py --interactive
```

按提示依次输入仓库路径、旧版本、新版本等信息。

#### 模式 3: 配置文件

```bash
# 生成配置文件模板
python main.py --gen-config my-scan.yaml

# 编辑配置后运行
python main.py --config my-scan.yaml
```

#### 模式 4: 自动 clone + 对比

```bash
python main.py --gitlab-url https://gitlab.com/group/project.git --auto
```

自动克隆仓库，然后交互式选择两个 ref 进行对比。

### 3. 查看报告

```bash
# 报告默认生成在 ./reports/ 目录
# 直接在浏览器中打开 HTML 文件即可
```

## 项目结构

```
code-diff-scanner/
├── main.py                    # 主入口
├── scanner/
│   ├── git_ops.py             # Git 操作（clone/fetch/diff）
│   ├── diff_render.py         # diff2html 渲染
│   ├── impact_analyzer.py     # jcci 影响分析
│   ├── report_builder.py      # Jinja2 报告组装
│   ├── cli_input.py           # 命令行参数解析
│   ├── interactive.py         # 交互式问答
│   └── config_loader.py       # YAML 配置加载
├── templates/
│   └── report.html            # 报告 HTML 模板
├── config.example.yaml        # 配置文件示例
├── requirements.txt
└── README.md
```

## 输出报告内容

生成的 HTML 报告包含以下模块：

1. **📊 变更概览** — 变更文件数、新增/删除行、净变化统计
2. **📁 变更文件列表** — 所有变更文件的清单
3. **🔍 详细代码差异** — diff2html 侧栏对比视图（语法高亮）
4. **⚠️ Java 影响分析** — jcci 分析结果：变更类 → 影响的类/方法 → 回归测试建议

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--repo`, `-r` | 本地仓库路径 |
| `--gitlab-url`, `-g` | GitLab 仓库 URL |
| `--old` | 旧版本（commit SHA / 分支 / tag） |
| `--new` | 新版本（commit SHA / 分支 / tag） |
| `--interactive`, `-i` | 交互式问答模式 |
| `--config`, `-c` | YAML 配置文件路径 |
| `--auto` | 自动 clone + 交互式选择 ref |
| `--output`, `-o` | 报告输出目录（默认 ./reports） |
| `--impact` | 启用 Java 影响分析 |
| `--no-impact` | 禁用 Java 影响分析 |
| `--keep-temp` | 保留临时 clone 的仓库 |
| `--gen-config` | 生成配置文件模板 |

## License

MIT
