Report metrics from your tornado_ web application to a statsd_ instance.

.. code-block:: python

   import asyncio
   import logging
   
   from tornado import ioloop, web
   
   import sprockets_statsd.mixins
   
   
   class MyHandler(sprockets_statsd.mixins.RequestHandler,
                   web.RequestHandler):
       async def get(self):
           with self.execution_timer('some-operation'):
               await self.do_something()
           self.set_status(204)
   
       async def do_something(self):
           await asyncio.sleep(1)
   
   
   class Application(sprockets_statsd.mixins.Application, web.Application):
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

This application will emit two timing metrics each time that the endpoint is invoked::

   applications.timers.some-operation:1001.3449192047119|ms
   applications.timers.MyHandler.GET.204:1002.4960041046143|ms

You will need to set the ``$STATSD_HOST`` environment variable to enable the statsd processing inside of the
application.  The ``RequestHandler`` class exposes methods that send counter and timing metrics to a statsd server.
The connection is managed by the ``Application`` provided that you call the ``start_statsd`` method during application
startup.

Metrics are sent by a ``asyncio.Task`` that is started by ``start_statsd``.  The request handler methods insert the
metric data onto a ``asyncio.Queue`` that the task reads from.  Metric data remains on the queue when the task is
not connected to the server and will be sent in the order received when the task establishes the server connection.

.. _statsd: https://github.com/statsd/statsd/
.. _tornado: https://tornadoweb.org/

