import asyncio
import datetime
import logging
import socket
import time
import typing
import unittest.mock

import asynctest

from sprockets_statsd import statsd
from tests import helpers


class ProcessorTestCase(asynctest.TestCase):
    ip_protocol: int

    async def setUp(self):
        self.test_timeout = 5.0
        super().setUp()
        await self.asyncSetUp()

    async def tearDown(self):
        await self.asyncTearDown()
        super().tearDown()

    async def wait_for(self, fut):
        try:
            await asyncio.wait_for(fut, timeout=self.test_timeout)
        except asyncio.TimeoutError:
            self.fail('future took too long to resolve')

    async def asyncSetUp(self):
        self.statsd_server = helpers.StatsdServer(self.ip_protocol)
        self.statsd_task = asyncio.create_task(self.statsd_server.run())
        await self.statsd_server.wait_running()

    async def asyncTearDown(self):
        self.statsd_server.close()
        await self.statsd_server.wait_closed()


class ProcessorTests(ProcessorTestCase):
    ip_protocol = socket.IPPROTO_TCP

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
        while processor.connected:
            await asyncio.sleep(0.1)
            if time.time() >= until:
                self.fail('processor never disconnected')

        # Start the server on the same port and let the client reconnect.
        self.statsd_task = asyncio.create_task(self.statsd_server.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())
        self.assertTrue(processor.connected)

        await self.wait_for(processor.stop())

    async def test_that_processor_can_be_cancelled(self):
        processor = statsd.Processor(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        task = asyncio.create_task(processor.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())

        task.cancel()
        await self.wait_for(processor.stopped.wait())

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
        await self.wait_for(processor.stop())

    async def test_that_stopping_when_not_running_is_safe(self):
        processor = statsd.Processor(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        await self.wait_for(processor.stop())

    def test_that_processor_fails_when_host_is_none(self):
        with self.assertRaises(RuntimeError) as context:
            statsd.Processor(host=None, port=12345)  # type: ignore[arg-type]
        self.assertIn('host', str(context.exception))

    async def test_starting_and_stopping_without_connecting(self):
        host, port = self.statsd_server.host, self.statsd_server.port
        self.statsd_server.close()
        await self.wait_for(self.statsd_server.wait_closed())
        processor = statsd.Processor(host=host, port=port)
        asyncio.create_task(processor.run())
        await self.wait_for(processor.running.wait())
        await processor.stop()

    async def test_that_protocol_exceptions_are_logged(self):
        processor = statsd.Processor(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        asyncio.create_task(processor.run())
        await self.wait_for(processor.running.wait())

        with self.assertLogs(processor.logger, level=logging.ERROR) as cm:
            processor.queue.put_nowait('not-bytes')  # type: ignore[arg-type]
            while processor.queue.qsize() > 0:
                await asyncio.sleep(0.1)

        for record in cm.records:
            if record.exc_info is not None and record.funcName == 'run':
                break
        else:
            self.fail('Expected run to log exception')

        await processor.stop()


class TCPProcessingTests(ProcessorTestCase):
    ip_protocol = socket.IPPROTO_TCP

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.processor = statsd.Processor(host=self.statsd_server.host,
                                          port=self.statsd_server.port,
                                          reconnect_sleep=0.25)
        asyncio.create_task(self.processor.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())

    async def asyncTearDown(self):
        await self.processor.stop()
        await super().asyncTearDown()

    async def test_connection_failures(self):
        # Change the port and close the transport, this will cause the
        # processor to reconnect to the new port and fail.
        self.processor.port = 1
        self.processor.protocol.transport.close()

        # Wait for the processor to be disconnected, then change the
        # port back and let the processor reconnect.
        while self.processor.connected:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)
        self.processor.port = self.statsd_server.port

        await self.wait_for(self.statsd_server.client_connected.acquire())

    async def test_socket_closure_while_processing_failed_event(self):
        state = {'first_time': True}
        real_process_metric = self.processor._process_metric

        async def fake_process_metric():
            if state['first_time']:
                self.processor.protocol.buffered_data = b'counter:1|c\n'
                self.processor.protocol.transport.close()
                state['first_time'] = False
            return await real_process_metric()

        self.processor._process_metric = fake_process_metric

        await self.wait_for(self.statsd_server.message_received.acquire())

    async def test_socket_closure_while_sending(self):
        state = {'first_time': True}
        protocol = typing.cast(statsd.TCPProtocol, self.processor.protocol)
        real_transport_write = protocol.transport.write

        def fake_transport_write(data):
            if state['first_time']:
                self.processor.protocol.transport.close()
                state['first_time'] = False
            return real_transport_write(data)

        protocol.transport.write = fake_transport_write
        self.processor.queue.put_nowait(b'counter:1|c')
        await self.wait_for(self.statsd_server.message_received.acquire())

    async def test_that_disconnected_logging_is_throttled(self):
        self.statsd_server.close()
        await self.statsd_server.wait_closed()

        self.processor.logger = unittest.mock.Mock()
        self.processor._connect_log_guard.threshold = 10
        self.processor._reconnect_sleep = 0
        while self.processor._connect_log_guard.counter < (20 + 1):
            await asyncio.sleep(0)
        self.assertLess(self.processor.logger.warning.call_count, 20)


class UDPProcessingTests(ProcessorTestCase):
    ip_protocol = socket.IPPROTO_UDP

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.connector = statsd.Connector(host=self.statsd_server.host,
                                          port=self.statsd_server.port,
                                          ip_protocol=self.ip_protocol,
                                          reconnect_sleep=0.25)
        await self.connector.start()

    async def asyncTearDown(self):
        await self.connector.stop()
        await super().asyncTearDown()

    async def test_sending_metrics(self):
        self.connector.incr('counter')
        self.connector.timing('timer', 0.001)
        await self.wait_for(self.statsd_server.message_received.acquire())
        await self.wait_for(self.statsd_server.message_received.acquire())

        self.assertEqual(self.statsd_server.metrics[0],
                         b'counters.counter:1|c')
        self.assertEqual(self.statsd_server.metrics[1], b'timers.timer:1.0|ms')

    async def test_that_client_sends_to_new_server(self):
        self.statsd_server.close()
        await self.statsd_server.wait_closed()

        self.connector.incr('should.be.lost')
        await asyncio.sleep(self.connector.processor._wait_timeout * 2)

        self.statsd_task = asyncio.create_task(self.statsd_server.run())
        await self.statsd_server.wait_running()

        self.connector.incr('should.be.recvd')
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assertEqual(self.statsd_server.metrics[0],
                         b'counters.should.be.recvd:1|c')

    async def test_that_client_handles_socket_closure(self):
        self.connector.processor.protocol.transport.close()
        await self.wait_for(
            asyncio.sleep(self.connector.processor._reconnect_sleep))

        self.connector.incr('should.be.recvd')
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assertEqual(self.statsd_server.metrics[0],
                         b'counters.should.be.recvd:1|c')


class ConnectorTests(ProcessorTestCase):
    ip_protocol = socket.IPPROTO_TCP

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.connector = statsd.Connector(self.statsd_server.host,
                                          self.statsd_server.port)
        await self.connector.start()
        await self.wait_for(self.statsd_server.client_connected.acquire())

    async def asyncTearDown(self):
        await self.wait_for(self.connector.stop())
        await super().asyncTearDown()

    def assert_metrics_equal(self, recvd: bytes, path, value, type_code):
        decoded = recvd.decode('utf-8')
        recvd_path, _, rest = decoded.partition(':')
        recvd_value, _, recvd_code = rest.partition('|')
        self.assertEqual(path, recvd_path, 'metric path mismatch')
        if type_code == 'ms':
            self.assertAlmostEqual(float(recvd_value),
                                   value,
                                   places=3,
                                   msg='metric value mismatch')
        else:
            self.assertEqual(recvd_value, str(value), 'metric value mismatch')
        self.assertEqual(recvd_code, type_code, 'metric type mismatch')

    async def test_adjusting_counter(self):
        self.connector.incr('simple.counter')
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[-1],
                                  'counters.simple.counter', 1, 'c')

        self.connector.incr('simple.counter', 10)
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[-1],
                                  'counters.simple.counter', 10, 'c')

        self.connector.decr('simple.counter')
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[-1],
                                  'counters.simple.counter', -1, 'c')

        self.connector.decr('simple.counter', 10)
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[-1],
                                  'counters.simple.counter', -10, 'c')

    async def test_adjusting_gauge(self):
        self.connector.gauge('simple.gauge', 100)
        self.connector.gauge('simple.gauge', -10, delta=True)
        self.connector.gauge('simple.gauge', 10, delta=True)
        for _ in range(3):
            await self.wait_for(self.statsd_server.message_received.acquire())

        self.assert_metrics_equal(self.statsd_server.metrics[0],
                                  'gauges.simple.gauge', '100', 'g')
        self.assert_metrics_equal(self.statsd_server.metrics[1],
                                  'gauges.simple.gauge', '-10', 'g')
        self.assert_metrics_equal(self.statsd_server.metrics[2],
                                  'gauges.simple.gauge', '+10', 'g')

    async def test_sending_timer(self):
        secs = 12.34
        self.connector.timing('simple.timer', secs)
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[0],
                                  'timers.simple.timer', 12340.0, 'ms')

    async def test_sending_timer_using_timedelta(self):
        secs = datetime.timedelta(seconds=12, milliseconds=340)
        self.connector.timing('simple.timer', secs)
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[0],
                                  'timers.simple.timer', 12340.0, 'ms')

    async def test_timing_context_manager(self):
        with unittest.mock.patch(
                'sprockets_statsd.statsd.time.time') as time_function:
            time_function.side_effect = [10.0, 22.345]
            with self.connector.timer('some.timer'):
                pass  # exercising context manager
            self.assertEqual(2, time_function.call_count)

        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[0],
                                  'timers.some.timer', 12345.0, 'ms')

    async def test_timer_is_monotonic(self):
        with unittest.mock.patch(
                'sprockets_statsd.statsd.time.time') as time_function:
            time_function.side_effect = [10.001, 10.000]
            with self.connector.timer('some.timer'):
                pass  # exercising context manager
            self.assertEqual(2, time_function.call_count)

        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assert_metrics_equal(self.statsd_server.metrics[0],
                                  'timers.some.timer', 0.0, 'ms')

    async def test_that_queued_metrics_are_drained(self):
        # The easiest way to test that the internal metrics queue
        # is drained when the processor is stopped is to monkey
        # patch the "process metric" method to enqueue a few
        # metrics and then terminate the processor.  It will exit
        # the run loop and drain the queue.
        real_process_metric = self.connector.processor._process_metric

        async def fake_process_metric():
            if not self.connector.processor.should_terminate:
                self.connector.incr('counter', 1)
                self.connector.incr('counter', 2)
                self.connector.incr('counter', 3)
                self.connector.processor.should_terminate = True
            return await real_process_metric()

        self.connector.processor._process_metric = fake_process_metric
        await self.wait_for(self.statsd_server.message_received.acquire())
        await self.wait_for(self.statsd_server.message_received.acquire())
        await self.wait_for(self.statsd_server.message_received.acquire())

    async def test_metrics_sent_while_disconnected_are_queued(self):
        self.statsd_server.close()
        await self.statsd_server.wait_closed()

        for value in range(50):
            self.connector.incr('counter', value)

        asyncio.create_task(self.statsd_server.run())
        await self.wait_for(self.statsd_server.client_connected.acquire())
        for value in range(50):
            await self.wait_for(self.statsd_server.message_received.acquire())
            self.assertEqual(f'counters.counter:{value}|c'.encode(),
                             self.statsd_server.metrics.pop(0))

    async def test_that_queue_full_logging_is_throttled(self):
        await self.connector.processor.stop()

        self.connector.logger = unittest.mock.Mock()
        self.connector._enqueue_log_guard.threshold = 10

        # fill up the queue
        for _ in range(self.connector.processor.queue.maxsize):
            self.connector.incr('counter')

        # then overflow it a bunch of times
        overflow_count = self.connector._enqueue_log_guard.threshold * 5
        for _ in range(overflow_count):
            self.connector.incr('counter')
        self.assertLess(self.connector.logger.warning.call_count,
                        overflow_count)


