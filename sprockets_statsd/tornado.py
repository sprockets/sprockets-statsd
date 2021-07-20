import contextlib
import logging
import os
import socket
import time
import typing

from tornado import ioloop, web

from sprockets_statsd import statsd


class Application(web.Application):
    """Mix this into your application to add a statsd connection.

    .. attribute:: statsd_connector
       :type: sprockets_statsd.statsd.AbstractConnector

       Connection to the StatsD server that is set between calls
       to :meth:`.start_statsd` and :meth:`.stop_statsd`.

    This mix-in is configured by the ``statsd`` settings key.  The
    value is a dictionary with the following keys.

    +-------------------+---------------------------------------------+
    | enabled           | should the statsd connector be enabled?     |
    +-------------------+---------------------------------------------+
    | host              | the statsd host to send metrics to          |
    +-------------------+---------------------------------------------+
    | port              | port number that statsd is listening on     |
    +-------------------+---------------------------------------------+
    | prefix            | segment to prefix to metrics                |
    +-------------------+---------------------------------------------+
    | protocol          | "tcp" or "udp"                              |
    +-------------------+---------------------------------------------+
    | reconnect_timeout | number of seconds to sleep after a statsd   |
    |                   | connection attempt fails                    |
    +-------------------+---------------------------------------------+
    | wait_timeout      | number of seconds to wait for a metric to   |
    |                   | arrive on the queue before verifying the    |
    |                   | connection                                  |
    +-------------------+---------------------------------------------+

    **enabled** defaults to the :envvar:`STATSD_ENABLED` environment
    variable coerced to a :class:`bool`.  If this variable is not set,
    then the statsd connector *WILL BE* enabled.  Set this to a *falsy*
    value to disable the connector.  The following values are considered
    *truthy*: a non-zero integer or a case-insensitive match of "on",
    "t", "true", or "yes".  All other values are considered *falsy*.

    **host** defaults to the :envvar:`STATSD_HOST` environment variable.
    If this value is not set, then the statsd connector *WILL NOT* be
    enabled.

    **port** defaults to the :envvar:`STATSD_PORT` environment variable
    with a back up default of 8125 if the environment variable is not
    set.

    **prefix** is prefixed to all metric paths.  This provides a
    namespace for metrics so that each applications metrics are maintained
    in separate buckets.  The default is to use the :envvar:`STATSD_PREFIX`
    environment variable.  If it is unset and the *service* and
    *environment* keys are set in ``settings``, then the default is
    ``applications.<service>.<environment>``.  This is a convenient way to
    maintain consistent metric paths when you are managing a larger number
    of services.

    .. rubric:: Warning

    If you want to run without a prefix, then you are required to explicitly
    set ``statsd.prefix`` to ``None``.  This prevents accidentally polluting
    the metric namespace with unqualified paths.

    **protocol** defaults to the :envvar:`STATSD_PROTOCOL` environment
    variable with a back default of "tcp" if the environment variable
    is not set.

    **reconnect_timeout** defaults to 1.0 seconds which limits the
    aggressiveness of creating new TCP connections.

    **wait_timeout** defaults to 0.1 seconds which ensures that the
    processor quickly responds to connection faults.

    """
    statsd_connector: typing.Optional[statsd.AbstractConnector]

    def __init__(self, *args: typing.Any, **settings: typing.Any):
        statsd_settings = settings.setdefault('statsd', {})
        statsd_settings.setdefault('enabled',
                                   os.environ.get('STATSD_ENABLED', 'yes'))
        statsd_settings.setdefault('host', os.environ.get('STATSD_HOST'))
        statsd_settings.setdefault('port',
                                   os.environ.get('STATSD_PORT', '8125'))
        statsd_settings.setdefault('protocol',
                                   os.environ.get('STATSD_PROTOCOL', 'tcp'))

        if 'prefix' not in statsd_settings:
            statsd_settings['prefix'] = os.environ.get('STATSD_PREFIX')
            if not statsd_settings['prefix']:
                try:
                    statsd_settings['prefix'] = '.'.join([
                        'applications',
                        settings['service'],
                        settings['environment'],
                    ])
                except KeyError:
                    raise RuntimeError(
                        'statsd configuration error: prefix is not set.  Set'
                        ' $STATSD_PREFIX or configure settings.statsd.prefix')

        super().__init__(*args, **settings)

        self.settings['statsd']['enabled'] = _parse_bool(
            self.settings['statsd']['enabled'])
        self.settings['statsd']['port'] = int(self.settings['statsd']['port'])
        self.statsd_connector = None

        try:
            self.on_start_callbacks.append(self.start_statsd)
            self.on_shutdown_callbacks.append(self.stop_statsd)
        except AttributeError:
            pass

    async def start_statsd(self, *_: typing.Any) -> None:
        """Start the connector during startup.

        Call this method during application startup to enable the statsd
        connection.  A new :class:`~sprockets_statsd.statsd.Connector`
        instance will be created and started.  This method does not return
        until the connector is running.

        """
        if self.statsd_connector is None:
            logger = self.__get_logger()
            if not self.settings['statsd']['enabled']:
                logger.info('statsd connector is disabled by configuration')
                self.statsd_connector = statsd.AbstractConnector()
            else:
                kwargs = self.settings['statsd'].copy()
                kwargs.pop('enabled', None)  # consume this one
                protocol = kwargs.pop('protocol', None)
                if protocol == 'tcp':
                    kwargs['ip_protocol'] = socket.IPPROTO_TCP
                elif protocol == 'udp':
                    kwargs['ip_protocol'] = socket.IPPROTO_UDP
                else:
                    return self.__handle_fatal_error(
                        f'statsd configuration error: {protocol} is not '
                        f'a valid protocol')

                logger.info('creating %s statsd connector', protocol.upper())
                try:
                    self.statsd_connector = statsd.Connector(**kwargs)
                except RuntimeError as error:
                    return self.__handle_fatal_error(
                        'statsd.Connector failed to start', error)

            await self.statsd_connector.start()

    async def stop_statsd(self, *_: typing.Any) -> None:
        """Stop the connector during shutdown.

        If the connector was started, then this method will gracefully
        terminate it.  The method does not return until after the
        connector is stopped.

        """
        if self.statsd_connector is not None:
            await self.statsd_connector.stop()
            self.statsd_connector = None

    def __handle_fatal_error(self,
                             message: str,
                             exc: typing.Optional[Exception] = None) -> None:
        logger = self.__get_logger()
        if exc is not None:
            logger.exception('%s', message)
        else:
            logger.error('%s', message)
        if hasattr(self, 'stop'):
            self.stop(ioloop.IOLoop.current())
        else:
            raise RuntimeError(message)

    def __get_logger(self) -> logging.Logger:
        try:
            return typing.cast(logging.Logger, getattr(self, 'logger'))
        except AttributeError:
            return logging.getLogger(__package__).getChild(
                'tornado.Application')


