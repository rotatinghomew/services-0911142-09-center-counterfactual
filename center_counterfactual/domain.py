from .validation import require_tables, unique, finite, references


SCHEMA = {'areas': {'area': ['str'], 'treated': ['boolean'], 'population': ['number'], 'shared_team': ['boolean']}, 'contacts': {'contact': ['str'], 'resident_area': ['str'], 'service_area': ['str'], 'household': ['str'], 'week': ['number'], 'channel': ['str'], 'reported_at': ['number'], 'source_key': ['str']}, 'events': {'event': ['str'], 'area': ['str'], 'week': ['number'], 'kind': ['str']}, 'sharing': {'sharing': ['str'], 'from_area': ['str'], 'to_area': ['str'], 'start': ['number'], 'stop': ['number'], 'kind': ['str']}}
OPTIONS = {'outcome': ['center', 'home', 'total'], 'donor_scope': ['all', 'unshared']}


def validate_structure(data, schema):
    require_tables(data, list(schema))
    for table, fields in schema.items():
        for row in data[table]:
            if not isinstance(row, dict) or any(field not in row for field in fields):
                raise ValueError("表字段缺失：" + table)
            for field, kinds in fields.items():
                value = row[field]
                kind = "null" if value is None else ("boolean" if isinstance(value,bool) else ("number" if isinstance(value,(int,float)) else type(value).__name__))
                if kind not in kinds:
                    raise ValueError("字段类型错误：" + table + "." + field)
                if kind == "number":
                    finite(value)


def validate(data):
    validate_structure(data, SCHEMA)
    unique(data['areas'], ['area'])
    unique(data['contacts'], ['contact'])
    unique(data['events'], ['event'])
    unique(data['sharing'], ['sharing'])
    references(data['contacts'], 'resident_area', data['areas'], 'area')
    references(data['contacts'], 'service_area', data['areas'], 'area')
    references(data['events'], 'area', data['areas'], 'area')
    for row in data["contacts"]:
        if row["channel"] not in {"home","center"}:
            raise ValueError("服务渠道无效")
        finite(row["week"], minimum=0)
    for row in data["sharing"]:
        if row["stop"] <= row["start"]:
            raise ValueError("共享服务区间无效")


def view(rows, measure, note):
    return {"summary":{"明细条数":len(rows),"有数值记录":sum(isinstance(row.get(measure),(int,float)) for row in rows)},
            "rows":rows,"series":[{"label":str(position+1),"y":row.get(measure)} for position,row in enumerate(rows)],
            "notes":[note]}


def explore(data, selection):
    for key, choices in OPTIONS.items():
        if selection.get(key) not in choices:
            raise ValueError("选择范围无效：" + key)
    areas = {row["area"] for row in data["areas"] if selection["donor_scope"] == "all" or not row["shared_team"] or row["treated"]}
    rows = []
    for area in sorted(areas):
        for week in range(104):
            records = [row for row in data["contacts"] if row["resident_area"] == area and row["week"] == week and (selection["outcome"] == "total" or row["channel"] == selection["outcome"])]
            rows.append({"area":area,"week":week,"reported_contacts":len(records),"households":len({row["household"] for row in records})})
    return view(rows, "reported_contacts", "当前为原始上报次数，包含重报，不能当作受益人数或中心开放效果。")
