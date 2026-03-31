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
from nat.plugins.ncms import research_agent as _research_agent  # noqa: F401
from nat.plugins.ncms import prd_agent as _prd_agent  # noqa: F401
from nat.plugins.ncms import design_agent as _design_agent  # noqa: F401
from nat.plugins.ncms import expert_agent as _expert_agent  # noqa: F401
from nat.plugins.ncms import archeologist_agent as _archeologist_agent  # noqa: F401

__all__ = ["NCMSMemoryConfig", "NCMSMemoryEditor"]
