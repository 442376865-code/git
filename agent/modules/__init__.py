"""业务模块包。

统一导出模块基类与各业务数据库，供各业务模块继承或复用。
"""

from .base import MedicalModule
from .health_db import HealthDB
from .knowledge_db import KnowledgeDB
from .medical_navigation_db import MedicalNavigationDB
from .medication_db import MedicationDB

__all__ = [
    "MedicalModule",
    "HealthDB",
    "KnowledgeDB",
    "MedicationDB",
    "MedicalNavigationDB",
]