import asyncio
import datetime
import logging
import socket
import time
import typing


class ThrottleGuard:
    """Prevent code from executing repeatedly.

    :param threshold: guarding threshold

    This abstraction allows code to execute the first "threshold"
    times and then only once per "threshold" times afterwards.  Use
    it to ensure that log statements are continuously written during
    persistent error conditions.  The goal is to provide regular
    feedback while limiting the amount of log spam.

    The following snippet will log the first 100 failures and then
    once every 100 failures thereafter:

    .. code-block:: python

       executions = 0
       guard = ThrottleGuard(100)
       for _ in range(1000):
           if guard.allow_execution():
               executions += 1
               logging.info('called %s times instead of %s times',
                            executions, guard.counter)

    """
    def __init__(self, threshold: int):
        self.counter = 0
        self.threshold = threshold

    def allow_execution(self) -> bool:
        """Should this execution be allowed?"""
        self.counter += 1
        allow = (self.counter < self.threshold
                 or (self.counter % self.threshold) == 0)
        return allow

    def reset(self) -> None:
        """Reset counter after error has resolved."""
        self.counter = 0


class Timer:
    """Implement Timer interface from python-statsd.

    Instances of this class are returned from
    :meth:`.AbstractConnector.timer` to maintain some compatibility
    with `python-statsd`_.  You should not create instances of this
    class yourself.

    This implementation follows the careful protocol created by
    python-statsd in that it raises :exc:`RuntimeError` in the
    following cases:

    * :meth:`.stop` is called before calling :meth:`.start`
    * :meth:`.send` is called before calling :meth:`.stop`
      which requires a prior call to :meth:`.start`

    The call to :meth:`.send` clears the timing values so calling it
    twice in a row will result in a :exc:`RuntimeError` as well.

    """
    _start_time: typing.Union[None, float]
    _finish_time: typing.Union[None, float]

    def __init__(self, connector: 'AbstractConnector', path: str):
        self._connector = connector
        self._path = path
        self._start_time, self._finish_time = None, None

    def start(self) -> 'Timer':
        """Start the timer and return `self`."""
        self._start_time = time.time()
        return self

    def stop(self, send: bool = True) -> 'Timer':
        """Stop the timer and send the timing.

        :param send: immediately send the recorded timing to the
            processor

        You can delay sending the timing by setting the `send`
        parameter to :data:`False`.

        """
        if self._start_time is None:
            raise RuntimeError('Timer.stop called before start')
        self._finish_time = time.time()
        if send:
            self.send()
        return self

    def send(self) -> None:
        """Send the recorded timing to the connector.

        This method will raise a :exc:`RuntimeError` if a timing has
        not been recorded by calling :meth:`start` and :meth:`stop` in
        sequence.

        """
        if self._start_time is None:
            raise RuntimeError('Timer.send called before start')
        if self._finish_time is None:
            raise RuntimeError('Timer.send called before stop')

        self._connector.timing(
            self._path,
            max(self._finish_time, self._start_time) - self._start_time)
        self._start_time, self._finish_time = None, None

    def __enter__(self) -> 'Timer':
        self.start()
        return self

    def __exit__(self, exc_type: typing.Union[typing.Type[Exception], None],
                 exc_val: typing.Union[Exception, None],
                 exc_tb: typing.Union[typing.Tuple, None]) -> None:
        self.stop()


