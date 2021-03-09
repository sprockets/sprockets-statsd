import contextlib
import os
import time

from tornado import web

from sprockets_statsd import statsd


class Application(web.Application):
    """Mix this into your application to add a statsd connection.

    This mix-in is configured by the ``statsd`` settings key.  The
    value should be a dictionary with the following keys.

    +--------+---------------------------------------------+
    | host   | the statsd host to send metrics to          |
    +--------+---------------------------------------------+
    | port   | TCP port number that statsd is listening on |
    +--------+---------------------------------------------+
    | prefix | segment to prefix to metrics                |
    +--------+---------------------------------------------+

    *host* defaults to the :envvar:`STATSD_HOST` environment variable.
    If this value is not set, then the statsd connector **WILL NOT**
    be enabled.

    *port* defaults to the :envvar:`STATSD_PORT` environment variable
    with a back up default of 8125 if the environment variable is not
    set.

    *prefix* defaults to ``applications.<service>.<environment>`` where
    *<service>* and *<environment>* are replaced with the keys from
    `settings` if they are present.

    """
    def __init__(self, *args, **settings):
        statsd_settings = settings.setdefault('statsd', {})
        statsd_settings.setdefault('host', os.environ.get('STATSD_HOST'))
        statsd_settings.setdefault('port',
                                   os.environ.get('STATSD_PORT', '8125'))

        prefix = ['applications']
        if 'service' in settings:
            prefix.append(settings['service'])
        if 'environment' in settings:
            prefix.append(settings['environment'])
        statsd_settings.setdefault('prefix', '.'.join(prefix))

        super().__init__(*args, **settings)

        self.settings['statsd']['port'] = int(self.settings['statsd']['port'])
        self.__statsd_connector = None

    async def start_statsd(self):
        """Start the connector during startup.

        Call this method during application startup to enable the statsd
        connection.  A new :class:`~sprockets_statsd.statsd.Connector`
        instance will be created and started.  This method does not return
        until the connector is running.

        """
        statsd_settings = self.settings['statsd']
        if statsd_settings.get('_connector') is None:
            connector = statsd.Connector(host=statsd_settings['host'],
                                         port=statsd_settings['port'])
            await connector.start()
            self.settings['statsd']['_connector'] = connector

    async def stop_statsd(self):
        """Stop the connector during shutdown.

        If the connector was started, then this method will gracefully
        terminate it.  The method does not return until after the
        connector is stopped.

        """
        connector = self.settings['statsd'].pop('_connector', None)
        if connector is not None:
            await connector.stop()


class RequestHandler(web.RequestHandler):
    """Mix this into your handler to send metrics to a statsd server."""
    __connector: statsd.Connector

    def initialize(self, **kwargs):
        super().initialize(**kwargs)
        self.__connector = self.settings.get('statsd', {}).get('_connector')

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
        if self.__connector is not None:
            self.__connector.inject_metric(self.__build_path('timers', *path),
                                           secs * 1000.0, 'ms')

    def increase_counter(self, *path, amount: int = 1):
        """Adjust a counter.

        :param path: path of the counter to adjust
        :param amount: amount to adjust the counter by.  Defaults to
            1 and can be negative

        """
        if self.__connector is not None:
            self.__connector.inject_metric(
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
