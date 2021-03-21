================
sprockets-statsd
================

.. include:: ../README.rst

Configuration
=============
The statsd connection is configured by the ``statsd`` application settings key.  The default values can be set by
the following environment variables.

.. envvar:: STATSD_HOST

   The host or IP address of the StatsD server to send metrics to.

.. envvar:: STATSD_PORT

   The TCP port number that the StatsD server is listening on.  This defaults to 8125 if it is not configured.

.. envvar:: STATSD_PROTOCOL

   The IP protocol to use when connecting to the StatsD server.  You can specify either "tcp" or "udp".  The
   default is "tcp" if it not not configured.

You can fine tune the metric payloads and the connector by setting additional values in the ``statsd`` key of
:attr:`tornado.web.Application.settings`.  See the :class:`sprockets_statsd.mixins.Application` class
documentation for a description of the supported settings.

Reference
=========

.. autoclass:: sprockets_statsd.statsd.Connector
   :members:

Mixin classes
-------------
.. autoclass:: sprockets_statsd.mixins.Application
   :members:

.. autoclass:: sprockets_statsd.mixins.RequestHandler
   :members:

Internals
---------
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
