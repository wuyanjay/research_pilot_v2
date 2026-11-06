# 选手 Agent 输入输出格式规范

## 云端运行时限与错误反馈（2026-10-24 更新）

A/B 类每题 `/solve` 最长 540 秒，C/D/E 类每题最长 900 秒，计入本题模型调用、工具、自检及实验等待。D 类平台实验执行最长 840 秒，RPC 最长 870 秒，官方客户端等待最长 880 秒；这些是嵌套上限，不是可叠加的额外预算。Agent 主进程继续使用镜像原启动命令和工作目录，Python 3.9 仅用于平台 D 类实验。

平台保留到任务停止及网关请求排空后的真实抓包、审计和运行追踪。执行时限延长不免除 AgentRuntime 要求；D 类真实零模型调用本身不构成启动失败。裁判失败或证据采集异常显示“待核查/待补评，未出成绩”，不是选手 0 分。反馈会分别标明业务错误、HTTP 状态、协议字段与追踪异常，不公开其他队伍信息、内部路径或原始模型响应。

本文规定五类案例的输入和输出格式。每道题单独执行，题目中的 `case_id` 是本次回答的唯一标识；Agent 必须在输出 JSON 中原样返回该值。输入中的 `output_schema_ref` 指明采用哪一类输出格式，具体格式见下文。

正式每次派题包含五类，按 1-3-2-3-2 分配独立测试案例，总计 11 个 case。逐题输入及输出 Schema 保持不变。题目只读文件位于 `assigned/`，结果 JSON 位于队伍目录根部。目录结构如下：

```text
data/<战队名字>/dispatch.json
data/<战队名字>/assigned/<case_id>.json   # 输入题目
data/<战队名字>/<case_id>.json            # Agent 输出
```

例如：`data/选手4/C-100.json`。Agent 必须先读取 `dispatch.json`，只处理其中列出的11 个 case_id；不能自行抽题或把 assigned 题目复制成答案。文件名必须与 JSON 内的 `case_id` 完全一致；Survey 和 Idea 的完整 Markdown 文件另存到 `data/expert`，具体命名规则见对应章节。

平台先生成本队派题记录并提供11 个只读题目文件，Agent 通过文件读取处理 JSON 对象。输出的每一题结果 JSON 保存在 `data/<战队名字>/<case_id>.json`。

## 派题记录与文件定位

完整 11 题派题示例、字段说明、路径解析和单题答案示例见 [dispatch.json示例与读取说明.md](dispatch.json示例与读取说明.md)，可单独下载 [dispatch.example.json](示例/dispatch.example.json)。示例仅用于理解格式；正式运行读取平台提供的本队派题记录。若外部平台逐题直接传入 JSON，是否提供派题文件及输入传递方式以该平台接入协议为准，不能仅凭界面中的 Single 判断。

`dispatch.json` 中 `case_ids` 列出本轮11 个题号，`cases[].question_file` 给出相对工作区根目录的题目路径。各题后缀可能相同，也可能不同；Agent 不需要实现随机派题。

正式派题记录使用 `schema_version: 3`、`cases_per_category: {"A": 1, "B": 3, "C": 2, "D": 3, "E": 2}`、`rules_version: "research-assistant-objective-v3"`。`case_ids` 必须列出 11 个不同题号，各类数量为 1/3/2/3/2，`cases` 必须与其一一对应，并包含 `case_id`、`function`、`title`、`question_file`、`question_sha256`。题号允许 A-01 至 A-100 等当前范围，不能只支持两位后缀。A 随机、B–E 固定；D 类 cases 条目还包含 difficulty。以下为定位示意，省略部分不构成可提交的完整派题记录：

