import asyncio
import logging
import typing


class Processor(asyncio.Protocol):
    def __init__(self, *, host, port: int = 8125, **kwargs):
        super().__init__(**kwargs)
        self.host = host
        self.port = port

        self.closed = asyncio.Event()
        self.connected = asyncio.Event()
        self.logger = logging.getLogger(__package__).getChild('Processor')
        self.running = False
        self.transport = None

        self._queue = asyncio.Queue()

    async def run(self):
        self.running = True
        while self.running:
            try:
                await self._connect_if_necessary()
                await self._process_metric()
            except asyncio.CancelledError:
                self.logger.info('task cancelled, exiting')
                break

        self.running = False
        self.logger.info('loop finished with %d metrics in the queue',
                         self._queue.qsize())
        if self.connected.is_set():
            num_ready = self._queue.qsize()
            self.logger.info('draining %d metrics', num_ready)
            for _ in range(num_ready):
                await self._process_metric()
            self.logger.debug('closing transport')
            self.transport.close()

        while self.connected.is_set():
            self.logger.debug('waiting on transport to close')
            await asyncio.sleep(0.1)

        self.logger.info('processor is exiting')
        self.closed.set()

    async def stop(self):
        self.running = False
        await self.closed.wait()

    def inject_metric(self, path: str, value: typing.Union[float, int, str],
                      type_code: str):
        payload = f'{path}:{value}|{type_code}\n'
        self._queue.put_nowait(payload.encode('utf-8'))

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
        try:
            metric = await asyncio.wait_for(self._queue.get(), 0.1)
        except asyncio.TimeoutError:
            return  # nothing to do
        else:
            self.transport.write(metric)
            self._queue.task_done()
