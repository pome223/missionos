class StateKeys:
    """ADK session.state のキー定数。

    Persistent keys: 次の turn にも残す制御データ。
    Temporary keys (temp:): 現在の invocation 中のみ有効。
    """

    # ── Task ──────────────────────────────────────────────────────────────
    TASK_GOAL = "task:goal"
    TASK_CONSTRAINTS = "task:constraints"
    TASK_SUCCESS_CRITERIA = "task:success_criteria"

    # ── Plan ──────────────────────────────────────────────────────────────
    PLAN_CURRENT = "plan:current"
    PLAN_APPROVED = "plan:approved"
    PLAN_RISK_LEVEL = "plan:risk_level"

    # ── Replay ────────────────────────────────────────────────────────────
    REPLAY_SOURCE_TASK_ID = "replay:source_task_id"
    REPLAY_FROM_STEP = "replay:from_step"
    REPLAY_CONTEXT = "replay:context"

    # ── Approval / Verification / Repair ──────────────────────────────────
    APPROVAL_STATUS = "approval:status"
    APPROVAL_REQUEST = "approval:request"
    VERIFY_LAST_REPORT = "verify:last_report"
    REPAIR_COUNT = "repair:count"

    # ── Memory ────────────────────────────────────────────────────────────
    MEMORY_LAST_CANDIDATE_IDS = "memory:last_candidate_ids"
    MEMORY_LAST_PROMOTED_IDS = "memory:last_promoted_ids"

    # ── Temporary (invocation-scoped) ─────────────────────────────────────
    TEMP_RETRIEVAL_BUNDLE = "temp:retrieval_bundle"
    TEMP_PLANNER_DRAFT = "plan:draft"  # not temp: so after_agent_callback can read it
    TEMP_EXECUTOR_OUTPUTS = "temp:executor_outputs"
    TEMP_ARTIFACT_REFS = "temp:artifact_refs"
    TEMP_VERIFICATION_INPUTS = "temp:verification_inputs"
    TEMP_REPAIR_PATCH = "temp:repair_patch"
    TEMP_CURRENT_BROWSER_NEW_TAB_COUNT = "temp:current_browser_new_tab_count"
    TEMP_CURRENT_BROWSER_OPENED_TAB_IDS = "temp:current_browser_opened_tab_ids"
    TEMP_CURRENT_BROWSER_ACTIVE_TAB_ID = "temp:current_browser_active_tab_id"
    TEMP_CURRENT_BROWSER_LAST_OBSERVED_TAB = "temp:current_browser_last_observed_tab"
    TEMP_CURRENT_BROWSER_CONTROL_UI_TAB = "temp:current_browser_control_ui_tab"
    TEMP_CURRENT_BROWSER_DESTINATION_TAB = "temp:current_browser_destination_tab"
    TEMP_CURRENT_BROWSER_SPREADSHEET_TARGET = "temp:current_browser_spreadsheet_target"
    TEMP_CURRENT_BROWSER_SPREADSHEET_CELL_EDIT_READY = (
        "temp:current_browser_spreadsheet_cell_edit_ready"
    )
    TEMP_CURRENT_TAB_EXTENSION_DISCONNECTED = "temp:current_tab_extension_disconnected"
    TEMP_CURRENT_TAB_EXTENSION_DISCONNECTED_RAW_ERROR = (
        "temp:current_tab_extension_disconnected_raw_error"
    )