```text
dispatch_id: d-example-001
team: 你的队伍名
schema_version: 3
selection_mode: random_a_fixed_bcde
dispatch_policy_version: research-assistant-dispatch-v3
dispatch_policy_sha256: <服务器配置的 SHA-256>
difficulty_by_case: {"D-01": "easy", "D-02": "medium", "D-05": "hard"}
cases_per_category: {"A": 1, "B": 3, "C": 2, "D": 3, "E": 2}
rules_version: research-assistant-objective-v3
case_ids: A-01, B-01, B-02, B-03, C-01, C-03, D-01, D-02, D-05, E-01, E-02
cases: 每个题号一个对象，question_file 为 data/你的队伍名/assigned/<case_id>.json
```

旧版 `cases_per_category: 1` 或未提供该字段的五题记录仅供兼容调试，不能生成新版完整自动榜分。Agent 按记录逐题读取，不自行补题，也不重复提交相同题号作为新案例。

Agent 必须核对派题记录中的 `team`、11 个不重复题号（A–E 分别 1/3/2/3/2 个），以及题目文件内部的 `case_id`；题目只能从本队 `assigned/` 读取。按 `materials` 中完整文件名或相对路径定位论文，再读取正文、检索关键词和核验页码。中文、空格和符号均为文件名的一部分；遇到重名或路径不明确时反馈问题，不猜测替换文件。

## 文档能力与目录权限验收

以下为接入验收要求，平台在正式作答前检查。文档能力属于必备功能，不增加五类结果 JSON 的字段。

| 能力 | 验收方式 |
| --- | --- |
| 文档读取 | 正确读取派题 JSON、Markdown、文本和允许的 PDF 正文；不能只读取标题 |
| 文档定位 | 按 `case_id`、`question_file` 和完整文件名定位题目及材料；按关键词定位正文证据和章节或页码 |
| 文档写入 | 写出 11 个 UTF-8 结果 JSON、C/E Markdown 和实际 D 图表，按需创建授权子目录 |
| 回读校验 | 检查文件存在且非空、JSON 可解析、题号一致、Markdown 正文一致、图片路径有效 |
| 目录权限 | 派题记录、题目和材料只读；仅本队结果、工作文件、报告和图片可写；不得访问其他队伍或平台内部目录 |
| 失败处理 | 材料缺失、权限不足或写入失败时反馈可见原因；D 类用 `materials_missing` 或 `failed`，其余题型保持原有 Schema |

JSON 和 Markdown 使用 UTF-8；输出 `path` 使用相对工作区根目录的路径。禁止绝对路径、`..` 路径片段和符号链接越界。`data/expert/` 只允许访问按命名规则划分的本队 C/E 报告文件，不能遍历其他队伍报告；完整权限表见 [赛题一说明文档.md](赛题一说明文档.md)。

验收失败应修正后再提交镜像或接入版本。复现实验代码、缓存和中间产物保存在本队工作子目录（例如 data/<队伍名>/work/），不能修改原始题目或共享论文。

## 通用输入字段

每类题目的具体输入字段略有差异，通用字段含义如下：

| 字段                  | 含义                                                                   |
| ------------------- | -------------------------------------------------------------------- |
| `case_id`           | 当前题目的唯一编号。输出必须原样返回。                                                  |
| `function`          | 题目类型：`Chat`、`Literature_Retrieval`、`Survey`、`Reproduction` 或 `Idea`。 |
| `title`             | 当前案例标题。                                                              |
| `input`             | 本题的任务说明和具体问题。                                                        |
| `materials`         | 本题允许使用的论文、数据或其他材料；无材料时为空数组。                                          |
| `constraints`       | 本题必须遵守的额外要求；无额外要求时为空数组。                                              |
| `expected_output`   | 对提交内容的简要要求。                                                          |
| `output_schema_ref` | 输出格式类别标记；Agent 按本文对应类别组织输出。                                          |

输入对象不包含 `output_schema`、评分细则或参考答案。不要从其他题目推断本题的材料或结果。

## Chat 聊天类

**输入结构**

