"""反事实建模：用未受影响片区构造 A0 的合成对照。

口径与边界（与 docs/analysis-design.md 对应）：

- 人群固定为“居民归属片区 = A0 的登记居民”，按 ``source_key`` 去重后按周汇总接触次数；
  跨区服务按居民归属计入本片区，次数口径永远不解释为受益人数。
- 对照资格随周变化：候选片区在自身中心开放、或与处理区存在共享队伍/跨区服务的
  有效区间内，其记录视为受污染。污染周落在处理后资料覆盖区间内的片区整体退出
  合成对照（受污染后的记录不能继续当成未受影响对照）；训练窗口本身必须完全干净。
- 模型为非负、权重和为 1 的合成控制（SCM），仅用训练窗口拟合一次，处理后不重新拟合。
- 先在次数尺度分别拟合 center、home，total 由两者相加得到，保证三渠道恒等；
  每千人口率仅作为展示尺度，不单独拟合。
- 诊断（拟合质量、留时段预测、安慰剂、留一地区、三渠道恒等、识别门槛）不足时，
  ``identified`` 为 False，结论只给“无法识别”，不输出平滑效果线。
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import minimize

WEEKS = 104
TREATED_AREA = "A0"
CHANNELS = ("center", "home", "total")
IDENTITY_TOLERANCE = 1e-6
MIN_DONORS = 2
MIN_TRAIN_WEEKS = 12
MIN_EVAL_WEEKS = 4
# 处理后 RMSPE 相对训练期 RMSPE 的倍数门槛：低于该倍数视为效果与噪声不可分
WEAK_FIT_RATIO = 2.0


def _finite_week(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(name + "必须是有限整数周")
    return int(value)


def validate_parameters(selection):
    """校验分析选择，返回规范化后的参数。"""
    if not isinstance(selection, dict):
        raise ValueError("选择条件必须是对象")
    outcome = selection.get("outcome", "total")
    if outcome not in CHANNELS:
        raise ValueError("服务指标无效")
    donor_scope = selection.get("donor_scope", "all")
    if donor_scope not in ("all", "unshared"):
        raise ValueError("候选地区范围无效")
    treatment_week = _finite_week(selection.get("treatment_week", 52), "开放周")
    if not 1 <= treatment_week <= WEEKS - 1:
        raise ValueError("开放周超出资料范围")
    train_end = _finite_week(selection.get("train_end", treatment_week - 1), "训练截止周")
    if not MIN_TRAIN_WEEKS <= train_end < treatment_week:
        raise ValueError("训练窗口至少需要12周且必须早于开放周")
    eval_start = _finite_week(selection.get("eval_start", 0), "留时段起始周")
    eval_end = _finite_week(selection.get("eval_end", train_end), "留时段结束周")
    if not (0 <= eval_start < eval_end <= train_end and eval_end - eval_start >= MIN_EVAL_WEEKS):
        raise ValueError("留时段必须位于训练窗口内且至少4周")
    placebo_week = selection.get("placebo_week", max(MIN_TRAIN_WEEKS, train_end // 2))
    if placebo_week is not None:
        placebo_week = _finite_week(placebo_week, "安慰剂开放周")
        if not MIN_TRAIN_WEEKS <= placebo_week <= train_end:
            raise ValueError("安慰剂开放周必须落在训练窗口内")
    excluded = selection.get("excluded_areas", [])
    if not isinstance(excluded, list) or any(not isinstance(area, str) for area in excluded):
        raise ValueError("手动剔除地区必须是字符串数组")
    rate_scale = selection.get("rate_per_1000", False)
    if not isinstance(rate_scale, bool):
        raise ValueError("人均率开关必须为布尔值")
    return {
        "outcome": outcome,
        "donor_scope": donor_scope,
        "treatment_week": treatment_week,
        "train_end": train_end,
        "eval_start": eval_start,
        "eval_end": eval_end,
        "placebo_week": placebo_week,
        "excluded_areas": list(dict.fromkeys(excluded)),
        "rate_per_1000": rate_scale,
    }


def build_series(data):
    """按居民归属片区构造去重后的周度渠道接触次数与登记人口。

    同一 ``source_key`` 的重报只计一次；跨区服务计入居民所属片区而非服务地点。
    """
    areas = [row["area"] for row in data["areas"]]
    population = {row["area"]: float(row["population"]) for row in data["areas"]}
    counts = {area: {channel: np.zeros(WEEKS, dtype=float) for channel in ("center", "home")}
              for area in areas}
    households = {area: set() for area in areas}
    seen_source = set()
    dropped_resends = 0
    for row in data["contacts"]:
        source = row["source_key"]
        if source in seen_source:
            dropped_resends += 1
            continue
        seen_source.add(source)
        area = row["resident_area"]
        counts[area][row["channel"]][row["week"]] += 1.0
        households[area].add(row["household"])
    series = {}
    for area in areas:
        center = counts[area]["center"]
        home = counts[area]["home"]
        series[area] = {"center": center, "home": home, "total": center + home,
                        "population": population[area],
                        "households_observed": len(households[area])}
    return series, dropped_resends


def donor_eligibility(data, params):
    """逐片区判定对照资格，记录排除原因、首个污染周与是否可用于处理后对照。"""
    treatment_week = params["treatment_week"]
    train_end = params["train_end"]
    excluded = set(params["excluded_areas"])
    open_weeks = {row["area"]: int(row["week"]) for row in data["events"]
                  if row.get("kind") == "center_open"}
    # 与处理区之间存在有向共享队伍或跨区服务的片区，区间 [start, stop)
    contaminated_intervals = {}
    for row in data["sharing"]:
        if row["to_area"] == TREATED_AREA or row["from_area"] == TREATED_AREA:
            other = row["to_area"] if row["from_area"] == TREATED_AREA else row["from_area"]
            contaminated_intervals.setdefault(other, []).append(
                (int(row["start"]), int(row["stop"]), row.get("kind", "sharing")))
    eligibility = []
    for row in data["areas"]:
        area = row["area"]
        if area == TREATED_AREA:
            continue
        reasons = []
        if area in excluded:
            reasons.append("manual_excluded")
        if params["donor_scope"] == "unshared" and row.get("shared_team"):
            reasons.append("shared_team_flag")
        own_open = open_weeks.get(area)
        if own_open is not None:
            reasons.append("future_center_open@" + str(own_open))
        for start, stop, kind in contaminated_intervals.get(area, []):
            reasons.append(kind + "@" + str(start) + "-" + str(stop))
        mask = np.zeros(WEEKS, dtype=bool)
        if own_open is not None:
            mask[own_open:] = True
        for start, stop, _kind in contaminated_intervals.get(area, []):
            mask[max(0, start):min(WEEKS, stop)] = True
        first_contaminated = None if not mask.any() else int(np.argmax(mask))
        clean_train_weeks = [week for week in range(train_end + 1) if not mask[week]]
        manual_or_flag = (area in excluded or
                          (params["donor_scope"] == "unshared" and row.get("shared_team")))
        # 可用于训练：训练窗口内全部周未污染（污染只可能发生在训练窗口之后）
        train_clean = len(clean_train_weeks) >= train_end + 1
        # 可用于处理后对照：资料覆盖的处理后时段内不能出现任何污染周。
        # 污染起点只要落在开放周与资料末尾之间，受污染记录就不能再当未受影响对照。
        post_clean = first_contaminated is None or first_contaminated >= WEEKS
        eligibility.append({
            "area": area,
            "reasons": reasons,
            "clean_train_weeks": clean_train_weeks,
            "first_contaminated_week": first_contaminated,
            "train_eligible": train_clean and not manual_or_flag,
            "post_eligible": post_clean and not manual_or_flag,
            # 同时满足训练干净、处理后全程未污染、未被手动/标记剔除，才进入合成对照
            "eligible": train_clean and post_clean and not manual_or_flag,
        })
    return eligibility


def _scm_weights(target, matrix):
    """非负、和为1的合成控制权重，最小化训练期平方误差。"""
    target = np.asarray(target, dtype=float)
    matrix = np.asarray(matrix, dtype=float)
    count = matrix.shape[1]
    if count == 0:
        raise ValueError("没有可用对照片区")

    def objective(weights):
        residual = target - matrix @ weights
        return float(residual @ residual)

    constraints = ({"type": "eq", "fun": lambda weights: weights.sum() - 1.0},)
    bounds = [(0.0, 1.0)] * count
    best = None
    starts = [np.full(count, 1.0 / count)]
    random = np.random.default_rng(20261114)
    for _ in range(12):
        starts.append(random.dirichlet(np.ones(count)))
    for start in starts:
        result = minimize(objective, start, method="SLSQP", bounds=bounds,
                          constraints=constraints,
                          options={"maxiter": 2000, "ftol": 1e-12})
        if result.success and (best is None or result.fun < best.fun):
            best = result
    if best is None:
        raise ValueError("合成对照权重求解失败")
    weights = best.x
    weights[weights < 1e-10] = 0.0
    weights = weights / weights.sum()
    return weights


def _rmspe(actual, predicted):
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    return float(np.sqrt(np.mean((actual - predicted) ** 2)))


def _fit_channel(series, train_end, donor_areas, treated_area, channel):
    target = series[treated_area][channel]
    donor_matrix = np.column_stack([series[area][channel] for area in donor_areas])
    weights = _scm_weights(target[:train_end + 1], donor_matrix[:train_end + 1])
    predicted = donor_matrix @ weights
    return weights, predicted


def _rounded(values, digits=4):
    return [None if value is None or not math.isfinite(value) else round(float(value), digits)
            for value in values]


def analyze(data, raw_selection):
    """主入口：返回可序列化的反事实分析结果。"""
    params = validate_parameters(raw_selection)
    series, dropped_resends = build_series(data)
    eligibility = donor_eligibility(data, params)
    eligible_areas = [item["area"] for item in eligibility if item["eligible"]]
    outcome = params["outcome"]
    treatment_week = params["treatment_week"]
    train_end = params["train_end"]
    eval_start, eval_end = params["eval_start"], params["eval_end"]
    rate = params["rate_per_1000"]
    pop = series[TREATED_AREA]["population"]

    notes = [
        "人群口径固定为居民归属片区=A0的登记居民；接触按source_key去重，跨区服务计入居民所属片区。",
        "所有数值为接触次数（可选每千登记人口率展示）；同一家庭可重复接触，次数变化不能表述为受益人数变化。",
        "合成对照只在训练窗口拟合一次；候选片区一旦在开放周或之后受到污染即退出处理后对照。",
    ]
    result = {
        "params": params,
        "method": {
            "name": "synthetic_control",
            "estimator": "非负、权重和为1的合成控制（SLSQP约束最小二乘），仅训练窗口拟合一次",
            "identity": "center与home分别在次数尺度拟合，total由两者相加得到，保证 total=center+home",
        },
        "data_prep": {
            "deduped_source_keys": len({row["source_key"] for row in data["contacts"]}),
            "dropped_resends": dropped_resends,
            "weeks": WEEKS,
            "treated_population": pop,
            "households_observed": series[TREATED_AREA]["households_observed"],
            "rate_per_1000": rate,
        },
        "eligibility": [
            {"area": item["area"], "eligible": item["eligible"],
             "train_eligible": item["train_eligible"], "post_eligible": item["post_eligible"],
             "reasons": item["reasons"],
             "clean_train_week_count": len(item["clean_train_weeks"]),
             "first_contaminated_week": item["first_contaminated_week"]}
            for item in eligibility
        ],
        "outcomes": {},
        "placebo": {},
        "leave_one_out": [],
        "identity_check": {},
        "identified": False,
        "notes": notes,
        "warnings": [],
    }

    if len(eligible_areas) < MIN_DONORS:
        result["warnings"].append("符合资格的对照片区少于2个，无法构造可信合成对照，结论为无法识别。")
        return result

    def scale(area, vector):
        return vector * (1000.0 / series[area]["population"]) if rate else vector

    # 主拟合：center、home 分别拟合，total 由恒等关系合成
    weights_map, predicted_map, gap_map, actual_map = {}, {}, {}, {}
    for channel in ("center", "home"):
        weights, predicted = _fit_channel(series, train_end, eligible_areas,
                                          TREATED_AREA, channel)
        weights_map[channel] = dict(zip(eligible_areas, weights))
        predicted_map[channel] = predicted
        actual_map[channel] = series[TREATED_AREA][channel].copy()
        gap_map[channel] = actual_map[channel] - predicted
    predicted_map["total"] = predicted_map["center"] + predicted_map["home"]
    actual_map["total"] = actual_map["center"] + actual_map["home"]
    gap_map["total"] = gap_map["center"] + gap_map["home"]

    for channel in CHANNELS:
        actual_show = scale(TREATED_AREA, actual_map[channel])
        predicted_show = scale(TREATED_AREA, predicted_map[channel])
        gap_show = actual_show - predicted_show
        post = slice(treatment_week, WEEKS)
        result["outcomes"][channel] = {
            "weights": {area: round(weight, 6) for area, weight in weights_map[channel].items()
                        if weight > 0.0} if channel != "total" else None,
            "actual": _rounded(actual_show),
            "counterfactual": _rounded(predicted_show),
            "gap": _rounded(gap_show),
            "pre_rmspe": round(_rmspe(actual_show[:train_end + 1],
                                      predicted_show[:train_end + 1]), 6),
            "holdout_rmspe": round(_rmspe(actual_show[eval_start:eval_end],
                                          predicted_show[eval_start:eval_end]), 6),
            "post_rmspe": round(float(np.sqrt(np.mean(gap_show[post] ** 2))), 6),
            "post_mean_gap": round(float(gap_show[post].mean()), 6),
            "post_total_gap": round(float(gap_show[post].sum()), 6),
            "post_weeks": WEEKS - treatment_week,
        }

    # 恒等核对（次数尺度，精确成立）
    identity_error = float(np.max(np.abs(predicted_map["center"] + predicted_map["home"]
                                         - predicted_map["total"])))
    result["identity_check"] = {
        "max_abs_identity_error": round(identity_error, 10),
        "actual_identity_error": 0.0,
        "passes": identity_error < IDENTITY_TOLERANCE,
    }

    selected = result["outcomes"][outcome]

    # 安慰剂一（in-time）：只使用安慰剂周之前的数据重新拟合，
    # 其后到真实开放周前为“伪处理期”。该窗口内若出现与真实处理后同量级的持续缺口，
    # 说明模型在没有处理的时期也会制造效果，因果解释不成立。
    placebo_week = params["placebo_week"]
    if placebo_week is not None:
        if outcome == "total":
            _w1, pc_pred = _fit_channel(series, placebo_week - 1, eligible_areas,
                                        TREATED_AREA, "center")
            _w2, ph_pred = _fit_channel(series, placebo_week - 1, eligible_areas,
                                        TREATED_AREA, "home")
            p_pred = pc_pred + ph_pred
        else:
            _pw, p_pred = _fit_channel(series, placebo_week - 1, eligible_areas,
                                       TREATED_AREA, outcome)
        p_actual = scale(TREATED_AREA, actual_map[outcome])
        p_pred_show = scale(TREATED_AREA, p_pred)
        pseudo_pre = slice(0, placebo_week)
        pseudo_post = slice(placebo_week, train_end + 1)
        pseudo_pre_rmspe = _rmspe(p_actual[pseudo_pre], p_pred_show[pseudo_pre])
        pseudo_post_rmspe = _rmspe(p_actual[pseudo_post], p_pred_show[pseudo_post])
        result["placebo"]["in_time"] = {
            "placebo_week": placebo_week,
            "pre_placebo_rmspe": round(pseudo_pre_rmspe, 6),
            "pseudo_post_mean_gap": round(float((p_actual - p_pred_show)[pseudo_post].mean()), 6),
            "pseudo_post_rmspe": round(pseudo_post_rmspe, 6),
            "pseudo_to_pre_rmspe_ratio": round(pseudo_post_rmspe / pseudo_pre_rmspe, 4)
            if pseudo_pre_rmspe > 0 else None,
        }

    # 安慰剂二：每个合格对照片区充当伪处理区，用其余对照重建（次数尺度）
    donor_placebo = []
    for area in eligible_areas:
        others = [candidate for candidate in eligible_areas if candidate != area]
        if len(others) < MIN_DONORS:
            continue
        try:
            if outcome == "total":
                _w1, p1 = _fit_channel(series, train_end, others, area, "center")
                _w2, p2 = _fit_channel(series, train_end, others, area, "home")
                p_pred = p1 + p2
            else:
                _w, p_pred = _fit_channel(series, train_end, others, area, outcome)
        except ValueError:
            continue
        p_actual = series[area][outcome].copy()
        post = slice(treatment_week, WEEKS)
        donor_placebo.append({
            "area": area,
            "pre_rmspe": round(_rmspe(p_actual[:train_end + 1], p_pred[:train_end + 1]), 6),
            "post_mean_gap": round(float((p_actual - p_pred)[post].mean()), 6),
            "post_rmspe": round(_rmspe(p_actual[post], p_pred[post]), 6),
        })
    result["placebo"]["donor_distribution"] = donor_placebo
    result["placebo"]["donor_count"] = len(donor_placebo)
    # 处理区缺口在伪处理分布中的极端程度（绝对值排序，1表示最极端）
    if donor_placebo:
        treated_gap = abs(selected["post_mean_gap"])
        ranked = sorted([treated_gap] + [abs(item["post_mean_gap"]) for item in donor_placebo],
                        reverse=True)
        result["placebo"]["treated_rank"] = ranked.index(treated_gap) + 1
    else:
        result["placebo"]["treated_rank"] = None

    # 留一地区：结论对单一对照的依赖
    for area in eligible_areas:
        others = [candidate for candidate in eligible_areas if candidate != area]
        if len(others) < MIN_DONORS:
            result["leave_one_out"].append({"dropped": area, "available": False})
            continue
        if outcome == "total":
            _w1, p1 = _fit_channel(series, train_end, others, TREATED_AREA, "center")
            _w2, p2 = _fit_channel(series, train_end, others, TREATED_AREA, "home")
            loo_pred = p1 + p2
        else:
            _w, loo_pred = _fit_channel(series, train_end, others, TREATED_AREA, outcome)
        loo_gap = scale(TREATED_AREA, actual_map[outcome] - loo_pred)
        full_weight = None
        if outcome != "total":
            full_weight = round(weights_map[outcome].get(area, 0.0), 6)
        result["leave_one_out"].append({
            "dropped": area,
            "available": True,
            "pre_rmspe": round(_rmspe(scale(TREATED_AREA, actual_map[outcome])[:train_end + 1],
                                      scale(TREATED_AREA, loo_pred)[:train_end + 1]), 6),
            "post_mean_gap": round(float(loo_gap[treatment_week:].mean()), 6),
            "weight_in_full_model": full_weight,
        })

    # 识别门槛
    post_rmspe = selected["post_rmspe"]
    pre_rmspe = selected["pre_rmspe"]
    diagnostics = {
        "donor_count": len(eligible_areas),
        "pre_rmspe": pre_rmspe,
        "holdout_rmspe": selected["holdout_rmspe"],
        "post_rmspe": post_rmspe,
        "post_to_pre_rmspe_ratio": round(post_rmspe / pre_rmspe, 4) if pre_rmspe > 0 else None,
        "identity_passes": result["identity_check"]["passes"],
    }
    warnings = result["warnings"]
    identified = True
    if pre_rmspe <= 1e-9:
        diagnostics["fit_verdict"] = "训练期误差接近零：合成轨迹几乎完全复制处理区，注意检查是否为机械重复"
    elif post_rmspe < WEAK_FIT_RATIO * pre_rmspe:
        identified = False
        diagnostics["fit_verdict"] = "处理后缺口不足训练期误差的2倍，效果与拟合噪声不可分"
        warnings.append("处理后缺口相对预处理RMSPE不够突出，不能宣称可识别的效果。")
    else:
        diagnostics["fit_verdict"] = "处理后缺口超过训练期误差的2倍"
    if pre_rmspe > 0 and selected["holdout_rmspe"] > pre_rmspe * 2:
        identified = False
        warnings.append("留时段预测误差明显大于训练期，外推能力不足。")
    in_time = result["placebo"].get("in_time")
    if in_time is not None and abs(selected["post_mean_gap"]) > 0:
        if abs(in_time["pseudo_post_mean_gap"]) > 0.5 * abs(selected["post_mean_gap"]):
            identified = False
            warnings.append("训练期虚构开放周即出现相近规模缺口，安慰剂检验不支持因果解释。")
        if in_time["pseudo_to_pre_rmspe_ratio"] is not None and \
                in_time["pseudo_to_pre_rmspe_ratio"] >= WEAK_FIT_RATIO:
            identified = False
            warnings.append("虚构开放周后的预测误差相对其前期放大超过2倍，模型在无处理时期也会失配。")
    treated_rank = result["placebo"].get("treated_rank")
    if treated_rank is not None:
        rank_ratio = treated_rank / (result["placebo"]["donor_count"] + 1)
        diagnostics["placebo_rank_ratio"] = round(rank_ratio, 4)
        # 处理区缺口若落在伪处理分布的中位数之后，说明对照自身就能产生更极端的缺口
        if rank_ratio > 0.5:
            identified = False
            warnings.append("处理区缺口在伪处理片区分布中不处于极端位置，对照自身即可出现类似缺口。")
    else:
        identified = False
        warnings.append("没有足够对照片区构造安慰剂分布，无法评估缺口的极端程度。")
    robust_gaps = [item["post_mean_gap"] for item in result["leave_one_out"] if item.get("available")]
    if robust_gaps and abs(selected["post_mean_gap"]) > 0:
        sign_changes = sum(1 for value in robust_gaps
                           if (value > 0) != (selected["post_mean_gap"] > 0))
        diagnostics["leave_one_out_sign_changes"] = sign_changes
        if sign_changes:
            identified = False
            warnings.append("剔除单个对照片区后效果方向改变，结论依赖单一地区。")
    if not result["identity_check"]["passes"]:
        identified = False
        warnings.append("三渠道恒等核对未通过，center/home/total口径不一致。")
    result["diagnostics"] = diagnostics
    result["identified"] = identified
    if not identified:
        result["conclusion"] = ("诊断不足：现有资料无法把A0的变化与共同时间因素区分开，"
                                "本处经验不具备可识别的因果解释，不输出平滑效果线。")
    else:
        result["conclusion"] = (
            "在动态对照资格与上述诊断通过的前提下，合成对照给出反事实轨迹；"
            "结果为单次个案研究，权重、资格与模型状态已随记录保留，不外推为全市因果结论。")
    return result
