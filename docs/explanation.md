# research_pilot 项目说明

## 赛事运行目录约定

下面保留赛事目录示意。`/app` 对应镜像工作目录，`/output` 是默认结果根；当前已接入本队 dispatch／assigned 读取、A/B/C/E 正式文件交付及 D 已有产物核对。论文、实验材料目录供后续业务工具接入使用，队伍名统一取实际工作区配置。图中的论文、图片名称为结构示意，实际文件名取自材料或实验产物。

```text
/
├── app/                        镜像层，WORKDIR
│   ├── agent.py
│   ├── requirements.txt
│   └── src/
│
├── data/                       只读挂载
│   ├── papers/                 论文库
│   └── experiments/
│       ├── run_experiment.py
│       └── <case_id>/
│
└── output/                     可写挂载，工作区根
    └── data/
        ├── <队伍名>/
        │   ├── dispatch.json   只读
        │   ├── assigned/       只读
        │   │   ├── A-01.json
        │   │   ├── B-0x.json
        │   │   ├── C-0x.json
        │   │   ├── D-0x.json
        │   │   └── E-0x.json
        │   ├── A-01.json
        │   ├── …
        │   └── work/
        ├── expert/             扁平共享，队名在文件名里
        │   ├── C-01-综述-<队伍名>.md
        │   ├── C-03-综述-<队伍名>.md
        │   ├── E-01-灵感Idea报告-<队伍名>.md
        │   └── E-02-灵感Idea报告-<队伍名>.md
        └── figure/
            └── <队伍名>/       按队隔离，文件名随意
                ├── D-01-result.png
                ├── D-02-result.png
                └── D-05-result.png
```

## dispatch.json

默认位置（只读；设置 `RESEARCH_RESULT_ROOT` 后按配置的工作区根定位）

- `/output/data/<队伍名>/dispatch.json`
- 工作区根为 `/output`；派题、题目、结果同在 `/output/data/` 下
- 拼路径用 `$RESEARCH_RESULT_ROOT/data/<队伍名>/dispatch.json`（默认 `/output`），不要用 `DATA_PATH`
- `<队伍名>` 优先取 `RESEARCH_WORKSPACE_TEAM`，其次本地 `TEAM_NAME`，否则为 `local-team`；与当前 `RuntimeConfig` 一致

读取规则与交付约定

当前派题入口采用单次单题的只读核对流程；答卷经公共组装、校验和对应题型的正式交付核对后返回。跨请求处理策略随后续业务联调进一步确定。

- 校验 dispatch.json 中 v3／11 题的元数据；每次 `/solve` 只处理请求 `case_id` 指定的一题，不自动执行全部 11 题
- 不自行抽题、不扫描未派发题目、不补题、不重复提交同一题号
- 按 `cases[].question_file` 定位题目：`/output/data/<队伍名>/assigned/<case_id>.json`
- 核对 `team`、11 个不重复题号（A–E 为 1/3/2/3/2）、题目文件内部 `case_id`
- 当前代码还核对 assigned 文件的 SHA-256、派题元数据与请求 JSON 的完整内容；通过后使用 assigned，不使用请求体补全或替代文件
- 正式结果文件名与答卷内部 `case_id` 统一取核对后的 assigned；JSON 文件路径由 `RuntimeConfig.result_json_path()` 提供，A/B/C/E 保存后回读核对，D 回读官方已有结果
- 派题记录只读
- 所引赛事文件的题号示例范围为 A-01～A-100，并描述 A 类随机、B–E 固定；当前代码不硬编码题号集合，题号正则接受至少两位数字后缀，实际处理范围由本轮 dispatch 中的当前题确定

字段结构示意

下面 `cases` 仅展示两条，摘要和标题使用占位符，不能直接作为有效派题记录。实际文件须包含与 11 个 `case_ids` 一一对应的完整条目。

```json
{
  "schema_version": 3,
  "dispatch_id": "<派题记录 ID>",
  "team": "<队伍名>",
  "seed": 2,
  "selection_mode": "random_a_fixed_bcde",
  "cases_per_category": {"A": 1, "B": 3, "C": 2, "D": 3, "E": 2},
  "rules_version": "research-assistant-objective-v3",
  "dispatch_policy_version": "research-assistant-dispatch-v3",
  "dispatch_policy_sha256": "<SHA-256>",
  "difficulty_by_case": {"D-01": "easy", "D-02": "medium", "D-05": "hard"},
  "case_ids": [
    "A-01",
    "B-01", "B-02", "B-03",
    "C-01", "C-03",
    "D-01", "D-02", "D-05",
    "E-01", "E-02"
  ],
  "cases": [
    {
      "case_id": "A-01",
      "function": "Chat",
      "title": "<题目标题>",
      "question_file": "data/<队伍名>/assigned/A-01.json",
      "question_sha256": "<SHA-256>"
    },
    {
      "case_id": "D-01",
      "function": "Reproduction",
      "title": "<题目标题>",
      "question_file": "data/<队伍名>/assigned/D-01.json",
      "question_sha256": "<SHA-256>",
      "difficulty": "easy"
    }
  ]
}
```

`cases` 与 `case_ids` 一一对应，实际为 11 条。

## 历史调试记录：重构前观察

以下保留重构前的开发线索，供后续业务接入参考。当前实现阶段以上述流程图和本轮验证为准。

| 题型 | 历史观察 | 当前进度与接入关注点 |
| --- | --- | --- |
| A | 旧版记录为“不用改动”。 | 当前 Chat 字段、公共组装和 JSON 交付已接通，下一步接入真实处理器。 |
| B | 旧版曾出现论文路径偶发读取错误，并仅提取第一页约 900 字符，排序容易受标题、作者和机构片段影响；扫描论文需要 OCR。 | 当前到达 `papers` 字段契约、真实文件名保持与 JSON 交付阶段；后续结合材料定位、正文／OCR 提取及排序证据推进。 |
| C | 旧版有引文次数超限引发 HTTP 500 的记录。 | 当前已有报告结构、精确本队路径、正文上限／引文次数检查以及 Markdown／JSON 一致性交付；后续报告生成可据此调整提示与业务校验。 |
| D | 旧版 `_case_hints` 曾针对 D-02 的 run/rank 阶段与 D-05 事件循环写定制提示，`_repair_prompt` 也含 D-05 自测修复提示。 | 当前已有通用字段契约及官方结果／图表只读核对，后续接入当轮材料与官方客户端，保持通用执行与真实产物边界。 |
| E | 旧版曾因 Agent 自设整题倒计时中止报告生成。 | 当前整题时限由赛事平台控制，Idea 字段、公共路径和文件交付已接通；下一步接入模型生成。 |
