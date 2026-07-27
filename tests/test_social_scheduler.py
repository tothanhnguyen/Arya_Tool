"""Tests for the isolated social publish scheduler."""

from laplace.social import scheduler as scheduler_module
from laplace.social.publishers import MockPublisher
from laplace.social.worker import WorkerOutcome


class FakeWorker:
    def __init__(self, outcomes=None):
        self.calls = 0
        self.outcomes = outcomes or []

    def process_due(self):
        self.calls += 1
        return self.outcomes


class FakeNotifier:
    def __init__(self):
        self.outcome_calls = []
        self.daily_calls = 0

    def notify_outcomes(self, outcomes):
        self.outcome_calls.append(outcomes)
        return len(outcomes)

    def send_daily_summaries(self):
        self.daily_calls += 1
        return 1


def test_publish_tick_is_noop_before_scheduler_starts():
    scheduler_module.stop_social_scheduler()

    assert scheduler_module.run_publish_tick() == []


def test_publish_tick_uses_configured_worker(monkeypatch):
    outcomes = [WorkerOutcome(1, "published", True)]
    worker = FakeWorker(outcomes)
    notifier = FakeNotifier()
    monkeypatch.setattr(scheduler_module, "_worker", worker)
    monkeypatch.setattr(scheduler_module, "_notifier", notifier)

    assert scheduler_module.run_publish_tick() == outcomes
    assert worker.calls == 1
    assert notifier.outcome_calls == [outcomes]
    assert scheduler_module.run_daily_summary() == 1
    assert notifier.daily_calls == 1

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
    assert {job.id for job in first.get_jobs()} == {
        "social_publish_worker",
        "social_daily_summary",
    }

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


def test_social_scheduler_rejects_invalid_daily_summary_hour():
    scheduler_module.stop_social_scheduler()

    try:
        scheduler_module.start_social_scheduler(
            publisher=MockPublisher(),
            interval_seconds=3600,
            daily_summary_hour=24,
        )
    except ValueError as exc:
        assert "between 0 and 23" in str(exc)
    else:
        raise AssertionError("invalid daily summary hour was accepted")
