"""Check the patched destination copies preserve exact weights and padding."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import torch
from vllm_gguf_plugin.quantization.params import (
    GGUFUninitializedWeightParameter, _store_gguf_loaded_weight,
    _gguf_embedding_weight_loader,
)
from vllm_gguf_plugin.quantization.linear import GGUFLinearMethod
from vllm_gguf_plugin.quantization.params import GGUFWeightParameter

class LoaderCopyTests(unittest.TestCase):
    def setUp(self):
        self.rank=patch('vllm.model_executor.parameter.get_tensor_model_parallel_rank',return_value=0)
        self.size=patch('vllm.model_executor.parameter.get_tensor_model_parallel_world_size',return_value=1)
        self.rank.start();self.size.start()
    def tearDown(self):self.rank.stop();self.size.stop()
    def test_quantized_bytes_are_exact_and_independent(self):
        source=torch.arange(60,dtype=torch.uint8).reshape(6,10)
        param=GGUFUninitializedWeightParameter(requires_grad=False,device='cpu')
        _store_gguf_loaded_weight(param,source)
        self.assertTrue(torch.equal(param.data,source))
        source.zero_()
        self.assertEqual(int(param.data[5,9]),59)

    def test_embedding_partition_and_padding_are_exact(self):
        source=torch.arange(60,dtype=torch.uint8).reshape(6,10)
        param=GGUFUninitializedWeightParameter(requires_grad=False,device='cpu')
        param.output_dim=0;param.tensor_shape=(4,10)
        layer=SimpleNamespace(shard_indices=SimpleNamespace(org_vocab_start_index=2,org_vocab_end_index=5))
        _gguf_embedding_weight_loader(layer,param,source)
        self.assertTrue(torch.equal(param.data[:3],source[2:5]))
        self.assertEqual(int(param.data[3].sum()),0)

    def test_fused_shards_are_exact_and_ordered(self):
        weight=GGUFWeightParameter(data=torch.empty(0,dtype=torch.uint8),weight_loader=lambda *a:None,input_dim=1,output_dim=0,tensor_shape=(3,4))
        weight.data_container=[];weight.shard_id=[];weight.shard_id_map={}
        _store_gguf_loaded_weight(weight,torch.full((1,4),7,dtype=torch.uint8),1)
        _store_gguf_loaded_weight(weight,torch.full((2,2),3,dtype=torch.uint8),0)
        self.assertEqual(weight.device.type,'cpu')
        self.assertTrue(all(t.device.type=='cpu' for t in weight.data_container))
        layer=torch.nn.Module();layer.register_parameter('weight',weight)
        GGUFLinearMethod(None)._create_padded_weight_param(layer)
        self.assertTrue(torch.equal(layer.weight.data,torch.tensor([[3,3,0,0],[3,3,0,0],[7,7,7,7]],dtype=torch.uint8)))
        self.assertEqual(layer.weight.shard_offset_map,{0:(0,2,2),1:(2,3,4)})
        self.assertEqual(weight.data_container,[])

    def test_single_fused_shard_is_materialized(self):
        weight=GGUFWeightParameter(data=torch.empty(0,dtype=torch.uint8),weight_loader=lambda *a:None,input_dim=1,output_dim=0,tensor_shape=(1,4))
        weight.data_container=[];weight.shard_id=[];weight.shard_id_map={}
        source=torch.full((1,4),9,dtype=torch.uint8)
        _store_gguf_loaded_weight(weight,source,0)
        layer=torch.nn.Module();layer.register_parameter('weight',weight)
        GGUFLinearMethod(None)._create_padded_weight_param(layer)
        self.assertTrue(torch.equal(layer.weight.data,source))

if __name__=='__main__':unittest.main(verbosity=2)
