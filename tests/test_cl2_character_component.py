"""CL contract controls; no network, API or production database access."""
import copy
from types import SimpleNamespace
import numpy as np
import pytest
from PIL import Image
from tagger import cl2_chars as c


def voc():
    return dict(num_tags=3, idx_to_tag={'0': 'alice example', '1': 'series example', '2': '1girl'},
        tag_to_idx={'alice example': 0, 'series example': 1, '1girl': 2},
        tag_to_category={'alice example': 'Character', 'series example': 'Copyright', '1girl': 'General'})


def test_vocabulary_character_category_not_copyright():
    assert c.vocabulary(voc()) == ({0: 'alice_example'}, 3)


@pytest.mark.parametrize('mutation', [
    lambda v: v.update(num_tags=True),
    lambda v: v['idx_to_tag'].update({'3': 'extra'}),
    lambda v: v['tag_to_idx'].update({'alice example': True}),
    lambda v: v['tag_to_category'].update({'alice example': 'character'}),
    lambda v: v['tag_to_category'].update({'alice example': 'General'}),
    lambda v: v['idx_to_tag'].update({'1': 'alice_example'}),
])
def test_invalid_vocabulary_refused(mutation):
    v = voc(); mutation(v)
    with pytest.raises(c.RequiredCharacterComponentError): c.vocabulary(v)


def test_card_preprocessing_exactly_matches_reference():
    image = Image.fromarray(np.arange(17*23*4, dtype=np.uint8).reshape(17,23,4), 'RGBA')
    reference = ((np.asarray(image.convert('RGB').resize((384,384), Image.Resampling.BICUBIC), dtype=np.float32)/255-.5)/.5).transpose(2,0,1)[None]
    np.testing.assert_array_equal(c.preprocess(image), reference)


def session(output):
    def run(names, inputs):
        assert names == ['logits']; assert set(inputs) == {'pixel_values'}
        return [np.asarray(output)]
    return SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name='pixel_values')], run=run)


def test_logits_sigmoid_once_and_extreme_values():
    p = c.prediction_vector(session([[-1000, 0, 1000]]), None, 3)
    np.testing.assert_array_equal(p, [0,.5,1])


@pytest.mark.parametrize('output', [[[float('nan')]], [[float('inf')]], [[1,2]], [1], [[1],[2]], []])
def test_invalid_logits_never_become_negative(output):
    with pytest.raises(c.RequiredCharacterComponentError): c.prediction_vector(session(output), None, 1)


@pytest.mark.parametrize('threshold', [True, None, 0, -.1, 1.1, float('nan'), float('inf'), '.5'])
def test_invalid_operating_point(threshold):
    with pytest.raises(c.RequiredCharacterComponentError): c.validate_threshold(threshold)


def test_zero_hits_is_valid_and_copyright_is_not_character():
    state = dict(sess=session([[-10,10,10]]), chars={0:'alice'}, tag_count=3, threshold=.55)
    assert c.char_tags(state, Image.new('RGB',(3,3))) == []


def test_a_name_far_below_the_best_one_is_not_attached():
    logit = lambda p: float(np.log(p / (1 - p)))
    chars = {0: 'alice', 1: 'bob', 2: 'carol'}
    def tags(*scores):
        state = dict(sess=session([[logit(p) for p in scores]]), chars=chars, tag_count=3, threshold=.45)
        return [name for name, _ in c.char_tags(state, Image.new('RGB', (3, 3)))]
    assert tags(.9, .8, .5) == ['alice', 'bob']       # .8 is within 80% of .9, .5 is not
    assert tags(.9, .75, .44) == ['alice', 'bob']     # below the threshold is never kept
    assert tags(.9, .70, .44) == ['alice']            # above the threshold, but under 80% of the best
    assert tags(.5, .46, .2) == ['alice', 'bob']      # two characters in one image survive when their scores are close
    assert tags(.4, .3, .2) == []
    assert c.DERIVER_CONTRACT != __import__('hashlib').sha256(
        b'kura-cl2-rgb-bicubic384-mean0.5-std0.5-sigmoid-character-v1').hexdigest()


def test_external_weights_verified_before_runtime_import(tmp_path, monkeypatch):
    for name in c.FILES: (tmp_path/name).write_bytes(b'not a model')
    monkeypatch.setattr(c, 'FILES', {name:c.digest(tmp_path/name) for name in c.FILES})
    (tmp_path/'model.onnx.data').write_bytes(b'changed')
    with pytest.raises(c.RequiredCharacterComponentError, match='artifact_digest_mismatch'):
        c.load(threshold=.55, model_dir=tmp_path)


