from bo.mapping import build_name_rules, find_unmapped, universe_key
from bo.rls import check_user, pick_column
from bo.sqltools import compare_structure, extract_sql, search, split_top_level, summarize
from bo.text import norm


def test_norm_is_accent_and_space_insensitive():
    assert norm("  Crème\xa0 Brûlée ") == "creme brulee"
    assert norm("İSTANBUL") == norm("istanbul")
    assert norm("Café") == "cafe"


def test_split_ignores_separators_inside_parentheses_and_words():
    assert split_top_level("a, f(b, c), d", [","]) == ["a", "f(b, c)", "d"]
    assert split_top_level("BRAND = 1 AND (x OR y)", [" AND ", " OR "]) == ["BRAND = 1", "(x OR y)"]


def test_summarize_counts_structure():
    sql = "SELECT a, b, SUM(c) FROM t JOIN u ON t.id = u.id WHERE a = 1 AND (b = 2 OR b = 3) GROUP BY a, b"
    s = summarize(sql)
    assert s["select_cols"] == 3 and s["where_conditions"] == 2 and s["join_count"] == 1 and s["has_where"]


def test_compare_ignores_renamed_tables_but_flags_lost_filter():
    old = "SELECT a, b FROM old_t WHERE a = 1 AND b = 2"
    renamed = "SELECT x, y FROM new_t WHERE x = 1 AND y = 2"
    lost = "SELECT x, y FROM new_t WHERE x = 1"
    assert compare_structure(old, renamed)[0] is True
    same, msg = compare_structure(old, lost)
    assert same is False and "WHERE conditions 2 → 1" in msg
    assert compare_structure(None, lost)[0] is None


def test_extract_sql_from_json_and_xml():
    assert extract_sql('{"specification": {"query": "SELECT a FROM t"}}') == "SELECT a FROM t"
    xml = "<spec><query><![CDATA[SELECT a FROM t WHERE b &lt; 2]]></query></spec>"
    assert extract_sql(xml).startswith("SELECT a FROM t")
    assert extract_sql('{"nothing": 1}') is None


def test_search_phrases_and_modes():
    results = [{"name": "Orders", "dps": [{"name": "Q1", "sql": "SELECT customer_id,\norder_date FROM orders"}]},
               {"name": "Stock", "dps": [{"name": "Q1", "sql": "SELECT item FROM stock"}]}]
    terms, hits = search(results, '"customer id" order_date')
    assert terms == ["customer id", "order", "date"] and len(hits) == 1 and hits[0]["report"] == "Orders"
    _, any_hits = search(results, "item customer", mode="any")
    assert len(any_hits) == 2


def test_universe_key_and_preferred_rules():
    assert universe_key("Sales.unx (ID: 5)") == "sales"
    rules = build_name_rules({"Sales": "1", "sales ": "2", "Finance": "3"}, {"Sales": "2"})
    assert rules["sales"] == [("sales ", "2")]
    assert rules["finance"] == [("Finance", "3")]


def test_find_unmapped_splits_blocking_and_ambiguous():
    mapping = {"mappings": {"content": {"mapping": [
        {"@status": "Ok", "source": {"id": "a"}, "target": {"id": "a2"}},
        {"@status": "Ambiguous", "source": {"name": "Customer"}, "target": {"id": "c2"}},
        {"@status": "NotFound", "source": {"name": "Margin"}, "target": None},
    ]}}}
    blocking, ambiguous = find_unmapped(mapping)
    assert blocking == ["Margin [NotFound]"]
    assert ambiguous == ["Customer → c2 [Ambiguous]"]


def test_pick_column_skips_id_columns():
    cols = ["Brand Code", "Brand Name", "Region"]
    assert pick_column(cols, ["Brand"]) == "Brand Name"
    assert pick_column(["Brand Id"], ["Brand Id"]) == "Brand Id"  # last resort: exact match incl. ids
    assert pick_column(cols, ["Country"]) is None


def test_check_user_allow_and_deny():
    rows = [{"Brand": "Alpha One", "Region": "South"}, {"Brand": "Beta", "Region": "North East"}]
    rule = {"brand": {"allow": ["alpha"], "deny": []}, "region": {"allow": [], "deny": ["north"]}}
    violations, used = check_user(rows, ["Brand", "Region"], rule, {"brand": ["Brand"], "region": ["Region"]})
    assert used == {"brand": "Brand", "region": "Region"}
    assert len(violations) == 2
    assert any("BRAND 'Beta' not allowed" in v for v in violations)
    assert any("REGION 'North East' is denied" in v for v in violations)
