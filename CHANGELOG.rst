Next release
------------
- Added ``Connector.timer`` method (addresses :issue:`8`)

:tag:`0.1.0 <0.0.1...0.1.0>` (10-May-2021)
------------------------------------------
- Added :envvar:`STATSD_ENABLED` environment variable to disable the Tornado integration
- Tornado application mixin automatically installs start/stop hooks if the application
  quacks like a ``sprockets.http.app.Application``.
- Limit logging when disconnected from statsd

:tag:`0.0.1 <832f8af7...0.0.1>` (08-Apr-2021)
---------------------------------------------
- Simple support for sending counters & timers to statsd over a TCP or UDP socket
