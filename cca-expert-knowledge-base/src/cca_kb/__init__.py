"""Expert design-experience knowledge base for TCP/QUIC congestion control.

Public surface:
    cca_kb.api          - stable, framework-agnostic access functions
    cca_kb.repository   - YAML repository loader
    cca_kb.retrieval    - hybrid retrieval service
    cca_kb.validation   - schema and integrity checks
"""

from cca_kb.config import Settings
from cca_kb.models import DesignExperience, Source

__all__ = ["Settings", "DesignExperience", "Source"]
__version__ = "0.1.0"
