"""Online evaluation of persisted agent runs.

The server imports only the modules that do not depend on deepeval:
``config``, ``goldens`` and ``repo``. ``adapter``, ``metrics``, ``evaluator``
and ``worker`` run inside the isolated ``bokaclaw-eval`` image, because
deepeval 4.3.3 requires a newer pydantic than the server pins.
"""
