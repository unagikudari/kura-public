"""CPU session allocation policy for the full multi-model worker.

No weights, preprocessing, graph optimization or decision thresholds change.
Disable retained CPU arenas and prepacked weight copies: several independently
loaded classifiers otherwise exceed the full worker's existing 6 GiB cgroup.
GPU sessions retain their separately qualified allocation policy.
"""

def bounded_cpu_options(ort, options=None, *, intra_op_threads=2):
    options = options if options is not None else ort.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    options.intra_op_num_threads = intra_op_threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.disable_prepacking", "1")
    return options
