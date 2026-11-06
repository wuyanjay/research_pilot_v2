# research_pilot_v2

参加“全国高校 Agent 网络创新挑战赛”赛题一的科研助手 Agent。当前版本：**0.1.0**。

Agent 通过 HTTP `POST /solve` 接收平台派发的八字段题目，按题型调用对应处理器，生成结构化答卷，并把正式结果写入本队工作区。派题记录、题目和材料均为只读；结果只写入本队目录。

## 当前能力

| 题型 | `function` | 状态 |
| --- | --- | --- |
| A 科研对话 | `Chat` | 已接入 |
| B 文献检索 | `Literature_Retrieval` | 已接入 |
| C 文献综述 | `Survey` | 已接入 |
| D 论文复现 | `Reproduction` | 占位实现 |
| E 研究 Idea | `Idea` | 占位实现 |

已实现的能力包括：核对 `dispatch.json` 与 `assigned/` 题目、校验题目 SHA-256、按题型处理、原子写入结果并回读、OpenTelemetry 追踪。

## 目录结构

```text
.
├── main.py                  # 启动入口，加载配置并启动 HTTP 服务
├── src/
│   ├── agent.py             # 业务 Agent：核对题目并调用对应 handler
│   ├── runtime.py           # FastAPI 路由、追踪与错误响应
│   ├── dispatcher/          # 读取并核对派题记录
│   ├── delivery/            # 正式答卷写入与回读
│   ├── handlers/            # A/B/C 处理器及提示词
│   ├── llm/                 # OpenAI-compatible 模型客户端
│   └── tools/               # PDF 解析、检索与摘要定位
├── tests/                   # 离线单元测试与 HTTP 集成测试
├── test_data/               # 本地测试论文与实验材料
├── test_output/             # 本地联调工作区（dispatch/assigned 为夹具）
├── docs/                    # 赛事规则、输入输出规范与说明文档
├── vendor/                  # 离线构建依赖（PyMuPDF wheel、OCR 语言文件）
├── scripts/                 # 容器 smoke 测试脚本
├── Dockerfile
├── compose.test.yaml
├── requirements.txt
└── requirements-dev.txt
```

`images/`、`.env`、`test_output/data/JTI/work/` 等运行产物和凭据不入库，见 `.gitignore`。

## 环境要求

- Python 3.11（与官方基础镜像一致）
- Docker（本地构建与联调）
- 赛事平台提供的模型网关地址、API Key 和模型名

Python 依赖见 `requirements.txt`；本地开发额外需要 `requirements-dev.txt`（包含 `httpx` 测试依赖）。`vendor/` 中已准备 PyMuPDF wheel 和 Tesseract 中英文语言文件，用于离线构建。

## 配置

通过进程环境变量或项目根目录的 `.env` 配置。官方平台会注入模型网关相关变量；本地调试时自行填写。

| 变量 | 说明 |
| --- | --- |
| `RESEARCH_RESULT_ROOT` | 工作区根目录，默认 `/output`；本地可指向 `test_output` 的绝对路径 |
| `RESEARCH_WORKSPACE_TEAM` / `TEAM_NAME` | 队伍名，默认 `JTI` |
| `DATA_PATH` | 论文与实验材料目录，默认 `/data` |
| `CHALLENGE_LLM_BASE_URL` / `PLATFORM_LLM_URL` | 模型网关地址 |
| `CHALLENGE_LLM_API_KEY` / `PLATFORM_API_KEY` | 模型网关 API Key |
| `CHALLENGE_LLM_MODEL` / `PLATFORM_LLM_MODEL` / `AGENT_MODEL_NAME` | 模型名 |
| `CHALLENGE_SESSION_ID` | 平台会话标识，可选；用于模型会话与追踪关联 |
| `AGENT_LLM_TIMEOUT_S` | 单次模型请求网络超时，默认 900 秒 |
| `AGENTHACK_FLUSH_ON_SOLVE` | 每次 `/solve` 后是否刷新追踪，默认 `true` |

`.env` 已被 `.gitignore` 排除，不要提交任何真实 Key。

本地 `.env` 示例（请替换为实际值）：

```dotenv
RESEARCH_RESULT_ROOT=/absolute/path/to/test_output
RESEARCH_WORKSPACE_TEAM=JTI
TEAM_NAME=JTI
DATA_PATH=/absolute/path/to/test_data
CHALLENGE_LLM_BASE_URL=https://example.invalid/v1
CHALLENGE_LLM_API_KEY=replace-me
CHALLENGE_LLM_MODEL=replace-me
AGENT_LLM_TIMEOUT_S=900
```

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python main.py
```

服务默认监听 `0.0.0.0:8080`。

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

单题请求（请求体必须与 `assigned/` 中的题目完全一致）：

```bash
curl -X POST http://127.0.0.1:8080/solve \
  -H 'Content-Type: application/json' \
  -H 'X-Research-Task-ID: local-1' \
  --data @test_output/data/JTI/assigned/B-01.json
```

正式结果会写入 `RESEARCH_RESULT_ROOT/data/<队伍名>/<case_id>.json`；C 类还会写入 `data/expert/<case_id>-综述-<队伍名>.md`。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试不访问外部模型服务；HTTP 集成测试使用内存传输和临时工作区。

## Docker 构建与联调

```bash
docker build --platform linux/amd64 -t research-pilot-v2:test .
docker compose -f compose.test.yaml run --rm research-pilot
```

`compose.test.yaml` 会把 `test_data/` 挂到 `/data`、`test_output/` 挂到 `/output`，并把 `dispatch.json` 与 `assigned/` 设为只读。

容器 smoke 测试（会调用真实模型，仅覆盖 A-01）：

```bash
python scripts/smoke_container.py
```

## 打包提交

按项目规则命名：`JTI-pilot-<日期+随机字符>`。

```bash
docker save jti-pilot-20261106-58d863:latest -o images/JTI-pilot-<日期+随机字符>.tar
shasum -a 256 images/JTI-pilot-<日期+随机字符>.tar > images/JTI-pilot-<日期+随机字符>.tar.sha256
```

`images/` 已被 `.gitignore` 排除，镜像归档不进入仓库。

## 版本说明

**v0.1.0**：当前项目代码快照。包含 A/B/C 三类处理器、派题核对、正式交付、模型客户端、PDF 工具和离线测试。

已知限制：

- D/E 两类尚未实现；
- B 类按论文库全库检索；已加入快速索引、词法/标题预筛、字段修复和失败降级，150+ 篇下的正式平台耗时仍需复测；
- C-03 材料整理阶段存在模型请求超时风险。

## 安全边界

- 只读取本队 `dispatch.json`、`assigned/` 和允许的论文材料，不访问其他队伍或平台内部目录；
- 结果只写入本队目录和 `data/expert/` 下按规则命名的本队报告；
- `.env`、API Key、镜像归档和本地运行产物不入库。