class AbstractConnector:
    """StatsD connector that does not send metrics or connect.

    Use this connector when you want to maintain the application
    interface without doing any real work.

    """
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def inject_metric(self, path: str, value: str, type_code: str) -> None:
        pass

    def incr(self, path: str, value: int = 1) -> None:
        """Increment a counter metric.

        :param path: counter to increment
        :param value: amount to increment the counter by

        """
        self.inject_metric(f'counters.{path}', str(value), 'c')

    def decr(self, path: str, value: int = 1) -> None:
        """Decrement a counter metric.

        :param path: counter to decrement
        :param value: amount to decrement the counter by

        This is equivalent to ``self.incr(path, -value)``.

        """
        self.inject_metric(f'counters.{path}', str(-value), 'c')

    def gauge(self, path: str, value: int, delta: bool = False) -> None:
        """Manipulate a gauge metric.

        :param path: gauge to adjust
        :param value: value to send
        :param delta: is this an adjustment of the gauge?

        If the `delta` parameter is ``False`` (or omitted), then
        `value` is the new value to set the gauge to.  Otherwise,
        `value` is an adjustment for the current gauge.

        """
        if delta:
            payload = f'{value:+d}'
        else:
            payload = str(value)
        self.inject_metric(f'gauges.{path}', payload, 'g')

    def timing(self, path: str,
               seconds: typing.Union[float, datetime.timedelta]) -> None:
        """Send a timer metric.

        :param path: timer to append a value to
        :param seconds: number of **seconds** to record

        """
        if isinstance(seconds, datetime.timedelta):
            seconds = seconds.total_seconds()
        self.inject_metric(f'timers.{path}', str(seconds * 1000.0), 'ms')

    def timer(self, path: str) -> Timer:
        """Send a timer metric using a context manager.

        :param path: timer to append the measured time to

        """
        return Timer(self, path)


