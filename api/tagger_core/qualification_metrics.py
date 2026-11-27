"""Validate worker confusion counts and the configured qualification floors."""
import math

from .learning_artifacts import ProtocolError


def quality_floor(metrics, floor):
    """Existing configured minima, never quietly filled with code defaults."""
    keys=('min_precision','min_recall','min_val_pos_support','min_pred_pos_support')
    try:
        bounds={k:float(floor[k]) for k in keys}
    except (KeyError,ValueError,TypeError):
        raise ProtocolError('quality policy incomplete') from None
    if any(not math.isfinite(v) or v<0 for v in bounds.values()):
        raise ProtocolError('quality policy invalid')
    if not 0<bounds['min_precision']<=1 or not 0<bounds['min_recall']<=1:
        raise ProtocolError('quality policy invalid')
    counts = {}
    for key in ('tp', 'fp', 'tn', 'fn'):
        value = metrics.get(key)
        if type(value) is not int or value < 0:
            raise ProtocolError('quality confusion matrix invalid')
        counts[key] = value
    # Derive rates from counts. Never let NaN or a caller's inconsistent rate
    # turn a failed measurement into an eligible candidate.
    for key, denominator in (('precision', counts['tp'] + counts['fp']),
                              ('recall', counts['tp'] + counts['fn'])):
        actual = metrics.get(key)
        expected = counts['tp'] / denominator if denominator else None
        if expected is None:
            if actual is not None:
                raise ProtocolError('quality rates inconsistent')
        elif (type(actual) not in (int, float) or not math.isfinite(actual)
              or abs(actual - expected) > 1e-12):
            raise ProtocolError('quality rates inconsistent')
    reasons=[]
    if metrics['tp']+metrics['fn']<bounds['min_val_pos_support']:reasons.append('positive_support')
    if metrics['tp']+metrics['fp']<bounds['min_pred_pos_support']:reasons.append('prediction_support')
    if metrics['precision'] is None or metrics['precision']<bounds['min_precision']:reasons.append('precision')
    if metrics['recall'] is None or metrics['recall']<bounds['min_recall']:reasons.append('recall')
    return {'passed':not reasons,'reasons':reasons,'policy':bounds}
