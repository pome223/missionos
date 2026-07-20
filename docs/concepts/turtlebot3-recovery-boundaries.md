# TurtleBot3 Recovery Boundaries

TurtleBot3 Recovery keeps five facts separate:

1. a Recovery action is proposed,
2. its meaning is converted into bounded Nav2 parameters,
3. a human approves that exact checkpoint,
4. the executor sends the bounded action, and
5. observations show whether the robot actually recovered.

The conversion step does not approve or execute anything. It records that an
action such as `avoid_obstacle`, `reroute`, or `return_home` still has the same
meaning after it becomes concrete Nav2 parameters. If the action or parameters
change, the old checkpoint cannot be reused.

An ACK also does not prove movement or success. Recovery success needs the
approved checkpoint, a sent dispatch request, an observed executor effect, the
completed goal sequence, and any required side or obstacle-clearance evidence.
Only then may the approved route-resume policy take effect.

Older stored checkpoints remain readable. New checkpoints add the stronger
intent, compilation, pre-dispatch, and outcome records without turning those
records into dispatch authority.
