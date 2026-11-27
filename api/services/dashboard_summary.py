"""Small read-only status snapshot; selection and observation are not deployment proof."""
from datetime import datetime, timezone
import math
from sqlalchemy import text
from api.tagger_core.quality_hold import held_names
from api.services.learning_execution import current as read_learning
from api.tagger_core.experiment_status import read as read_experiment


async def snapshot(db):
    row = (await db.execute(text('SELECT release_id FROM tagger_release_active'))).one_or_none()
    held = sorted(await held_names(db))
    learning=await read_learning(db)
    if learning and not all(type(learning.get(k)) in (int,float) and math.isfinite(learning[k]) for k in ('observed_at','age_seconds')):
        learning=None
    return dict(state='available', observed_at=datetime.now(timezone.utc).isoformat(),
                selected_release=row[0] if row else None, machine_output_holds=held,
                learning=learning, experiment=read_experiment(),
                deployment_verified=False, model_quality_verified=False)
