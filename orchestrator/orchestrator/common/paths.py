from pathlib import Path


ARTIFACTS_DIR = str(Path.home() / '.ros' / 'orchestrator_captures')   # files skills save for the agent and the recorder
STATE_DIR     = Path.home() / 'orch_state'                             # what the robot keeps across runs
RUNS_DIR      = Path.home() / 'orch_runs'                              # one folder per recorded run
