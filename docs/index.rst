sprockets-statsd
================

.. include:: ../README.rst

Tornado configuration
=====================
The Tornado statsd connection is configured by the ``statsd`` application settings key.  The default values can be set
by the following environment variables.

.. envvar:: STATSD_HOST

   The host or IP address of the StatsD server to send metrics to.

.. envvar:: STATSD_PORT

   The TCP port number that the StatsD server is listening on.  This defaults to 8125 if it is not configured.

.. envvar:: STATSD_PREFIX

   Optional prefix to use for metric paths.  See the documentation for :class:`~sprockets_statsd.tornado.Application`
   for addition notes on setting the path prefix when using the Tornado helpers.

.. envvar:: STATSD_PROTOCOL

   The IP protocol to use when connecting to the StatsD server.  You can specify either "tcp" or "udp".  The
   default is "tcp" if it not not configured.

.. envvar:: STATSD_ENABLED

   Define this variable and set it to a *falsy* value to **disable** the Tornado integration.  If you omit
   this variable, then the connector is enabled.  The following values are considered *truthy*:

   - non-zero integer
   - case-insensitive match of ``yes``, ``true``, ``t``, or ``on``

   All other values are considered *falsy*.  You only want to define this environment variables when you
   want to explicitly disable an otherwise installed and configured connection.

If you are using the Tornado helper clases, then you can fine tune the metric payloads and the connector by
setting additional values in the ``statsd`` key of :attr:`tornado.web.Application.settings`.  See the
:class:`sprockets_statsd.tornado.Application` class documentation for a description of the supported settings.

Reference
=========

.. autoclass:: sprockets_statsd.statsd.Connector
   :members:

Tornado helpers
---------------
.. autoclass:: sprockets_statsd.tornado.Application
   :members:

.. autoclass:: sprockets_statsd.tornado.RequestHandler
   :members:

Internals
---------
.. autoclass:: sprockets_statsd.statsd.AbstractConnector
   :members:

.. autoclass:: sprockets_statsd.statsd.Processor
   :members:

.. autoclass:: sprockets_statsd.statsd.StatsdProtocol
   :members:

.. autoclass:: sprockets_statsd.statsd.TCPProtocol
   :members:

Release history
===============

.. include:: ../CHANGELOG.rst

.. toctree::
   :maxdepth: 2
