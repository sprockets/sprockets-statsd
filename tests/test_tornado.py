import asyncio
import os
import socket
import time
import typing
import unittest.mock

import sprockets.http.app
import sprockets.http.testing
from tornado import testing, web

import sprockets_statsd.statsd
import sprockets_statsd.tornado
from tests import helpers

ParsedMetric = typing.Tuple[str, float, str]


class Handler(sprockets_statsd.tornado.RequestHandler, web.RequestHandler):
    async def get(self):
        with self.execution_timer('execution-timer'):
            await asyncio.sleep(0.1)
        self.increase_counter('request-count')
        self.write('true')


class Application(sprockets_statsd.tornado.Application, web.Application):
    def __init__(self, **settings):
        super().__init__([web.url('/', Handler)], **settings)


class AsyncTestCaseWithTimeout(testing.AsyncTestCase):
    def run_coroutine(self, coro):
        loop: asyncio.AbstractEventLoop = self.io_loop.asyncio_loop
        try:
            loop.run_until_complete(
                asyncio.wait_for(coro,
                                 timeout=testing.get_async_test_timeout()))
        except asyncio.TimeoutError:
            self.fail(f'coroutine {coro} took too long to complete')


class ApplicationTests(AsyncTestCaseWithTimeout):
    def setUp(self):
        super().setUp()
        self._environ = {}

    def tearDown(self):
        super().tearDown()
        for name, value in self._environ.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

    def setenv(self, **variables):
        for name, value in variables.items():
            self._environ.setdefault(name, os.environ.pop(name, None))
            os.environ[name] = value

    def unsetenv(self, *names):
        for name in names:
            self._environ.setdefault(name, os.environ.pop(name, None))

    def test_statsd_setting_defaults(self):
        self.unsetenv('STATSD_ENABLED', 'STATSD_HOST', 'STATSD_PORT',
                      'STATSD_PREFIX', 'STATSD_PROTOCOL')

        app = sprockets_statsd.tornado.Application(statsd={'prefix': ''})
        self.assertIn('statsd', app.settings)
        self.assertTrue(app.settings['statsd']['enabled'])
        self.assertIsNone(app.settings['statsd']['host'],
                          'default host value should be None')
        self.assertEqual(8125, app.settings['statsd']['port'])
        self.assertEqual('', app.settings['statsd']['prefix'])
        self.assertEqual('tcp', app.settings['statsd']['protocol'])

    def test_that_statsd_settings_read_from_environment(self):
        self.setenv(STATSD_ENABLED='no',
                    STATSD_HOST='statsd',
                    STATSD_PORT='5218',
                    STATSD_PREFIX='my-service',
                    STATSD_PROTOCOL='udp')

        app = sprockets_statsd.tornado.Application()
        self.assertIn('statsd', app.settings)
        self.assertFalse(app.settings['statsd']['enabled'])
        self.assertEqual('statsd', app.settings['statsd']['host'])
        self.assertEqual(5218, app.settings['statsd']['port'])
        self.assertEqual('my-service', app.settings['statsd']['prefix'])
        self.assertEqual('udp', app.settings['statsd']['protocol'])

    def test_prefix_when_only_service_is_set(self):
        with self.assertRaises(RuntimeError):
            sprockets_statsd.tornado.Application(service='blah')

    def test_prefix_when_only_environment_is_set(self):
        with self.assertRaises(RuntimeError):
            sprockets_statsd.tornado.Application(environment='whatever')

    def test_prefix_default_when_service_and_environment_are_set(self):
        app = sprockets_statsd.tornado.Application(environment='development',
                                                   service='my-service')
        self.assertIn('statsd', app.settings)
        self.assertEqual('applications.my-service.development',
                         app.settings['statsd']['prefix'])

    def test_overridden_settings(self):
        self.setenv(STATSD_ENABLED='0',
                    STATSD_HOST='statsd',
                    STATSD_PORT='9999',
                    STATSD_PREFIX='service',
                    STATSD_PROTOCOL='tcp')

        app = sprockets_statsd.tornado.Application(
            statsd={
                'enabled': 'true',
                'host': 'statsd.example.com',
                'port': 5218,
                'prefix': 'myapp',
                'protocol': 'udp',
            })
        self.assertEqual('statsd.example.com', app.settings['statsd']['host'])
        self.assertTrue(app.settings['statsd']['enabled'])
        self.assertEqual(5218, app.settings['statsd']['port'])
        self.assertEqual('myapp', app.settings['statsd']['prefix'])
        self.assertEqual('udp', app.settings['statsd']['protocol'])

    def test_that_starting_without_host_fails(self):
        self.unsetenv('STATSD_HOST')
        app = sprockets_statsd.tornado.Application(statsd={'prefix': 'app'})
        with self.assertRaises(RuntimeError):
            self.run_coroutine(app.start_statsd())

    def test_creating_without_prefix_on_purpose(self):
        self.unsetenv('STATSD_PREFIX')
        app = sprockets_statsd.tornado.Application(statsd={
            'host': 'statsd.example.com',
            'protocol': 'udp',
            'prefix': None,
        })
        self.assertEqual(None, app.settings['statsd']['prefix'])

    def test_starting_with_calculated_prefix(self):
        self.unsetenv('STATSD_PREFIX')
        app = sprockets_statsd.tornado.Application(
            environment='development',
            service='my-service',
            statsd={
                'host': 'statsd.example.com',
                'protocol': 'udp',
            })
        try:
            self.run_coroutine(app.start_statsd())
            self.assertEqual('applications.my-service.development',
                             app.settings['statsd']['prefix'])
        finally:
            self.run_coroutine(app.stop_statsd())

    def test_starting_twice(self):
        app = sprockets_statsd.tornado.Application(statsd={
            'host': 'localhost',
            'port': '8125',
            'prefix': 'my-service',
        })
        try:
            self.run_coroutine(app.start_statsd())
            connector = app.statsd_connector
            self.assertIsNotNone(connector, 'statsd.Connector not created')

            self.run_coroutine(app.start_statsd())
            self.assertIs(app.statsd_connector, connector,
                          'statsd.Connector should not be recreated')
        finally:
            self.run_coroutine(app.stop_statsd())

    def test_stopping_without_starting(self):
        app = sprockets_statsd.tornado.Application(statsd={
            'host': 'localhost',
            'port': '8125',
            'prefix': 'my-service',
        })
        self.run_coroutine(app.stop_statsd())

    def test_optional_parameters(self):
        app = sprockets_statsd.tornado.Application(
            statsd={
                'host': 'localhost',
                'port': '8125',
                'prefix': 'my-service',
                'reconnect_sleep': 0.5,
                'wait_timeout': 0.25,
            })
        self.run_coroutine(app.start_statsd())

        processor = app.statsd_connector.processor
        self.assertEqual(0.5, processor._reconnect_sleep)
        self.assertEqual(0.25, processor._wait_timeout)
        self.run_coroutine(app.stop_statsd())

    def test_starting_with_invalid_protocol(self):
        app = sprockets_statsd.tornado.Application(statsd={
            'host': 'localhost',
            'prefix': 'my-service',
            'protocol': 'unknown'
        })
        with self.assertRaises(RuntimeError):
            self.run_coroutine(app.start_statsd())

    def test_that_protocol_strings_are_translated(self):
        app = sprockets_statsd.tornado.Application(statsd={
            'host': 'localhost',
            'prefix': 'my-service',
            'protocol': 'tcp',
        })
        self.run_coroutine(app.start_statsd())
        self.assertEqual(socket.IPPROTO_TCP,
                         app.statsd_connector.processor._ip_protocol)
        self.run_coroutine(app.stop_statsd())

        app = sprockets_statsd.tornado.Application(statsd={
            'host': 'localhost',
            'prefix': 'my-service',
            'protocol': 'udp',
        })
        self.run_coroutine(app.start_statsd())
        self.assertEqual(socket.IPPROTO_UDP,
                         app.statsd_connector.processor._ip_protocol)
        self.run_coroutine(app.stop_statsd())

    def test_disabling_statsd_prefix(self):
        app = sprockets_statsd.tornado.Application(
            service='my-service',
            version='1.0.0',
            statsd={
                'host': 'localhost',
                'prefix': '',
                'protocol': 'udp',
            },
        )
        self.run_coroutine(app.start_statsd())
        self.assertEqual(app.statsd_connector.prefix, '')
        self.run_coroutine(app.stop_statsd())

    def test_disabling_statsd_connector(self):
        app = sprockets_statsd.tornado.Application(
            environment='development',
            service='my-service',
            version='1.0.0',
            statsd={
                'enabled': 'no',
                'host': 'localhost',
            },
        )
        self.run_coroutine(app.start_statsd())
        self.assertIsNotNone(app.statsd_connector)
        self.assertIsInstance(app.statsd_connector,
                              sprockets_statsd.statsd.AbstractConnector)


