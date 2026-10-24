"""`python3 -m agent "task"` runs the native computer-use loop (agent.cu). The grounded planner loop stays at
`python3 -m agent.run`."""
import sys

from .cu import main

sys.exit(main())