class RequestHandler(web.RequestHandler):
    """Mix this into your handler to send metrics to a statsd server."""
    statsd_connector: typing.Optional[statsd.AbstractConnector]

    def initialize(self, **kwargs: typing.Any) -> None:
        super().initialize(**kwargs)
        self.application: Application
        self.statsd_connector = self.application.statsd_connector

    def __build_path(self, *path: typing.Any) -> str:
        return '.'.join(str(c) for c in path)

    def record_timing(self, secs: float, *path: typing.Any) -> None:
        """Record the duration.

        :param secs: number of seconds to record
        :param path: path to record the duration under

        """
        if self.statsd_connector is not None:
            self.statsd_connector.timing(self.__build_path(*path), secs)

    def increase_counter(self, *path: typing.Any, amount: int = 1) -> None:
        """Adjust a counter.

        :param path: path of the counter to adjust
        :param amount: amount to adjust the counter by.  Defaults to
            1 and can be negative

        """
        if self.statsd_connector is not None:
            self.statsd_connector.incr(self.__build_path(*path), amount)

    @contextlib.contextmanager
    def execution_timer(
            self, *path: typing.Any) -> typing.Generator[None, None, None]:
        """Record the execution duration of a block of code.

        :param path: path to record the duration as

        """
        start = time.time()
        try:
            yield
        finally:
            self.record_timing(time.time() - start, *path)

    def on_finish(self) -> None:
        """Extended to record the request time as a duration.

        This method extends :meth:`tornado.web.RequestHandler.on_finish`
        to record ``self.request.request_time`` as a timing metric.

        """
        super().on_finish()
        self.record_timing(self.request.request_time(),
                           self.__class__.__name__, self.request.method,
                           self.get_status())


def _parse_bool(value: str) -> bool:
    try:
        return int(value) != 0
    except ValueError:
        return value.lower() in {'true', 't', 'yes', 'on'}