class StatsdTestCase(AsyncTestCaseWithTimeout, testing.AsyncHTTPTestCase):
    Application = web.Application

    def setUp(self):
        self.statsd_server = None
        super().setUp()

    def tearDown(self):
        if self.statsd_server is not None:
            self.statsd_server.close()
            self.run_coroutine(self.statsd_server.wait_closed())
        super().tearDown()

    def get_app(self):
        self.statsd_server = helpers.StatsdServer(socket.IPPROTO_TCP)
        self.io_loop.spawn_callback(self.statsd_server.run)
        self.run_coroutine(self.statsd_server.wait_running())
        self.app = self.Application(shutdown_limit=0.5,
                                    statsd={
                                        'host': self.statsd_server.host,
                                        'port': self.statsd_server.port,
                                        'prefix': 'applications.service',
                                        'protocol': 'tcp',
                                    })
        return self.app

    def wait_for_metrics(self, metric_count=3):
        timeout_remaining = testing.get_async_test_timeout()
        for _ in range(metric_count):
            start = time.time()
            try:
                self.io_loop.run_sync(
                    self.statsd_server.message_received.acquire,
                    timeout=timeout_remaining)
            except TimeoutError:
                self.fail()
            timeout_remaining -= (time.time() - start)

    def parse_metric(self, metric_line: bytes) -> ParsedMetric:
        decoded = metric_line.decode()
        path, _, rest = decoded.partition(':')
        value, _, type_code = rest.partition('|')
        try:
            parsed_value = float(value)
        except ValueError:
            self.fail(f'value of {path} is not a number: value={value!r}')
        return path, parsed_value, type_code

    def find_metric(self, needle: str) -> ParsedMetric:
        encoded = needle.encode()
        for line in self.statsd_server.metrics:
            if encoded in line:
                return self.parse_metric(line)
        self.fail(f'failed to find metric containing {needle!r}')


