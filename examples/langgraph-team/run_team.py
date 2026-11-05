"""Start the whole team (orchestrator + two sub-agents) and stop it with Ctrl+C.

    python run_team.py
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS = ["calculator_agent.py", "text_agent.py", "orchestrator_agent.py"]

if __name__ == "__main__":
    processes = [subprocess.Popen([sys.executable, os.path.join(HERE, name)], cwd=HERE) for name in AGENTS]
    print("Team running: orchestrator http://127.0.0.1:9100/  calculator :9101  text-tools :9102  (Ctrl+C to stop)")
    try:
        while all(p.poll() is None for p in processes):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in processes:
            p.terminate()
