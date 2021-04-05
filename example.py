import asyncio
import logging

from tornado import ioloop, web

import sprockets_statsd.tornado


class MyHandler(sprockets_statsd.tornado.RequestHandler,
                web.RequestHandler):
    async def get(self):
        with self.execution_timer('some-operation'):
            await self.do_something()
        self.set_status(204)

    async def do_something(self):
        await asyncio.sleep(1)


class Application(sprockets_statsd.tornado.Application, web.Application):
    def __init__(self, **settings):
        super().__init__([web.url('/', MyHandler)], **settings)

    async def on_start(self):
        await self.start_statsd()

    async def on_stop(self):
        await self.stop_statsd()


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    app = Application()
    app.listen(8888)
    iol = ioloop.IOLoop.current()
    try:
        iol.add_callback(app.on_start)
        iol.start()
    except KeyboardInterrupt:
        iol.add_future(asyncio.ensure_future(app.on_stop()),
                       lambda f: iol.stop())
        iol.start()
