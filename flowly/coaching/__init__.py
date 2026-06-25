"""Meeting Coach — real-time coaching during meetings.

Listens to audio segments, transcribes them, and uses a 3-stage gate
pipeline to decide when to interject with helpful tips. Integrates
with KG and memory so tips are grounded in the user's knowledge base.
"""

from .manager import CoachingManager
from .gate import FREQUENCY_THRESHOLDS

__all__ = ["CoachingManager", "FREQUENCY_THRESHOLDS"]
