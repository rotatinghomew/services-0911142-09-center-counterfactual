from pathlib import Path
import json

from .analysis import analyze
from .domain import explore, validate
from .storage import Store, fingerprint


class Workbench:
    def __init__(self, root, state):
        self.root = Path(root)
        self.product = json.loads((self.root / "product.json").read_text(encoding="utf-8"))
        self.store = Store(Path(state) / "research.sqlite3")
        initial = json.loads((self.root / "fixtures" / "data.json").read_text(encoding="utf-8"))
        validate(initial)
        self.default_revision = self.store.put_dataset("演示材料", initial)

    def context(self):
        return {"product": self.product, "default_revision": self.default_revision,
                "datasets": self.store.datasets(), "notebooks": self.store.notebooks(),
                "studies": self.store.studies()}

    def upload(self, body):
        data = body["data"]
        try:
            validate(data)
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("字段结构不完整或类型不符") from error
        return {"revision": self.store.put_dataset(body.get("label", ""), data)}

    def inspect(self, body):
        revision = body.get("revision", self.default_revision)
        selection = body.get("selection", self.product["selection"])
        if not isinstance(selection, dict):
            raise ValueError("选择条件必须是对象")
        data = self.store.dataset(revision)
        result = explore(data, selection)
        return {"revision": revision, "selection": selection, "data_sha256": fingerprint(data), "view": result}

    def study(self, body):
        revision = body.get("revision", self.default_revision)
        selection = body.get("selection", {})
        if not isinstance(selection, dict):
            raise ValueError("选择条件必须是对象")
        data = self.store.dataset(revision)
        # 描述性浏览所需的 outcome/donor_scope 与建模参数合并，服务端统一口径
        merged = dict(self.product["selection"])
        merged.update(selection)
        analysis_result = analyze(data, merged)
        return {"revision": revision, "selection": analysis_result["params"],
                "data_sha256": fingerprint(data), "analysis": analysis_result}

    def save_study(self, body):
        result = self.study(body)
        return self.store.save_study(body.get("title", ""), result["revision"],
                                     result["selection"], result)

    def save(self, body):
        result = self.inspect(body)
        return self.store.save(body.get("title", ""), result["revision"], result["selection"], result)
