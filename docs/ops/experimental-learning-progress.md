# Experimental learning observations

The dashboard reads `~/.local/state/kura-tagger/experiment-progress.json` on the API host (or the equivalent XDG_STATE_HOME). This is an observational hint, not a registration, quality verdict, or publication authority. It does not alter normal `learning-progress.json`, labels, or serving policy.

Run the observer on the experimental host, naming the two explicit units and the result directory. The current adapter understands the final-stage comparison protocol (extraction count; two arms of five epochs). Do not use it for another training protocol without adapting and testing the projection.

```sh
python3 -m tagger.observe_experiment \
  --training-unit kura-smegma-last-stage-full-20261115.service \
  --finish-unit kura-smegma-last-stage-finish-20261115.service \
  --result-dir "$HOME/.local/state/kura/smegma-backbone-20261115/last-stage-full-train" \
  --tag smegma --host nodeb --experiment last-stage-full-train
```

A collector may transfer this JSON over the existing SSH transport and call `tagger.experiment_status.write(value)` on the API host. It must retain the source timestamp, accept only a successful observer exit, and have a finite timeout. Failed observation must not renew old data. The reader treats observations older than 120 seconds as unverified, rejects symlinks, non-regular files, group/world-writable files, invalid fields, and files over 16 KiB. Journal contents and arbitrary exception text are never projected. Progress means the last reported stage/count, not proof that an active process is advancing. Collect at 30-second intervals while the finite experiment runs.

Both training and packaging failure receipts take precedence over successful receipts. A completed experiment still needs quality review and does not become a deployed model.

## Difficult examples

Human inability to judge is not a negative label. Leave an uncertain item unconfirmed; remove a tag when it is actually wrong. A model's disagreement is not sufficient to discard a human-confirmed positive. Future explicit uncertainty/exclusion controls must preserve this distinction, including in the training snapshot. This progress feature does not implement such a control. Reduced coverage may be acceptable, but precision claims still require an appropriate independent evaluation; neither a small high-threshold subset nor a previously consulted holdout establishes production precision.
