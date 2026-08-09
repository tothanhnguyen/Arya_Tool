"""Offline contract tests for the QA-03B live driver."""

from __future__ import annotations

import importlib
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from laplace.ops import qa03b_smoke as qa


class _OfflineClient:
    def __init__(
        self,
        handler: Callable[[object], object] | None = None,
    ) -> None:
        self.requests: list[object] = []
        self._handler = handler

    def send(self, request: object) -> object:
        self.requests.append(request)
        if self._handler is None:
            raise AssertionError("unexpected injected client call")
        return self._handler(request)


def _load_driver_module():
    return importlib.import_module("laplace.ops.qa03b_live_driver")


def _driver(
    *,
    auth: _OfflineClient | None = None,
    application: _OfflineClient | None = None,
    postgrest: _OfflineClient | None = None,
    storage: _OfflineClient | None = None,
    probe: _OfflineClient | None = None,
):
    live = _load_driver_module()
    clients = {
        "auth_client": auth or _OfflineClient(),
        "application_client": application or _OfflineClient(),
        "postgrest_client": postgrest or _OfflineClient(),
        "storage_client": storage or _OfflineClient(),
        "probe_client": probe or _OfflineClient(),
    }
    return live.LiveSmokeDriver(
        **clients,
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
        random_bytes=lambda size: b"x" * size,
    ), tuple(clients.values())


def _tagger() -> qa.OpaqueTagger:
    return qa.OpaqueTagger(bytearray(b"k" * 32))


def _empty_ledger(ledger: qa.FixtureLedger) -> bool:
    return all(count == 0 for _inventory_class, count in ledger.manifest().counts)


def test_import_and_construction_have_no_client_or_socket_side_effects(monkeypatch):
    def block_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("live driver attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", block_network)
    live = _load_driver_module()
    importlib.reload(live)

    driver, clients = _driver()

    assert isinstance(driver, live.LiveSmokeDriver)
    assert all(client.requests == [] for client in clients)


def test_live_driver_errors_expose_only_a_stable_code():
    live = _load_driver_module()

    for code in live.LiveDriverCode:
        error = live.LiveDriverError(code)
        rendered = f"{error!s} {error!r}"

        assert error.code is code
        assert code.value in rendered
        assert re.fullmatch(r"[a-z][a-z0-9_]*", str(error))
        assert "ClientResponse" not in rendered
        assert "body" not in rendered
        assert "request" not in rendered


@pytest.mark.parametrize("status", [200, 409])
def test_ambiguous_or_conflicting_first_create_never_registers_a_target(status):
    live = _load_driver_module()
    auth = _OfflineClient(lambda _request: live.ClientResponse(status=status, body={}))
    driver, _clients = _driver(auth=auth)
    ledger = qa.FixtureLedger()
    tagger = _tagger()

    with pytest.raises(live.LiveDriverError) as caught:
        driver.run_initial_flow(
            marker="opaque-marker-123456",
            ledger=ledger,
            tagger=tagger,
        )

    assert isinstance(caught.value.code, live.LiveDriverCode)
    assert _empty_ledger(ledger)
    assert len(auth.requests) == 1


def test_malformed_success_is_mapped_without_retaining_the_response_body():
    live = _load_driver_module()
    raw_value = "raw-external-value-must-not-escape"
    auth = _OfflineClient(
        lambda _request: live.ClientResponse(
            status=201,
            body={"unexpected": raw_value},
            headers={"x-opaque": raw_value},
        )
    )
    driver, _clients = _driver(auth=auth)
    ledger = qa.FixtureLedger()

    with pytest.raises(live.LiveDriverError) as caught:
        driver.run_initial_flow(
            marker="opaque-marker-123456",
            ledger=ledger,
            tagger=_tagger(),
        )

    assert raw_value not in str(caught.value)
    assert raw_value not in repr(caught.value)
    assert raw_value not in repr(driver)
    assert _empty_ledger(ledger)


def test_fatal_interruption_is_not_converted_to_an_external_error():
    def interrupt(_request: object) -> object:
        raise KeyboardInterrupt

    auth = _OfflineClient(interrupt)
    driver, _clients = _driver(auth=auth)
    ledger = qa.FixtureLedger()

    with pytest.raises(KeyboardInterrupt):
        driver.run_initial_flow(
            marker="opaque-marker-123456",
            ledger=ledger,
            tagger=_tagger(),
        )

    assert _empty_ledger(ledger)


def test_unexpected_content_generation_is_owned_for_cleanup_but_blocks_manifest():
    ledger = qa.FixtureLedger()
    social_post = qa.ExactCleanupTarget(
        inventory_class=qa.InventoryClass.SOCIAL_POST,
        resource=qa.CleanupResource.SOCIAL_POST,
        selector=qa.CleanupSelector.ID_EQ,
        values=(103,),
    )
    unexpected_generation = qa.ExactCleanupTarget(
        inventory_class=qa.InventoryClass.CONTENT_GENERATION_UNEXPECTED,
        resource=qa.CleanupResource.CONTENT_GENERATION,
        selector=qa.CleanupSelector.ID_EQ,
        values=(102,),
    )
    publish_job = qa.ExactCleanupTarget(
        inventory_class=qa.InventoryClass.PUBLISH_JOB_HAPPY,
        resource=qa.CleanupResource.PUBLISH_JOB,
        selector=qa.CleanupSelector.ID_EQ,
        values=(101,),
    )
    for target in (social_post, unexpected_generation, publish_job):
        ledger.register(target)

    assert not ledger.manifest("initial").complete
    assert ledger.cleanup_plan() == (
        publish_job,
        unexpected_generation,
        social_post,
    )