class ConnectorOptionTests(ProcessorTestCase):
    ip_protocol = socket.IPPROTO_TCP

    def test_protocol_values(self):
        connector = statsd.Connector(host=self.statsd_server.host,
                                     port=self.statsd_server.port)
        self.assertEqual(socket.IPPROTO_TCP, connector.processor._ip_protocol)

        connector = statsd.Connector(host=self.statsd_server.host,
                                     port=self.statsd_server.port,
                                     ip_protocol=socket.IPPROTO_UDP)
        self.assertEqual(socket.IPPROTO_UDP, connector.processor._ip_protocol)

        with self.assertRaises(RuntimeError):
            statsd.Connector(host=self.statsd_server.host,
                             port=self.statsd_server.port,
                             ip_protocol=socket.IPPROTO_GRE)

    def test_invalid_port_values(self):
        for port in {None, 0, -1, 'not-a-number'}:
            with self.assertRaises(RuntimeError) as context:
                statsd.Connector(host=self.statsd_server.host, port=port)
            self.assertIn('port', str(context.exception))
            self.assertIn(repr(port), str(context.exception))

    async def test_that_metrics_are_dropped_when_queue_overflows(self):
        connector = statsd.Connector(host=self.statsd_server.host,
                                     port=1,
                                     max_queue_size=10)
        await connector.start()
        self.addCleanup(connector.stop)

        # fill up the queue with incr's
        for expected_size in range(1, connector.processor.queue.maxsize + 1):
            connector.incr('counter')
            self.assertEqual(connector.processor.queue.qsize(), expected_size)

        # the following decr's should be ignored
        for _ in range(10):
            connector.decr('counter')
            self.assertEqual(connector.processor.queue.qsize(), 10)

        # make sure that only the incr's are in the queue
        for _ in range(connector.processor.queue.qsize()):
            metric = await connector.processor.queue.get()
            self.assertEqual(metric, b'counters.counter:1|c')


