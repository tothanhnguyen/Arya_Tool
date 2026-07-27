"""Social affiliate dashboard.

The module is intentionally self-contained so it can be mounted on the main
application when the social domain is ready, while remaining easy to test in
isolation.
"""

from laplace.web.social.views import router

__all__ = ["router"]
