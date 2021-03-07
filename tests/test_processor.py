import asyncio
import time
import unittest

from sprockets_statsd import statsd

from tests import helpers


class ProcessorTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.test_timeout = 5.0

    async def wait_for(self, fut):
        try:
            await asyncio.wait_for(fut, timeout=self.test_timeout)
        except asyncio.TimeoutError:
            self.fail('future took too long to resolve')

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.statsd_server = helpers.StatsdServer()
        self.statsd_task = asyncio.create_task(self.statsd_server.run())
        await self.statsd_server.wait_running()

    async def asyncTearDown(self):
        self.statsd_task.cancel()
        await self.statsd_server.wait_closed()
        await super().asyncTearDown()


class ProcessorTests(ProcessorTestCase):
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


class MetricProcessingTests(ProcessorTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.processor = statsd.Processor(host=self.statsd_server.host,
                                          port=self.statsd_server.port)
        asyncio.create_task(self.processor.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())

    async def asyncTearDown(self):
        await self.wait_for(self.processor.stop())
        await super().asyncTearDown()

    def assert_metrics_equal(self, recvd: bytes, path, value, type_code):
        recvd = recvd.decode('utf-8')
        recvd_path, _, rest = recvd.partition(':')
        recvd_value, _, recvd_code = rest.partition('|')
        self.assertEqual(path, recvd_path, 'metric path mismatch')
        self.assertEqual(recvd_value, str(value), 'metric value mismatch')
        self.assertEqual(recvd_code, type_code, 'metric type mismatch')

    async def test_sending_simple_counter(self):
        self.processor.inject_metric('simple.counter', 1000, 'c')
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[0],
                                  'simple.counter', 1000, 'c')

    async def test_adjusting_gauge(self):
        self.processor.inject_metric('simple.gauge', 100, 'g')
        self.processor.inject_metric('simple.gauge', -10, 'g')
        self.processor.inject_metric('simple.gauge', '+10', 'g')
        for _ in range(3):
            await self.wait_for(self.statsd_server.message_received.acquire())

        self.assert_metrics_equal(self.statsd_server.metrics[0],
                                  'simple.gauge', '100', 'g')
        self.assert_metrics_equal(self.statsd_server.metrics[1],
                                  'simple.gauge', '-10', 'g')
        self.assert_metrics_equal(self.statsd_server.metrics[2],
                                  'simple.gauge', '+10', 'g')

    async def test_sending_timer(self):
        secs = 12.34
        self.processor.inject_metric('simple.timer', secs * 1000.0, 'ms')
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[0],
                                  'simple.timer', 12340.0, 'ms')

    async def test_that_queued_metrics_are_drained(self):
        # The easiest way to test that the internal metrics queue
        # is drained when the processor is stopped is to monkey
        # patch the "process metric" method to enqueue a few
        # metrics and then set running to false.  It will exit
        # the run loop and drain the queue.
        real_process_metric = self.processor._process_metric

        async def fake_process_metric():
            if self.processor.running:
                self.processor.inject_metric('counter', 1, 'c')
                self.processor.inject_metric('counter', 2, 'c')
                self.processor.inject_metric('counter', 3, 'c')
                self.processor.running = False
            return await real_process_metric()

        self.processor._process_metric = fake_process_metric
        await self.wait_for(self.statsd_server.message_received.acquire())
        await self.wait_for(self.statsd_server.message_received.acquire())
        await self.wait_for(self.statsd_server.message_received.acquire())

    async def test_metrics_sent_while_disconnected_are_queued(self):
        self.statsd_server.close()
        await self.statsd_server.wait_closed()

        for value in range(50):
            self.processor.inject_metric('counter', value, 'c')

        asyncio.create_task(self.statsd_server.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())
        for value in range(50):
            await self.wait_for(self.statsd_server.message_received.acquire())
            self.assertEqual(f'counter:{value}|c'.encode(),
                             self.statsd_server.metrics.pop(0))

    async def test_socket_closure_while_processing_failed_event(self):
        state = {'first_time': True}
        real_process_metric = self.processor._process_metric

        async def fake_process_metric():
            if state['first_time']:
                self.processor._failed_sends.append(b'counter:1|c\n')
                self.processor.transport.close()
                state['first_time'] = False
            return await real_process_metric()

        self.processor._process_metric = fake_process_metric

        await self.wait_for(self.statsd_server.message_received.acquire())