def test_duplicate_vocabulary_key_refused():
    with pytest.raises(c.RequiredCharacterComponentError):
        c.json.loads('{"num_tags":1,"num_tags":2}', object_pairs_hook=c._unique)


def test_card_general_edge_name_is_not_a_character():
    v=voc()
    v['idx_to_tag']['2']=' eyes'
    v['tag_to_idx'].pop('1girl');v['tag_to_idx'][' eyes']=2
    v['tag_to_category'].pop('1girl');v['tag_to_category'][' eyes']='General'
    assert c.vocabulary(v)==({0:'alice_example'},3)


def test_release_description_covers_external_weights_and_operating_point():
    desc=c.release_description(threshold=.55, evidence='fixture qualification evidence')
    assert {r['content_sha256'] for r in desc['components']} == set(c.FILES.values())
    assert len(desc['components'])==3
    assert all(r['deriver_sha256']==c.DERIVER_CONTRACT for r in desc['components'])
    assert all(r['params']['distribution']=='official_hf_per_node' for r in desc['components'])
    assert desc['threshold_row']=={'producer':'cl_tagger_v2','category':4,'threshold':.55,'evidence':'fixture qualification evidence'}
    assert desc != c.release_description(threshold=.35, evidence='fixture qualification evidence')


def test_release_description_requires_threshold_evidence():
    with pytest.raises(c.RequiredCharacterComponentError, match='evidence_required'):
        c.release_description(threshold=.55, evidence='')


@pytest.mark.parametrize('provider', ['CPUExecutionProvider', 'ROCMExecutionProvider', 'CUDAExecutionProvider'])
def test_explicit_provider_loads_and_keeps_cpu_operator_fallback(monkeypatch, provider):
    monkeypatch.setenv('KURA_CL2_PROVIDER', provider)
    monkeypatch.setenv('KURA_CL2_GPU_MEM_LIMIT_BYTES', str(3*1024**3))
    seen = []
    runtime = SimpleNamespace(get_available_providers=lambda: [provider, 'CPUExecutionProvider'])
    def create(path, options, providers, provider_options):
        if provider != 'CPUExecutionProvider':
            assert provider_options[0] == {'device_id':'0', 'gpu_mem_limit':str(3*1024**3), 'arena_extend_strategy':'kSameAsRequested'}
        seen.extend(providers)
        return SimpleNamespace(get_providers=lambda: providers)
    runtime.InferenceSession = create
    c.create_session(runtime, 'model.onnx', object())
    assert seen == ([provider] if provider == 'CPUExecutionProvider' else [provider, 'CPUExecutionProvider'])


def test_gpu_library_load_failure_cannot_silently_use_cpu(monkeypatch):
    monkeypatch.setenv('KURA_CL2_PROVIDER', 'ROCMExecutionProvider')
    monkeypatch.setenv('KURA_CL2_GPU_MEM_LIMIT_BYTES', str(3*1024**3))
    runtime = SimpleNamespace(get_available_providers=lambda: ['ROCMExecutionProvider', 'CPUExecutionProvider'],
        InferenceSession=lambda *a, **kw: SimpleNamespace(get_providers=lambda: ['CPUExecutionProvider']))
    with pytest.raises(c.RequiredCharacterComponentError, match='cl2_provider_fallback'):
        c.create_session(runtime, 'model.onnx', object())


@pytest.mark.parametrize('provider,reason', [('auto', 'invalid'), ('CUDAExecutionProvider', 'unavailable')])
def test_invalid_or_missing_provider_refused_before_loading(monkeypatch, provider, reason):
    monkeypatch.setenv('KURA_CL2_PROVIDER', provider)
    monkeypatch.setenv('KURA_CL2_GPU_MEM_LIMIT_BYTES', str(3*1024**3))
    runtime = SimpleNamespace(get_available_providers=lambda: ['CPUExecutionProvider'])
    with pytest.raises(c.RequiredCharacterComponentError, match='cl2_provider_' + reason):
        c.create_session(runtime, 'model.onnx', object())


def test_no_provider_declaration_preserves_cpu_default(monkeypatch):
    monkeypatch.delenv('KURA_CL2_PROVIDER', raising=False)
    assert c.execution_providers(SimpleNamespace(get_available_providers=lambda: ['ROCMExecutionProvider','CPUExecutionProvider'])) == ['CPUExecutionProvider']


