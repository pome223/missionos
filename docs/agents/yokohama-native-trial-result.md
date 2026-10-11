# Recorded native vehicle-service regression

A retained fixed-condition Yokohama PX4/Gazebo SITL trial completed simulated
payload delivery, return landing and disarm through the separate vehicle service.
This is a historical execution report, not a new flight of the publication commit.

## Observed result

| Check | Recorded outcome |
|---|---|
| Controller | Exit 0; passed |
| AeroVLA | Two flight-decision requests; one separate warmup |
| ANWM | Two requests producing four candidate forecasts |
| Delivery and return | Simulated payload receipt, return landing and disarm |
| Independent verdicts | `decisions`, `pad_queue`, `pad_advisory`, `payload`, `sitl`, and `vehicle_service` all passed |
| Cleanup | Recorded model shutdown, owned VM/disk absence and owned-container absence passed |

Native decisions occurred at the two existing city checkpoints. Sea legs used
AP, and the pad mission judge used a fixture with a separate CPU advisory.
The successful trial used one allocation; it did not exercise capacity retries
or every failure branch. It establishes neither physical delivery or human
receipt nor incremental AI utility, general obstacle prediction or energy savings.
Cleanup above is the trial's recorded result, not a new cloud inventory check.

## Execution provenance

The trial ran historical source revision
`e2e177d574b1854d07c6e9612ef841b9b57cf5c4` plus a saved deadline patch.
The patch SHA-256 is
`a7d8f854a7a79d60bdf83998cc28ca8accb2a3b092f9e55a870e9cec0c589b3e`;
it was byte-identical to the later implementation diff ending at
`6bbc29964aaa47a8f2ea90dd3811b03688766205`. Those historical revisions identify
locally retained provenance; they are not imported public history.

The 17 approved source hashes, five verifier entrypoints, controller and lifecycle
helper all matched that later implementation revision. The table below was also
rechecked against the publication tree. Documentation, CI integration and a new
fixture smoke were added for publication; the historical trial was not rerun.
The binding covers these 24 files, not all transitive dependencies or installed
packages. Twenty-two retained artifact hashes matched the original index during
review. The raw receipts remain outside this public repository; this report does
not provide independently replayable native flight evidence.

The historical invocation had this form; the variables represent private local
inputs and the command is not a fresh-clone reproduction recipe:

```sh
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B scripts/yokohama_native_trial.py \
  --plan "$TRIAL_DIR/plan-final.json" --execute \
  --operator-approval "$TRIAL_DIR/operator-approval-final.json"
```

Native execution also needs the reviewed external payload described in
[yokohama-native-trial-controller.md](yokohama-native-trial-controller.md).
For a CPU-only check of the public Gateway and service process boundary, run:

```sh
PYTHONPATH=.:packages/missionos-cli/src:packages/missionos-gateway/src \
  python scripts/smoke_yokohama_vehicle_service_gateway.py
```

That fixture check exercises real loopback HTTP and a service subprocess. It
uses simulated runner/verifier outputs and makes no native model or flight claim.

## Approved file content bindings

| Relative path | SHA-256 |
|---|---|
| `scripts/run_yokohama_vehicle_service.py` | `6457585670799016816a79f4ef08ac5460b86ac321536f142aa35a086c56b8aa` |
| `src/runtime/yokohama_execution_service.py` | `96974489625a3e68c2e408d9434201bf7c8da77ed2675c2562cb3b5cc2313c88` |
| `scripts/yokohama_sitl.py` | `b7378c21dd5fdf7f558ade5f744f083dabd870366d635e0447e07064bc7cf9c6` |
| `scripts/yokohama_local_preflight.py` | `9102f596c07d16b264decb63f57ec985cbd03ae2b391eeb7bafd96dbab4aca7e` |
| `scripts/yokohama_sitl_worker.py` | `233518edcceee5ccea3a0052e9a98d7ee12893fe8a2b94b08d70266c75ea121c` |
| `scripts/yokohama_decision_worker.py` | `888d991c7dc3c8082daba746419188e912af2e8d1543e6f6b4332521bf37988d` |
| `scripts/yokohama_pad_worker.py` | `143699920729a4fac6c69e43a6d9b242186ab20f2295d8b5b6343800d59b74e3` |
| `scripts/yokohama_flight_worker.py` | `ca3af906aa054bb7aae3947800281e7d2e31cf5d29e76fcec8962efd461d62cd` |
| `scripts/yokohama_altitude_contract.py` | `71fc461c952ad2a0c34c46358bfdf204c1c3ef9d3fc58259ee29e3fa2efa4195` |
| `scripts/smoke_px4_gazebo_sitl_mission_upload.py` | `b3673e9edc9ac89e0397937ccb18a63b9fd3aef5ecdc86cc97c34a3ae01b25af` |
| `scripts/yokohama_decision_host.py` | `0fea7ae722cdc5cdf95714bb11f5b22297ab21ab92cda0f0060ee325f314073a` |
| `scripts/yokohama_pad_advisory_host.py` | `1681d8cb41658c1b60aef37efa0f6b4c0abc5772a2d93a37ccbc6b2ec6c6c313` |
| `src/runtime/yokohama_pad_queue.py` | `62d912e00677709cc3aa5ae98239f883c0bbe1f1f0e0eef3015236941c2dbf08` |
| `src/runtime/yokohama_pad_advisory_contract.py` | `3ccb271d50fb3e845607becc00b9ba22fc8b4d9dcced8da3be91f04b7d99df31` |
| `src/runtime/yokohama_native.py` | `0f16f25db09f7de43bdff30ea031ea9e6bb5ac0a85b839e5aa304e95d1d93c44` |
| `docs/examples/yokohama-urban-scene/files.sha256.json` | `857a3297231dc837864b7f83d92701f9d9bc1af5b80b7d3e8526115194aabed2` |
| `docs/examples/yokohama-pad-state/model/model.json` | `63e8442160efb00470e91fb30133bb9e36433f1f3378b8cf33db24a9de057506` |
| `scripts/verify_yokohama_decisions.py` | `3dd039700162e99206c88e0445975b6bb468f52b030bae42d0801592c2186cd4` |
| `scripts/verify_yokohama_pad_queue.py` | `81574ff47bdc88c146eb2d95eb41381e8cf6ffd2f37a5772d4169bcd62354401` |
| `scripts/verify_yokohama_pad_advisory.py` | `6f63a70af8dcc55622c5270590185b40e2806f32e765a56954f950c0712b49b5` |
| `scripts/verify_yokohama_payload.py` | `b524a558591ea0b5dc1d6e82097b9ebbf5f08783d2f89b54ac872dc3643bbba8` |
| `scripts/verify_yokohama_sitl.py` | `dc689bb8968d382a9d09437363db11868d810cff4456b73eb9f1ff3ea494e8db` |
| `scripts/yokohama_native_trial.py` | `783731c38254e4cc1284dd26ebaedf40d415f5086cd3110e1ba692c395128e1a` |
| `scripts/yokohama_cloud_lifecycle.py` | `68d79cf7feeaa3bf5eb6550c9bec1791f73322fca7d2bad579c6ccf19454d09a` |
