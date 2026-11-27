"""Bounded CPU options must not change identity, model selection or GPU policy."""
from types import SimpleNamespace
import hashlib,json,sys
import pytest
from tagger import cl2_chars
from tagger.onnx_memory import bounded_cpu_options

class Options:
    def __init__(self):
        self.config={};self.graph_optimization_level='unchanged';self.marker='preserve'
    def add_session_config_entry(self,k,v):self.config[k]=v


def test_cpu_allocations_bound_without_touching_graph_optimization():
    ort=SimpleNamespace(SessionOptions=Options,ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL='sequential'))
    options=Options();result=bounded_cpu_options(ort,options)
    assert result is options
    assert options.graph_optimization_level=='unchanged' and options.marker=='preserve'
    assert options.enable_cpu_mem_arena is False and options.enable_mem_pattern is False
    assert options.config=={'session.disable_prepacking':'1'}
    assert options.intra_op_num_threads==2 and options.inter_op_num_threads==1
    assert options.execution_mode=='sequential'

@pytest.mark.parametrize('provider',['CPUExecutionProvider','CUDAExecutionProvider','ROCMExecutionProvider'])
def test_cl_memory_policy_keeps_identity_and_leaves_gpu_options_unchanged(tmp_path,monkeypatch,provider):
    vocab=dict(num_tags=1,idx_to_tag={'0':'character'},tag_to_idx={'character':0},tag_to_category={'character':'Character'})
    for name in cl2_chars.FILES:(tmp_path/name).write_text(json.dumps(vocab) if name.endswith('.json') else 'fake')
    monkeypatch.setattr(cl2_chars,'FILES',{name:hashlib.sha256((tmp_path/name).read_bytes()).hexdigest() for name in cl2_chars.FILES})
    ort=SimpleNamespace(SessionOptions=Options,ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL='sequential'),get_available_providers=lambda:[provider])
    monkeypatch.setitem(sys.modules,'onnxruntime',ort);monkeypatch.setenv('KURA_CL2_PROVIDER',provider)
    seen=[]
    def session(runtime,path,options):
        seen.append(options)
        return SimpleNamespace(get_inputs=lambda:[SimpleNamespace(shape=['batch_size',3,384,384])],get_outputs=lambda:[SimpleNamespace(name='logits')])
    monkeypatch.setattr(cl2_chars,'create_session',session);monkeypatch.setattr(cl2_chars,'session_facts',lambda *a:{})
    baseline=cl2_chars.load(threshold=.45,model_dir=tmp_path,intra_op_threads=4)
    bounded=cl2_chars.load(threshold=.45,model_dir=tmp_path,intra_op_threads=4,bounded_cpu_memory=True)
    assert baseline['source']==bounded['source']
    assert seen[0].config=={} and seen[0].intra_op_num_threads==4
    if provider=='CPUExecutionProvider':
        assert seen[1].config=={'session.disable_prepacking':'1'} and seen[1].intra_op_num_threads==4
    else:
        assert vars(seen[0])==vars(seen[1])


def test_bounded_options_preserve_explicit_thread_budget():
    from types import SimpleNamespace
    from tagger.onnx_memory import bounded_cpu_options
    options = SimpleNamespace(add_session_config_entry=lambda *a: None)
    ort = SimpleNamespace(ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL='sequential'))
    assert bounded_cpu_options(ort, options, intra_op_threads=4).intra_op_num_threads == 4
