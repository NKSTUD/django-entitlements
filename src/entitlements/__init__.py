"""Plans, quotas and credits for Django. Import the functions from here: `import entitlements`."""

from importlib.metadata import version

__version__ = version("django-entitlements")

_API = {
    "Denied",
    "Status",
    "can",
    "check",
    "consume",
    "counter",
    "define",
    "forget",
    "grant",
    "override",
    "release",
    "report",
    "requires",
    "status",
    "subscribe",
    "subscriptions",
    "unsubscribe",
    "use",
}
__all__ = sorted(_API)


def __getattr__(name):  # lazy, so `import entitlements` works before Django's app registry is ready
    if name in _API:
        from . import api

        return getattr(api, name)
    raise AttributeError(name)