```json
{
  "case_id": "A-99",
  "function": "Chat",
  "title": "室内植物光照与生长：科研对话",
  "input": "我想研究不同光照条件对室内植物生长速度的影响。请解释光照强度、每日照射时长和生长速度之间可能有什么关系，并问我一个最关键的研究范围限定问题。",
  "materials": [],
  "constraints": [],
  "expected_output": "解释光照条件与植物生长速度的可能关系，并提出一个研究范围限定问题。",
  "output_schema_ref": "Chat"
}
```

| 字段                  | 含义                     |
| ------------------- | ---------------------- |
| `case_id`           | 题目编号；输出中必须保持为 `A-99`。  |
| `function`          | 固定为 `Chat`。            |
| `title`             | 对话任务的标题。               |
| `input`             | 用户提出的科研问题及回答要求。        |
| `materials`         | Chat 示例不依赖外部材料，因此为空数组。 |
| `constraints`       | 额外回答限制；此例没有额外限制。       |
| `expected_output`   | 概括回答应覆盖的内容。            |
| `output_schema_ref` | 表示使用 Chat 输出格式。        |

**输出结构**

```json
{
  "case_id": "题目中的编号",
  "answer": "对问题的回答",
  "clarifying_question": "一个用于限定研究范围的问题"
}
```

| 字段                    | 含义                        |
| --------------------- | ------------------------- |
| `case_id`             | 必填。必须与输入完全一致。             |
| `answer`              | 必填。非空字符串，直接回答当前问题。        |
| `clarifying_question` | 必填。非空字符串，提出一个最关键的澄清问题。 |

**输出示例**

```json
{
  "case_id": "A-99",
  "answer": "光照强度影响植物单位时间接收的能量，每日照射时长影响光合作用可持续的时间，两者共同影响生长速度。但不同植物的适宜范围不同，光照过强或过久也可能造成胁迫，因此需要结合植物种类和生长阶段分析。",
  "clarifying_question": "你准备研究哪一种植物，以及它处于哪个生长阶段？"
}
```

## Literature_Retrieval 文献检索类

**输入结构**

```json
{
  "case_id": "B-99",
  "function": "Literature_Retrieval",
  "title": "城市热岛：绿地与地表温度：文献检索",
  "input": "从本地论文中检索研究城市绿地与地表温度关系的论文，重点关注遥感数据、绿地覆盖率和不同城市区域的温度差异。排除只讨论植物分类、没有分析城市温度的论文。",
  "materials": ["本地论文文件夹内的 PDF"],
  "constraints": [
    "只返回本地实际存在的 PDF",
    "每篇论文提供唯一的完整 PDF 文件名和具体匹配理由",
    "按与检索需求的相关性排序"
  ],
  "expected_output": "返回相关论文列表，每篇包含完整文件名和匹配理由。",
  "output_schema_ref": "Literature_Retrieval"
}
``` 

| 字段                  | 含义                              |
| ------------------- | ------------------------------- |
| `case_id`           | 题目编号；输出中必须保持为 `B-99`。           |
| `function`          | 固定为 `Literature_Retrieval`。     |
| `title`             | 检索任务的标题。                        |
| `input`             | 检索主题、重点方向和排除条件。                 |
| `materials`         | 可检索的本地文献范围。                     |
| `constraints`       | 文件真实性、结果字段和排序要求。                |
| `expected_output`   | 概括检索结果的形式。                      |
| `output_schema_ref` | 表示使用 Literature_Retrieval 输出格式。 |

**输出结构**

```json
{
  "case_id": "题目中的编号",
  "papers": [
    {
      "filename": "完整且唯一的 PDF 文件名.pdf",
      "reason": "该论文与检索需求的具体匹配理由"
    }
  ]
}
```

| 字段                  | 含义                              |
| ------------------- | ------------------------------- |
| `case_id`           | 必填。必须与输入完全一致。                   |
| `papers`            | 论文结果数组，按相关性从高到低排列；没有相关论文时返回空数组。 |
| `papers[].filename` | 本地实际存在的完整 PDF 文件名。              |
| `papers[].reason`   | 说明该论文具体覆盖了哪些检索要求。               |

