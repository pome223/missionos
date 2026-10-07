# A small synthetic deployment-decision comparison

This fixture compares five policies for releasing three test objects within
24 seconds after temporary jams. Retry costs four seconds and waiting costs
two. These are test assumptions, not Flight 14 mechanism timings or a coupled
spacecraft flight.

The policies see sensor/history evidence, not the future recovery schedule.
Thirty tuning sequences and sixty evaluation sequences remain separate. The
known-distribution optimum and future-aware oracle are different references.
The historical history-rule and optimum released the same count in all sixty
evaluation sequences; their utility difference concerned time/attempt costs,
not more deployment. This supplied no measured deployment-count headroom for
an LLM comparison, which was not run.

```sh
python scripts/run_starship_dispenser_experiment.py --approve-synthetic \
  --output-dir output/starship-dispenser-new
python scripts/run_starship_dispenser_experiment.py \
  --verify output/starship-dispenser-new/study.json
```

The public command generates its own synthetic JSON, verification, manifest,
and offline comparison report. Use a new output directory. It makes no model
or hardware call, and establishes no flight recovery, satellite service, or
model superiority. See the [experiment contract](../agents/starship-dispenser-experiment.md).
