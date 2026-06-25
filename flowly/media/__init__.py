"""Generated-media providers (image / video / audio).

A small, provider-agnostic layer so generation tools (``image_generate`` and,
later, video/speech) share one shape: a curated model catalog + a backend client
that produces a file under ``<flowly home>/media`` for the existing delivery path
(``message`` media attachments → ``/api/media`` + channel uploads).
"""