文件名仅字母大小写不同时，检索评分按同一篇论文匹配并去重；其他字符须与完整文件名一致。读取材料时仍使用论文库中的实际文件名，以兼容区分大小写的服务器文件系统。

**输出示例**

```json
{
  "case_id": "B-99",
  "papers": [
    {
      "filename": "Example_2024_Remote_Sensing_of_Urban_Green_Space_and_Surface_Temperature.pdf",
      "reason": "使用遥感数据分析城市绿地覆盖率与地表温度的关系，直接符合主要检索主题。"
    },
    {
      "filename": "Example_2022_Neighborhood_Vegetation_and_Urban_Heat.pdf",
      "reason": "比较不同城市区域的植被状况与热环境差异，覆盖了区域温度比较这一检索要求。"
    }
  ]
}
```

示例文件名仅用于展示格式，实际回答必须使用本地文献库中核实存在的完整文件名。

## Survey 文献综述类

**输入结构**

```json
{
  "case_id": "C-99",
  "function": "Survey",
  "title": "电动汽车电池回收：回收工艺与生命周期评估综述",
  "input": "阅读本题指定论文，撰写一篇至少 800 字的 Markdown 综述，比较湿法冶金、直接回收和梯次利用的适用条件、资源消耗与环境影响。正文事实和数据须用 [编号] 或 [编号, p.页码] 标注，结尾用“## 参考文献”列出至少 5 篇实际引用的指定论文及完整 PDF 文件名。",
  "materials": ["本地论文文件夹内指定的电池回收 PDF"],
  "constraints": [
    "只依据本题指定论文",
    "不得编造事实、数据、页码或参考文献",
    "将综述保存到指定路径"
  ],
  "expected_output": "返回 case_id、path 和完整 Markdown 综述。",
  "output_schema_ref": "Survey"
}
```

| 字段                  | 含义                    |
| ------------------- | --------------------- |
| `case_id`           | 题目编号；输出中必须保持为 `C-99`。 |
| `function`          | 固定为 `Survey`。         |
| `title`             | 综述任务的标题。              |
| `input`             | 综述主题、比较维度、长度和引用要求。    |
| `materials`         | 本题允许使用的指定论文。          |
| `constraints`       | 证据边界、真实性和保存要求。        |
| `expected_output`   | 要求返回结果索引和完整综述正文。      |
| `output_schema_ref` | 表示使用 Survey 输出格式。     |

**输出结构**

Agent 应提交一个 JSON 文件，并在 `data/expert` 指定的位置另存完整 Markdown 文件，命名规则为 `case_id-综述-队伍名字.md`。中间只使用固定的“综述”标识，不加入论文主题名。JSON 的 `markdown` 字段必须包含与 Markdown 文件相同的完整正文。

```json
{
  "case_id": "题目中的编号",
  "path": "data/expert/C-99-综述-雄起c++.md",
  "markdown": "完整 Markdown 综述正文"
}
```

| 字段         | 含义                                      |
| ---------- | --------------------------------------- |
| `case_id`  | 必填。必须与输入完全一致。                           |
| `path`     | Markdown 文件的相对路径，须符合本题指定路径。             |
| `markdown` | 完整综述正文；本例要求至少 800 字，正文事实和数据须可核验地引用指定论文。 |

**JSON 输出示例**

