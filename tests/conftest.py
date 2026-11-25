"""Keep product tests hermetic.

Tool availability probes may reach the workspace, the shell executor or the
network. Product tests replace the agent SDK with fakes and must not depend on
those dependencies, so probing is off unless a test turns it back on.
"""

import os

os.environ["AGENT_TOOL_PROBES"] = "0"
