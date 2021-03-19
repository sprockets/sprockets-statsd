import asyncio
import logging
import typing


class Connector:
    """Sends metrics to a statsd server.

    :param host: statsd server to send metrics to
    :param port: TCP port that the server is listening on
    :param kwargs: additional keyword parameters are passed
        to the :class:`.Processor` initializer

    This class maintains a TCP connection to a statsd server and
    sends metric lines to it asynchronously.  You must call the
    :meth:`start` method when your application is starting.  It
    creates a :class:`~asyncio.Task` that manages the connection
    to the statsd server.  You must also call :meth:`.stop` before
    terminating to ensure that all metrics are flushed to the
    statsd server.

    When the connector is *should_terminate*, metric payloads are sent by
    calling the :meth:`.inject_metric` method.  The payloads are
    stored in an internal queue that is consumed whenever the
    connection to the server is active.

    .. attribute:: processor
       :type: Processor

       The statsd processor that maintains the connection and
       sends the metric payloads.

    """
    def __init__(self, host: str, port: int = 8125, **kwargs):
        self.processor = Processor(host=host, port=port, **kwargs)
        self._processor_task = None

    async def start(self):
        """Start the processor in the background.

        This is a *blocking* method and does not return until the
        processor task is actually running.

        """
        self._processor_task = asyncio.create_task(self.processor.run())
        await self.processor.running.wait()

    async def stop(self):
        """Stop the background processor.

        Items that are currently in the queue will be flushed to
        the statsd server if possible.  This is a *blocking* method
        and does not return until the background processor has
        stopped.

        """
        await self.processor.stop()

    def inject_metric(self, path: str, value, type_code: str):
        """Send a metric to the statsd server.

        :param path: formatted metric name
        :param value: metric value as a number or a string.  The
            string form is required for relative gauges.
        :param type_code: type of the metric to send

        This method formats the payload and inserts it on the
        internal queue for future processing.

        """
        payload = f'{path}:{value}|{type_code}'
        self.processor.queue.put_nowait(payload.encode('utf-8'))


class StatsdProtocol:
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
    logger: logging.Logger

    def __init__(self):
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

    def connection_made(self, transport: asyncio.Transport):
        """Capture the new transport and set the connected event."""
        server, port = transport.get_extra_info('peername')
        self.logger.info('connected to statsd %s:%s', server, port)
        self.transport = transport
        self.connected.set()

    def connection_lost(self, exc: typing.Optional[Exception]):
        """Clear the connected event."""
        self.logger.warning('statsd server connection lost: %s', exc)
        self.connected.clear()


class TCPProtocol(StatsdProtocol, asyncio.Protocol):
    """StatsdProtocol implementation over a TCP/IP connection."""
    transport: asyncio.WriteTransport

    def eof_received(self):
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


class Processor:
    """Maintains the statsd connection and sends metric payloads.

    :param host: statsd server to send metrics to
    :param port: TCP port that the server is listening on
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

    protocol: typing.Union[StatsdProtocol, None]

    def __init__(self,
                 *,
                 host,
                 port: int = 8125,
                 reconnect_sleep: float = 1.0,
                 wait_timeout: float = 0.1):
        super().__init__()
        if not host:
            raise RuntimeError('host must be set')
        try:
            port = int(port)
            if not port or port < 1:
                raise RuntimeError(
                    f'port must be a positive integer: {port!r}')
        except TypeError:
            raise RuntimeError(f'port must be a positive integer: {port!r}')

        self.host = host
        self.port = port
        self._reconnect_sleep = reconnect_sleep
        self._wait_timeout = wait_timeout

        self.running = asyncio.Event()
        self.stopped = asyncio.Event()
        self.stopped.set()
        self.logger = logging.getLogger(__package__).getChild('Processor')
        self.should_terminate = False
        self.protocol = None
        self.queue = asyncio.Queue()

        self._failed_sends = []

    @property
    def connected(self) -> bool:
        """Is the processor connected?"""
        if self.protocol is None:
            return False
        return self.protocol.connected.is_set()

    async def run(self):
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
                break

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

    async def stop(self):
        """Stop the processor.

        This is an asynchronous but blocking method.  It does not
        return until enqueued metrics are flushed and the processor
        connection is closed.

        """
        self.should_terminate = True
        await self.stopped.wait()

    async def _connect_if_necessary(self):
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
                loop = asyncio.get_running_loop()
                transport, protocol = await loop.create_connection(
                    protocol_factory=TCPProtocol,
                    host=self.host,
                    port=self.port)
                self.protocol = typing.cast(TCPProtocol, protocol)
                self.protocol.buffered_data = buffered_data
                self.logger.info('connection established to %s',
                                 transport.get_extra_info('peername'))
            except IOError as error:
                self.logger.warning('connection to %s:%s failed: %s',
                                    self.host, self.port, error)
                await asyncio.sleep(self._reconnect_sleep)

    async def _process_metric(self):
        try:
            metric = await asyncio.wait_for(self.queue.get(),
                                            self._wait_timeout)
            self.logger.debug('received %r from queue', metric)
            self.queue.task_done()
        except asyncio.TimeoutError:
            # we still want to invoke the protocol send in case
            # it has queued metrics to send
            metric = b''

        try:
            self.protocol.send(metric)
        except Exception as error:
            self.logger.exception('exception occurred when sending metric: %s',
                                  error)
