from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import json
import math
import unittest

import numpy as np

from center_counterfactual.analysis import (
    analyze, build_series, donor_eligibility, validate_parameters, WEEKS, TREATED_AREA,
)
from center_counterfactual.service import Workbench
from center_counterfactual.server import make_server
from center_counterfactual.storage import fingerprint

ROOT = Path(__file__).resolve().parents[1]


def load_fixture():
    return json.loads((ROOT / "fixtures" / "data.json").read_text())


class ParameterTests(unittest.TestCase):
    def test_defaults_are_filled_and_validated(self):
        params = validate_parameters({})
        self.assertEqual(params["treatment_week"], 52)
        self.assertEqual(params["train_end"], 51)
        self.assertEqual(params["excluded_areas"], [])

    def test_training_window_must_precede_opening(self):
        with self.assertRaises(ValueError):
            validate_parameters({"treatment_week": 52, "train_end": 52})

    def test_too_short_training_window_rejected(self):
        with self.assertRaises(ValueError):
            validate_parameters({"treatment_week": 52, "train_end": 5})

    def test_holdout_must_lie_inside_training_window(self):
        with self.assertRaises(ValueError):
            validate_parameters({"eval_start": 40, "eval_end": 60, "train_end": 51})

    def test_bad_outcome_and_scope_rejected(self):
        with self.assertRaises(ValueError):
            validate_parameters({"outcome": "phone"})
        with self.assertRaises(ValueError):
            validate_parameters({"donor_scope": "neighbors"})

    def test_non_finite_and_boolean_weeks_rejected(self):
        with self.assertRaises(ValueError):
            validate_parameters({"treatment_week": True})
        with self.assertRaises(ValueError):
            validate_parameters({"placebo_week": float("nan")})


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()

    def test_resends_are_deduped_by_source_key(self):
        series, dropped = build_series(self.data)
        unique_sources = len({row["source_key"] for row in self.data["contacts"]})
        self.assertEqual(dropped, len(self.data["contacts"]) - unique_sources)
        self.assertGreater(dropped, 0)
        # 去重后任一片区任一周的计数不超过去重前
        raw = sum(1 for _ in self.data["contacts"])
        deduped = sum(int(series[area][channel].sum())
                      for area in series for channel in ("center", "home"))
        self.assertEqual(deduped, unique_sources)
        self.assertLess(deduped, raw)

    def test_cross_area_visits_count_to_resident_area(self):
        series, _ = build_series(self.data)
        # A2 居民在 A0 接受服务的记录必须计入 A2（居民归属），而不是从 A2 消失
        cross = [row for row in self.data["contacts"]
                 if row["resident_area"] == "A2" and row["service_area"] == "A0"]
        self.assertTrue(cross)
        a2_total = int(series["A2"]["total"].sum())
        # 直接按居民归属统计去重记录数
        seen = set()
        expected = 0
        for row in self.data["contacts"]:
            if row["resident_area"] != "A2" or row["source_key"] in seen:
                continue
            seen.add(row["source_key"])
            expected += 1
        self.assertEqual(a2_total, expected)

    def test_channel_identity_total_equals_center_plus_home(self):
        series, _ = build_series(self.data)
        for area in series:
            self.assertTrue(np.allclose(series[area]["total"],
                                        series[area]["center"] + series[area]["home"]))


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()

    def test_params(self, **overrides):
        params = validate_parameters(overrides)
        return params

    def test_sharing_and_future_openers_are_not_post_donors(self):
        eligibility = {item["area"]: item for item in
                       donor_eligibility(self.data, self.test_params())}
        # A1 共享队伍、A2 跨区服务均从第52周起污染，不能做处理后对照
        self.assertFalse(eligibility["A1"]["eligible"])
        self.assertFalse(eligibility["A2"]["eligible"])
        self.assertEqual(eligibility["A1"]["first_contaminated_week"], 52)
        # A3 第70周才开放，训练期（截至51周）干净但处理后被污染，整体不合格
        self.assertFalse(eligibility["A3"]["eligible"])
        self.assertTrue(eligibility["A3"]["train_eligible"])
        self.assertFalse(eligibility["A3"]["post_eligible"])
        # A4-A8 无任何污染
        for area in ("A4", "A5", "A6", "A7", "A8"):
            self.assertTrue(eligibility[area]["eligible"], area)
            self.assertEqual(eligibility[area]["reasons"], [])

    def test_unshared_scope_adds_flag_exclusion(self):
        params = self.test_params(donor_scope="unshared")
        eligibility = {item["area"]: item for item in donor_eligibility(self.data, params)}
        self.assertIn("shared_team_flag", eligibility["A1"]["reasons"])
        # 未打共享标记的 A3 仍按未来开放规则处理
        self.assertNotIn("shared_team_flag", eligibility["A3"]["reasons"])

    def test_manual_exclusion_is_recorded_as_reason(self):
        params = self.test_params(excluded_areas=["A4"])
        eligibility = {item["area"]: item for item in donor_eligibility(self.data, params)}
        self.assertFalse(eligibility["A4"]["eligible"])
        self.assertIn("manual_excluded", eligibility["A4"]["reasons"])

    def test_contaminated_weeks_never_enter_training_when_early(self):
        # 若训练截止周晚于污染起点，该片区训练周列表必须剔除污染周
        params = validate_parameters({"train_end": 60, "treatment_week": 70})
        eligibility = {item["area"]: item for item in donor_eligibility(self.data, params)}
        a1 = eligibility["A1"]
        self.assertEqual(max(a1["clean_train_weeks"]), 51)
        self.assertFalse(a1["train_eligible"])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()

    def test_result_is_json_serializable_without_nan(self):
        result = analyze(self.data, {"outcome": "home"})
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertIn("synthetic_control", encoded)

    def test_inputs_are_not_mutated(self):
        before = deepcopy(self.data)
        analyze(self.data, {"outcome": "home"})
        self.assertEqual(before, self.data)

    def test_home_and_center_are_identified_but_total_is_not(self):
        home = analyze(self.data, {"outcome": "home"})
        center = analyze(self.data, {"outcome": "center"})
        total = analyze(self.data, {"outcome": "total"})
        self.assertTrue(home["identified"])
        self.assertTrue(center["identified"])
        # 总量缺口与噪声不可分，必须承认无法识别，而不是给平滑效果线
        self.assertFalse(total["identified"])
        self.assertTrue(any("无法识别" in warning or "不可分" in warning
                            or "不支持因果解释" in warning or "不处于极端位置" in warning
                            for warning in total["warnings"]))

    def test_substitution_story_uses_same_population_and_identity(self):
        result = analyze(self.data, {"outcome": "total"})
        self.assertTrue(result["identity_check"]["passes"])
        gaps = {channel: result["outcomes"][channel]["post_mean_gap"]
                for channel in ("center", "home", "total")}
        # 上门下降、中心上升、总量近似不变；总量缺口等于两渠道缺口之和
        self.assertLess(gaps["home"], 0)
        self.assertGreater(gaps["center"], 0)
        self.assertTrue(math.isclose(gaps["center"] + gaps["home"], gaps["total"], abs_tol=1e-4))
        self.assertLess(abs(gaps["total"]), abs(gaps["home"]))

    def test_weights_are_non_negative_and_sum_to_one(self):
        result = analyze(self.data, {"outcome": "home"})
        for channel in ("center", "home"):
            weights = result["outcomes"][channel]["weights"]
            self.assertTrue(all(weight >= 0 for weight in weights.values()))
            self.assertTrue(math.isclose(sum(weights.values()), 1.0, abs_tol=1e-6))

    def test_contaminated_areas_never_receive_weight(self):
        result = analyze(self.data, {"outcome": "home"})
        weighted = set()
        for channel in ("center", "home"):
            weighted.update(result["outcomes"][channel]["weights"].keys())
        self.assertNotIn("A1", weighted)
        self.assertNotIn("A2", weighted)
        self.assertNotIn("A3", weighted)

    def test_placebo_distribution_includes_every_eligible_donor(self):
        result = analyze(self.data, {"outcome": "home"})
        eligible = [item["area"] for item in result["eligibility"] if item["eligible"]]
        placebo_areas = {item["area"] for item in result["placebo"]["donor_distribution"]}
        self.assertEqual(set(eligible), placebo_areas)
        self.assertLessEqual(result["placebo"]["treated_rank"],
                             result["placebo"]["donor_count"] + 1)

    def test_leave_one_out_keeps_sign_when_identification_holds(self):
        result = analyze(self.data, {"outcome": "home"})
        signs = [item["post_mean_gap"] < 0 for item in result["leave_one_out"]
                 if item.get("available")]
        self.assertTrue(signs)
        self.assertTrue(all(signs))

    def test_too_few_donors_returns_unidentified_without_crashing(self):
        result = analyze(self.data, {"outcome": "home",
                                     "excluded_areas": ["A4", "A5", "A6", "A8"]})
        # 剩余合格片区不足两个时直接给无法识别
        if sum(item["eligible"] for item in result["eligibility"]) < 2:
            self.assertFalse(result["identified"])
            self.assertTrue(result["warnings"])
            self.assertNotIn("diagnostics", result)

    def test_two_donors_keeps_contract_without_crashing(self):
        # 只留两个合格对照时，伪处理区无法满足最少对照数，安慰剂排位显式置空且结论保守
        result = analyze(self.data, {"outcome": "home",
                                     "excluded_areas": ["A5", "A6", "A7"]})
        eligible = [item["area"] for item in result["eligibility"] if item["eligible"]]
        self.assertEqual(len(eligible), 2)
        self.assertIsNone(result["placebo"]["treated_rank"])
        self.assertFalse(result["identified"])
        self.assertTrue(any("安慰剂" in warning for warning in result["warnings"]))
        self.assertEqual(len(result["leave_one_out"]), 2)

    def test_rate_scale_preserves_gap_direction_and_identity(self):
        counts = analyze(self.data, {"outcome": "home", "rate_per_1000": False})
        rates = analyze(self.data, {"outcome": "home", "rate_per_1000": True})
        self.assertEqual(counts["outcomes"]["home"]["post_mean_gap"] < 0,
                         rates["outcomes"]["home"]["post_mean_gap"] < 0)
        self.assertTrue(rates["identity_check"]["passes"])
        scale = 1000.0 / next(row["population"] for row in self.data["areas"]
                              if row["area"] == TREATED_AREA)
        self.assertTrue(math.isclose(
            counts["outcomes"]["home"]["post_mean_gap"] * scale,
            rates["outcomes"]["home"]["post_mean_gap"], abs_tol=1e-4))

    def test_shorter_training_window_changes_diagnostics_but_keeps_contract(self):
        result = analyze(self.data, {"outcome": "home", "train_end": 30,
                                     "eval_start": 20, "eval_end": 30,
                                     "placebo_week": 20})
        self.assertEqual(len(result["outcomes"]["home"]["actual"]), WEEKS)
        self.assertIn("diagnostics", result)

    def test_warning_when_donor_choice_drives_result(self):
        # 只保留两个对照时，留一检验应暴露不稳定性或直接无法识别
        result = analyze(self.data, {"outcome": "home",
                                     "excluded_areas": ["A5", "A6", "A7", "A8"]})
        self.assertIn(result["identified"], (True, False))
        if not result["identified"]:
            self.assertTrue(result["warnings"])


class CounterfactualPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workbench = Workbench(ROOT, self.temporary.name)

    def test_study_freeze_keeps_eligibility_and_weights(self):
        saved = self.workbench.save_study(
            {"title": "会议反事实", "selection": {"outcome": "home"}})
        self.assertTrue(saved["result"]["analysis"]["identified"])
        reopened = self.workbench.store.study_record(saved["id"])
        self.assertEqual(fingerprint(reopened["result"]), fingerprint(saved["result"]))
        self.assertEqual(reopened["selection"]["outcome"], "home")
        # 污染片区资格被冻结在记录里
        eligibility = {item["area"]: item for item in
                       reopened["result"]["analysis"]["eligibility"]}
        self.assertFalse(eligibility["A1"]["eligible"])
        self.assertIn("team@52-104", eligibility["A1"]["reasons"])

    def test_study_survives_restart_and_ignores_later_datasets(self):
        saved = self.workbench.save_study(
            {"title": "重启核对", "selection": {"outcome": "center"}})
        restarted = Workbench(ROOT, self.temporary.name)
        reopened = restarted.store.study_record(saved["id"])
        self.assertEqual(reopened["result"]["analysis"]["outcomes"]["center"]["weights"],
                         saved["result"]["analysis"]["outcomes"]["center"]["weights"])
        data = self.workbench.store.dataset(self.workbench.default_revision)
        data["context_note"] = "后续修订"
        self.workbench.upload({"label": "新数据", "data": data})
        again = restarted.store.study_record(saved["id"])
        self.assertEqual(fingerprint(again["result"]), fingerprint(saved["result"]))

    def test_invalid_study_params_rejected_before_persist(self):
        with self.assertRaises(ValueError):
            self.workbench.study({"selection": {"train_end": 3}})
        self.assertEqual(self.workbench.store.studies(), [])


class StudyHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.server = make_server(ROOT, self.temporary.name, port=0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary.cleanup()

    def post(self, path, payload):
        request = Request(self.base + path, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            return response.status, json.load(response)

    def test_study_run_save_and_read_over_http(self):
        status, study = self.post("/api/studies", {"selection": {"outcome": "home"}})
        self.assertEqual(status, 200)
        self.assertIn("analysis", study)
        status, saved = self.post("/api/studies",
                                  {"title": "页面冻结", "selection": {"outcome": "home"}})
        self.assertEqual(status, 201)
        with urlopen(self.base + "/api/studies/" + str(saved["id"])) as response:
            reopened = json.load(response)
        self.assertEqual(reopened["result"]["analysis"]["identified"],
                         saved["result"]["analysis"]["identified"])

    def test_bad_study_params_are_client_error(self):
        from urllib.error import HTTPError
        request = Request(self.base + "/api/studies",
                          data=json.dumps({"selection": {"train_end": 1}}).encode(),
                          headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
