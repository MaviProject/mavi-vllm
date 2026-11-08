import json
import os
from pathlib import Path
import sys
import core

core.initialize()
cfg = core.config()
image_pixels=core.image_max_pixels(cfg['context_length'])
core.set_meta('applied_config', {'concurrency': cfg['concurrency'], 'context_length': cfg['context_length'],'image_max_pixels':image_pixels})
home = Path('/home/mavi/inference')
cuda = home/'venv-vllm/lib/python3.12/site-packages/nvidia/cu13'
os.environ.update(HOME='/home/mavi', CUDA_HOME=str(cuda), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                  OMP_NUM_THREADS='1', VLLM_USE_V2_MODEL_RUNNER='0', VLLM_API_KEY=core.internal_key())
os.environ['PYTORCH_ALLOC_CONF']='garbage_collection_threshold:0.8'
os.environ['PATH'] = str(home/'venv-vllm/bin')+':'+str(cuda/'bin')+':/usr/local/bin:/usr/bin:/bin'
model = core.MODEL_DIR
args = [str(home/'venv-vllm/bin/vllm'), 'serve', str(model/core.MODEL_WEIGHT),
        '--tokenizer',str(model/'hf-config'),'--hf-config-path',str(model/'hf-config'),
        '--load-format','gguf','--quantization','gguf','--dtype','bfloat16',
        '--reasoning-parser','qwen3','--enable-auto-tool-choice','--tool-call-parser','qwen3_coder',
        '--served-model-name',core.INTERNAL_MODEL,'--host','127.0.0.1','--port','8000',
        '--max-model-len',str(cfg['context_length']),'--max-num-seqs',str(cfg['concurrency']),
        '--max-num-batched-tokens','1024','--gpu-memory-utilization','0.90','--enforce-eager',
        '--default-chat-template-kwargs','{"enable_thinking":false}','--no-enable-prefix-caching',
        '--limit-mm-per-prompt','{"image":1,"video":0}',
        '--mm-processor-kwargs',json.dumps({'size':{'longest_edge':image_pixels,'shortest_edge':65536}}),
        '--model-loader-extra-config',json.dumps({'mm_proj':str(model/core.VISION_WEIGHT)}),
        '--speculative-config','{"method":"mtp","num_speculative_tokens":1}']
os.execv(args[0], args)