```json
{
  "case_id": "C-99",
  "path": "data/expert/C-99-综述-雄起c++.md",
  "markdown": "# 电动汽车电池回收综述\n\n## 引言\n\n电动汽车电池退役后仍可能含有可回收的金属材料，也可能在满足安全和性能要求时进入梯次利用环节。评估回收方案时，不能只比较材料回收率，还应考虑电池状态、拆解成本、能源消耗和后续用途。[1, p. 3]\n\n## 回收路线比较\n\n湿法冶金通常通过浸出和分离回收目标金属，适用于需要提取多种金属的场景，但其环境影响会受到试剂使用和废液处理方式影响。[2, p. 5] 直接回收则尝试保留正极材料结构，可能减少重新合成所需的处理步骤；其效果依赖电池化学体系、老化状态和分选质量。[3, p. 4] 梯次利用关注退役电池在储能等场景中的再使用，因此需要额外评估剩余容量、安全性和一致性。[4, p. 6]\n\n## 适用条件与评价边界\n\n不同论文可能采用不同的电池化学体系、回收边界和生命周期假设，因此回收率或碳排放结果不能脱离实验条件直接横向排名。综述应分别说明各研究的样本、工艺边界和评价指标，并区分实验结果与作者提出的潜在应用。[1, p. 8][5, p. 7]\n\n……此处继续完成至少 800 字的综述正文，并逐项比较指定论文的方法、证据和局限……\n\n## 参考文献\n\n[1] 作者等，论文标题，完整 PDF 文件名。\n[2] 作者等，论文标题，完整 PDF 文件名。\n[3] 作者等，论文标题，完整 PDF 文件名。\n[4] 作者等，论文标题，完整 PDF 文件名。\n[5] 作者等，论文标题，完整 PDF 文件名。"
}
```

**另存的 Markdown 文件示例**

保存为 `data/expert/C-99-综述-雄起c++.md`，内容必须与 JSON 中 `markdown` 字段一致。正式提交时，省略示例中的省略号并完成全文；示例页码和参考文献是格式占位，必须替换为真实可核验内容。

## Reproduction 论文复现类

**输入结构**

```json
{
  "case_id": "D-xx",
  "function": "Reproduction",
  "title": "光伏发电预测：论文复现",
  "input": "请复现指定论文中的一个光伏发电预测实验，自行核验代码、数据和运行环境并实际执行。核心指标恰好三项：name=MAE，unit=kW；name=RMSE，unit=kW；name=Inference Latency，unit=ms。使用本轮提供的数据与冻结脚本，记录依赖版本、切分和运行命令。返回 case_id、status、metrics、figures；每项 metrics 包含 name、unit、reference、observed、relative_error。observed 来自本次运行；未公开可核验对照值时 reference 和 relative_error 填 null。只有运行成功且结果可追溯时填写 completed，材料缺失用 materials_missing，实验失败用 failed，未知值用 null。图片保存到 data/figure/<队伍名>/，每项 figures 含相对 path、caption、x_label、y_label、legend；无实际图片时 figures=[]。无需读取额外核心指标说明文档。",
  "materials": [
    "指定论文 PDF"
  ],
  "constraints": [
    "必须在单台机器实际运行实验，不得从论文或参考配置抄取数值冒充实测结果",
    "指标名称、单位和提交要求按本题 input 填写，执行数据和脚本使用本轮允许材料",
    "固定 Python 3.9、随机种子 42、单进程和离线环境",
    "只返回三个核心指标和实际生成的图表",
    "图片统一放入 data/figure/<战队名字>/ 目录，figures[].path 使用相对于项目根目录的路径",
    "case_id 必须与当前题目一致"
  ],
  "expected_output": "返回 case_id、status、恰好三项 metrics 和 figures。",
  "output_schema_ref": "Reproduction"
}
```

| 字段                  | 含义                      |
| ------------------- | ----------------------- |
| `case_id`           | 题目编号；输出中必须保持为 `D-xx`。   |
| `function`          | 固定为 `Reproduction`。     |
| `title`             | 复现实验的标题。                |
| `input`             | 本题实验、三项指标的精确名称和单位、运行及提交要求，无需另读指标文档。 |
| `materials`         | 本题允许使用的论文、数据和脚本，不要求额外核心指标文件。 |
| `constraints`       | 实验真实性、指标和图片保存要求。        |
| `expected_output`   | 要求返回实验状态、指标和图表信息。       |
| `output_schema_ref` | 表示使用 Reproduction 输出格式。 |

**输出结构**

