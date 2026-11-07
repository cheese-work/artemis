"""Listener/stream ownership shared by B2 and the lease-scoped R1 tunnel."""

import asyncio


class StreamOwner:
    def __init__(self):
        self.clients = {}
        self.client_streams = {}
        self.tasks = set()
        self.listener = None

    async def close_streams(self):
        if self.listener is not None:
            self.listener.close()
            await self.listener.wait_closed()
        current = asyncio.current_task()
        tasks = [task for task in self.tasks if task is not current]
        for task in tasks:
            task.cancel()
        writers = list(self.clients)
        for writer in writers:
            writer.close()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        outcomes += await asyncio.gather(
            *(writer.wait_closed() for writer in writers), return_exceptions=True
        )
        return all(
            not isinstance(outcome, BaseException) or isinstance(outcome, asyncio.CancelledError)
            for outcome in outcomes
        )
