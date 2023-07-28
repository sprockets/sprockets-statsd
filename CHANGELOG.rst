Next
----
- Removed debug-level logging of processing metric
- Stop testing Python 3.7

:tag:`1.0.0 <0.1.0...1.0.0>` (20-Jul-2021)
------------------------------------------
- Added ``Connector.timer`` method (addresses :issue:`8`)
- Implement ``Timer`` abstraction from python-statsd
- ``Connector.timing`` now accepts :class:`datetime.timedelta` instances in addition
  to :class:`float` instances

:tag:`0.1.0 <0.0.1...0.1.0>` (10-May-2021)
------------------------------------------
- Added :envvar:`STATSD_ENABLED` environment variable to disable the Tornado integration
- Tornado application mixin automatically installs start/stop hooks if the application
  quacks like a ``sprockets.http.app.Application``.
- Limit logging when disconnected from statsd

:tag:`0.0.1 <832f8af7...0.0.1>` (08-Apr-2021)
---------------------------------------------
- Simple support for sending counters & timers to statsd over a TCP or UDP socket