```json
{
  "case_id": "题目中的编号",
  "status": "completed",
  "metrics": [
    {
      "name": "指标名称",
      "unit": "单位",
      "reference": 0.0,
      "observed": 0.0,
      "relative_error": 0.0
    }
  ],
  "figures": [
    {
      "path": "data/figure/选手1/D-xx-图表文件.png",
      "caption": "图表说明及实验条件",
      "x_label": "横轴名称",
      "y_label": "纵轴名称及单位",
      "legend": "每条曲线或数据系列的含义"
    }
  ]
}
```

| 字段                         | 含义                                                                                                                             |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `case_id`                  | 必填。必须与输入完全一致。                                                                                                                  |
| `status`                   | 实验状态：`completed`、`failed` 或 `materials_missing`。只有实际运行且结果可追溯时使用 `completed`。                                                   |
| `metrics`                  | 必须恰好包含题目指定的三项指标。                                                                                                               |
| `metrics[].name`           | 使用本题 `input` 给出的精确名称。 |
| `metrics[].unit`           | 使用本题 `input` 给出的精确单位，不自行改写或转换尺度。 |
| `metrics[].reference`      | 论文或平台公开实验协议中的对照值；可核验时填 JSON 数字，材料未提供时填 `null`，不能读取内部评分配置。 |
| `metrics[].observed`       | 本次实际运行测得的值，JSON 数字；未运行或无法核验时填 `null`。 |
| `metrics[].relative_error` | 按本题约定计算的误差，JSON 数字；对照值或实测值不可核验时填 `null`。 |
| `figures`                  | 本次运行实际生成的图表列表。                                                                                                                 |
| `figures[].path`           | 相对于项目根目录的图片路径，必须位于 `data/figure/<战队名字>/`，例如 `data/figure/选手1/D-xx-result.png`。路径必须是相对路径，不能使用绝对路径或 `..` 跳出项目目录。图片文件名和扩展名没有固定要求。 |
| `figures[].caption`        | 图注，说明图表内容和必要的实验条件。                                                                                                             |
| `figures[].x_label`        | 横轴名称，必要时包含单位。                                                                                                                  |
| `figures[].y_label`        | 纵轴名称，必要时包含单位。                                                                                                                  |
| `figures[].legend`         | 图例说明，逐项说明每条曲线或数据系列代表的含义；单系列图也须给出清晰标识。                                                                                          |

**输出示例**

```json
{
  "case_id": "D-xx",
  "status": "completed",
  "metrics": [
    {
      "name": "MAE",
      "unit": "kW",
      "reference": 12.4,
      "observed": 12.8,
      "relative_error": 0.0323
    },
    {
      "name": "RMSE",
      "unit": "kW",
      "reference": 18.6,
      "observed": 19.1,
      "relative_error": 0.0269
    },
    {
      "name": "Inference Latency",
      "unit": "ms",
      "reference": 42.0,
      "observed": 44.1,
      "relative_error": 0.05
    }
  ],
  "figures": [
    {
      "path": "data/figure/选手1/D-xx_prediction_comparison.png",
      "caption": "本次运行中光伏发电实际值与预测值的对比",
      "x_label": "时间",
      "y_label": "发电功率（kW）",
      "legend": "实际值；预测值"
    }
  ]
}
```

指标数值仅演示 JSON 字段和数字类型，不能作为复现实测值。Agent 不得猜测或伪造实验结果、运行图表和文件路径。

当前 D 题已将三项指标名称、单位、运行和提交要求直接写入本队 `assigned/<case_id>.json` 的 `input`。参赛者无需另外阅读 D 类指标说明文档；上面的光伏示例只说明字段结构。状态为 `failed` 或 `materials_missing` 时仍保留题目指定的三个指标及名称、单位，未知值用 `null`；`figures` 只列实际生成的图，无图时用空数组。相关错误通过平台日志或反馈渠道报告，不添加顶层错误字段。此调整只消除额外指标文档依赖，实验所需的论文、数据和脚本仍须按 `materials` 定位和读取。

