"""Test recover_orphan_tasks: don task mo coi khi app khoi dong."""

from laplace.services.tasks import create_task, get_or_create_user, recover_orphan_tasks


def test_orphan_running_and_pending_marked_failed(session):
    user = get_or_create_user(session)
    running = create_task(session, user_id=user.id, request="dang chay do")
    running.status = "running"
    pending = create_task(session, user_id=user.id, request="chua chay")  # pending
    awaiting = create_task(session, user_id=user.id, request="cho confirm")
    awaiting.status = "awaiting_confirm"
    done = create_task(session, user_id=user.id, request="xong roi")
    done.status = "done"
    session.commit()

    n = recover_orphan_tasks(session)
    session.commit()
    session.expire_all()

    assert n == 2
    assert running.status == "failed"
    assert "interrupted" in (running.error or "")
    assert running.finished_at is not None
    assert pending.status == "failed"
    # awaiting_confirm resume duoc theo thiet ke -> khong dong
    assert awaiting.status == "awaiting_confirm"
    assert done.status == "done"


def test_recover_noop_when_clean(session):
    user = get_or_create_user(session)
    t = create_task(session, user_id=user.id, request="x")
    t.status = "done"
    session.commit()
    assert recover_orphan_tasks(session) == 0
