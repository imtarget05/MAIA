"""MAIA Video Pipeline — encode/decode/transcode module (T1).

Standalone module following the Service Desk conventions:
- ``VID_``-prefixed settings (``settings.py``)
- own PostgreSQL schema ``vid_`` via a separate ``Base`` (``models.py``)
- DB-backed job queue with lease/heartbeat (``worker.py``) — no broker
- FFmpeg subprocess engine with platform encoder profiles (``encoder.py``)
"""