class ConnectorTimerTests(ProcessorTestCase):
    ip_protocol = socket.IPPROTO_TCP

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.connector = statsd.Connector(self.statsd_server.host,
                                          self.statsd_server.port)
        await self.connector.start()
        await self.wait_for(self.statsd_server.client_connected.acquire())

    async def asyncTearDown(self):
        await self.wait_for(self.connector.stop())
        await super().asyncTearDown()

    async def test_that_stop_raises_if_not_started(self):
        timer = self.connector.timer('whatever')
        with self.assertRaises(RuntimeError):
            timer.stop()

    async def test_that_start_returns_instance(self):
        timer = self.connector.timer('whatever')
        self.assertIs(timer, timer.start())

    async def test_that_stop_returns_instance(self):
        timer = self.connector.timer('whatever')
        timer.start()
        self.assertIs(timer, timer.stop())

    async def test_that_timing_is_sent_by_stop(self):
        timer = self.connector.timer('whatever')
        timer.start()
        self.assertTrue(self.statsd_server.message_received.locked(),
                        'timing sent to server unexpectedly')
        timer.stop()
        await self.wait_for(self.statsd_server.message_received.acquire())

    async def test_that_timing_send_can_be_delayed(self):
        timer = self.connector.timer('whatever')
        timer.start()
        self.assertTrue(self.statsd_server.message_received.locked(),
                        'timing sent to server unexpectedly')
        timer.stop(send=False)
        self.assertTrue(self.statsd_server.message_received.locked(),
                        'timing sent to server unexpectedly')

        timer.send()
        await self.wait_for(self.statsd_server.message_received.acquire())

    async def test_that_send_raises_when_already_sent(self):
        timer = self.connector.timer('whatever')
        timer.start()
        timer.stop(send=False)
        timer.send()
        await self.wait_for(self.statsd_server.message_received.acquire())
        with self.assertRaises(RuntimeError):
            timer.send()

    async def test_that_send_raises_when_not_started(self):
        timer = self.connector.timer('whatever')
        with self.assertRaises(RuntimeError):
            timer.send()

    async def test_that_send_raises_when_not_stopped(self):
        timer = self.connector.timer('whatever')
        timer.start()
        with self.assertRaises(RuntimeError):
            timer.send()

    async def test_that_timer_can_be_reused(self):
        timer = self.connector.timer('whatever')
        with timer:
            pass  # exercising context manager
        await self.wait_for(self.statsd_server.message_received.acquire())
        self.assertTrue(self.statsd_server.message_received.locked())

        with timer:
            pass  # exercising context manager
        await self.wait_for(self.statsd_server.message_received.acquire())
