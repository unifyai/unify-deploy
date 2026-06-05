__all__ = ["router"]


def __getattr__(name):
    if name == "router":
        from communication.infra.views import router

        return router
    raise AttributeError(name)
