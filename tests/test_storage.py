"""Tests for the result IDs and for saving and loading results."""
import dataclasses
import getpass
import json
import platform
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from src.exceptions import ResultStorageError
from src.results import make_result, new_result_id
from src.settings import AmmeterSettings, ConnectionSettings
from src.storage import ResultStore, to_dict
from tests.conftest import FakeAmmeter, FakeClock, result_of


def fake_result(name="greenlee", fail_at=(), batch_id=None):
    return result_of(FakeAmmeter(FakeClock(), name=name, cost=0.003, fail_at=fail_at),
                     count=10, max_failures=3, batch_id=batch_id)


@pytest.fixture
def store(tmp_path):
    return ResultStore(tmp_path / "runs")


def write(store, name, payload):
    store.directory.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, dict):
        payload = json.dumps(dict(payload, id=name))
    (store.directory / f"{name}.json").write_text(payload, encoding="utf-8")


# IDs

def test_id_format():
    assert re.fullmatch(r"\d{8}T\d{6}Z_greenlee_[0-9a-f]{8}", fake_result().id)


def test_ids_in_the_same_second_are_unique():
    now = datetime.now(timezone.utc)
    assert len({new_result_id("greenlee", now) for _ in range(10_000)}) == 10_000


def test_unsafe_ammeter_names_are_cleaned():
    now = datetime.now(timezone.utc)
    assert new_result_id("my meter/../x", now).split("_")[1] == "my-meter-x"


# Save and load

def test_round_trip(store):
    result = fake_result()
    path = store.save(result)
    loaded = store.load(result.id)
    assert loaded.run == result.run and loaded.analysis == result.analysis
    assert loaded.metadata == result.metadata and loaded.saved_path == path
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1 and data["analysis"]["current"]["count"] == 10


def test_no_computer_or_user_name_in_the_file(store):
    text = store.save(fake_result()).read_text(encoding="utf-8")
    assert platform.node() not in text and getpass.getuser() not in text


def test_concurrent_saves_leave_no_temporary_files(store):
    results = [fake_result(batch_id="b1") for _ in range(30)]
    errors = []

    def save(result):
        try:
            store.save(result)
        # Catch everything here, so the test can check that nothing went wrong.
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=save, args=(r,)) for r in results]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(list(store.directory.glob("*.json"))) == 30
    assert not list(store.directory.glob("*.tmp"))


def test_temporary_files_are_random_and_in_the_target_directory(store):
    names = set()
    real_mkstemp = tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        names.add(name)
        return fd, name

    with mock.patch("src.storage.tempfile.mkstemp", side_effect=spy):
        for _ in range(5):
            store.save(fake_result())
    assert len(names) == 5
    assert all(str(store.directory) in name for name in names)


def test_existing_result_is_never_overwritten(store):
    result = fake_result()
    store.save(result)
    with pytest.raises(ResultStorageError, match="already exists"):
        store.save(result)


def test_failed_rename_removes_the_temporary_file(store):
    with mock.patch("src.storage.os.replace", side_effect=OSError(28, "No space left on device")):
        with pytest.raises(ResultStorageError, match="No space left"):
            store.save(fake_result())
    assert not list(store.directory.glob("*.tmp"))


def test_directory_that_cannot_be_created(tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    with pytest.raises(ResultStorageError):
        ResultStore(blocker / "runs").save(fake_result())


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "a/b", "a b"])
def test_path_traversal_ids_are_refused(store, bad_id):
    with pytest.raises(ResultStorageError, match="Invalid result ID"):
        store.load(bad_id)


# Reading broken and older files

@pytest.fixture
def base():
    return to_dict(fake_result("entes"))


def test_unknown_id(store):
    with pytest.raises(ResultStorageError, match="No result"):
        store.load("20200101T000000Z_x_00000000")


def test_corrupt_json(store):
    write(store, "corrupt", "{not json")
    with pytest.raises(ResultStorageError, match="Invalid JSON"):
        store.load("corrupt")


def test_missing_required_field_is_named(store, base):
    del base["run"]["samples"]
    write(store, "nosamples", base)
    with pytest.raises(ResultStorageError, match="run.samples"):
        store.load("nosamples")


def test_newer_format_version_is_refused(store, base):
    write(store, "newer", dict(base, schema_version=99))
    with pytest.raises(ResultStorageError, match="version 99"):
        store.load("newer")


def test_wrong_value_type(store, base):
    base["run"]["samples"][0]["started_s"] = "soon"
    write(store, "wrong", base)
    with pytest.raises(ResultStorageError, match="Invalid result data"):
        store.load("wrong")


def test_older_file_without_optional_fields_loads_with_defaults(store, base):
    for key in ("batch_id", "metadata", "analysis"):
        base.pop(key)
    for key in ("stop_reason", "overruns"):
        base["run"].pop(key)
    for key in ("max_consecutive_failures", "late_tolerance", "max_late_fraction"):
        base["run"]["plan"].pop(key)
    for s in base["run"]["samples"]:
        s.pop("error")
    write(store, "older", base)
    loaded = store.load("older")
    assert loaded.batch_id is None and loaded.metadata == {} and loaded.run.overruns == 0
    assert loaded.run.plan.late_tolerance == 0.5 and loaded.run.plan.max_late_fraction == 0.1
    # The statistics were calculated again from the samples.
    assert loaded.analysis.current.count == 10


def test_statistics_come_from_the_samples_not_the_file(store, base):
    base["analysis"]["current"]["mean"] = 999
    write(store, "tampered", base)
    assert store.load("tampered").analysis.current.mean != 999


def test_file_name_must_match_the_id(store, base):
    store.directory.mkdir(parents=True)
    (store.directory / "renamed.json").write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ResultStorageError, match="does not match"):
        store.load("renamed")


def test_list_skips_unreadable_files_with_a_warning(store, base, caplog):
    write(store, "corrupt", "{not json")
    write(store, "good", base)
    assert [r.id for r in store.list()] == ["good"]
    assert "Skipping unreadable" in caplog.text


# List, filters and latest

def test_list_filters_and_order(store):
    a = fake_result("greenlee", batch_id="B1")
    dead = fake_result("entes", fail_at=range(100), batch_id="B1")
    later = fake_result("greenlee")
    later_run = dataclasses.replace(later.run,
                                    started_at=later.run.started_at + timedelta(hours=1))
    b = make_result(later_run, AmmeterSettings("greenlee", 5000, "CMD"), ConnectionSettings())
    for result in (a, dead, b):
        store.save(result)
    # Newest first.
    assert store.list()[0].id == b.id
    assert {r.id for r in store.list(batch_id="B1")} == {a.id, dead.id}
    assert [r.id for r in store.list(ammeter="entes")] == [dead.id]
    assert [r.id for r in store.list(status="aborted")] == [dead.id]
    assert store.latest("greenlee").id == b.id and store.latest("fluke") is None


def test_since_without_a_time_zone_is_taken_as_utc(store):
    result = fake_result()
    store.save(result)
    naive_past = datetime(2020, 1, 1)
    naive_future = datetime(2100, 1, 1)
    assert [r.id for r in store.list(since=naive_past)] == [result.id]
    assert store.list(since=naive_future) == []


def test_missing_directory_lists_nothing(tmp_path):
    assert ResultStore(tmp_path / "missing").list() == []
