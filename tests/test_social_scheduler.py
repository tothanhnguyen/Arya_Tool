"""Tests for the isolated social publish scheduler."""

from laplace.social import scheduler as scheduler_module
from laplace.social.publishers import MockPublisher


class FakeWorker:
    def __init__(self):
        self.calls = 0

    def process_due(self):
        self.calls += 1
        return []


def test_publish_tick_is_noop_before_scheduler_starts():
    scheduler_module.stop_social_scheduler()

    assert scheduler_module.run_publish_tick() == []


def test_publish_tick_uses_configured_worker(monkeypatch):
    worker = FakeWorker()
    monkeypatch.setattr(scheduler_module, "_worker", worker)

    assert scheduler_module.run_publish_tick() == []
    assert worker.calls == 1

    scheduler_module.stop_social_scheduler()


def test_social_scheduler_starts_once_and_stops(session):
    scheduler_module.stop_social_scheduler()
    session.commit()

    first = scheduler_module.start_social_scheduler(
        publisher=MockPublisher(), interval_seconds=3600
    )
    second = scheduler_module.start_social_scheduler(
        publisher=MockPublisher(), interval_seconds=3600
    )

    assert second is first
    assert scheduler_module.is_social_scheduler_running()
    assert [job.id for job in first.get_jobs()] == ["social_publish_worker"]

    scheduler_module.stop_social_scheduler()
    assert not scheduler_module.is_social_scheduler_running()


def test_social_scheduler_rejects_invalid_interval():
    scheduler_module.stop_social_scheduler()

    try:
        scheduler_module.start_social_scheduler(
            publisher=MockPublisher(), interval_seconds=-1
        )
    except ValueError as exc:
        assert "at least 1 second" in str(exc)
    else:
        raise AssertionError("invalid interval was accepted")
