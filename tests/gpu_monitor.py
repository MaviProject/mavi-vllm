import datetime,json,os,time
from pathlib import Path
import pynvml,psutil
root=Path('/home/mavi/mavi-vllm-platform/data/production-qa');root.mkdir(parents=True,exist_ok=True)
pynvml.nvmlInit();handle=pynvml.nvmlDeviceGetHandleByIndex(0)
(root/'monitor.pid').write_text(str(os.getpid()))
with (root/'gpu-samples.jsonl').open('a',buffering=1) as f:
    while True:
        mem=pynvml.nvmlDeviceGetMemoryInfo(handle);util=pynvml.nvmlDeviceGetUtilizationRates(handle)
        row=dict(time=time.time(),gpu_used_mb=mem.used/2**20,gpu_free_mb=mem.free/2**20,gpu_util=util.gpu,temperature=pynvml.nvmlDeviceGetTemperature(handle,pynvml.NVML_TEMPERATURE_GPU),ram_available_mb=psutil.virtual_memory().available/2**20,swap_used_mb=psutil.swap_memory().used/2**20)
        f.write(json.dumps(row)+'\n');time.sleep(.5)
