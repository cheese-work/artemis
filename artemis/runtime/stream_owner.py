"""Listener/stream ownership shared by B2 and the lease-scoped R1 tunnel."""

import asyncio

CLOSE_SECONDS = 5


class StreamOwner:
    def __init__(self):
        self.clients = {}
        self.client_streams = {}
        self.tasks = set()
        self.listener = None

    async def close_streams(self):
        if self.listener is not None:
            self.listener.close()
        current = asyncio.current_task()
        tasks = [task for task in self.tasks if task is not current]
        for task in tasks:
            task.cancel()
        writers = list(self.clients)
        for writer in writers:
            writer.close()

        async def wait():
            if self.listener is not None:
                await self.listener.wait_closed()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            outcomes += await asyncio.gather(
                *(writer.wait_closed() for writer in writers), return_exceptions=True
            )
            return all(
                not isinstance(outcome, BaseException)
                or isinstance(outcome, asyncio.CancelledError)
                for outcome in outcomes
            )

        try:
            return await asyncio.wait_for(wait(), CLOSE_SECONDS)
        except TimeoutError:
            for writer in writers:
                if getattr(writer, "transport", None) is not None:
                    writer.transport.abort()
            return False
