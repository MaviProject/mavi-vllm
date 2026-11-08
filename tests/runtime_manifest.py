import difflib,hashlib,importlib.metadata as metadata,json,platform,subprocess,zipfile
from pathlib import Path
root=Path('/home/mavi/mavi-vllm-platform/data/production-qa')
source=Path('/home/mavi/inference/src/vllm-gguf-plugin-e454affe0ca23c9dc8754ec3033270cb30622ed5')
manifest={'python':platform.python_version(),'kernel':platform.release(),'driver':subprocess.check_output(['nvidia-smi','--query-gpu=name,driver_version,memory.total','--format=csv,noheader'],text=True).strip(),'packages':sorted([{'name':p.metadata['Name'],'version':p.version} for p in metadata.distributions()],key=lambda p:p['name'].lower()),'plugin_commit':'e454affe0ca23c9dc8754ec3033270cb30622ed5','plugin_files':{}}
app=root.parents[1]
manifest['platform_files']={str(p.relative_to(app)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('backend','static') for p in (app/folder).glob('*') if p.is_file() and p.suffix in ('.cpp','.hpp','.js','.html','.css')}
binary=Path('/usr/local/bin/mavi-vllm-backend')
if binary.is_file():manifest['backend_sha256']=hashlib.sha256(binary.read_bytes()).hexdigest()
diff=[]
for name in ('quantization/params.py','quantization/linear.py','loader.py','plugin.py','weights_adapter/qwen3_5.py'):
    p=source/'vllm_gguf_plugin'/name
    manifest['plugin_files'][name]=hashlib.sha256(p.read_bytes()).hexdigest()
    backup=p.with_suffix('.py.pre-memory-qa')
    if backup.exists():diff.extend(difflib.unified_diff(backup.read_text().splitlines(True),p.read_text().splitlines(True),fromfile='a/'+name,tofile='b/'+name))
(root/'runtime-manifest.json').write_text(json.dumps(manifest,indent=2))
(root/'gguf-memory-stability.patch').write_text(''.join(diff))
with zipfile.ZipFile(root/'gguf-plugin-tested-source.zip','w',compression=zipfile.ZIP_DEFLATED) as archive:
    for p in (source/'vllm_gguf_plugin').rglob('*.py'):archive.write(p,p.relative_to(source))
    for p in source.glob('LICENSE*'):archive.write(p,p.name)
print('Runtime manifest and exact tested plugin source saved')
