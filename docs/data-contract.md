# 资料与统计口径

`areas` 指居民归属片区，A0 为本次中心开放地区，其余为候选对照；`population` 为同口径登记人口。`contacts` 是接触上报，`source_key` 相同为同一次接触重报，`household` 可重复接受多个渠道服务。`resident_area` 是居民所属片区，`service_area` 是实际服务地点，不可混淆。时间以周编号计，`reported_at` 为资料可见周。`events` 记录各区中心开放时间；A0 第52周开放，A3 第70周开放。`sharing` 给出有向共享队伍或跨区服务的有效区间 `[start,stop)`，不是笼统的永久剔除名单。`center/home/total` 分别按渠道或全部接触浏览，`unshared` 仅作资料筛选，不能代替分析时的动态对照资格。既有页面显示原始次数，尚未实现反事实。新增分析需明确居民人群、去重、训练截止和效果尺度，任何开业前已存在的中心渠道仅表示其他中心接触，不等于新设施提前开放。

## 原始表

- `areas`：9条示例，主键为 `area`。字段：`area`, `treated`, `population`, `shared_team`。
- `contacts`：15369条示例，主键为 `contact`。字段：`contact`, `resident_area`, `service_area`, `household`, `week`, `channel`, `reported_at`, `source_key`。
- `events`：2条示例，主键为 `event`。字段：`event`, `area`, `week`, `kind`。
- `sharing`：2条示例，主键为 `sharing`。字段：`sharing`, `from_area`, `to_area`, `start`, `stop`, `kind`。

## 版本与扩展

`fixtures/data.json` 是完整导入材料，`product.json` 是当前浏览器默认选择。全量导入校验后按规范化内容指纹保存，相同内容不重复建版本。原始事实不原地修改；资料修订形成新版本。`null` 表示明确未观测，不因筛选或合计自动变为0。

`center_counterfactual/domain.py` 是领域入口，现阶段仅做字段校验及资料描述；`service.py` 连接范围选择、数据版本与服务端计算后的存档；`storage.py` 管理不可变数据和保存材料；`analysis.py` 实现基于动态对照资格的合成控制反事实（设计见 `docs/analysis-design.md`），研究记录存入独立的 `studies` 表；`web/app.js` 提供浏览、反事实交互和导出。保存的材料同时包含原数据与选择，打开旧材料不重算为新数据。新增分析不限内部组织方式，但不能丢失现有原始记录核对与历史材料读取能力。统计方法、模型选择和业务结论没有预设答案。
