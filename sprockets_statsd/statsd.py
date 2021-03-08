import asyncio
import logging
import typing


class Connector:
    """Sends metrics to a statsd server.

    :param host: statsd server to send metrics to
    :param port: TCP port that the server is listening on

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
    def __init__(self, host: str, port: int = 8125):
        self.processor = Processor(host=host, port=port)
        self._processor_task = None

    async def start(self):
        """Start the processor in the background."""
        self._processor_task = asyncio.create_task(self.processor.run())

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
        payload = f'{path}:{value}|{type_code}\n'
        self.processor.queue.put_nowait(payload.encode('utf-8'))


class Processor(asyncio.Protocol):
    """Maintains the statsd connection and sends metric payloads.

    :param host: statsd server to send metrics to
    :param port: TCP port that the server is listening on

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

    .. attribute:: connected
       :type: asyncio.Event

       Is the TCP connection currently connected?

    .. attribute:: stopped
       :type: asyncio.Event

       Is the background task currently stopped?  This is the event that
       :meth:`.run` sets when it exits and that :meth:`.stop` blocks on
       until the task stops.

    """
    def __init__(self, *, host, port: int = 8125):
        super().__init__()
        self.host = host
        self.port = port

        self.stopped = asyncio.Event()
        self.stopped.set()
        self.connected = asyncio.Event()
        self.logger = logging.getLogger(__package__).getChild('Processor')
        self.should_terminate = False
        self.transport = None
        self.queue = asyncio.Queue()

        self._failed_sends = []

    async def run(self):
        """Maintains the connection and processes metric payloads."""
        self.stopped.clear()
        self.should_terminate = False
        while not self.should_terminate:
            try:
                await self._connect_if_necessary()
                await self._process_metric()
            except asyncio.CancelledError:
                self.logger.info('task cancelled, exiting')
                break

        self.should_terminate = True
        self.logger.info('loop finished with %d metrics in the queue',
                         self.queue.qsize())
        if self.connected.is_set():
            num_ready = self.queue.qsize()
            self.logger.info('draining %d metrics', num_ready)
            for _ in range(num_ready):
                await self._process_metric()
            self.logger.debug('closing transport')
            self.transport.close()

        while self.connected.is_set():
            self.logger.debug('waiting on transport to close')
            await asyncio.sleep(0.1)

        self.logger.info('processor is exiting')
        self.stopped.set()

    async def stop(self):
        """Stop the processor.

        This is an asynchronous but blocking method.  It does not
        return until enqueued metrics are flushed and the processor
        connection is closed.

        """
        self.should_terminate = True
        await self.stopped.wait()

    def eof_received(self):
        self.logger.warning('received EOF from statsd server')
        self.connected.clear()

    def connection_made(self, transport: asyncio.Transport):
        server, port = transport.get_extra_info('peername')
        self.logger.info('connected to statsd %s:%s', server, port)
        self.transport = transport
        self.connected.set()

    def connection_lost(self, exc: typing.Optional[Exception]):
        self.logger.warning('statsd server connection lost')
        self.connected.clear()

    async def _connect_if_necessary(self, wait_time: float = 0.1):
        try:
            await asyncio.wait_for(self.connected.wait(), wait_time)
        except asyncio.TimeoutError:
            try:
                self.logger.debug('starting connection to %s:%s', self.host,
                                  self.port)
                await asyncio.get_running_loop().create_connection(
                    protocol_factory=lambda: self,
                    host=self.host,
                    port=self.port)
            except IOError as error:
                self.logger.warning('connection to %s:%s failed: %s',
                                    self.host, self.port, error)

    async def _process_metric(self):
        processing_failed_send = False
        if self._failed_sends:
            self.logger.debug('using previous send attempt')
            metric = self._failed_sends[0]
            processing_failed_send = True
        else:
            try:
                metric = await asyncio.wait_for(self.queue.get(), 0.1)
                self.logger.debug('received %r from queue', metric)
            except asyncio.TimeoutError:
                return
            else:
                # Since we `await`d the state of the transport may have
                # changed.  Sending on the closed transport won't return
                # an error since the send is async.  We can catch the
                # problem here though.
                if self.transport.is_closing():
                    self.logger.debug('preventing send on closed transport')
                    self._failed_sends.append(metric)
                    return

        self.transport.write(metric)
        if self.transport.is_closing():
            # Writing to a transport does not raise exceptions, it
            # will close the transport if a low-level error occurs.
            self.logger.debug('transport closed by writing')
        else:
            self.logger.debug('sent %r to statsd', metric)
            if processing_failed_send:
                self._failed_sends.pop(0)
            else:
                self.queue.task_done()