class RequestHandlerTests(StatsdTestCase, AsyncTestCaseWithTimeout,
                          testing.AsyncHTTPTestCase):
    Application = Application

    def setUp(self):
        super().setUp()
        self.run_coroutine(self.app.start_statsd())

    def tearDown(self):
        self.run_coroutine(self.app.stop_statsd())
        super().tearDown()

    def test_the_request_metric_is_sent_last(self):
        rsp = self.fetch('/')
        self.assertEqual(200, rsp.code)
        self.wait_for_metrics()

        path, _, type_code = self.find_metric('Handler.GET.200')
        self.assertEqual(path, 'applications.service.timers.Handler.GET.200')
        self.assertEqual('ms', type_code)

    def test_execution_timer(self):
        rsp = self.fetch('/')
        self.assertEqual(200, rsp.code)
        self.wait_for_metrics()

        path, _, type_code = self.find_metric('execution-timer')
        self.assertEqual('applications.service.timers.execution-timer', path)
        self.assertEqual('ms', type_code)

    def test_counter(self):
        rsp = self.fetch('/')
        self.assertEqual(200, rsp.code)
        self.wait_for_metrics()

        path, value, type_code = self.find_metric('request-count')
        self.assertEqual('applications.service.counters.request-count', path)
        self.assertEqual(1.0, value)
        self.assertEqual('c', type_code)

    def test_handling_request_without_statsd_configured(self):
        self.io_loop.run_sync(self.app.stop_statsd)

        rsp = self.fetch('/')
        self.assertEqual(200, rsp.code)


class SprocketsHttpInteropTests(StatsdTestCase, AsyncTestCaseWithTimeout,
                                sprockets.http.testing.SprocketsHttpTestCase):
    class Application(sprockets_statsd.tornado.Application,
                      sprockets.http.app.Application):
        def __init__(self, **settings):
            super().__init__([web.url('/', Handler)], **settings)

    def setUp(self):
        super().setUp()
        self.let_callbacks_run()

    def let_callbacks_run(self):
        self.io_loop.run_sync(lambda: asyncio.sleep(0))

    def test_that_callbacks_are_installed(self):
        self.assertIn(self.app.start_statsd, self.app.on_start_callbacks)
        self.assertIn(self.app.stop_statsd, self.app.on_shutdown_callbacks)

    def test_that_statsd_connector_is_enabled(self):
        # verifies that the start callback actually runs correctly.
        self.assertIsNotNone(self.app.statsd_connector,
                             'statsd_connecter was never created')

    def test_that_misconfiguration_stops_application(self):
        another_app = self.Application(statsd={
            'host': '',
            'prefix': 'whatever',
        })
        another_app.stop = unittest.mock.Mock(wraps=another_app.stop)
        another_app.start(self.io_loop)
        self.let_callbacks_run()
        another_app.stop.assert_called_once_with(self.io_loop)
