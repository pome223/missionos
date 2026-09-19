# Small predictors for individual missions

A mission can use a small model trained to answer a concrete question: if this
operation is performed in the current state, what happens to the relevant objects?

MissionOS separates that question from execution. The model supplies a forecast;
the decision process weighs it alongside current observations and rules. Execution
and outcome checks remain separate. A forecast is not approval or proof of success.

The reusable part is the interface: identify the mission and controller, submit
current state and candidate operations, return predictions over stated time
intervals, and compare them with subsequent observations. Different missions can
use different models and state representations. A model is used only with the
contract it was registered for.

The first implementation connects a lightweight stacking predictor to a local
simulator lab. It uses exact object states and material properties, not images
alone. Useful stop decisions have been observed, but better overall scores than
a simple width-dependent stopping rule have not been established. The lab does
not yet connect forecasts to the main MissionOS Agent or hardware execution.

[Implementation and verification contract](../agents/mission-prediction-contract.md)

Mission Assurance can receive a forecast as model-inferred evidence, keeping it
separate from observed facts. A receipt records whether it was accepted as
judgment material and why. Accepting evidence does not establish feasibility or
authorize execution. Unavailable or outdated forecasts are excluded.
