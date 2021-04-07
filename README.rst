Asynchronously send metrics to a statsd_ instance.

|build| |coverage| |sonar| |docs| |source|

.. COMMENTED OUT FOR THE TIME BEING
   |docs| |download| |license|

This library provides connectors to send metrics to a statsd_ instance using either TCP or UDP.

.. code-block:: python

   import asyncio
   import time

   import sprockets_statsd.statsd

   statsd = sprockets_statsd.statsd.Connector(
      host=os.environ.get('STATSD_HOST', '127.0.0.1'))

   async def do_stuff():
      start = time.time()
      response = make_some_http_call()
      statsd.timing(f'timers.http.something.{response.code}',
                    (time.time() - start))

   async def main():
      await statsd.start()
      try:
         do_stuff()
      finally:
         await statsd.stop()

The ``Connector`` instance maintains a resilient connection to the target StatsD instance, formats the metric data
into payloads, and sends them to the StatsD target.  It defaults to using TCP as the transport but will use UDP if
the ``ip_protocol`` keyword is set to ``socket.IPPROTO_UDP``.  The ``Connector.start`` method starts a background
``asyncio.Task`` that is responsible for maintaining the connection.  The ``timing`` method enqueues a timing
metric to send and the task consumes the internal queue when it is connected.

The following convenience methods are available.  You can also call ``inject_metric`` for complete control over
the payload.

+--------------+--------------------------------------+
| ``incr``     | Increment a counter metric           |
+--------------+--------------------------------------+
| ``decr``     | Decrement a counter metric           |
+--------------+--------------------------------------+
| ``gauge``    | Adjust or set a gauge metric         |
+--------------+--------------------------------------+
| ``timing``   | Append a duration to a timer metric  |
+--------------+--------------------------------------+

Tornado helpers
===============
The ``sprockets_statsd.tornado`` module contains mix-in classes that make reporting metrics from your tornado_ web
application simple.  You will need to install the ``sprockets_statsd[tornado]`` extra to ensure that the Tornado
requirements for this library are met.

.. code-block:: python

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
           settings['statsd'] = {
               'host': os.environ['STATSD_HOST'],
               'prefix': 'applications.my-service',
           }
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

   applications.my-service.timers.some-operation:1001.3449192047119|ms
   applications.my-service.timers.MyHandler.GET.204:1002.4960041046143|ms

You will need to set the ``$STATSD_HOST`` environment variable to enable the statsd processing inside of the
application.  The ``RequestHandler`` class exposes methods that send counter and timing metrics to a statsd server.
The connection is managed by the ``Application`` provided that you call the ``start_statsd`` method during application
startup.

Metrics are sent by a ``asyncio.Task`` that is started by ``start_statsd``.  The request handler methods insert the
metric data onto a ``asyncio.Queue`` that the task reads from.  Metric data remains on the queue when the task is
not connected to the server and will be sent in the order received when the task establishes the server connection.

.. _statsd: https://github.com/statsd/statsd/
.. _tornado: https://tornadoweb.org/

.. |build| image:: https://img.shields.io/github/workflow/status/sprockets/sprockets-statsd/Testing/main?style=social
   :target: https://github.com/sprockets/sprockets-statsd/actions/workflows/run-tests.yml
.. |coverage| image:: https://img.shields.io/codecov/c/github/sprockets/sprockets-statsd?style=social
   :target: https://app.codecov.io/gh/sprockets/sprockets-statsd
.. |docs| image:: https://img.shields.io/readthedocs/sprockets-statsd.svg?style=social
   :target: https://sprockets-statsd.readthedocs.io/en/latest/?badge=latest
.. |download| image:: https://img.shields.io/pypi/pyversions/sprockets-statsd.svg?style=social
   :target: https://pypi.org/project/sprockets-statsd/
.. |license| image:: https://img.shields.io/pypi/l/sprockets-statsd.svg?style=social
   :target: https://github.com/sprockets/sprockets-statsd/blob/master/LICENSE.txt
.. |sonar| image:: https://img.shields.io/sonar/quality_gate/sprockets_sprockets-statsd?server=https%3A%2F%2Fsonarcloud.io&style=social
   :target: https://sonarcloud.io/dashboard?id=sprockets_sprockets-statsd
.. |source| image:: https://img.shields.io/badge/source-github.com-green.svg?style=social
   :target: https://github.com/sprockets/sprockets-statsd
