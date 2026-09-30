"""Source registry for managing job sources"""

import importlib
from typing import Dict, List, Optional

from hirevia.config import Config
from hirevia.sources.base import JobSource

# Source name -> module path
SOURCE_MODULES = {
    "greenhouse": "hirevia.sources.greenhouse",
    "lever": "hirevia.sources.lever",
    "ashby": "hirevia.sources.ashby",
    "wellfound": "hirevia.sources.wellfound",
    "company_careers": "hirevia.sources.company_careers",
    "smartrecruiters": "hirevia.sources.smartrecruiters",
    "naukri": "hirevia.sources.naukri",
    "linkedin": "hirevia.sources.linkedin",
    "indeed": "hirevia.sources.indeed",
    "internshala": "hirevia.sources.internshala",
    "cutshort": "hirevia.sources.cutshort",
    "hirist": "hirevia.sources.hirist",
    "instahyre": "hirevia.sources.instahyre",
    "unstop": "hirevia.sources.unstop",
    "telegram": "hirevia.sources.telegram_source",
    "whatsapp": "hirevia.sources.whatsapp_source",
}


class SourceRegistry:
    def __init__(self, config: Config):
        self.config = config
        self._sources: Dict[str, JobSource] = {}
        self._load_sources()

    def _load_sources(self):
        """Load enabled sources."""
        for source_name in self.config.sources.get_enabled_sources():
            if source_name in SOURCE_MODULES:
                try:
                    module = importlib.import_module(SOURCE_MODULES[source_name])
                    # Look for a class named {SourceName}Source or similar
                    class_name = "".join(word.capitalize() for word in source_name.split("_")) + "Source"
                    if hasattr(module, class_name):
                        source_class = getattr(module, class_name)
                        self._sources[source_name] = source_class()
                    else:
                        # Try to find any JobSource subclass
                        for attr_name in dir(module):
                            attr = getattr(module, attr_name)
                            if isinstance(attr, type) and issubclass(attr, JobSource) and attr != JobSource:
                                self._sources[source_name] = attr()
                                break
                    logger.debug("Loaded source: %s", source_name)
                except Exception as e:
                    logger.warning(f"Failed to load source {source_name}: {e}")
            else:
                logger.warning(f"Unknown source: {source_name}")

    def get_enabled_sources(self) -> List[JobSource]:
        return list(self._sources.values())

    def get_source(self, name: str) -> Optional[JobSource]:
        return self._sources.get(name)

    def list_sources(self) -> List[str]:
        return list(self._sources.keys())


import logging
logger = logging.getLogger(__name__)