import asyncio
import time
import unittest

from sprockets_statsd import statsd

from tests import helpers


class ProcessorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.test_timeout = 5.0

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.statsd_server = helpers.StatsdServer()
        self.statsd_task = asyncio.create_task(self.statsd_server.run())
        await self.statsd_server.wait_running()

    async def asyncTearDown(self):
        self.statsd_task.cancel()
        await self.statsd_server.wait_closed()
        await super().asyncTearDown()

    async def wait_for(self, fut):
        try:
            await asyncio.wait_for(fut, timeout=self.test_timeout)
        except asyncio.TimeoutError:
            self.fail('future took too long to resolve')

    async def test_that_processor_connects_and_disconnects(self):
        processor = statsd.Processor(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        asyncio.create_task(processor.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())
        await self.wait_for(processor.stop())

        self.assertEqual(1, self.statsd_server.connections_made)
        self.assertEqual(1, self.statsd_server.connections_lost)

    async def test_that_processor_reconnects(self):
        processor = statsd.Processor(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        asyncio.create_task(processor.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())

        # Now that the server is running and the client has connected,
        # cancel the server and let it die off.
        self.statsd_server.close()
        await self.statsd_server.wait_closed()
        until = time.time() + self.test_timeout
        while processor.connected.is_set():
            await asyncio.sleep(0.1)
            if time.time() >= until:
                self.fail('processor never disconnected')

        # Start the server on the same port and let the client reconnect.
        self.statsd_task = asyncio.create_task(self.statsd_server.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())
        self.assertTrue(processor.connected.is_set())

        await self.wait_for(processor.stop())

    async def test_that_processor_can_be_cancelled(self):
        processor = statsd.Processor(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        task = asyncio.create_task(processor.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())

        task.cancel()
        await self.wait_for(processor.closed.wait())

    async def test_shutdown_when_disconnected(self):
        processor = statsd.Processor(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        asyncio.create_task(processor.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())

        self.statsd_server.close()
        await self.statsd_server.wait_closed()

        await self.wait_for(processor.stop())

    async def test_socket_resets(self):
        processor = statsd.Processor(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        asyncio.create_task(processor.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())

        self.statsd_server.transports[0].close()
        await self.wait_for(self.statsd_server.client_connected.acquire())

    async def test_connection_failures(self):
        processor = statsd.Processor(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        asyncio.create_task(processor.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())

        # Change the port and close the transport, this will cause the
        # processor to reconnect to the new port and fail.
        processor.port = 1
        processor.transport.close()

        # Wait for the processor to be disconnected, then change the
        # port back and let the processor reconnect.
        while processor.connected.is_set():
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)
        processor.port = self.statsd_server.port

        await self.wait_for(self.statsd_server.client_connected.acquire())
