import pytest

from laplace import db as db_module
from laplace.db import Base, reset_engine_for_tests
from laplace.tools import base as tools_base


@pytest.fixture(autouse=True)
def _registry_isolation():
    """Snapshot/restore tool registry de test nay khong pha registry cua test khac."""
    saved = dict(tools_base._REGISTRY)
    yield
    tools_base._REGISTRY.clear()
    tools_base._REGISTRY.update(saved)


@pytest.fixture()
def session(tmp_path):
    """Moi test mot SQLite file rieng, schema tao moi."""
    url = f"sqlite:///{tmp_path}/test.db"
    reset_engine_for_tests(url)
    import laplace.models  # noqa: F401

    Base.metadata.create_all(db_module._engine)
    with db_module.session_scope() as s:
        yield s
