"""The tagging capability's core-side state: artifact store and kinds, lifecycle
transitions, governed config and GT sync. These live in the core because the API and
the database own them; the tagger package (inference, training, workers) imports them
from here and never the other way round (release gate G7, release/capability_policy.json).
"""
