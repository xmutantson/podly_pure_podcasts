import functools
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class TimeoutException(Exception):
    """A completed operation exceeded its soft time budget."""


def timeout_decorator(timeout: int) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Report overruns after completion; never abandon a worker thread.

    Running in the caller preserves the executor concurrency limit and prevents
    retries racing a timed-out worker's file/database writes. This soft deadline
    cannot interrupt a stuck operation; network calls need their own timeouts.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            start = time.monotonic()
            result = func(*args, **kwargs)
            if time.monotonic() - start > timeout:
                raise TimeoutException(
                    f"Function '{func.__name__}' completed after exceeding "
                    f"its soft timeout of {timeout} seconds."
                )
            return result

        return wrapper

    return decorator
