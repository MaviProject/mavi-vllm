"""Reduce temporary GGUF loader GPU copies; fail closed on upstream drift."""
from pathlib import Path
import shutil

package=Path('/home/mavi/inference/src/vllm-gguf-plugin-e454affe0ca23c9dc8754ec3033270cb30622ed5/vllm_gguf_plugin')
p=package/'quantization/params.py'
text=p.read_text()
old='''    loaded_weight = _clone_loaded_weight(loaded_weight).to(device=param.device)
    if shard_id is None:
        _materialize_parameter_data(
            param, tuple(loaded_weight.shape), loaded_weight.dtype
        )
        param.data.copy_(loaded_weight)
        return

    if shard_id not in param.shard_id_map:'''
new='''    if shard_id is None:
        # The iterator owns independent CPU tensors. Copy directly into the
        # destination instead of allocating a second GPU-sized staging tensor.
        _materialize_parameter_data(
            param, tuple(loaded_weight.shape), loaded_weight.dtype
        )
        param.data.copy_(loaded_weight)
        return

    loaded_weight = _clone_loaded_weight(loaded_weight).to(device=param.device)
    if shard_id not in param.shard_id_map:'''
old_embedding='''    loaded_weight = _clone_loaded_weight(loaded_weight).to(device=param.device)
    start_idx = layer.shard_indices.org_vocab_start_index'''
new_embedding='''    # Narrow on the source device, then copy once into the padded destination.
    start_idx = layer.shard_indices.org_vocab_start_index'''
if old in text:
    assert text.count(old)==1 and text.count(old_embedding)==1
    shutil.copy2(p,p.with_suffix('.py.pre-memory-qa'))
    p.write_text(text.replace(old,new).replace(old_embedding,new_embedding))
else:
    assert 'Copy directly into the' in text and new_embedding in text, 'Unexpected plugin revision'
print('GGUF direct destination copies installed')
p=package/'loader.py'
text=p.read_text()
old='''            model.load_weights(
                self._iter_weights(adapter, plan, model_config),
            )
            process_weights_after_loading(model, model_config, target_device)
        return model'''
new='''            # Replaced vocabulary modules can retain cyclic Python references.
            # Collect them before loading quantized weights; only unused cache
            # is returned to CUDA, as in vLLM's base model loader.
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            model.load_weights(
                self._iter_weights(adapter, plan, model_config),
            )
            process_weights_after_loading(model, model_config, target_device)
            gc.collect()
            torch.cuda.empty_cache()
        return model'''
if old in text:
    assert text.count(old)==1
    shutil.copy2(p,p.with_suffix('.py.pre-memory-qa'))
    p.write_text(text.replace(old,new))
else:
    assert new in text, 'Unexpected loader revision'
print('GGUF unused initialization buffers collected')
p=package/'quantization/params.py'
text=p.read_text()
old='''    loaded_weight = _clone_loaded_weight(loaded_weight).to(device=param.device)
    if shard_id not in param.shard_id_map:'''
new='''    # Keep fused shards in system RAM until their one final GPU concatenation.
    loaded_weight = _clone_loaded_weight(loaded_weight).to(device="cpu")
    if shard_id not in param.shard_id_map:'''
old_sentinel='''    if not isinstance(param, UninitializedParameter) and param.data.numel() == 0:
        param.data = loaded_weight


def _store_gguf_weight_type'''
new_sentinel='''    # Preserve the parameter's destination device even when shard data is CPU.


def _store_gguf_weight_type'''
if old in text:
    assert text.count(old)==1 and text.count(old_sentinel)==1
    p.write_text(text.replace(old,new).replace(old_sentinel,new_sentinel))
else:
    assert new in text and new_sentinel in text, 'Unexpected fused shard revision'
p=package/'quantization/linear.py'
text=p.read_text()
old='if len(data_container := weight.data_container) > 1:'
new='if len(data_container := weight.data_container) >= 1:'
if old in text:
    assert text.count(old)==1
    shutil.copy2(p,p.with_suffix('.py.pre-memory-qa'))
    p.write_text(text.replace(old,new))
else:
    assert new in text, 'Unexpected linear revision'
print('GGUF fused shards staged in CPU RAM')
