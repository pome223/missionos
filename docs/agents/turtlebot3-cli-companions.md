# TurtleBot3 CLI Companion Correlation

`missionos chat --robot turtlebot3` may open `operate`, `watch`, and `map` as
companion surfaces while the synchronous Gateway request is still running.
These surfaces are read-only or operator-input clients of the Gateway; they are
not alternate sources of task identity or authority.

## Task identity contract

The exact durable task ID is the correlation boundary:

1. Before a synchronous home-robot run, chat records the set of existing
   home-robot task IDs returned by the selected Gateway.
2. While the request is in flight, only a newly observed home-robot task may be
   bound to the conversation.
3. Once the response contains a task ID, the operation result takes precedence
   over any outer or previously stored task reference.
4. `operate`, `watch`, `map`, and the final chat status monitor all receive that
   same task ID.
5. An existing task is never rebound merely because it is still `running` or
   `pending`.
6. If multiple new home-robot tasks appear concurrently, discovery is
   ambiguous and opens none of them; the exact operation response must resolve
   the task instead.
7. If the initial task listing is unavailable, early discovery is disabled. An
   error is not treated as an observed empty baseline.
8. After one task is bound, chat stops listing for alternatives and waits for
   the exact operation response.

The task list is discovery evidence during a synchronous request. It does not
prove dispatch, motion, recovery success, or completion. Terminal truth comes
from `GET /tasks/{task_id}` and the task's source-backed artifacts.

## Gateway and authentication contract

Generated companion scripts inherit the chat process's:

- selected Gateway URL
- request timeout
- CLI state path
- working directory
- Gateway API key through a mode-`0600` temporary file

The API key value is not written into shell commands. The temporary key file is
removed when the owning chat session stops its companions.

## Display and authority boundaries

`missionos_cli.chat_companions` owns companion discovery, script construction,
lifetime, and final-task monitoring. `missionos_cli.operate_view` owns read-only
operator-panel assembly and its refresh fingerprint. The fingerprint includes
the checkpoint hash and observed-point count so a revision or new observation
replaces stale presentation.

Neither module may:

- choose or revise a Recovery action
- create an approval artifact
- send a dispatch
- infer motion from an ACK
- infer success from a terminal label alone

Approval and dispatch remain on the existing CLI/Gateway authority path. A
terminal task update can replace stale conversational presentation, but it must
still preserve the distinction among task terminal state, recovery outcome,
delivery completion, and physical execution.
