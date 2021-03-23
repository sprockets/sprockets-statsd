import contextlib
import os
import time

from tornado import web

from sprockets_statsd import statsd


class Application(web.Application):
    """Mix this into your application to add a statsd connection.

    .. attribute:: statsd_connector
       :type: sprockets_statsd.statsd.Connector

       Connection to the StatsD server that is set between calls
       to :meth:`.start_statsd` and :meth:`.stop_statsd`.

    This mix-in is configured by the ``statsd`` settings key.  The
    value is a dictionary with the following keys.

    +-------------------+---------------------------------------------+
    | host              | the statsd host to send metrics to          |
    +-------------------+---------------------------------------------+
    | port              | port number that statsd is listening on     |
    +-------------------+---------------------------------------------+
    | prefix            | segment to prefix to metrics.               |
    +-------------------+---------------------------------------------+
    | reconnect_timeout | number of seconds to sleep after a statsd   |
    |                   | connection attempt fails                    |
    +-------------------+---------------------------------------------+
    | wait_timeout      | number of seconds to wait for a metric to   |
    |                   | arrive on the queue before verifying the    |
    |                   | connection                                  |
    +-------------------+---------------------------------------------+

    **host** defaults to the :envvar:`STATSD_HOST` environment variable.
    If this value is not set, then the statsd connector *WILL NOT* be
    enabled.

    **port** defaults to the :envvar:`STATSD_PORT` environment variable
    with a back up default of 8125 if the environment variable is not
    set.

    **prefix** is prefixed to all metric paths.  This provides a
    namespace for metrics so that each applications metrics are maintained
    in separate buckets.

    If the *service* and *environment* keys are set in ``settings``,
    then the default prefix is ``applications.<service>.<environment>``.
    This is a convenient way to maintain consistent metric paths when
    you are managing a larger number of services.

    **reconnect_timeout** defaults to 1.0 seconds which limits the
    aggressiveness of creating new TCP connections.

    **wait_timeout** defaults to 0.1 seconds which ensures that the
    processor quickly responds to connection faults.

    """
    def __init__(self, *args, **settings):
        statsd_settings = settings.setdefault('statsd', {})
        statsd_settings.setdefault('host', os.environ.get('STATSD_HOST'))
        statsd_settings.setdefault('port',
                                   os.environ.get('STATSD_PORT', '8125'))

        try:
            prefix = '.'.join([
                'applications',
                settings['service'],
                settings['environment'],
            ])
        except KeyError:
            prefix = None
        statsd_settings.setdefault('prefix', prefix)

        super().__init__(*args, **settings)

        self.settings['statsd']['port'] = int(self.settings['statsd']['port'])
        self.statsd_connector = None

    async def start_statsd(self):
        """Start the connector during startup.

        Call this method during application startup to enable the statsd
        connection.  A new :class:`~sprockets_statsd.statsd.Connector`
        instance will be created and started.  This method does not return
        until the connector is running.

        """
        if self.statsd_connector is None:
            statsd_settings = self.settings['statsd']
            kwargs = {
                'host': statsd_settings['host'],
                'port': statsd_settings['port'],
            }
            if 'reconnect_sleep' in statsd_settings:
                kwargs['reconnect_sleep'] = statsd_settings['reconnect_sleep']
            if 'wait_timeout' in statsd_settings:
                kwargs['wait_timeout'] = statsd_settings['wait_timeout']

            self.statsd_connector = statsd.Connector(**kwargs)
            await self.statsd_connector.start()

    async def stop_statsd(self):
        """Stop the connector during shutdown.

        If the connector was started, then this method will gracefully
        terminate it.  The method does not return until after the
        connector is stopped.

        """
        if self.statsd_connector is not None:
            await self.statsd_connector.stop()
            self.statsd_connector = None


class RequestHandler(web.RequestHandler):
    """Mix this into your handler to send metrics to a statsd server."""
    statsd_connector: statsd.Connector

    def initialize(self, **kwargs):
        super().initialize(**kwargs)
        self.application: Application
        self.statsd_connector = self.application.statsd_connector

    def __build_path(self, *path):
        full_path = '.'.join(str(c) for c in path)
        if self.settings.get('statsd', {}).get('prefix', ''):
            return f'{self.settings["statsd"]["prefix"]}.{full_path}'
        return full_path

    def record_timing(self, secs: float, *path):
        """Record the duration.

        :param secs: number of seconds to record
        :param path: path to record the duration under

        """
        if self.statsd_connector is not None:
            self.statsd_connector.inject_metric(
                self.__build_path('timers', *path), secs * 1000.0, 'ms')

    def increase_counter(self, *path, amount: int = 1):
        """Adjust a counter.

        :param path: path of the counter to adjust
        :param amount: amount to adjust the counter by.  Defaults to
            1 and can be negative

        """
        if self.statsd_connector is not None:
            self.statsd_connector.inject_metric(
                self.__build_path('counters', *path), amount, 'c')

    @contextlib.contextmanager
    def execution_timer(self, *path):
        """Record the execution duration of a block of code.

        :param path: path to record the duration as

        """
        start = time.time()
        try:
            yield
        finally:
            self.record_timing(time.time() - start, *path)

    def on_finish(self):
        """Extended to record the request time as a duration.

        This method extends :meth:`tornado.web.RequestHandler.on_finish`
        to record ``self.request.request_time`` as a timing metric.

        """
        super().on_finish()
        self.record_timing(self.request.request_time(),
                           self.__class__.__name__, self.request.method,
                           self.get_status())