@pytest.mark.parametrize('limit', ['', 'auto', '-1', '0', '100', '99999999999999999999'])
def test_gpu_without_bounded_memory_refuses_before_session(monkeypatch,limit):
    monkeypatch.setenv('KURA_CL2_PROVIDER','ROCMExecutionProvider')
    monkeypatch.setenv('KURA_CL2_GPU_MEM_LIMIT_BYTES',limit)
    from unittest.mock import Mock
    runtime=SimpleNamespace(get_available_providers=lambda:['ROCMExecutionProvider'],InferenceSession=Mock())
    with pytest.raises(c.RequiredCharacterComponentError,match='cl2_gpu_memory_limit'):
        c.create_session(runtime,'model.onnx',object())
    runtime.InferenceSession.assert_not_called()


@pytest.mark.parametrize('threads', ['0','17','-1','auto','1000000000000'])
def test_thread_limit_rejects_before_model_access(monkeypatch,threads):
    monkeypatch.setenv('KURA_CL2_THREADS',threads)
    with pytest.raises(c.RequiredCharacterComponentError,match='cl2_threads_invalid'):
        c.load(threshold=.45,model_dir='/nonexistent')


def test_runtime_load_defaults_to_offline():
    import inspect
    assert inspect.signature(c.load).parameters['local_files_only'].default is True


def test_source_identity_ignores_checkout_comments(tmp_path,monkeypatch):
    import sys
    for name in c.FILES:
        (tmp_path/name).write_text(c.json.dumps(voc()) if name.endswith('.json') else 'fixture')
    monkeypatch.setattr(c,'FILES',{n:c.digest(tmp_path/n) for n in c.FILES})
    ort=SimpleNamespace(SessionOptions=lambda:SimpleNamespace(),ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL=0))
    monkeypatch.setitem(sys.modules,'onnxruntime',ort)
    sess=SimpleNamespace(get_inputs=lambda:[SimpleNamespace(shape=['batch_size',3,384,384])],get_outputs=lambda:[SimpleNamespace(name='logits')])
    monkeypatch.setattr(c,'create_session',lambda *a:sess)
    monkeypatch.setattr(c,'session_facts',lambda *a:{})
    comment=tmp_path/'loader.py';comment.write_text('# first comment')
    monkeypatch.setattr(c,'__file__',str(comment))
    first=c.load(threshold=.45,model_dir=tmp_path)['source']
    comment.write_text('# changed comment')
    assert c.load(threshold=.45,model_dir=tmp_path)['source']==first


def test_receipt_reads_actual_rocm_options_not_environment(monkeypatch):
    monkeypatch.setenv('KURA_CL2_GPU_MEM_LIMIT_BYTES',str(9*1024**3))
    sess=SimpleNamespace(get_providers=lambda:['ROCMExecutionProvider','CPUExecutionProvider'],
        get_provider_options=lambda:{'ROCMExecutionProvider':{'device_id':'0','gpu_mem_limit':'268435456','arena_extend_strategy':'kSameAsRequested'}})
    facts=c.session_facts(SimpleNamespace(__version__='1.22.2'),sess,
        SimpleNamespace(intra_op_num_threads=1,inter_op_num_threads=1))
    assert facts['gpu_mem_limit']==268435456
    assert facts['device_id']==0 and facts['arena_extend_strategy']=='kSameAsRequested'


def test_cuda_low_memory_prevents_optimized_weight_copies(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    provider = "CUDAExecutionProvider"
    monkeypatch.setenv("KURA_CL2_PROVIDER", provider)
    monkeypatch.setenv("KURA_CL2_GPU_MEM_LIMIT_BYTES", str(2 * 1024**3))
    monkeypatch.setenv("KURA_CL2_CUDA_LOW_MEMORY", "1")
    options = SimpleNamespace(graph_optimization_level=99, add_session_config_entry=Mock())
    session = SimpleNamespace(get_providers=lambda: [provider])
    runtime = SimpleNamespace(
        get_available_providers=lambda: [provider, "CPUExecutionProvider"],
        GraphOptimizationLevel=SimpleNamespace(ORT_DISABLE_ALL=0),
        InferenceSession=Mock(return_value=session),
    )
    assert c.create_session(runtime, "model.onnx", options) is session
    assert options.graph_optimization_level == 0
    options.add_session_config_entry.assert_called_once_with("session.disable_prepacking", "1")
    assert runtime.InferenceSession.call_args.kwargs["provider_options"][0]["gpu_mem_limit"] == str(2 * 1024**3)
