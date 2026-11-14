# 应用接口

服务绑定本地地址，输入输出为 UTF-8 JSON。错误输入为400，未找到对象为404，超过8MB请求为413。页面与服务端共用口径，前端不另算一份统计答案。

- `GET /health`：检查启动是否成功。
- `GET /api/context`：产品配置、默认数据和已保存材料目录。
- `GET /api/data?revision=版本`：查看完整原始表。
- `POST /api/datasets`：传入 `label` 与完整 `data`，校验后返回不可变 `revision`。
- `POST /api/explore`：传入可选的 `revision` 和 `selection`，返回实际选择、资料指纹与 `view`。现有 view 的 summary、rows、series、notes 只用于描述。
- `POST /api/notebooks`：传入选择与 `title`，由服务端重新生成描述后保存，不接受客户端伪造结果。
- `GET /api/notebooks` 与 `GET /api/notebooks/编号`：目录以及冻结资料、选择和结果。
- `POST /api/studies`：反事实研究。不带 `title` 时返回当场计算结果（200）；带 `title` 时
  额外冻结到 `studies` 表并返回保存记录（201）。`selection` 支持 `outcome`、`donor_scope`、
  `treatment_week`、`train_end`、`eval_start`、`eval_end`、`placebo_week`、`excluded_areas`、
  `rate_per_1000`，全部由服务端校验，非法参数返回400。结果含逐片区动态对照资格与原因、
  非负且和为1的合成权重、逐周实际/反事实/缺口、拟合与留时段 RMSPE、两类安慰剂、
  留一地区、三渠道恒等核对及 `identified` 识别门槛。
- `GET /api/studies` 与 `GET /api/studies/编号`：研究目录与冻结研究（含原数据快照）。
  研究与描述性 `notebooks` 分表存储，互不改变原有含义。

新增分析可扩展接口，但已有导入、描述和历史读取仍供业务使用，不能静默改换其含义。
反事实的口径、诊断与边界见 `docs/analysis-design.md`。
