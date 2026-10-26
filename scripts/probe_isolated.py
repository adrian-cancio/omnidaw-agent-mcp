"""Launch only our extracted host with isolated settings, bounded lifetime, no token output."""
import json
import os
import subprocess
import time
from pathlib import Path

root = Path.home() / '.cache' / 'omnidaw-evidence'
root.mkdir(parents=True, exist_ok=True)
run = root / ('probe-' + str(time.time_ns()))
run.mkdir(parents=True, exist_ok=True)
env = os.environ.copy()
for key, folder in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache')]:
    (run / folder).mkdir(parents=True, exist_ok=True)
    env[key] = str(run / folder)
env.pop('OMNI_API_TOKEN', None)
env.pop('OMNI_API_ENDPOINT', None)
app = Path(os.environ.get('OMNI_APPIMAGE_PATH', str(Path.home() / 'Applications' / 'Omni.AppImage')))
with (run / 'host.log').open('w') as log:
    process = subprocess.Popen([str(app), '--api'], env=env, stdout=log, stderr=log)
    try:
        endpoint = run / 'config/omni/api/endpoint.json'
        for _ in range(40):
            if endpoint.exists() or process.poll() is not None:
                break
            time.sleep(.5)
        print(json.dumps({'run': str(run), 'pid': process.pid, 'exit': process.poll(), 'isolated_endpoint_exists': endpoint.exists()}))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