class Connector(AbstractConnector):
    """Sends metrics to a statsd server.

    :param host: statsd server to send metrics to
    :param port: socket port that the server is listening on
    :keyword ip_protocol: IP protocol to use for the underlying
        socket -- either ``socket.IPPROTO_TCP`` for TCP or
        ``socket.IPPROTO_UDP`` for UDP sockets.
    :keyword prefix: optional string to prepend to metric paths
    :param kwargs: additional keyword parameters are passed
        to the :class:`.Processor` initializer

    This class maintains a connection to a statsd server and
    sends metric lines to it asynchronously.  You must call the
    :meth:`start` method when your application is starting.  It
    creates a :class:`~asyncio.Task` that manages the connection
    to the statsd server.  You must also call :meth:`.stop` before
    terminating to ensure that all metrics are flushed to the
    statsd server.

    Metrics are optionally prefixed with :attr:`prefix` before the
    metric type prefix.  This *should* be used to prevent metrics
    from being overwritten when multiple applications share a StatsD
    instance.  Each metric type is also prefixed by one of the
    following strings based on the metric type:

    +-------------------+---------------+-----------+
    | Method call       | Prefix        | Type code |
    +-------------------+---------------+-----------+
    | :meth:`.incr`     | ``counters.`` | ``c``     |
    +-------------------+---------------+-----------+
    | :meth:`.decr`     | ``counters.`` | ``c``     |
    +-------------------+---------------+-----------+
    | :meth:`.gauge`    | ``gauges.``   | ``g``     |
    +-------------------+---------------+-----------+
    | :meth:`.timing`   | ``timers.``   | ``ms``    |
    +-------------------+---------------+-----------+

    When the connector is *should_terminate*, metric payloads are
    sent by calling the :meth:`.inject_metric` method.  The payloads
    are stored in an internal queue that is consumed whenever the
    connection to the server is active.

    .. attribute:: prefix
       :type: str

       String to prefix to all metrics *before* the metric type prefix.

    .. attribute:: processor
       :type: Processor

       The statsd processor that maintains the connection and
       sends the metric payloads.

    """
    logger: logging.Logger
    prefix: str
    processor: 'Processor'

    def __init__(self,
                 host: str,
                 port: int = 8125,
                 *,
                 prefix: str = '',
                 **kwargs: typing.Any) -> None:
        super().__init__()
        self.logger = logging.getLogger(__package__).getChild('Connector')
        self.prefix = f'{prefix}.' if prefix else prefix
        self.processor = Processor(host=host, port=port, **kwargs)
        self._enqueue_log_guard = ThrottleGuard(100)
        self._processor_task: typing.Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Start the processor in the background.

        This is a *blocking* method and does not return until the
        processor task is actually running.

        """
        self._processor_task = asyncio.create_task(self.processor.run())
        await self.processor.running.wait()

    async def stop(self) -> None:
        """Stop the background processor.

        Items that are currently in the queue will be flushed to
        the statsd server if possible.  This is a *blocking* method
        and does not return until the background processor has
        stopped.

        """
        await self.processor.stop()

    def inject_metric(self, path: str, value: str, type_code: str) -> None:
        """Send a metric to the statsd server.

        :param path: formatted metric name
        :param value: formatted metric value
        :param type_code: type of the metric to send

        This method formats the payload and inserts it on the
        internal queue for future processing.

        """
        payload = f'{self.prefix}{path}:{value}|{type_code}'
        try:
            self.processor.enqueue(payload.encode('utf-8'))
            self._enqueue_log_guard.reset()
        except asyncio.QueueFull:
            if self._enqueue_log_guard.allow_execution():
                self.logger.warning('statsd queue is full, discarding metric')


class StatsdProtocol(asyncio.BaseProtocol):
    """Common interface for backend protocols/transports.

    UDP and TCP transports have different interfaces (sendto vs write)
    so this class adapts them to a common protocol that our code
    can depend on.

    .. attribute:: buffered_data
       :type: bytes

       Bytes that are buffered due to low-level transport failures.
       Since protocols & transports are created anew with each connect
       attempt, the :class:`.Processor` instance ensures that data
       buffered on a transport is copied over to the new transport
       when creating a connection.

    .. attribute:: connected
       :type: asyncio.Event

       Is the protocol currently connected?

    """
    buffered_data: bytes
    ip_protocol: int = socket.IPPROTO_NONE
    logger: logging.Logger
    transport: typing.Optional[asyncio.BaseTransport]

    def __init__(self) -> None:
        self.buffered_data = b''
        self.connected = asyncio.Event()
        self.logger = logging.getLogger(__package__).getChild(
            self.__class__.__name__)
        self.transport = None

    def send(self, metric: bytes) -> None:
        """Send a metric payload over the transport."""
        raise NotImplementedError()

    async def shutdown(self) -> None:
        """Shutdown the transport and wait for it to close."""
        raise NotImplementedError()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Capture the new transport and set the connected event."""
        # NB - this will return a 4-part tuple in some cases
        server, port = transport.get_extra_info('peername')[:2]
        self.logger.info('connected to statsd %s:%s', server, port)
        self.transport = transport
        self.transport.set_protocol(self)
        self.connected.set()

    def connection_lost(self, exc: typing.Optional[Exception]) -> None:
        """Clear the connected event."""
        self.logger.warning('statsd server connection lost: %s', exc)
        self.connected.clear()


class TCPProtocol(StatsdProtocol, asyncio.Protocol):
    """StatsdProtocol implementation over a TCP/IP connection."""
    ip_protocol = socket.IPPROTO_TCP
    transport: asyncio.WriteTransport

    def eof_received(self) -> None:
        self.logger.warning('received EOF from statsd server')
        self.connected.clear()

    def send(self, metric: bytes) -> None:
        """Send `metric` to the server.

        If sending the metric fails, it will be saved in
        ``self.buffered_data``.  The processor will save and
        restore the buffered data if it needs to create a
        new protocol object.

        """
        if not self.buffered_data and not metric:
            return

        self.buffered_data = self.buffered_data + metric + b'\n'
        while (self.transport is not None and self.connected.is_set()
               and self.buffered_data):
            line, maybe_nl, rest = self.buffered_data.partition(b'\n')
            line += maybe_nl
            self.transport.write(line)
            if self.transport.is_closing():
                self.logger.warning('transport closed during write')
                break
            self.buffered_data = rest

    async def shutdown(self) -> None:
        """Close the transport after flushing any outstanding data."""
        self.logger.info('shutting down')
        if self.connected.is_set():
            self.send(b'')  # flush buffered data
            self.transport.close()
            while self.connected.is_set():
                await asyncio.sleep(0.1)


