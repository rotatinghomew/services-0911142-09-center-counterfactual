# 社区中心开放效果研究台

这是供服务规划人员核对资料、查看业务范围和留存讨论依据的本地应用。当前提供中心开放前后的地区服务时间序列，并支持导入、图表、原始记录、保存与重新打开。反事实分析区用其他未受影响片区构造 A0 的合成对照：按 `events` 与 `sharing` 动态判定对照资格，提供拟合质量、留时段预测、两类安慰剂、留一地区与三渠道恒等诊断；诊断不足时明确标注无法识别，不输出平滑效果线。示例为独立生成的虚构数据，不含真实家庭、医疗资料或对外服务凭据。

## 使用环境

使用 Python 3.11，独立容器镜像锁定到 3.11.14。页面为原生浏览器界面，数据处理通过本地 HTTP 服务完成，持久化使用应用内置数据库。基础浏览不依赖数值库；研究扩展所需的数值计算和建模库已按 `requirements.txt` 固定所有传递版本。依赖安装及镜像构建只在环境准备时做，启动和检查不访问网络。

```bash
python3 -m pip install -r requirements.txt -r requirements-build.txt -r requirements-test.txt
python3 -m playwright install --with-deps chromium
python3 -m center_counterfactual --host 127.0.0.1 --port 8080
```

浏览器打开本地8080端口。容器方式先准备镜像，运行时不需要其他服务：

```bash
docker build -t center_counterfactual:local .
docker run --rm -p 8080:8080 -v center_counterfactual-state:/app/.state center_counterfactual:local
```

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 tests/browser_smoke.py
python3 tests/browser_study.py
```

检查包含真实领域字段、非法导入、边界数据保留、HTTP交互、版本指纹及重启后存档。反事实检查覆盖参数校验、去重与跨区口径、动态对照资格、SCM 权重约束、安慰剂与留一诊断、识别门槛、研究冻结重启及页面交互。运行检查使用临时状态，不改动资料。

字段语义与现有接口分别见 `docs/data-contract.md` 和 `docs/http.md`；反事实口径、诊断与外推边界见 `docs/analysis-design.md`。短时操作见 `docs/walkthrough.md`，环境准备不包含在正式操作中。
