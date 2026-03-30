# SPDX-License-Identifier: Apache-2.0
"""NCMS memory provider plugin for NVIDIA NeMo Agent Toolkit.

Importing this module triggers the @register_memory and @register_function
decorators, making ncms_memory, ask_knowledge, and announce_knowledge
available as NAT config types.
"""

from nat.plugins.ncms.config import NCMSMemoryConfig
from nat.plugins.ncms.editor import NCMSMemoryEditor

# Import to trigger decorator registration
from nat.plugins.ncms import register as _register  # noqa: F401
from nat.plugins.ncms import tools as _tools  # noqa: F401

__all__ = ["NCMSMemoryConfig", "NCMSMemoryEditor"]