## Idea 研究灵感类

**输入结构**

```json
{
  "case_id": "E-99",
  "function": "Idea",
  "title": "城市树荫：热暴露与步行路线：研究灵感",
  "input": "阅读本题指定的城市气候与步行规划论文，提出一个降低夏季步行热暴露的路线规划研究方案。题目给定的模拟观察显示：最短路线经过的无遮荫路段较多，但本次观察没有测试任何替代规划策略。请区分模拟观察和论文结论，并说明研究问题、方法、实验、指标、可行性和创新点。",
  "materials": ["本题指定的城市气候与步行规划论文 PDF"],
  "constraints": [
    "只把题目给出的模拟观察作为待分析现象，不得写成论文实测结论",
    "区分已有方法与提出的新机制",
    "说明基线、数据、评价指标和失败边界",
    "将完整 Markdown 报告另存到 data/expert"
  ],
  "expected_output": "返回 case_id、path 和完整 Markdown 灵感 Idea 报告。",
  "output_schema_ref": "Idea"
}
```

| 字段                  | 含义                       |
| ------------------- | ------------------------ |
| `case_id`           | 题目编号；输出中必须保持为 `E-99`。    |
| `function`          | 固定为 `Idea`。              |
| `title`             | 研究灵感任务的标题。               |
| `input`             | 研究背景、模拟观察和方案要求。          |
| `materials`         | 本题指定的论文或数据材料。            |
| `constraints`       | 证据边界、报告内容和保存要求。          |
| `expected_output`   | 要求返回结果索引和完整 Markdown 报告。 |
| `output_schema_ref` | 表示使用 Idea 输出格式。          |

**输出结构**

Agent 应提交一个 JSON 文件，并在 `data/expert` 指定的位置另存完整 Markdown 灵感 Idea 报告。输出只保留 `case_id`、`path` 和 `markdown` 三个顶层字段；不得再输出结构化的 `research_question`、`technical_route`、`metrics` 等旧版字段。`markdown` 字段必须与另存文件的完整内容逐字一致。

```json
{
  "case_id": "题目中的编号",
  "path": "data/expert/E-99-灵感Idea报告-雄起c++.md",
  "markdown": "完整 Markdown 灵感 Idea 报告正文"
}
```

| 字段         | 含义                                                           |
| ---------- | ------------------------------------------------------------ |
| `case_id`  | 必填。必须与输入完全一致。                                                |
| `path`     | 必填。另存 Markdown 文件的相对路径，必须位于 `data/expert`，并以当前 `case_id` 开头。 |
| `markdown` | 必填。完整 Markdown 灵感 Idea 报告，至少 800 个字符，且必须与 `path` 指向的文件一致。    |

**报告内容要求**

报告应围绕题目给定论文和模拟观察组织，而不是把多个 JSON 字段简单拼接。正文至少覆盖以下内容，并使用清晰的 Markdown 标题：

- 研究问题与问题边界；
- 研究动机、已有方法局限和模拟观察的证据边界；
- 核心 Idea 与技术路线；
- 基线、数据集、实验设计、消融方案和量化指标；
- 多篇指定论文的互补关系；
- 可行性、实施成本、失败边界和风险；
- 相对已有工作的创新论证及可证伪方式。

正文中的论文事实、数字和归因必须能够追溯到本题指定的 PDF；模拟观察只能作为本题输入中的待验证现象，不能改写成论文实测结论。报告结尾必须使用 `## 参考文献` 标题，列出实际引用的指定论文及完整 PDF 文件名。

**输出示例**

