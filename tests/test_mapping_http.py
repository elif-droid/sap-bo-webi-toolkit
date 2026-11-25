"""convert_document against a small fake BO server (no network)."""

from unittest import mock

import pytest

from bo import mapping
from bo.mapping import ALREADY_CONVERTED, convert_document

SERVER = "https://bo.test/biprws"


class Resp:
    def __init__(self, status=200, data=None, text=None):
        self.status_code, self._data = status, data
        self.text = text if text is not None else ("" if data is None else str(data))
        self.headers = {}

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


class FakeBO:
    """Two data providers: DP1 on universe 10, DP2 on universe 20."""

    def __init__(self, ambiguous=False, missing=False):
        self.source = {"DP1": "10", "DP2": "20"}
        self.ambiguous, self.missing = ambiguous, missing

    def get(self, url, params=None, **_):
        doc = f"{SERVER}/raylight/v1/documents/1"
        if url == f"{doc}/dataproviders":
            return Resp(data={"dataproviders": {"dataprovider": [
                {"id": k, "name": k, "dataSourceId": v, "dataSourceType": "unv"} for k, v in self.source.items()]}})
        if url == f"{doc}/dataproviders/mappings":
            status = "Ambiguous" if self.ambiguous else "Ok"
            target = None if self.missing else {"id": "x"}
            return Resp(data={"mappings": {"content": {"mapping": [
                {"@status": status if not self.missing else "NotFound", "source": {"name": "Obj"}, "target": target}]}}})
        if url.endswith("/specification"):
            return Resp(text="SELECT a, b FROM t WHERE a = 1")
        for dp in self.source:
            if url == f"{doc}/dataproviders/{dp}":
                return Resp(data={"dataprovider": {"dataSourceId": self.source[dp]}})
        return Resp(data={})

    def post(self, url, params=None, **_):
        if url.endswith("/dataproviders/mappings"):
            self.source[params["originDataproviderIds"]] = str(params["targetDatasourceId"])
            return Resp(data={"success": {}})
        return Resp(data={})


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        bo = FakeBO(**kw)
        monkeypatch.setattr("requests.get", bo.get)
        monkeypatch.setattr("requests.post", bo.post)
        return bo
    return install


def test_apply_moves_every_query_and_checks_sql(fake):
    bo = fake()
    r = convert_document(SERVER, {}, 1, target_id=99, apply=True)
    assert r.error is None and r.applied == 2 and bo.source == {"DP1": "99", "DP2": "99"}
    assert r.old_map == {"DP1": "10", "DP2": "20"}
    assert "2/2 queries consistent" in r.sql_summary and not r.sql_changed


def test_dry_run_changes_nothing(fake):
    bo = fake()
    r = convert_document(SERVER, {}, 1, target_id=99, apply=False)
    assert r.applied == 2 and bo.source == {"DP1": "10", "DP2": "20"}


def test_blocking_object_stops_the_report(fake):
    fake(missing=True)
    r = convert_document(SERVER, {}, 1, target_id=99)
    assert r.error and "no match" in r.error and r.applied == 0


def test_ambiguous_is_applied_but_flagged(fake):
    fake(ambiguous=True)
    r = convert_document(SERVER, {}, 1, target_id=99)
    assert r.error is None and r.applied == 2 and len(r.ambiguous) == 2


def test_already_converted(fake):
    bo = fake()
    bo.source = {"DP1": "99", "DP2": "99"}
    assert convert_document(SERVER, {}, 1, target_id=99).error == ALREADY_CONVERTED


def test_resolver_partial_conversion_records_pending(fake):
    bo = fake()

    def resolver(dp):
        return ("99", "matched", None) if dp["id"] == "DP1" else (None, "no target", "Finance")

    r = convert_document(SERVER, {}, 1, apply=True, resolver=resolver)
    assert r.applied == 1 and bo.source["DP2"] == "20"
    assert r.pending == [("NAME:finance", "Finance")] and len(r.skipped) == 1


def test_name_resolver_uses_overrides_then_names():
    with mock.patch.object(mapping, "source_universe_name", side_effect=[("Sales.unv", "dp"), ("Old", "dp")]):
        res = mapping.make_name_resolver(SERVER, {}, {"sales": "500"}, {"7": "700"})
        assert res({"dataSourceId": "7"})[0] == "700"
        assert res({"dataSourceId": "1"})[0] == "500"
        target, _, pending = res({"dataSourceId": "2"})
        assert target is None and pending == "Old"
