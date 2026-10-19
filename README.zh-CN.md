# WebAgent

从 AIPentest 抽取的轻量 Web Agent。输入任务目标与网址，由模型自主观察页面、选择操作、
检查结果并调整计划。系统提供执行与审计能力，保留 **less is more** 的设计思想。

项目包含语义页面快照、浏览器会话隔离、七个浏览器原语、通用 AgentLoop、工作区、可选
Docker Python 沙箱和执行审计。Python 直接连接浏览器 MCP 服务，可以独立运行。

## 快速启动

需要 Python 3.11+、Node.js 20+ 和 Chromium；脚本执行另需 Docker。

```bash
git clone https://github.com/aoao921/WebAgent.git
cd WebAgent
python -m venv .venv
```

Windows PowerShell 激活：`.\.venv\Scripts\Activate.ps1`；Linux/macOS 激活：
`source .venv/bin/activate`。

第一个终端启动浏览器：

```bash
python -m pip install -e '.[dev]'
cd browser
npm ci
npx playwright install chromium
npm start
```

第二个终端从仓库根目录运行：

```bash
webagent demo
```

演示使用真实 Chromium 访问临时本地网页，填写姓名、点击保存并检查结果。模型是脚本
模拟的，不消耗 API Key，用于检查安装与执行链路，不代表真实模型的任务能力。

## 接入模型

把 `.env.example` 复制为 `.env`，填写自己的 `LLM_API_KEY`、`LLM_BASE_URL` 和
`LLM_MODEL`。模型需要支持 OpenAI 兼容的 Chat Completions 工具调用。

```bash
webagent run --url https://example.com --goal "阅读网页并概括页面内容"
```

默认只允许起始网址的 origin。确需跨站时，可通过 `--allow-origin` 增加明确授权的
来源。浏览器会在网络请求发出前检查范围；页面依赖的第三方资源也可能被阻断。
来源限制不等于页面操作权限管理，同源页面上的操作仍由用户目标与模型决策决定。

## 开启脚本执行

```bash
docker pull python:3.12-slim
webagent run --url https://example.com --goal "提取页面数据并编写脚本分析" --coding
```

默认提供工作区读写；`--coding` 才暴露 `python_run`。脚本运行于无网络 Docker，
通过工作区处理数据。目标网站交互仍使用浏览器工具。

## 审计与验证

每次执行在 `var/<run_id>/` 生成操作记录、私有对话、最终结果和文件产物。
`events.jsonl` 只展示事件元数据；`state.sqlite3`、`result.json` 和工作区可能包含
任务输入或页面数据，属于私有运行数据，请勿上传。操作与文件通过 ID、哈希关联。

```bash
webagent inspect RUN_ID
python -m pytest -q
python -m ruff check src tests
```

浏览器服务默认只监听本机，无平台认证。登录状态只在当前浏览器会话中存在。
完整用法、跨来源配置与测试命令见 [英文 README](README.md)；实现与验证状态见
[技术规范](TECHNICAL_SPECIFICATION.md)。许可证为 MIT。