```json
{
  "case_id": "E-99",
  "path": "data/expert/E-99-灵感Idea报告-雄起c++.md",
  "markdown": "# 城市树荫：热暴露与步行路线：灵感 Idea 报告\n\n## 研究问题与边界\n\n在夏季高温条件下，如何减少步行者热暴露，同时将路线绕行控制在可接受范围内？题目给出的观察只说明示例最短路线经过较多无遮荫路段，尚未测试替代策略，因此不能视为论文结论。\n\n## 核心 Idea 与技术路线\n\n……继续完成不少于 800 个字符的报告，并覆盖实验、证据边界、可行性、创新论证和参考文献……\n\n## 参考文献\n\n[1] 作者等，论文标题，完整 PDF 文件名。"
}
```

**另存的 Markdown 文件示例**

保存为 `data/expert/E-99-灵感Idea报告-雄起c++.md`，内容必须与 JSON 中的 `markdown` 字段一致。正式提交时，省略示例中的省略号并完成全文；示例引用和页码只是格式占位，必须替换为真实可核验内容。

## 提交检查

- 输入对象中的 `case_id` 必须在输出 JSON 中原样返回。
- 输出只包含当前类型约定的字段，不添加自然语言前缀、Markdown 代码围栏或额外顶层字段。
- 文献检索只提交可核实的本地完整 PDF 文件名。
- Survey 和 Idea JSON 中的 `markdown` 必须与 `path` 指向的另存 Markdown 文件内容保持一致。
- E 类 Idea 报告必须另存到 `data/expert`，并保留模拟观察与论文证据的边界，不得把未验证的改进效果写成已完成实验。
- 复现指标必须来自本次真实运行；图表路径必须指向实际存在的图片文件。

## 评测反馈与成绩字段

参赛者只写原有五类答案字段，不填写分数、专家分、Token 用量或服务器终止状态。正式本轮提交共 11 个结果 JSON，C/E 各两份 Markdown，D 图表按题目要求生成。

平台逐题保存 `status`、`score`、`raw_scores`、`scores`、`errors`，类别保存 `average_score`。客观自动榜分为 `automatic_leaderboard_score`（0～100），最终综合成绩为独立 `composite_score`。专家与 Token 复核合计占综合成绩 30%，自动成绩占 70%；复核权重留空或未完成时综合成绩为 null。初赛只使用自动榜分。

选手非法答案保留 invalid 状态并按本题规则计分，选手失败或超时保留终止状态；平台裁判、实验服务或证据链异常待核查/待补评，成绩为空，不判为选手 0 分。pending/running 尚未完成时不生成完整榜分。合法 D 类 failed/materials_missing 保持现有三项指标与 figures 字段，不增加错误或评分字段。

版本兼容：当前正式轮次为 v3 的 11 题。旧 25 题与旧五题记录只供历史兼容，不属于当前正式题量；Agent 以平台本轮提供的题目及派题记录为准，不自行补题。


## D类当前版本：layered-d-v3

本补充只涉及D-01/D-02/D-05，三题均需本队实现run_experiment(request)，旧完整算法helper/模拟分不适用。D-01简单、D-02中等、D-05困难为设计定位；方法、数据接口、失败部分分与40分细则见[D类实验要求](D类实验要求.md)。下载research-experiments.zip与data/experiments三题材料同版。其他类别规则与原文保持。


本轮D-01/D-02/D-05实验随机种子统一为42；以layered-d-v3协议和本轮冻结数据为准。


## Survey / Idea 正文上限（c1-output-limits-v1）

新版派题的 Markdown 正文按 `## 参考文献` 章节之前的内容计数，空格、标点及 Markdown 符号均计入；参考文献章节不计入正文上限：

- `C-*` Survey：最多 5000 字符，建议约 3500 字符；正文文中引用标记最多 40 次。复合数字标注如 `[1,2]` 按一组标记计一次，同一自然段内重复引用应合并。
- `E-*` Idea：最多 4500 字符，建议 1800～3200 字符；禁止程序动态拼接、硬编码扩写或使用无关的通用段落。

超过上限会由平台判为提交结果无效。此项只增加输出限制，不改变 `case_id`、`path`、`markdown` 字段或另存文件规则，也不追溯重算旧评测。
