"""Same real markup controls with candidate purpose; synthetic images only."""
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('human_review_fixture',Path(__file__).with_name('human-review.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
f.PURPOSE='candidate_evaluation'
if __name__=='__main__':f.f.main()