class UDPProtocol(StatsdProtocol, asyncio.DatagramProtocol):
    """StatsdProtocol implementation over a UDP/IP connection."""
    ip_protocol = socket.IPPROTO_UDP
    transport: asyncio.DatagramTransport

    def send(self, metric: bytes) -> None:
        if metric:
            self.transport.sendto(metric)

    async def shutdown(self) -> None:
        self.logger.info('shutting down')
        self.transport.close()


class Processor:
    """Maintains the statsd connection and sends metric payloads.

    :param host: statsd server to send metrics to
    :param port: TCP port that the server is listening on
    :param max_queue_size: only allow this many elements to be
        stored in the queue before discarding metrics
    :param reconnect_sleep: number of seconds to sleep after socket
        error occurs when connecting
    :param wait_timeout: number os seconds to wait for a message to
        arrive on the queue

    This class implements :class:`~asyncio.Protocol` for the statsd
    TCP connection.  The :meth:`.run` method is run as a background
    :class:`~asyncio.Task` that consumes payloads from an internal
    queue, connects to the TCP server as required, and sends the
    already formatted payloads.

    .. attribute:: host
       :type: str

       IP address or DNS name for the statsd server to send metrics to

    .. attribute:: port
       :type: int

       TCP port number that the statsd server is listening on

    .. attribute:: should_terminate
       :type: bool

       Flag that controls whether the background task is active or
       not.  This flag is set to :data:`False` when the task is started.
       Setting it to :data:`True` will cause the task to shutdown in
       an orderly fashion.

    .. attribute:: queue
       :type: asyncio.Queue

       Formatted metric payloads to send to the statsd server.  Enqueue
       payloads to send them to the server.

    .. attribute:: running
       :type: asyncio.Event

       Is the background task currently running?  This is the event that
       :meth:`.run` sets when it starts and it remains set until the task
       exits.

    .. attribute:: stopped
       :type: asyncio.Event

       Is the background task currently stopped?  This is the event that
       :meth:`.run` sets when it exits and that :meth:`.stop` blocks on
       until the task stops.

    """

    logger: logging.Logger
    protocol: typing.Optional[StatsdProtocol]
    queue: asyncio.Queue
    _create_transport: typing.Callable[[], typing.Coroutine[
        typing.Any, typing.Any, typing.Tuple[asyncio.BaseTransport,
                                             StatsdProtocol]]]

    def __init__(self,
                 *,
                 host: str,
                 port: int = 8125,
                 ip_protocol: int = socket.IPPROTO_TCP,
                 max_queue_size: int = 1000,
                 reconnect_sleep: float = 1.0,
                 wait_timeout: float = 0.1) -> None:
        super().__init__()
        if not host:
            raise RuntimeError('host must be set')
        try:
            port = int(port)
            if not port or port < 1:
                raise RuntimeError(
                    f'port must be a positive integer: {port!r}')
        except (TypeError, ValueError):
            raise RuntimeError(f'port must be a positive integer: {port!r}')

        transport_creators = {
            socket.IPPROTO_TCP: self._create_tcp_transport,
            socket.IPPROTO_UDP: self._create_udp_transport,
        }
        try:
            factory = transport_creators[ip_protocol]
        except KeyError:
            raise RuntimeError(f'ip_protocol {ip_protocol} is not supported')
        else:
            self._create_transport = factory  # type: ignore

        self.host = host
        self.port = port
        self._ip_protocol = ip_protocol
        self._connect_log_guard = ThrottleGuard(100)
        self._reconnect_sleep = reconnect_sleep
        self._wait_timeout = wait_timeout

        self.running = asyncio.Event()
        self.stopped = asyncio.Event()
        self.stopped.set()
        self.logger = logging.getLogger(__package__).getChild('Processor')
        self.should_terminate = False
        self.protocol = None
        self.queue = asyncio.Queue(maxsize=max_queue_size)

    @property
    def connected(self) -> bool:
        """Is the processor connected?"""
        return self.protocol is not None and self.protocol.connected.is_set()

    def enqueue(self, metric: bytes) -> None:
        self.queue.put_nowait(metric)

    async def run(self) -> None:
        """Maintains the connection and processes metric payloads."""
        self.running.set()
        self.stopped.clear()
        self.should_terminate = False
        while not self.should_terminate:
            try:
                await self._connect_if_necessary()
                if self.connected:
                    await self._process_metric()
            except asyncio.CancelledError:
                self.logger.info('task cancelled, exiting')
                self.should_terminate = True
            except Exception as error:
                self.logger.exception('unexpected error occurred: %s', error)
                self.should_terminate = True

        self.should_terminate = True
        self.logger.info('loop finished with %d metrics in the queue',
                         self.queue.qsize())
        if self.connected:
            num_ready = max(self.queue.qsize(), 1)
            self.logger.info('draining %d metrics', num_ready)
            for _ in range(num_ready):
                await self._process_metric()
            self.logger.debug('closing transport')
        if self.protocol is not None:
            await self.protocol.shutdown()

        self.logger.info('processor is exiting')
        self.running.clear()
        self.stopped.set()

    async def stop(self) -> None:
        """Stop the processor.

        This is an asynchronous but blocking method.  It does not
        return until enqueued metrics are flushed and the processor
        connection is closed.

        """
        self.should_terminate = True
        await self.stopped.wait()

    async def _create_tcp_transport(
            self) -> typing.Tuple[asyncio.BaseTransport, StatsdProtocol]:
        t, p = await asyncio.get_running_loop().create_connection(
            protocol_factory=TCPProtocol, host=self.host, port=self.port)
        return t, typing.cast(StatsdProtocol, p)

    async def _create_udp_transport(
            self) -> typing.Tuple[asyncio.BaseTransport, StatsdProtocol]:
        t, p = await asyncio.get_running_loop().create_datagram_endpoint(
            protocol_factory=UDPProtocol,
            remote_addr=(self.host, self.port),
            reuse_port=True)
        return t, typing.cast(StatsdProtocol, p)

    async def _connect_if_necessary(self) -> None:
        if self.protocol is not None:
            try:
                await asyncio.wait_for(self.protocol.connected.wait(),
                                       self._wait_timeout)
            except asyncio.TimeoutError:
                self.logger.debug('protocol is no longer connected')

        if not self.connected:
            try:
                buffered_data = b''
                if self.protocol is not None:
                    buffered_data = self.protocol.buffered_data

                t, p = await self._create_transport()  # type: ignore[misc]
                transport, self.protocol = t, p
                self.protocol.buffered_data = buffered_data
                self.logger.info(
                    'connection established to %s after %s attempts',
                    transport.get_extra_info('peername'),
                    self._connect_log_guard.counter)
                self._connect_log_guard.reset()
            except IOError as error:
                if self._connect_log_guard.allow_execution():
                    self.logger.warning(
                        'connection to %s:%s failed: %s (%s attempts)',
                        self.host, self.port, error,
                        self._connect_log_guard.counter)
                await asyncio.sleep(self._reconnect_sleep)

    async def _process_metric(self) -> None:
        try:
            metric = await asyncio.wait_for(self.queue.get(),
                                            self._wait_timeout)
            self.logger.debug('received %r from queue', metric)
            self.queue.task_done()
        except asyncio.TimeoutError:
            # we still want to invoke the protocol send in case
            # it has queued metrics to send
            metric = b''

        assert self.protocol is not None  # AFAICT, this cannot happen
        self.protocol.send(metric)
