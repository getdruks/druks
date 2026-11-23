import pytest
from druks import testing


def test_a_worker_names_its_database_apart_from_every_other_worker(monkeypatch):
    monkeypatch.setattr(testing, "_WORKER", "gw3")

    url = testing._worker_database_url("postgresql+psycopg://druks:druks@db:5432/druks_test")

    assert url.database == "druks_test_gw3"
    assert url.host == "db"


def test_a_single_process_run_keeps_the_database_it_was_given(monkeypatch):
    monkeypatch.setattr(testing, "_WORKER", "")

    url = testing._worker_database_url("postgresql+psycopg://druks:druks@db:5432/druks_test")

    assert url.database == "druks_test"
    assert testing._worker_redis_url("redis://cache:6379/15") == "redis://cache:6379/15"


def test_a_worker_takes_the_redis_index_that_counts_down_from_its_number(monkeypatch):
    monkeypatch.setattr(testing, "_WORKER", "gw2")

    assert testing._worker_redis_url("redis://cache:6379/15") == "redis://cache:6379/13"


@pytest.mark.parametrize(("worker", "path"), [("gw15", "/15"), ("gw1", "/0"), ("gw4", "/3")])
def test_a_worker_never_takes_the_applications_redis_index(monkeypatch, worker, path):
    monkeypatch.setattr(testing, "_WORKER", worker)

    with pytest.raises(RuntimeError, match=f"{worker} has no Redis index left"):
        testing._worker_redis_url(f"redis://cache:6379{path}")
