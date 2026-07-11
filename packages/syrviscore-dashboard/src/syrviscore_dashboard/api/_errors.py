"""Map framework-agnostic docker_util exceptions to HTTP responses."""

from fastapi import HTTPException

from ..docker_util import ContainerNotFound, DockerUnavailable, InvalidName, NotManaged


def as_http(exc: Exception) -> HTTPException:
    if isinstance(exc, InvalidName):
        return HTTPException(status_code=400, detail="invalid service name")
    if isinstance(exc, ContainerNotFound):
        return HTTPException(status_code=404, detail="container not found: {}".format(exc))
    if isinstance(exc, NotManaged):
        return HTTPException(
            status_code=403, detail="not a SyrvisCore-managed container: {}".format(exc)
        )
    if isinstance(exc, DockerUnavailable):
        return HTTPException(status_code=503, detail="docker unavailable: {}".format(exc))
    return HTTPException(status_code=500, detail=str(exc))
