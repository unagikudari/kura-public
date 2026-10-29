"""Keep owned background work alive until its resources can be released."""
import asyncio


async def await_owned(task):
    """Keep a worker's resources alive, even on repeated cancellation."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # consume a worker failure without losing cancellation
        raise

