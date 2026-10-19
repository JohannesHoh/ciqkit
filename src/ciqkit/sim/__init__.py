"""A broker for the Connect IQ Simulator.

The simulator is a single machine-wide GUI that runs one app at a time, so
everything that drives it -- screenshot runs, test suites, interactive
sessions, several coding agents at once -- has to take turns. This package puts
one owner in front of it and schedules preemptible, chunked jobs by priority.

See ``docs/design/broker.md`` for the rationale and the scheduler model.
"""
from .protocol import Job, Chunk, ChunkKind, Priority, JobState, Op

__all__ = ["Job", "Chunk", "ChunkKind", "Priority", "JobState", "Op"]
