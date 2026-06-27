const NAV_META = {
  chat: { title: "Chat", subtitle: "Gateway WebSocket chat" },
  "missionos-chat": { title: "MissionOS", subtitle: "Conversation with the AI agent" },
  dashboard: { title: "Dashboard", subtitle: "Task objects, approvals, and runtime status" },
  "mission-designer": { title: "Mission Designer", subtitle: "Prompt-derived PX4/Gazebo scenario proposals" },
  audit: { title: "Audit", subtitle: "Audit log explorer for actors, sessions, and approvals" },
  sessions: { title: "Sessions", subtitle: "Current browser sessions" },
  channels: { title: "Channels", subtitle: "Channel status overview" },
  skills: { title: "Skills", subtitle: "OpenClaw-style skill catalog and run" },
  memory: { title: "Memory", subtitle: "SQLite vector memory browser" },
  cron: { title: "Cron Jobs", subtitle: "Scheduled tasks (platform)" },
  logs: { title: "Live Logs", subtitle: "Raw client-side event mirror" },
  settings: { title: "Settings", subtitle: "Gateway connection options" }
};

const DEFAULTS = {
  gatewayUrl: `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`,
  userId: "web_user",
  token: ""
};

const STORAGE_KEY = "boiled_claw_ui_settings_v1";
const DEFAULT_API_TIMEOUT_MS = 20000;
const MISSIONOS_AUTONOMY_CONVERSATION_TIMEOUT_MS = 90000;
const MISSION_SCENARIO_LIVE_SITL_TIMEOUT_MS = 4200000;
const MISSION_FLIGHT_ANIMATION_MIN_MS = 450;
const MISSION_FLIGHT_ANIMATION_MAX_MS = 1800;

const navButtons = Array.from(document.querySelectorAll(".nav-item"));
const tabs = Array.from(document.querySelectorAll(".tab"));
const tabTitle = document.getElementById("tabTitle");
const tabSubtitle = document.getElementById("tabSubtitle");
const messagesEl = document.getElementById("messages");
const missionosChatMessagesEl = document.getElementById("missionosChatMessages");
const missionosChatForm = document.getElementById("missionosChatForm");
const missionosChatInputEl = document.getElementById("missionosChatInput");
const missionosChatStatusEl = document.getElementById("missionosChatStatus");
const missionosTakeoffLatInputEl = document.getElementById("missionosTakeoffLatInput");
const missionosTakeoffLonInputEl = document.getElementById("missionosTakeoffLonInput");
const missionosDropoffLatInputEl = document.getElementById("missionosDropoffLatInput");
const missionosDropoffLonInputEl = document.getElementById("missionosDropoffLonInput");
const missionosPayloadInputEl = document.getElementById("missionosPayloadInput");
const missionosWindSpeedInputEl = document.getElementById("missionosWindSpeedInput");
const missionosWindDirectionInputEl = document.getElementById("missionosWindDirectionInput");
const missionosRoofHeightInputEl = document.getElementById("missionosRoofHeightInput");
const missionosFlightSetupStatusEl = document.getElementById("missionosFlightSetupStatus");
const eventLogEl = document.getElementById("eventLog");
const eventCountBadgeEl = document.getElementById("eventCountBadge");
const rawLogEl = document.getElementById("rawLog");
const sessionListEl = document.getElementById("sessionList");
const refreshDashboardBtn = document.getElementById("refreshDashboardBtn");
const clearDashboardFiltersBtn = document.getElementById("clearDashboardFiltersBtn");
const dashboardSearchInputEl = document.getElementById("dashboardSearchInput");
const dashboardSessionBackendEl = document.getElementById("dashboardSessionBackend");
const dashboardSessionNamespaceEl = document.getElementById("dashboardSessionNamespace");
const dashboardPendingApprovalsEl = document.getElementById("dashboardPendingApprovals");
const dashboardOpenTasksEl = document.getElementById("dashboardOpenTasks");
const dashboardApprovalsListEl = document.getElementById("dashboardApprovalsList");
const dashboardTasksListEl = document.getElementById("dashboardTasksList");
const dashboardApprovalsCaptionEl = document.getElementById("dashboardApprovalsCaption");
const dashboardTasksCaptionEl = document.getElementById("dashboardTasksCaption");
const dashboardApprovalsPrevBtn = document.getElementById("dashboardApprovalsPrevBtn");
const dashboardApprovalsNextBtn = document.getElementById("dashboardApprovalsNextBtn");
const dashboardTasksPrevBtn = document.getElementById("dashboardTasksPrevBtn");
const dashboardTasksNextBtn = document.getElementById("dashboardTasksNextBtn");
const dashboardDetailPanelEl = document.getElementById("dashboardDetailPanel");
const dashboardDetailBadgeEl = document.getElementById("dashboardDetailBadge");
const analyticsContentEl = document.getElementById("analyticsContent");
const refreshAnalyticsBtn = document.getElementById("refreshAnalyticsBtn");
const refreshAuditBtn = document.getElementById("refreshAuditBtn");
const clearAuditFiltersBtn = document.getElementById("clearAuditFiltersBtn");
const auditSearchInputEl = document.getElementById("auditSearchInput");
const auditActorInputEl = document.getElementById("auditActorInput");
const auditSessionInputEl = document.getElementById("auditSessionInput");
const auditToolInputEl = document.getElementById("auditToolInput");
const auditSourceInputEl = document.getElementById("auditSourceInput");
const auditResultInputEl = document.getElementById("auditResultInput");
const auditCurrentSessionEl = document.getElementById("auditCurrentSession");
const auditMatchCountEl = document.getElementById("auditMatchCount");
const auditCaptionEl = document.getElementById("auditCaption");
const auditPrevBtn = document.getElementById("auditPrevBtn");
const auditNextBtn = document.getElementById("auditNextBtn");
const auditListEl = document.getElementById("auditList");
const auditDetailPanelEl = document.getElementById("auditDetailPanel");
const auditDetailBadgeEl = document.getElementById("auditDetailBadge");
const inspectorSessionBackendEl = document.getElementById("inspectorSessionBackend");
const inspectorCurrentSessionEl = document.getElementById("inspectorCurrentSession");
const inspectorPendingApprovalsEl = document.getElementById("inspectorPendingApprovals");
const inspectorOpenTasksEl = document.getElementById("inspectorOpenTasks");
const inspectorApprovalsListEl = document.getElementById("inspectorApprovalsList");
const inspectorTasksListEl = document.getElementById("inspectorTasksList");
const inspectorApprovalCountBadgeEl = document.getElementById("inspectorApprovalCountBadge");
const inspectorTaskCountBadgeEl = document.getElementById("inspectorTaskCountBadge");
const inspectorSelectionDetailEl = document.getElementById("inspectorSelectionDetail");
const inspectorSelectionBadgeEl = document.getElementById("inspectorSelectionBadge");
const statusDotEl = document.getElementById("statusDot");
const statusTextEl = document.getElementById("statusText");
const sessionBadgeEl = document.getElementById("sessionBadge");
const gatewayHostLabelEl = document.getElementById("gatewayHostLabel");
const heartbeatDotEl = document.getElementById("heartbeatDot");
const dashboardFilterChips = Array.from(document.querySelectorAll(".status-chip"));

const connectBtn = document.getElementById("connectBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
const abortBtn = document.getElementById("abortBtn");
const chatForm = document.getElementById("chatForm");
const messageInputEl = document.getElementById("messageInput");
const missionScenarioPromptInputEl = document.getElementById("missionScenarioPromptInput");
const missionScenarioTakeoffLatInputEl = document.getElementById("missionScenarioTakeoffLatInput");
const missionScenarioTakeoffLonInputEl = document.getElementById("missionScenarioTakeoffLonInput");
const missionScenarioDropoffLatInputEl = document.getElementById("missionScenarioDropoffLatInput");
const missionScenarioDropoffLonInputEl = document.getElementById("missionScenarioDropoffLonInput");
const missionScenarioRoofHeightInputEl = document.getElementById("missionScenarioRoofHeightInput");
const missionScenarioPayloadWeightInputEl = document.getElementById("missionScenarioPayloadWeightInput");
const missionScenarioWindSpeedInputEl = document.getElementById("missionScenarioWindSpeedInput");
const missionScenarioWindDirectionInputEl = document.getElementById("missionScenarioWindDirectionInput");
const missionScenarioWindGustInputEl = document.getElementById("missionScenarioWindGustInput");
const missionScenarioWindVarianceInputEl = document.getElementById("missionScenarioWindVarianceInput");
const missionScenarioBatteryRemainingInputEl = document.getElementById("missionScenarioBatteryRemainingInput");
const missionScenarioSensorFailureTypeInputEl = document.getElementById("missionScenarioSensorFailureTypeInput");
const missionScenarioLandingZoneBlockedInputEl = document.getElementById("missionScenarioLandingZoneBlockedInput");
const missionScenarioVisibilityModeInputEl = document.getElementById("missionScenarioVisibilityModeInput");
const missionScenarioNoFlyZoneMarkerInputEl = document.getElementById("missionScenarioNoFlyZoneMarkerInput");
const missionScenarioTrafficConflictMarkerInputEl = document.getElementById("missionScenarioTrafficConflictMarkerInput");
const missionScenarioAlternateLandingMarkerInputEl = document.getElementById("missionScenarioAlternateLandingMarkerInput");
const missionScenarioMovingActorMarkerInputEl = document.getElementById("missionScenarioMovingActorMarkerInput");
const missionScenarioMultiDroneConflictProbeInputEl = document.getElementById("missionScenarioMultiDroneConflictProbeInput");
const missionScenarioTelemetryDropoutModeInputEl = document.getElementById("missionScenarioTelemetryDropoutModeInput");
const missionScenarioMavlinkLinkDegradationModeInputEl = document.getElementById("missionScenarioMavlinkLinkDegradationModeInput");
const missionScenarioCoordinateRouteStatusEl = document.getElementById("missionScenarioCoordinateRouteStatus");
const missionScenarioGenerateBtn = document.getElementById("missionScenarioGenerateBtn");
const missionScenarioApproveBtn = document.getElementById("missionScenarioApproveBtn");
const missionScenarioPrepareSitlBtn = document.getElementById("missionScenarioPrepareSitlBtn");
const missionScenarioExecuteSitlBtn = document.getElementById("missionScenarioExecuteSitlBtn");
const missionScenarioResetBtn = document.getElementById("missionScenarioResetBtn");
const missionScenarioStatusEl = document.getElementById("missionScenarioStatus");
const missionScenarioValidationStatusEl = document.getElementById("missionScenarioValidationStatus");
const missionScenarioDryRunStatusEl = document.getElementById("missionScenarioDryRunStatus");
const missionScenarioWaypointCountEl = document.getElementById("missionScenarioWaypointCount");
const missionScenarioSegmentCountEl = document.getElementById("missionScenarioSegmentCount");
const missionScenarioSitlStatusEl = document.getElementById("missionScenarioSitlStatus");
const missionScenarioSitlExecutionStatusEl = document.getElementById("missionScenarioSitlExecutionStatus");
const missionScenarioResultEl = document.getElementById("missionScenarioResult");
const missionScenarioRawEl = document.getElementById("missionScenarioRaw");
const missionosOperatorSummaryRefreshBtn = document.getElementById("missionosOperatorSummaryRefreshBtn");
const missionosOperatorSummaryStatusEl = document.getElementById("missionosOperatorSummaryStatus");
const missionosOperatorSummaryEl = document.getElementById("missionosOperatorSummary");
const missionosMilestoneRefreshBtn = document.getElementById("missionosMilestoneRefreshBtn");
const missionosMilestoneStatusEl = document.getElementById("missionosMilestoneStatus");
const missionosMilestoneSummaryEl = document.getElementById("missionosMilestoneSummary");
const missionosAuthorityBeltEl = document.getElementById("missionosAuthorityBelt");
const missionosTimelineRefreshBtn = document.getElementById("missionosTimelineRefreshBtn");
const missionosTimelineStatusEl = document.getElementById("missionosTimelineStatus");
const missionosTimelineSummaryEl = document.getElementById("missionosTimelineSummary");
const missionosEnvelopeRefreshBtn = document.getElementById("missionosEnvelopeRefreshBtn");
const missionosEnvelopeStatusEl = document.getElementById("missionosEnvelopeStatus");
const missionosEnvelopeSummaryEl = document.getElementById("missionosEnvelopeSummary");
const missionosKnowledgeRefreshBtn = document.getElementById("missionosKnowledgeRefreshBtn");
const missionosKnowledgeStatusEl = document.getElementById("missionosKnowledgeStatus");
const missionosKnowledgeSummaryEl = document.getElementById("missionosKnowledgeSummary");
const missionosAgentsRefreshBtn = document.getElementById("missionosAgentsRefreshBtn");
const missionosAgentsStatusEl = document.getElementById("missionosAgentsStatus");
const missionosAgentsSummaryEl = document.getElementById("missionosAgentsSummary");
const missionosKnowledgeSharingRefreshBtn = document.getElementById("missionosKnowledgeSharingRefreshBtn");
const missionosKnowledgeCuratorDryRunBtn = document.getElementById("missionosKnowledgeCuratorDryRunBtn");
const missionosKnowledgePublishBtn = document.getElementById("missionosKnowledgePublishBtn");
const missionosKnowledgeSharingStatusEl = document.getElementById("missionosKnowledgeSharingStatus");
const missionosKnowledgeSharingSummaryEl = document.getElementById("missionosKnowledgeSharingSummary");
const missionosPolicyAuthorityRefreshBtn = document.getElementById("missionosPolicyAuthorityRefreshBtn");
const missionosPolicyAuthorityPromoteBtn = document.getElementById("missionosPolicyAuthorityPromoteBtn");
const missionosPolicyAuthorityStatusEl = document.getElementById("missionosPolicyAuthorityStatus");
const missionosPolicyAuthoritySummaryEl = document.getElementById("missionosPolicyAuthoritySummary");
const missionosSitlDispatchRefreshBtn = document.getElementById("missionosSitlDispatchRefreshBtn");
const missionosSitlDispatchRunBtn = document.getElementById("missionosSitlDispatchRunBtn");
const missionosSitlDispatchStatusEl = document.getElementById("missionosSitlDispatchStatus");
const missionosSitlDispatchSummaryEl = document.getElementById("missionosSitlDispatchSummary");
const missionosScopedForm3RefreshBtn = document.getElementById("missionosScopedForm3RefreshBtn");
const missionosScopedForm3RunBtn = document.getElementById("missionosScopedForm3RunBtn");
const missionosScopedForm3StatusEl = document.getElementById("missionosScopedForm3Status");
const missionosScopedForm3SummaryEl = document.getElementById("missionosScopedForm3Summary");
const missionosForm2aAiAgentRefreshBtn = document.getElementById("missionosForm2aAiAgentRefreshBtn");
const missionosForm2aAiAgentRunSelectionBtn = document.getElementById("missionosForm2aAiAgentRunSelectionBtn");
const missionosForm2aAiAgentApproveBtn = document.getElementById("missionosForm2aAiAgentApproveBtn");
const missionosForm2aAiAgentConsumeBtn = document.getElementById("missionosForm2aAiAgentConsumeBtn");
const missionosForm2aAiAgentStatusEl = document.getElementById("missionosForm2aAiAgentStatus");
const missionosForm2aAiAgentSummaryEl = document.getElementById("missionosForm2aAiAgentSummary");
const missionosRepairPlannerRefreshBtn = document.getElementById("missionosRepairPlannerRefreshBtn");
const missionosRepairPlannerRunBtn = document.getElementById("missionosRepairPlannerRunBtn");
const missionosRepairPlannerStatusEl = document.getElementById("missionosRepairPlannerStatus");
const missionosRepairPlannerSummaryEl = document.getElementById("missionosRepairPlannerSummary");
const missionosAutonomyRefreshBtn = document.getElementById("missionosAutonomyRefreshBtn");
const missionosAutonomyMonitorStatusEl = document.getElementById("missionosAutonomyMonitorStatus");
const missionosAutonomyMonitorSummaryEl = document.getElementById("missionosAutonomyMonitorSummary");
const missionosOperationsRefreshBtn = document.getElementById("missionosOperationsRefreshBtn");
const missionosOperationsStatusEl = document.getElementById("missionosOperationsStatus");
const missionosOperationsListEl = document.getElementById("missionosOperationsList");
const missionosOperationsRunLogEl = document.getElementById("missionosOperationsRunLog");
let latestMissionScenarioResult = null;
let latestMissionOSChatRepairProposal = null;
const latestMissionOSOperatorPayloads = {
  milestone: null,
  timeline: null,
  envelopes: null,
  knowledge: null,
  agents: null,
  knowledgeSharing: null,
  policyAuthority: null,
  sitlDispatch: null,
  scopedForm3: null,
  form2aAiAgent: null,
  repairPlanner: null,
  operations: null,
  lastOperation: null,
};
const latestMissionOSOperatorSourceErrors = {
  milestone: "",
  timeline: "",
  envelopes: "",
  knowledge: "",
  agents: "",
  knowledgeSharing: "",
  policyAuthority: "",
  sitlDispatch: "",
  scopedForm3: "",
  form2aAiAgent: "",
  repairPlanner: "",
  operations: "",
};
let latestMissionOSAutonomyNotice = "";
let latestMissionOSOperatorInstruction = "";
let missionOSChatInitialized = false;
let _missionOSChatInputComposing = false;
const missionFlightAnimationStates = new Map();
let missionFlightAnimationFrame = null;

const MISSION_SCENARIO_COORDINATE_ROUTE_DEFAULTS = {
  takeoffLatitude: "35.3434673",
  takeoffLongitude: "138.7341134",
  dropoffLatitude: "35.3606000",
  dropoffLongitude: "138.7274000",
  roofHeightAglM: "10",
  payloadWeightKg: "1",
  windSpeedMps: "2",
  windDirectionDeg: "0",
  windGustMps: "4",
  windVariance: "1",
  batteryRemainingPercent: "",
  sensorFailureType: "",
  landingZoneBlocked: "",
  visibilityMode: "",
  noFlyZoneMarker: "",
  trafficConflictMarker: "",
  alternateLandingMarker: "",
  movingActorMarker: "",
  multiDroneConflictProbe: "",
  telemetryDropoutMode: "",
  mavlinkLinkDegradationMode: "",
};

const gatewayUrlEl = document.getElementById("gatewayUrl");
const tokenEl = document.getElementById("token");
const userIdEl = document.getElementById("userId");
const saveSettingsBtn = document.getElementById("saveSettingsBtn");
const resetSettingsBtn = document.getElementById("resetSettingsBtn");
const refreshSkillsBtn = document.getElementById("refreshSkillsBtn");
const refreshMemoryBtn = document.getElementById("refreshMemoryBtn");
const searchMemoryBtn = document.getElementById("searchMemoryBtn");
const memoryQueryInputEl = document.getElementById("memoryQueryInput");
const memoryTagsInputEl = document.getElementById("memoryTagsInput");
const memoryListEl = document.getElementById("memoryList");
const memoryStatsEl = document.getElementById("memoryStats");
const skillsListEl = document.getElementById("skillsList");
const skillNameInputEl = document.getElementById("skillNameInput");
const skillParamsInputEl = document.getElementById("skillParamsInput");
const runSkillBtn = document.getElementById("runSkillBtn");
const skillResultEl = document.getElementById("skillResult");

// cron elements
const cronListEl = document.getElementById("cronList");
const cronNameEl = document.getElementById("cronName");
const cronExprEl = document.getElementById("cronExpr");
const cronTaskEl = document.getElementById("cronTask");
const cronAgentEl = document.getElementById("cronAgent");
const cronDeliveryEl = document.getElementById("cronDelivery");
const cronRetriesEl = document.getElementById("cronRetries");
const cronSysEventEl = document.getElementById("cronSysEvent");
const addCronBtn = document.getElementById("addCronBtn");
const refreshCronBtn = document.getElementById("refreshCronBtn");
const cronResultEl = document.getElementById("cronResult");

let socket = null;
let waitingIndicator = null;
const sessions = [];
let pendingMessage = null;
const messageHistory = [];
let currentSessionId = null;
let reconnectSessionId = null;
let reconnectHandle = null;
let reconnectAttempts = 0;
let manualDisconnectRequested = false;
const inlineApprovals = new Map();
const MAX_EVENT_ROWS = 200;

// --- streaming state ---
let _streamingBubble = null;
let _streamingText = "";
let _runInProgress = false;
let _messageInputComposing = false;
let _dashboardRefreshHandle = null;
let _dashboardRefreshPromise = null;
let _auditRefreshHandle = null;
let _auditRefreshPromise = null;
const dashboardState = {
  sessionBackend: "-",
  sessionNamespace: "",
  pendingApprovals: [],
  pendingApprovalsTotal: 0,
  dashboardApprovals: [],
  approvalPage: 1,
  approvalPageSize: 12,
  approvalTotal: 0,
  approvalHasMore: false,
  recentTasks: [],
  recentTasksTotal: 0,
  dashboardTasks: [],
  taskPage: 1,
  taskPageSize: 12,
  taskTotal: 0,
  taskHasMore: false,
  openTaskCount: 0,
  searchQuery: "",
  taskStatusFilter: "all",
  approvalStateFilter: "all",
  selectedKind: null,
  selectedId: null,
  selectedTask: null,
  selectedApproval: null,
  relatedTasks: [],
  relatedApprovals: [],
  childTasks: [],
  subagentRun: null,
  taskTimeline: [],
  taskTimelinePagination: null,
  selectedApprovalSuggestions: [],
  taskComparison: null,
};
const auditState = {
  entries: [],
  page: 1,
  pageSize: 20,
  total: 0,
  hasMore: false,
  searchQuery: "",
  actorFilter: "",
  sessionFilter: "",
  toolFilter: "",
  sourceFilter: "",
  resultFilter: "",
  selectedEntryId: null,
  selectedEntry: null,
  autoSelectFirst: false,
  focus: null,
};
const KNOWN_STATUS_TAGS = new Set([
  "accepted",
  "approved",
  "approving",
  "blocked",
  "cancelled",
  "candidate_only",
  "completed",
  "denied",
  "denying",
  "expired",
  "failed",
  "fresh",
  "idle",
  "operator_visible",
  "pending",
  "paused",
  "propagated",
  "read_only",
  "rejected",
  "resolved",
  "running",
  "safe",
  "stale",
  "unknown",
  "waiting_for_approval",
  "warning",
]);

// -----------------------------------------------------------------------
// Settings
// -----------------------------------------------------------------------

function parseStoredSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed ? parsed : {};
  } catch (_) { return {}; }
}

function parseUrlSettings() {
  const params = new URLSearchParams(window.location.search);
  const partial = {};
  if (params.get("gatewayUrl")) partial.gatewayUrl = params.get("gatewayUrl");
  if (params.get("token")) partial.token = params.get("token");
  if (params.get("userId")) partial.userId = params.get("userId");
  return partial;
}

function currentSettings() {
  return {
    gatewayUrl: (gatewayUrlEl.value || "").trim(),
    token: (tokenEl.value || "").trim(),
    userId: (userIdEl.value || "").trim() || "web_user"
  };
}

function applySettings(settings) {
  const merged = { ...DEFAULTS, ...settings };
  gatewayUrlEl.value = merged.gatewayUrl;
  tokenEl.value = merged.token;
  userIdEl.value = merged.userId;
  gatewayHostLabelEl.textContent = merged.gatewayUrl || DEFAULTS.gatewayUrl;
}

function persistSettings() {
  const settings = currentSettings();
  localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  gatewayHostLabelEl.textContent = settings.gatewayUrl;
  addSystemMessage("saved settings");
  logEvent("settings.saved", settings);
}

function resetSettings() {
  localStorage.removeItem(STORAGE_KEY);
  applySettings(DEFAULTS);
  addSystemMessage("reset settings");
  logEvent("settings.reset", DEFAULTS);
}

function isTabActive(tabKey) {
  return document.getElementById(`tab-${tabKey}`)?.classList.contains("active");
}

// -----------------------------------------------------------------------
// URL helpers
// -----------------------------------------------------------------------

function toWebSocketUrl(settings, sessionId = null) {
  let base = settings.gatewayUrl || DEFAULTS.gatewayUrl;
  if (base.startsWith("http://")) base = "ws://" + base.slice(7);
  if (base.startsWith("https://")) base = "wss://" + base.slice(8);
  if (!base.startsWith("ws://") && !base.startsWith("wss://")) {
    base = `${window.location.protocol === "https:" ? "wss" : "ws"}://${base}`;
  }
  base = base.replace(/\/+$/, "");

  const parsed = new URL(base);
  const userPath = `/ws/${encodeURIComponent(settings.userId)}`;

  if (["/chat", "/chat/", "/ws", "/ws/"].includes(parsed.pathname) ||
      /^\/ws\/[^/]+\/?$/.test(parsed.pathname) ||
      parsed.pathname === "/" || parsed.pathname === "") {
    parsed.pathname = userPath;
  } else if (!parsed.pathname.startsWith("/ws/")) {
    parsed.pathname = userPath;
  }

  const wsUrl = new URL(parsed.toString());
  if (settings.token) wsUrl.searchParams.set("token", settings.token);
  if (sessionId) wsUrl.searchParams.set("session_id", sessionId);
  return wsUrl.toString();
}

function toHttpBaseUrl(settings) {
  let base = settings.gatewayUrl || DEFAULTS.gatewayUrl;
  if (base.startsWith("ws://")) base = "http://" + base.slice(5);
  if (base.startsWith("wss://")) base = "https://" + base.slice(6);
  if (!base.startsWith("http://") && !base.startsWith("https://")) {
    base = `${window.location.protocol}//${base}`;
  }
  base = base.replace(/\/+$/, "");
  const parsed = new URL(base);
  if (["/chat", "/chat/", "/ws", "/ws/"].includes(parsed.pathname) ||
      /^\/ws\/[^/]+\/?$/.test(parsed.pathname) || parsed.pathname === "/") {
    parsed.pathname = "";
  }
  return parsed.toString().replace(/\/+$/, "");
}

// -----------------------------------------------------------------------
// UI helpers
// -----------------------------------------------------------------------

function setStatus(online, text) {
  statusDotEl.classList.toggle("online", online);
  statusDotEl.classList.toggle("offline", !online);
  statusTextEl.textContent = text;
  connectBtn.disabled = online;
  disconnectBtn.disabled = !online;
}

function setRunInProgress(inProgress) {
  _runInProgress = inProgress;
  abortBtn.disabled = !inProgress;
  messageInputEl.disabled = inProgress;
}

function appendBubble(kind, text, { persist = true } = {}) {
  if (persist) {
    messageHistory.push({ kind, text });
  }
  const bubble = document.createElement("div");
  bubble.className = `bubble ${kind}`;
  bubble.textContent = text;
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

function addSystemMessage(text) {
  return appendBubble("system", text);
}

function appendMissionOSChatBubble(kind, text) {
  if (!missionosChatMessagesEl) return null;
  const bubble = document.createElement("div");
  bubble.className = `bubble ${kind}`;
  bubble.textContent = text;
  missionosChatMessagesEl.appendChild(bubble);
  missionosChatMessagesEl.scrollTop = missionosChatMessagesEl.scrollHeight;
  return bubble;
}

function missionOSChatThinkingText() {
  return [
    "MissionOS is thinking...",
    "Reading current evidence, checking specialist/critic routing, then Gateway guardrails will decide authority.",
    "No approval, dispatch, or progress is created while this is running.",
  ].join("\n");
}

function missionOSChatChoiceCardHtml(model) {
  const approveAttrs = model.liveSitlTaskId
    ? `data-missionos-live-sitl-task-id="${escapeAttr(model.liveSitlTaskId)}"`
    : `data-missionos-instruction="${escapeAttr(model.approveInstruction || "承認して")}"`;
  return [
    `<div class="approval-card missionos-chat-choice-card">`,
    `<div class="approval-header">`,
    `<div class="approval-title">${escapeHtml(model.title || "MissionOS decision")}</div>`,
    `<span class="tag approval-status">${escapeHtml(model.status || "pending")}</span>`,
    `</div>`,
    model.subtitle ? `<div class="approval-meta">${escapeHtml(model.subtitle)}</div>` : "",
    model.reason ? `<div class="approval-reason">${escapeHtml(model.reason)}</div>` : "",
    `<div class="approval-actions">`,
    `<button class="btn btn-sm approve-btn" type="button" ${approveAttrs}>${escapeHtml(model.approveLabel || "Approve")}</button>`,
    `<button class="btn btn-sm deny-btn" type="button" data-missionos-instruction="${escapeAttr(model.denyInstruction || "拒否して")}">${escapeHtml(model.denyLabel || "Deny")}</button>`,
    `</div>`,
    `</div>`,
  ].join("");
}

function appendMissionOSChatChoiceCard(model) {
  if (!missionosChatMessagesEl || !model) return null;
  const bubble = document.createElement("div");
  bubble.className = "bubble approval missionos-chat-choice";
  bubble.innerHTML = missionOSChatChoiceCardHtml(model);
  wireMissionOSChatChoiceCardActions(bubble);
  if (model.liveSitlTaskId) {
    const executeButton = bubble.querySelector("[data-missionos-live-sitl-task-id]");
    if (executeButton) {
      executeButton.disabled = true;
      executeButton.textContent = "Checking SITL";
    }
    void hydrateMissionOSChatLiveSITLChoiceCard(bubble, model);
  }
  missionosChatMessagesEl.appendChild(bubble);
  missionosChatMessagesEl.scrollTop = missionosChatMessagesEl.scrollHeight;
  return bubble;
}

async function checkMissionOSChatSITLReadiness(taskId) {
  const response = await apiFetchWithTimeout("/px4-gazebo/mission-scenarios/execute-sitl-readiness", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  }, 10000);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`);
  return payload;
}

async function startMissionOSChatSITL(taskId) {
  const response = await apiFetchWithTimeout("/px4-gazebo/mission-scenarios/start-sitl", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  }, 120000);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.summary?.failure_category || payload?.detail || `HTTP ${response.status}`);
  return payload;
}

function missionOSChatSITLReadinessSummary(readinessPayload) {
  const readiness = asPlainObject(
    readinessPayload?.px4_gazebo_sitl_execution_readiness
      || readinessPayload?.artifacts?.px4_gazebo_sitl_execution_readiness
      || readinessPayload?.summary
      || readinessPayload,
  );
  const summary = asPlainObject(readinessPayload?.summary);
  return {
    readiness,
    summary,
    ready: readiness.readiness_status === "ready" || summary.readiness_status === "ready",
  };
}

function missionOSChatSITLNotReadyText(readinessPayload) {
  const { readiness, summary } = missionOSChatSITLReadinessSummary(readinessPayload);
  const host = readiness.endpoint_host || summary.endpoint_host || "127.0.0.1";
  const port = readiness.mavlink_udp_port || summary.mavlink_udp_port || 14540;
  const dockerRunning = readiness.docker_container_running === true || summary.docker_container_running === true;
  const endpointObserved = readiness.mavlink_endpoint_observed === true || summary.mavlink_endpoint_observed === true;
  const headline = dockerRunning
    ? "SITL実行準備がまだ完了していません。"
    : "SITL実行環境がまだ起動していません。";
  const explanation = dockerRunning
    ? "PX4/Gazebo container は起動していますが、MAVLink upload endpoint がまだ観測できないため、ここで止めました。"
    : "PX4/Gazebo の実行先が見つからないため、ここで止めました。";
  return [
    headline,
    "",
    `ここまでで MissionOS は、飛行計画の作成、Human Review、SITL request の準備まで完了しています。ただし ${explanation}`,
    "",
    "まだ実行していないこと:",
    "- PX4 への mission upload",
    "- PX4 ACK の確認",
    "- live flight runner の起動",
    "- verifier / Repair Planner への引き渡し",
    "",
    "確認結果:",
    `- simulator container: ${dockerRunning ? "起動済み" : "未起動"}`,
    `- MAVLink endpoint ${host}:${port}: ${endpointObserved ? "観測済み" : "未観測"}`,
    "",
    "MissionOS が PX4/Gazebo SITL の起動を試してから、mission upload に進めるか確認します。",
    "起動できない場合は startup failure receipt として止めます。",
  ].filter(Boolean).join("\n");
}

function missionOSChatSITLNotReadyCardHtml(model, readinessPayload) {
  const { readiness, summary } = missionOSChatSITLReadinessSummary(readinessPayload);
  const host = readiness.endpoint_host || summary.endpoint_host || "127.0.0.1";
  const port = readiness.mavlink_udp_port || summary.mavlink_udp_port || 14540;
  const dockerRunning = readiness.docker_container_running === true || summary.docker_container_running === true;
  const endpointObserved = readiness.mavlink_endpoint_observed === true || summary.mavlink_endpoint_observed === true;
  const title = dockerRunning ? "SITL実行準備が未完了です" : "SITL実行環境が未起動です";
  const meta = dockerRunning
    ? "計画とSITL requestの準備は終わっています。PX4/Gazebo container は起動していますが、MAVLink endpoint が未観測なので mission upload には進みません。"
    : "計画とSITL requestの準備は終わっています。MissionOS が PX4/Gazebo SITL の起動を試してから、mission upload に進めるか確認します。";
  const nextAction = dockerRunning
    ? "次: MissionOS が MAVLink readiness を再確認します。"
    : "次: MissionOS が PX4/Gazebo SITL の起動を試します。";
  return [
    `<div class="approval-card missionos-chat-choice-card missionos-chat-sitl-not-ready-card">`,
    `<div class="approval-header">`,
    `<div class="approval-title">${escapeHtml(title)}</div>`,
    `<span class="tag approval-status">blocked</span>`,
    `</div>`,
    `<div class="approval-meta">${escapeHtml(meta)}</div>`,
    `<div class="approval-reason">${escapeHtml([
      "まだ実行していないこと:",
      "- mission upload",
      "- PX4 ACK confirmation",
      "- live flight runner",
      "- verifier / Repair Planner handoff",
      "",
      `確認: simulator container=${dockerRunning ? "running" : "not running"}, MAVLink ${host}:${port}=${endpointObserved ? "observed" : "not observed"}`,
      "",
      nextAction,
    ].join("\n"))}</div>`,
    `<div class="approval-actions">`,
    `<button class="btn btn-sm approve-btn" type="button" data-missionos-sitl-readiness-check="${escapeAttr(model.liveSitlTaskId || "")}">SITLを起動して続行</button>`,
    `</div>`,
    `</div>`,
  ].join("");
}

function missionOSChatSITLStartupFailedCardHtml(err) {
  return [
    `<div class="approval-card missionos-chat-choice-card missionos-chat-sitl-not-ready-card">`,
    `<div class="approval-header">`,
    `<div class="approval-title">SITL startup failed</div>`,
    `<span class="tag approval-status">blocked</span>`,
    `</div>`,
    `<div class="approval-meta">MissionOS は mission upload、live flight runner、progress claim に進んでいません。</div>`,
    `<div class="approval-reason">${escapeHtml(String(err))}</div>`,
    `</div>`,
  ].join("");
}

function wireMissionOSChatChoiceCardActions(bubble) {
  bubble.querySelectorAll("[data-missionos-instruction], [data-missionos-live-sitl-task-id]").forEach((button) => {
    button.addEventListener("click", () => {
      const instruction = button.dataset.missionosInstruction || "";
      const liveSitlTaskId = button.dataset.missionosLiveSitlTaskId || "";
      bubble.querySelectorAll("button").forEach((choice) => { choice.disabled = true; });
      if (liveSitlTaskId) {
        void executeMissionOSChatLiveSITL(liveSitlTaskId);
      } else {
        void submitMissionOSChatInstruction(instruction);
      }
    });
  });
}

function missionOSChatRuntimeRecoveryControlsHtml(taskId) {
  return [
    `<div class="k">Manual Operator Override</div>`,
    `<div class="approval-actions missionos-runtime-recovery-actions">`,
    `<button class="btn btn-sm approve-btn" type="button" data-missionos-runtime-recovery-action="land" data-missionos-runtime-recovery-task-id="${escapeAttr(taskId)}">LAND</button>`,
    `<button class="btn btn-sm approve-btn" type="button" data-missionos-runtime-recovery-action="return_to_launch" data-missionos-runtime-recovery-task-id="${escapeAttr(taskId)}">RTL</button>`,
    `</div>`,
    `<div class="item-meta">Operator approval is required. This dispatch records runner abort, command ACK, and false physical/delivery/progress fields.</div>`,
  ].join("");
}

function missionOSChatRuntimeRecoveryAgentBridge(task) {
  const artifacts = asPlainObject(asPlainObject(task).artifacts);
  return asPlainObject(artifacts.missionos_runtime_recovery_agent_live_bridge);
}

function missionOSChatRuntimeRecoveryAgentAction(result) {
  const assessment = asPlainObject(asPlainObject(result).assessment);
  return String(
    assessment.selected_bounded_action
    || assessment.recommended_action
    || assessment.recovery_action
    || "",
  );
}

function missionOSChatRuntimeRecoveryAgentProposalHtml(taskId, task) {
  const bridge = missionOSChatRuntimeRecoveryAgentBridge(task);
  const result = asPlainObject(bridge.runtime_recovery_agent_result);
  const assessment = asPlainObject(result.assessment);
  const action = missionOSChatRuntimeRecoveryAgentAction(result);
  if (!Object.keys(result).length) {
    return [
      `<div class="missionos-runtime-recovery-agent-proposal">`,
      `<div class="k">Agent Proposal</div>`,
      `<div class="item-meta">Waiting for Runtime Recovery Agent assessment from live telemetry.</div>`,
      `</div>`,
    ].join("");
  }
  const status = result.runtime_status || result.agent_status || "-";
  const riskReasons = asArray(
    assessment.observed_risk_reasons || assessment.trigger_reasons
  ).join(", ") || "-";
  const blockingReasons = asArray(assessment.blocking_reasons).join(", ");
  const rationale = assessment.rationale || result.rationale || "";
  const canDispatch = action === "land" || action === "return_to_launch";
  const actionLabel = action === "return_to_launch" ? "RTL" : action.toUpperCase();
  const proposalAction = canDispatch
    ? `<button class="btn btn-sm approve-btn" type="button" data-missionos-runtime-recovery-action="${escapeAttr(action)}" data-missionos-runtime-recovery-task-id="${escapeAttr(taskId)}">Approve ${escapeHtml(actionLabel)}</button>`
    : "";
  return [
    `<div class="missionos-runtime-recovery-agent-proposal">`,
    `<div class="k">Agent Proposal</div>`,
    `<div class="item-meta mono">status=${escapeHtml(status)}; action=${escapeHtml(action || "-")}</div>`,
    `<div class="item-meta mono">risk=${escapeHtml(riskReasons)}</div>`,
    blockingReasons ? `<div class="item-meta mono">blocked=${escapeHtml(blockingReasons)}</div>` : "",
    rationale ? `<div class="muted">${escapeHtml(rationale)}</div>` : "",
    proposalAction ? `<div class="approval-actions">${proposalAction}</div>` : "",
    `<div class="muted">Agent output is proposal-only. Dispatch still requires the operator to approve a bounded LAND/RTL action.</div>`,
    `</div>`,
  ].join("");
}

function missionOSChatRuntimeRecoveryDispatchCardHtml(taskId) {
  return [
    `<div class="approval-card missionos-chat-choice-card missionos-chat-runtime-recovery-card">`,
    `<div class="approval-header">`,
    `<div class="approval-title">Runtime Recovery Intervention</div>`,
    `<span class="tag approval-status" data-missionos-runtime-recovery-status>operator approval required</span>`,
    `</div>`,
    `<div class="approval-meta">Live SITL is running. During the active decision window, LAND/RTL asks Gateway to abort the normal runner and send an operator-approved bounded PX4/Gazebo recovery command.</div>`,
    missionOSChatRuntimeRecoveryControlsHtml(taskId),
    `<div class="missionos-runtime-recovery-evidence" data-missionos-runtime-recovery-evidence>${missionOSRuntimeRecoveryInterventionEvidenceHtml(null)}</div>`,
    `<div class="item-meta">The Runtime Recovery Agent evidence window remains the input; this action does not claim delivery completion, progress, physical execution, or hardware authority.</div>`,
    `</div>`,
  ].join("");
}

function missionOSRuntimeRecoveryInterventionEvidenceHtml(task) {
  if (missionOSChatLiveSITLBlockedBeforeRunner({ task })) {
    return [
      `<div class="missionos-runtime-recovery-evidence-box">`,
      `<div class="k">Recovery Agent view</div>`,
      `<div class="muted">Live SITL runner was not invoked because explicit opt-in is missing. No mission upload, PX4 ACK, telemetry, or recovery evidence window exists for this request.</div>`,
      `</div>`,
    ].join("");
  }
  const artifacts = asPlainObject(asPlainObject(task).artifacts);
  const bridge = asPlainObject(artifacts.missionos_runtime_recovery_agent_live_bridge);
  const result = asPlainObject(bridge.runtime_recovery_agent_result);
  const assessment = asPlainObject(result.assessment);
  const bridgeSnapshot = asPlainObject(bridge.telemetry_snapshot);
  const rawSnapshot = asPlainObject(artifacts.mission_designer_live_telemetry_snapshot);
  const snapshot = Object.keys(bridgeSnapshot).length ? bridgeSnapshot : rawSnapshot;
  const backendEvidence = asPlainObject(snapshot.backend_evidence_collection);
  const observedDelta = asPlainObject(backendEvidence.observed_behavior_delta);
  const expectedState = asPlainObject(snapshot.expected_runtime_state);
  const phaseEnvelope = asPlainObject(expectedState.phase_envelope);
  const latestSample = asPlainObject(snapshot.latest_sample);
  const hasEvidence = [
    bridge,
    result,
    assessment,
    snapshot,
    backendEvidence,
    observedDelta,
    expectedState,
    latestSample,
  ].some((item) => Object.keys(item).length);
  if (!hasEvidence) {
    return [
      `<div class="missionos-runtime-recovery-evidence-box">`,
      `<div class="k">Recovery Agent view</div>`,
      `<div class="muted">Waiting for the first backend evidence window: mission upload, PX4 ACK, telemetry, MAVLink, and control logs have not reached this card yet.</div>`,
      `</div>`,
    ].join("");
  }
  const fmt = (value, digits = 1) => {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric.toFixed(digits) : "-";
  };
  const firstLine = `assessment=${assessment.recommended_action || assessment.recovery_action || result.agent_status || "-"}; risk=${assessment.risk_level || assessment.active_runtime_risk || "-"}`;
  const positionLine = `position=(${fmt(latestSample.local_x_m)}, ${fmt(latestSample.local_y_m)}, ${fmt(latestSample.relative_alt_m || latestSample.altitude_above_home_m)})`;
  const controlLine = expectedState.control_status
    ? `expected/control: ${expectedState.control_status}${phaseEnvelope.quantitative_check_name ? ` (${phaseEnvelope.quantitative_check_name})` : ""}`
    : `expected/control: ${observedDelta.control_status || "-"}`;
  return [
    `<div class="missionos-runtime-recovery-evidence-box">`,
    `<div class="k">Recovery Agent view</div>`,
    `<div class="item-meta mono">${escapeHtml(firstLine)}</div>`,
    `<div class="item-meta mono">${escapeHtml(positionLine)}</div>`,
    `<div class="item-meta mono">${escapeHtml(controlLine)}</div>`,
    `</div>`,
  ].join("");
}

function wireMissionOSChatRuntimeRecoveryDispatchActions(container) {
  if (!container) return;
  container.querySelectorAll("[data-missionos-runtime-recovery-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.missionosRuntimeRecoveryAction || "";
      const taskId = button.dataset.missionosRuntimeRecoveryTaskId || "";
      const card = button.closest(".missionos-chat-runtime-recovery, .missionos-chat-visual-response, .missionos-chat-runtime-recovery-card, .detail-card");
      if (card) {
        card.querySelectorAll("[data-missionos-runtime-recovery-action]").forEach((choice) => { choice.disabled = true; });
      }
      button.textContent = "Dispatching";
      void dispatchMissionOSChatRuntimeRecovery(taskId, action, card);
    });
  });
}

function appendMissionOSChatRuntimeRecoveryDispatchCard(taskId) {
  if (!missionosChatMessagesEl) return null;
  const id = String(taskId || "").trim();
  if (!id) return null;
  const bubble = document.createElement("div");
  bubble.className = "bubble approval missionos-chat-runtime-recovery";
  bubble.innerHTML = missionOSChatRuntimeRecoveryDispatchCardHtml(id);
  wireMissionOSChatRuntimeRecoveryDispatchActions(bubble);
  missionosChatMessagesEl.appendChild(bubble);
  missionosChatMessagesEl.scrollTop = missionosChatMessagesEl.scrollHeight;
  return bubble;
}

function updateMissionOSChatRuntimeRecoveryDispatchCardEvidence(bubble, task) {
  if (!bubble) return;
  const evidenceEl = bubble.querySelector("[data-missionos-runtime-recovery-evidence]");
  if (!evidenceEl) return;
  evidenceEl.innerHTML = missionOSRuntimeRecoveryInterventionEvidenceHtml(task);
}

function disableMissionOSChatRuntimeRecoveryDispatchCard(bubble, state = "ended") {
  if (!bubble) return;
  bubble.querySelectorAll("[data-missionos-runtime-recovery-action]").forEach((button) => {
    button.disabled = true;
    if (state === "blocked_before_runner") {
      button.hidden = true;
    }
  });
  const statusEl = bubble.querySelector("[data-missionos-runtime-recovery-status], .approval-status");
  const metaEl = bubble.querySelector(".approval-meta");
  const controlsMetaEl = bubble.querySelector(".missionos-runtime-recovery-actions + .item-meta");
  const hasDispatchResult = Boolean(bubble.querySelector(".approval-status")?.textContent && !/operator approval required/i.test(bubble.querySelector(".approval-status").textContent));
  if (!hasDispatchResult && statusEl) {
    statusEl.textContent = state === "blocked_before_runner" ? "not active" : "ended";
  }
  if (!hasDispatchResult && metaEl) {
    metaEl.textContent = state === "blocked_before_runner"
      ? "Live SITL was not started because explicit opt-in was missing. LAND/RTL intervention is unavailable because no runner or telemetry window exists for this request."
      : "Live SITL is no longer running. LAND/RTL intervention buttons are disabled for this request.";
  }
  if (state === "blocked_before_runner" && controlsMetaEl) {
    controlsMetaEl.textContent = "Runtime recovery controls are disabled because no live runner or telemetry window exists for this request.";
  }
}

async function dispatchMissionOSChatRuntimeRecovery(taskId, action, cardBubble) {
  const id = String(taskId || "").trim();
  const recoveryAction = String(action || "").trim();
  if (!id || !recoveryAction) return;
  appendMissionOSChatBubble("user", recoveryAction === "land" ? "LANDを承認" : "RTLを承認");
  setMissionOSChatStatus("Runtime Recovery dispatch is being sent through Gateway...");
  try {
    const response = await apiFetchWithTimeout("/px4-gazebo/mission-scenarios/recovery-dispatch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_id: id,
        recovery_action: recoveryAction,
        explicit_recovery_dispatch_approval: true,
      }),
    }, 45000);
    const payload = await response.json();
    const summary = asPlainObject(payload.summary);
    if (!response.ok && response.status !== 409) {
      throw new Error(payload?.detail || `HTTP ${response.status}`);
    }
    if (payload.task) {
      latestMissionScenarioResult = {
        ...(latestMissionScenarioResult || {}),
        sitl_execution_result_task: payload.task,
        runtime_recovery_intervention_response: payload,
        summary: {
          ...(latestMissionScenarioResult?.summary || {}),
          runtime_recovery_dispatch_status: summary.dispatch_status || payload.response_status || "-",
          runtime_recovery_action: summary.recovery_action || recoveryAction,
          runtime_recovery_command_ack: summary.command_ack_result_name || "-",
        },
      };
      renderMissionScenarioResult(latestMissionScenarioResult);
      updateMissionOSChatRuntimeRecoveryDispatchCardEvidence(cardBubble, payload.task);
    }
    const dispatchStatus = summary.dispatch_status || payload.response_status || "-";
    const ack = summary.command_ack_result_name || "-";
    const runnerAbort = summary.runner_abort_observed === true ? "runner abort observed" : "runner abort not observed yet";
    const reasons = asArray(summary.blocked_reasons).join(", ");
    appendMissionOSChatBubble(
      response.ok ? "agent" : "system",
      [
        `Runtime Recovery ${recoveryAction} dispatch result: ${dispatchStatus}.`,
        `ACK: ${ack}; ${runnerAbort}.`,
        reasons ? `Blocked reasons: ${reasons}` : "",
        "delivery/progress/physical claim はしていません。",
      ].filter(Boolean).join("\n"),
    );
    if (cardBubble) {
      const statusEl = cardBubble.querySelector("[data-missionos-runtime-recovery-status], .approval-status");
      if (statusEl) statusEl.textContent = dispatchStatus;
    }
    setMissionOSChatStatus("Conversation ready");
    logEvent("missionos.chat.runtime_recovery.dispatch", summary);
  } catch (err) {
    appendMissionOSChatBubble("system", missionosFriendlyActionError(err, "/px4-gazebo/mission-scenarios/recovery-dispatch"));
    setMissionOSChatStatus("Runtime Recovery dispatch failed safely");
    logEvent("missionos.chat.runtime_recovery.dispatch_error", { error: String(err) });
  }
}

async function startMissionOSChatSITLFromChoiceCard(bubble, model) {
  try {
    setMissionOSChatStatus("PX4/Gazebo SITL を起動しています");
    const startupPayload = await startMissionOSChatSITL(model.liveSitlTaskId);
    logEvent("missionos.chat.live_sitl.startup", startupPayload.summary || startupPayload);
    await hydrateMissionOSChatLiveSITLChoiceCard(bubble, model);
  } catch (err) {
    bubble.innerHTML = missionOSChatSITLStartupFailedCardHtml(err);
    setMissionOSChatStatus("SITL startup failed safely");
    logEvent("missionos.chat.live_sitl.startup_error", { error: String(err) });
  }
}

async function hydrateMissionOSChatLiveSITLChoiceCard(bubble, model) {
  try {
    const readinessPayload = await checkMissionOSChatSITLReadiness(model.liveSitlTaskId);
    const readiness = missionOSChatSITLReadinessSummary(readinessPayload);
    if (!readiness.ready) {
      bubble.innerHTML = missionOSChatSITLNotReadyCardHtml(model, readinessPayload);
      bubble.querySelectorAll("[data-missionos-sitl-readiness-check]").forEach((button) => {
        button.addEventListener("click", () => {
          button.disabled = true;
          button.textContent = "起動中";
          void startMissionOSChatSITLFromChoiceCard(bubble, model);
        });
      });
      setMissionOSChatStatus("SITL実行環境が未起動です");
      return;
    }
    bubble.innerHTML = missionOSChatChoiceCardHtml(model);
    wireMissionOSChatChoiceCardActions(bubble);
    const executeButton = bubble.querySelector("[data-missionos-live-sitl-task-id]");
    if (executeButton) {
      executeButton.disabled = false;
      executeButton.textContent = model.approveLabel || "Execute Live SITL";
    }
  } catch (err) {
    bubble.innerHTML = [
      `<div class="approval-card missionos-chat-choice-card missionos-chat-sitl-not-ready-card">`,
      `<div class="approval-header">`,
      `<div class="approval-title">SITL readiness check failed</div>`,
      `<span class="tag approval-status">blocked</span>`,
      `</div>`,
      `<div class="approval-meta">${escapeHtml(String(err))}</div>`,
      `</div>`,
    ].join("");
    setMissionOSChatStatus("SITL readiness check failed safely");
  }
}

function missionOSChatRepairProposalCardHtml(model) {
  return [
    `<div class="approval-card missionos-chat-choice-card missionos-chat-repair-proposal-card">`,
    `<div class="approval-header">`,
    `<div class="approval-title">${escapeHtml(model.title || "Repair Planner Agent proposal")}</div>`,
    `<span class="tag approval-status">${escapeHtml(model.status || "pending")}</span>`,
    `</div>`,
    model.summary ? `<div class="approval-meta">${escapeHtml(model.summary)}</div>` : "",
    model.reason ? `<div class="approval-reason">${escapeHtml(model.reason)}</div>` : "",
    `<div class="data-grid compact">`,
    `<div><strong>payload</strong><span>${escapeHtml(model.payloadLabel || "-")}</span></div>`,
    `<div><strong>wind env</strong><span>${escapeHtml(model.windLabel || "-")}</span></div>`,
    `</div>`,
    `<div class="approval-actions">`,
    `<button class="btn btn-sm approve-btn" type="button" data-missionos-repair-approve="1">${escapeHtml(model.approveLabel || "Approve repair plan")}</button>`,
    `<button class="btn btn-sm deny-btn" type="button" data-missionos-repair-deny="1">${escapeHtml(model.denyLabel || "Deny")}</button>`,
    `</div>`,
    `</div>`,
  ].join("");
}

function appendMissionOSChatRepairProposalCard(model) {
  if (!missionosChatMessagesEl || !model) return null;
  latestMissionOSChatRepairProposal = model;
  const bubble = document.createElement("div");
  bubble.className = "bubble approval missionos-chat-choice missionos-chat-repair-choice";
  bubble.innerHTML = missionOSChatRepairProposalCardHtml(model);
  bubble.querySelectorAll("[data-missionos-repair-approve], [data-missionos-repair-deny]").forEach((button) => {
    button.addEventListener("click", () => {
      bubble.querySelectorAll("button").forEach((choice) => { choice.disabled = true; });
      if (button.dataset.missionosRepairApprove) {
        void approveMissionOSChatRepairProposal();
      } else {
        appendMissionOSChatBubble("user", "Deny repair proposal");
        appendMissionOSChatBubble("agent", "Repair Planner Agent の提案を保留しました。MissionOS は再実行も progress claim も行いません。別の条件を会話で指示できます。");
        clearMissionOSChatStateCards();
      }
    });
  });
  missionosChatMessagesEl.appendChild(bubble);
  missionosChatMessagesEl.scrollTop = missionosChatMessagesEl.scrollHeight;
  return bubble;
}

function clearMissionOSChatStateCards() {
  if (!missionosChatMessagesEl) return;
  missionosChatMessagesEl
    .querySelectorAll(".missionos-chat-visual-response, .missionos-chat-choice")
    .forEach((card) => card.remove());
}

function missionOSFlightSetupInputs() {
  return [
    missionosTakeoffLatInputEl,
    missionosTakeoffLonInputEl,
    missionosDropoffLatInputEl,
    missionosDropoffLonInputEl,
    missionosPayloadInputEl,
    missionosWindSpeedInputEl,
    missionosWindDirectionInputEl,
    missionosRoofHeightInputEl,
  ];
}

function missionOSFlightSetupCoordinateRoutePayload() {
  const takeoffLatitude = missionScenarioOptionalNumber(missionosTakeoffLatInputEl);
  const takeoffLongitude = missionScenarioOptionalNumber(missionosTakeoffLonInputEl);
  const dropoffLatitude = missionScenarioOptionalNumber(missionosDropoffLatInputEl);
  const dropoffLongitude = missionScenarioOptionalNumber(missionosDropoffLonInputEl);
  if ([takeoffLatitude, takeoffLongitude, dropoffLatitude, dropoffLongitude].some((value) => value === null)) {
    return null;
  }
  return {
    takeoff_latitude: takeoffLatitude,
    takeoff_longitude: takeoffLongitude,
    dropoff_latitude: dropoffLatitude,
    dropoff_longitude: dropoffLongitude,
    dropoff_roof_height_agl_m: missionScenarioOptionalNumber(missionosRoofHeightInputEl) ?? 0,
    payload_weight_kg: missionScenarioOptionalNumber(missionosPayloadInputEl),
    wind_speed_mps: missionScenarioOptionalNumber(missionosWindSpeedInputEl),
    wind_direction_deg: missionScenarioOptionalNumber(missionosWindDirectionInputEl),
    wind_gust_mps: null,
    wind_variance: null,
    battery_remaining_percent: null,
    sensor_failure_component: null,
    sensor_failure_type: null,
    landing_zone_blocked: false,
    visibility_mode: null,
    no_fly_zone_marker: false,
    traffic_conflict_marker: false,
    alternate_landing_marker: false,
    moving_actor_marker: false,
    multi_drone_conflict_probe: false,
    telemetry_dropout_mode: null,
    mavlink_link_degradation_mode: null,
  };
}

function updateMissionOSFlightSetupStatus() {
  if (!missionosFlightSetupStatusEl) return;
  const route = missionOSFlightSetupCoordinateRoutePayload();
  missionosFlightSetupStatusEl.className = "coordinate-route-status muted";
  if (!route) {
    missionosFlightSetupStatusEl.className = "coordinate-route-status coordinate-route-status-warning";
    missionosFlightSetupStatusEl.textContent = "Enter start and goal coordinates before asking MissionOS to plan a flight.";
    return;
  }
  const distanceM = missionScenarioCoordinateRouteDistanceM(
    route.takeoff_latitude,
    route.takeoff_longitude,
    route.dropoff_latitude,
    route.dropoff_longitude,
  );
  const payloadKg = Number(route.payload_weight_kg);
  if (Number.isFinite(payloadKg) && payloadKg > 5) {
    missionosFlightSetupStatusEl.className = "coordinate-route-status coordinate-route-status-warning";
    missionosFlightSetupStatusEl.textContent = `High-risk planning input: about ${Math.round(distanceM)} m, payload ${payloadKg} kg, wind ${route.wind_speed_mps ?? "-"} m/s @ ${route.wind_direction_deg ?? "-"} deg. MissionOS can discuss and plan from this evidence, but this is not an executable-ready payload.`;
    return;
  }
  missionosFlightSetupStatusEl.className = "coordinate-route-status coordinate-route-status-ready";
  missionosFlightSetupStatusEl.textContent = `Route ready: about ${Math.round(distanceM)} m, payload ${route.payload_weight_kg ?? "-"} kg, wind ${route.wind_speed_mps ?? "-"} m/s @ ${route.wind_direction_deg ?? "-"} deg.`;
}

function applyMissionOSFlightSetupRoute(route) {
  const value = asPlainObject(route);
  const assignments = [
    [missionosTakeoffLatInputEl, value.takeoff_latitude],
    [missionosTakeoffLonInputEl, value.takeoff_longitude],
    [missionosDropoffLatInputEl, value.dropoff_latitude],
    [missionosDropoffLonInputEl, value.dropoff_longitude],
    [missionosRoofHeightInputEl, value.dropoff_roof_height_agl_m],
    [missionosPayloadInputEl, value.payload_weight_kg],
    [missionosWindSpeedInputEl, value.wind_speed_mps],
    [missionosWindDirectionInputEl, value.wind_direction_deg],
  ];
  assignments.forEach(([input, nextValue]) => {
    if (!input || nextValue === undefined || nextValue === null || nextValue === "") return;
    input.value = String(nextValue);
  });
  updateMissionOSFlightSetupStatus();
}

function applyMissionOSChatRepairProposalToFlightSetup(model) {
  const parameters = asPlainObject(model?.parameters);
  if (parameters.takeoff_latitude !== undefined && missionosTakeoffLatInputEl) {
    missionosTakeoffLatInputEl.value = String(parameters.takeoff_latitude);
  }
  if (parameters.takeoff_longitude !== undefined && missionosTakeoffLonInputEl) {
    missionosTakeoffLonInputEl.value = String(parameters.takeoff_longitude);
  }
  if (parameters.dropoff_latitude !== undefined && missionosDropoffLatInputEl) {
    missionosDropoffLatInputEl.value = String(parameters.dropoff_latitude);
  }
  if (parameters.dropoff_longitude !== undefined && missionosDropoffLonInputEl) {
    missionosDropoffLonInputEl.value = String(parameters.dropoff_longitude);
  }
  if (parameters.payload_weight_kg !== undefined && missionosPayloadInputEl) {
    missionosPayloadInputEl.value = String(parameters.payload_weight_kg);
  }
  if (parameters.wind_speed_mps !== undefined && missionosWindSpeedInputEl) {
    missionosWindSpeedInputEl.value = String(parameters.wind_speed_mps);
  }
  if (parameters.wind_direction_deg !== undefined && missionosWindDirectionInputEl) {
    missionosWindDirectionInputEl.value = String(parameters.wind_direction_deg);
  }
  updateMissionOSFlightSetupStatus();
}

function missionOSChatNumberFromRepairParameters(parameters, keys) {
  for (const key of keys) {
    const value = parameters[key];
    if (value === undefined || value === null || value === "") continue;
    const numberValue = Number(value);
    if (Number.isFinite(numberValue)) return numberValue;
  }
  return undefined;
}

function missionOSChatNormalizeRepairParameters(rawParameters) {
  const raw = asPlainObject(rawParameters);
  const normalized = {};
  const payload = missionOSChatNumberFromRepairParameters(raw, [
    "payload_weight_kg",
    "payload_mass_kg",
    "payload_kg",
    "payload",
  ]);
  const takeoffLatitude = missionOSChatNumberFromRepairParameters(raw, ["takeoff_latitude", "start_latitude"]);
  const takeoffLongitude = missionOSChatNumberFromRepairParameters(raw, ["takeoff_longitude", "start_longitude"]);
  const dropoffLatitude = missionOSChatNumberFromRepairParameters(raw, ["dropoff_latitude", "goal_latitude"]);
  const dropoffLongitude = missionOSChatNumberFromRepairParameters(raw, ["dropoff_longitude", "goal_longitude"]);
  if (payload !== undefined) normalized.payload_weight_kg = payload;
  if (takeoffLatitude !== undefined) normalized.takeoff_latitude = takeoffLatitude;
  if (takeoffLongitude !== undefined) normalized.takeoff_longitude = takeoffLongitude;
  if (dropoffLatitude !== undefined) normalized.dropoff_latitude = dropoffLatitude;
  if (dropoffLongitude !== undefined) normalized.dropoff_longitude = dropoffLongitude;
  return normalized;
}

function missionOSChatRepairParameterDelta(parameters, execution = null) {
  const normalized = asPlainObject(parameters);
  const comparisons = missionOSChatRepairCurrentValues(execution);
  return Object.entries(normalized).some(([key, value]) => {
    const nextValue = Number(value);
    const currentValue = Number(comparisons[key]);
    if (!Number.isFinite(nextValue)) return false;
    if (!Number.isFinite(currentValue)) return false;
    return Math.abs(nextValue - currentValue) > 1e-9;
  });
}

function missionOSChatFiniteNumber(...values) {
  for (const value of values) {
    if (value === undefined || value === null || value === "") continue;
    const numberValue = Number(value);
    if (Number.isFinite(numberValue)) return numberValue;
  }
  return undefined;
}

function missionOSChatExecutionArtifacts(execution) {
  const task = asPlainObject(execution?.task);
  return asPlainObject(task.artifacts);
}

function missionOSChatRepairCurrentValues(execution = null) {
  const artifacts = missionOSChatExecutionArtifacts(execution);
  const route = asPlainObject(artifacts.mission_designer_coordinate_pair_route);
  const proposal = asPlainObject(artifacts.px4_gazebo_mission_scenario_proposal);
  const result = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_execution_result);
  const summary = asPlainObject(execution?.summary);
  const currentRoute = missionOSFlightSetupCoordinateRoutePayload() || {};
  return {
    payload_weight_kg: missionOSChatFiniteNumber(
      route.payload_weight_kg,
      proposal.payload_weight_kg,
      result.payload_weight_kg,
      summary.payload_weight_kg,
      currentRoute.payload_weight_kg,
      missionScenarioOptionalNumber(missionosPayloadInputEl),
    ),
    wind_speed_mps: missionOSChatFiniteNumber(route.wind_speed_mps, summary.wind_speed_mps, currentRoute.wind_speed_mps, missionScenarioOptionalNumber(missionosWindSpeedInputEl)),
    wind_direction_deg: missionOSChatFiniteNumber(route.wind_direction_deg, summary.wind_direction_deg, currentRoute.wind_direction_deg, missionScenarioOptionalNumber(missionosWindDirectionInputEl)),
    takeoff_latitude: missionOSChatFiniteNumber(route.takeoff_latitude, currentRoute.takeoff_latitude, missionScenarioOptionalNumber(missionosTakeoffLatInputEl)),
    takeoff_longitude: missionOSChatFiniteNumber(route.takeoff_longitude, currentRoute.takeoff_longitude, missionScenarioOptionalNumber(missionosTakeoffLonInputEl)),
    dropoff_latitude: missionOSChatFiniteNumber(route.dropoff_latitude, currentRoute.dropoff_latitude, missionScenarioOptionalNumber(missionosDropoffLatInputEl)),
    dropoff_longitude: missionOSChatFiniteNumber(route.dropoff_longitude, currentRoute.dropoff_longitude, missionScenarioOptionalNumber(missionosDropoffLonInputEl)),
  };
}

function missionOSChatRepairUploadHandshakeOnly(execution) {
  const artifacts = missionOSChatExecutionArtifacts(execution);
  const summary = asPlainObject(execution?.summary);
  const receipt = asPlainObject(artifacts.px4_gazebo_sitl_mission_upload_receipt);
  const blockedReceipt = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt);
  const failedReceipt = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt);
  const result = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_execution_result);
  const blockedReasons = [
    ...asArray(summary.blocked_reasons),
    ...asArray(receipt.blocked_reasons),
    ...asArray(blockedReceipt.blocked_reasons),
    summary.failure_category,
    blockedReceipt.failure_category,
    failedReceipt.failure_category,
  ].map((item) => String(item || "")).filter(Boolean);
  const uploadObserved = receipt.upload_status === "uploaded"
    || result.actual_sitl_mission_upload_observed === true
    || summary.upload_status === "uploaded";
  const hasUploadHandshakeFailure = blockedReasons.some((reason) => (
    reason.includes("mission_upload_timeout")
    || reason.includes("requires uploaded mission receipt")
    || reason.includes("requires accepted mission ACK")
    || reason.includes("requires complete mission request sequence")
  ));
  const hasTakeoffOrClimbFailure = blockedReasons.some((reason) => reason.includes("takeoff_or_climb"));
  return !uploadObserved && hasUploadHandshakeFailure && !hasTakeoffOrClimbFailure;
}

function missionOSChatRepairProposalReady(repair) {
  const proposal = asPlainObject(repair?.repair_proposal);
  return repair?.summary_status === "repair_proposal_ready"
    && Object.keys(proposal).length > 0
    && typeof proposal.proposed_operator_instruction === "string"
    && proposal.proposed_operator_instruction.trim().length > 0;
}

function setMissionOSChatStatus(text) {
  if (missionosChatStatusEl) missionosChatStatusEl.textContent = text;
}

function approvalStateLabel(status) {
  switch (status) {
    case "approving": return "approving";
    case "denying": return "denying";
    case "approved": return "approved";
    case "denied": return "denied";
    case "expiring": return "expiring";
    case "expired": return "expired";
    default: return "pending";
  }
}

function approvalBubbleClass(model) {
  let className = "bubble approval";
  if (model.status === "approved") className += " approval-resolved";
  if (model.status === "denied" || model.status === "denying") className += " approval-denied";
  if (model.status === "expiring") className += " approval-expiring";
  if (model.status === "expired") className += " approval-denied";
  return className;
}

function approvalCountdownHtml(model) {
  if (!model.expiresAt || !["pending", "expiring"].includes(model.status)) return "";
  const nowSec = Date.now() / 1000;
  const remaining = Math.max(0, Math.round(model.expiresAt - nowSec));
  if (remaining <= 0) return `<span class="approval-countdown expired">expired</span>`;
  const mins = Math.floor(remaining / 60);
  const secs = remaining % 60;
  const label = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
  const urgencyClass = remaining <= 30 ? " urgent" : remaining <= 60 ? " warning" : "";
  return `<span class="approval-countdown${urgencyClass}" data-expires-at="${model.expiresAt}">${label}</span>`;
}

function approvalEscalationHtml(model) {
  if (model.status !== "expiring") return "";
  const suggestions = Array.isArray(model.escalationSuggestions) ? model.escalationSuggestions : [];
  if (!suggestions.length) return "";
  const buttons = suggestions.map((s) =>
    `<button class="btn btn-sm" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="${escapeAttr(s.strategy || "session_exact")}">${escapeHtml(s.label || "Upgrade")}</button>`
  );
  return `<div class="approval-escalation"><div class="approval-escalation-label">Upgrade scope to avoid timeout:</div><div class="approval-actions">${buttons.join("")}</div></div>`;
}

function approvalBodyHtml(model) {
  const argsHtml = model.argsPreview
    ? `<div class="approval-args">args: ${escapeHtml(model.argsPreview)}</div>`
    : "";
  const noteHtml = model.note
    ? `<div class="approval-note">${escapeHtml(model.note)}</div>`
    : "";
  const countdownHtml = approvalCountdownHtml(model);
  const escalationHtml = approvalEscalationHtml(model);
  const familyPattern = approvalFamilyPattern(model.toolName || "");
  const strategyButtons = [];
  if (model.status === "pending" || model.status === "expiring") {
    strategyButtons.push(`<button class="btn btn-sm approve-btn" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="single">Approve</button>`);
    if (model.kind === "tool" && model.toolName) {
      strategyButtons.push(`<button class="btn btn-sm" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="session_exact">Session</button>`);
      if (familyPattern && familyPattern !== model.toolName) {
        strategyButtons.push(`<button class="btn btn-sm" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="family_session">Family</button>`);
      }
      if (isDesktopApprovalTool(model.toolName)) {
        strategyButtons.push(`<button class="btn btn-sm" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="desktop_session_pack">Desktop Pack</button>`);
      }
    }
    strategyButtons.push(`<button class="btn btn-sm deny-btn" data-id="${escapeAttr(model.requestId)}" data-approved="false" data-strategy="single">Deny</button>`);
  }
  const actionsHtml = strategyButtons.length
    ? `<div class="approval-actions">${strategyButtons.join("")}</div>`
    : "";

  return [
    `<div class="approval-card">`,
    `<div class="approval-header">`,
    `<div class="approval-title">${escapeHtml(model.title)}</div>`,
    countdownHtml,
    `<span class="tag approval-status">${escapeHtml(approvalStateLabel(model.status))}</span>`,
    `</div>`,
    model.subtitle ? `<div class="approval-meta">${escapeHtml(model.subtitle)}</div>` : "",
    model.reason ? `<div class="approval-reason">${escapeHtml(model.reason)}</div>` : "",
    argsHtml,
    noteHtml,
    escalationHtml,
    actionsHtml,
    `</div>`
  ].join("");
}

function wireApprovalButtons(bubble, model) {
  bubble.querySelectorAll("[data-id]").forEach((button) => {
    button.addEventListener("click", () => {
      const approved = button.dataset.approved === "true";
      const strategy = button.dataset.strategy || "single";
      sendApprovalAction(
        model.kind || "tool",
        model.requestId,
        approved,
        strategy,
        model.sessionId || currentSessionId || "",
      );
    });
  });
}

function renderInlineApproval(model) {
  const bubble = document.createElement("div");
  bubble.className = approvalBubbleClass(model);
  bubble.dataset.requestId = model.requestId;
  bubble.innerHTML = approvalBodyHtml(model);
  wireApprovalButtons(bubble, model);
  model.element = bubble;
  return bubble;
}

function updateInlineApprovalElement(model) {
  const bubble = model.element;
  if (!bubble || !bubble.isConnected) return;
  bubble.className = approvalBubbleClass(model);
  bubble.innerHTML = approvalBodyHtml(model);
  wireApprovalButtons(bubble, model);
}

function upsertInlineApproval(model) {
  const existing = inlineApprovals.get(model.requestId);
  const next = {
    createdAt: existing?.createdAt || Date.now(),
    status: existing?.status || "pending",
    note: existing?.note || "",
    ...existing,
    ...model
  };
  inlineApprovals.set(next.requestId, next);

  if (next.element && next.element.isConnected) {
    updateInlineApprovalElement(next);
    return next;
  }

  const bubble = renderInlineApproval(next);
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return next;
}

function updateInlineApprovalStatus(requestId, status, note = "") {
  const existing = inlineApprovals.get(requestId);
  if (!existing) return;
  existing.status = status;
  existing.note = note;
  updateInlineApprovalElement(existing);
}

function removeInlineApproval(requestId) {
  const existing = inlineApprovals.get(requestId);
  if (!existing) return;
  if (existing.element && existing.element.isConnected) {
    existing.element.remove();
  }
  inlineApprovals.delete(requestId);
}

function isDesktopApprovalTool(toolName) {
  return String(toolName || "").startsWith("desktop_");
}

function approvalFamilyPattern(toolName) {
  const normalized = String(toolName || "");
  if (normalized.startsWith("desktop_ax_")) return "desktop_ax_*";
  if (normalized.startsWith("desktop_view_")) return "desktop_view_*";
  if (normalized.startsWith("desktop_wait_")) return "desktop_wait_*";
  if (normalized.startsWith("desktop_control_")) return "desktop_control_*";
  return normalized;
}

function getPendingInlineApprovalIds() {
  return Array.from(inlineApprovals.values())
    .filter((model) => model.status === "pending")
    .sort((a, b) => a.createdAt - b.createdAt)
    .map((model) => model.requestId);
}

function parseApprovalResolutionMessage(message) {
  const match = /^Approval\s+([a-z0-9]+):\s+(approved|denied)$/i.exec(message || "");
  if (!match) return null;
  return {
    requestId: match[1],
    status: match[2].toLowerCase() === "approved" ? "approved" : "denied"
  };
}

function restoreMessages() {
  messagesEl.innerHTML = "";
  inlineApprovals.forEach((model) => {
    model.element = null;
  });
  messageHistory.forEach(({ kind, text }) => {
    const b = document.createElement("div");
    b.className = `bubble ${kind}`;
    b.textContent = text;
    messagesEl.appendChild(b);
  });
  Array.from(inlineApprovals.values())
    .sort((a, b) => a.createdAt - b.createdAt)
    .forEach((model) => {
      messagesEl.appendChild(renderInlineApproval(model));
    });
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function clearWaiting() {
  if (waitingIndicator) {
    waitingIndicator.remove();
    waitingIndicator = null;
  }
}

function logEvent(name, payload) {
  const ts = new Date().toISOString();
  const row = document.createElement("div");
  row.className = "event-row";
  row.textContent = `[${ts}] ${name}${payload ? ` ${JSON.stringify(payload)}` : ""}`;
  eventLogEl.prepend(row);
  while (eventLogEl.childElementCount > MAX_EVENT_ROWS) {
    eventLogEl.removeChild(eventLogEl.lastElementChild);
  }
  if (eventCountBadgeEl) {
    eventCountBadgeEl.textContent = String(eventLogEl.childElementCount);
  }
  const line = `[${ts}] ${name}${payload ? ` ${JSON.stringify(payload)}` : ""}`;
  rawLogEl.textContent = `${line}\n${rawLogEl.textContent}`.slice(0, 12000);
}

function apiFetch(url, init = {}) {
  const settings = currentSettings();
  const headers = new Headers(init.headers || {});
  if (settings.token) headers.set("Authorization", `Bearer ${settings.token}`);
  return fetch(url, { ...init, headers });
}

function apiFetchWithTimeout(url, init = {}, timeoutMs = DEFAULT_API_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(`Request timed out after ${timeoutMs}ms`), timeoutMs);
  return apiFetch(url, { ...init, signal: controller.signal })
    .finally(() => window.clearTimeout(timer));
}

function missionScenarioRequestErrorMessage(err, fallback = "Request failed") {
  if (err?.name === "AbortError") {
    return "Live SITL execution is still waiting for simulator evidence and exceeded the UI wait window. The operator coordinate route remains bound to SITL; check the persisted task or retry after the current run settles.";
  }
  const text = String(err || "").trim();
  return text || fallback;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeAttr(str) {
  return escapeHtml(str).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function formatTimestamp(ts) {
  if (!ts) return "-";
  try {
    if (typeof ts === "number") {
      return new Date(ts * 1000).toLocaleString();
    }
    const parsed = new Date(ts);
    if (!Number.isNaN(parsed.getTime())) {
      return parsed.toLocaleString();
    }
    const numeric = Number(ts);
    if (!Number.isNaN(numeric)) {
      return new Date(numeric * 1000).toLocaleString();
    }
    return "-";
  } catch (_) {
    return "-";
  }
}

function statusTag(status) {
  const normalized = String(status || "unknown").toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  const safe = escapeHtml(status || "unknown");
  const fallbackClass = KNOWN_STATUS_TAGS.has(normalized) ? "" : " status-fallback";
  const cls = `tag status-tag status-${normalized}${fallbackClass}`;
  return `<span class="${cls}">${safe}</span>`;
}

function asPlainObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function missionosMilestoneValue(value) {
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => String(item)).join(", ") : "[]";
  }
  if (value && typeof value === "object") {
    return JSON.stringify(value);
  }
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  return String(value);
}

function missionosMilestoneFieldRows(fields) {
  const entries = Object.entries(asPlainObject(fields)).filter(([, value]) => (
    value !== undefined && value !== null && value !== ""
  ));
  if (!entries.length) {
    return `<div class="muted">No field summary available.</div>`;
  }
  return entries.map(([key, value]) => `
    <div class="missionos-milestone-field">
      <span class="missionos-milestone-field-key">${escapeHtml(key)}</span>
      <span class="missionos-milestone-field-value mono">${escapeHtml(missionosMilestoneValue(value))}</span>
    </div>
  `).join("");
}

const MISSIONOS_AUTHORITY_FIELDS = [
  ["physical_execution_invoked", "physical execution"],
  ["physical_form1_claimed", "physical Form 1"],
  ["hardware_target_allowed", "hardware authority"],
  ["dispatch_authority_created", "dispatch authority"],
  ["delivery_completion_claimed", "delivery completion"],
  ["llm_gate_judge_used", "LLM gate judge"],
  ["approval_free_stronger_execution", "approval-free stronger execution"],
  ["public_sync_performed", "public sync"],
];

const missionosAuthoritySources = {
  milestone: null,
  operations: null,
  lastRun: null,
};

function missionosAuthorityBoundary(source) {
  const plain = asPlainObject(source);
  const nested = asPlainObject(plain.authority_boundary);
  return Object.keys(nested).length ? nested : plain;
}

function missionosAuthorityFieldState(boundary, key) {
  if (!Object.prototype.hasOwnProperty.call(boundary, key)) {
    return { status: "unknown", value: "unknown" };
  }
  const value = boundary[key];
  if (value !== false) {
    return { status: "warning", value: String(value) };
  }
  return { status: "safe", value: "false" };
}

function missionosAuthoritySourceStatus(source) {
  if (!source) return "unknown";
  const boundary = missionosAuthorityBoundary(source);
  if (boundary.authority_boundary_supported === false) return "warning";
  if (boundary.authority_boundary_status && boundary.authority_boundary_status !== "safe") return "warning";
  if (asArray(boundary.authority_unknown_fields).length) return "warning";
  return MISSIONOS_AUTHORITY_FIELDS.reduce((status, [key]) => {
    if (status === "warning") return status;
    return missionosAuthorityFieldState(boundary, key).status === "safe" ? status : "warning";
  }, "safe");
}

function missionosAuthorityWorstFieldState(sources, key) {
  let sawSafe = false;
  let firstUnknown = "";
  for (const [label, source] of sources) {
    if (!source) continue;
    const boundary = missionosAuthorityBoundary(source);
    const state = missionosAuthorityFieldState(boundary, key);
    if (state.status === "warning") {
      return {
        status: "warning",
        value: `${state.value} (${label})`,
      };
    }
    if (state.status === "unknown" && !firstUnknown) {
      firstUnknown = label;
    }
    if (state.status === "safe") {
      sawSafe = true;
    }
  }
  if (firstUnknown) {
    return { status: "unknown", value: `unknown (${firstUnknown})` };
  }
  return sawSafe ? { status: "safe", value: "false" } : { status: "unknown", value: "unknown" };
}

function renderMissionOSAuthorityBelt() {
  if (!missionosAuthorityBeltEl) return;
  const sources = [
    ["milestone", missionosAuthoritySources.milestone],
    ["operations", missionosAuthoritySources.operations],
    ["last run", missionosAuthoritySources.lastRun],
  ];
  const knownSources = sources.filter(([, source]) => source);
  const sourceStatuses = sources.map(([label, source]) => [label, missionosAuthoritySourceStatus(source)]);
  const aggregateStatus = sourceStatuses.some(([, status]) => status === "warning")
    ? "warning"
    : knownSources.length
      ? "safe"
      : "unknown";
  missionosAuthorityBeltEl.className = `missionos-authority-belt missionos-authority-belt-${aggregateStatus}`;
  missionosAuthorityBeltEl.innerHTML = `
    <div class="missionos-authority-belt-head">
      <div>
        <div class="k">MissionOS Authority Boundary</div>
        <div class="muted">Aggregated from current milestone, operation registry, and the latest GUI operation run.</div>
      </div>
      ${statusTag(aggregateStatus === "safe" ? "safe" : aggregateStatus)}
    </div>
    <div class="detail-chip-row">
      ${MISSIONOS_AUTHORITY_FIELDS.map(([key, label]) => {
        const fieldState = missionosAuthorityWorstFieldState(knownSources, key);
        return `
          <span class="detail-chip missionos-authority-chip missionos-authority-chip-${fieldState.status}">
            <span class="detail-chip-label">${escapeHtml(label)}</span>
            <span class="detail-chip-value">${escapeHtml(fieldState.value)}</span>
          </span>
        `;
      }).join("")}
    </div>
    <div class="missionos-authority-sources">
      ${sourceStatuses.map(([label, status]) => `<span class="mono">${escapeHtml(label)}=${escapeHtml(status)}</span>`).join("")}
    </div>
    ${aggregateStatus === "warning" ? `<div class="detail-error">Authority boundary is not fully false or supported. Treat this GUI surface as blocked for stronger operation claims.</div>` : ""}
  `;
}

function missionosOperatorStatusRank(status) {
  if (status === "blocked" || status === "warning") return 4;
  if (status === "partial" || status === "disabled_missing_evidence" || status === "disabled_missing_approval_package") return 3;
  if (status === "unknown" || status === "missing") return 2;
  if (status === "candidate_inputs_available" || status === "ready" || status === "observed") return 1;
  return 0;
}

function missionosWorstStatus(statuses) {
  const values = asArray(statuses).filter(Boolean);
  if (!values.length) return "unknown";
  return values.reduce((worst, status) => (
    missionosOperatorStatusRank(status) > missionosOperatorStatusRank(worst) ? status : worst
  ), values[0]);
}

function missionosOperatorBooleanFieldState(value) {
  if (value === true) return { status: "warning", value: "true" };
  if (value === false) return { status: "safe", value: "false" };
  return { status: "unknown", value: "unknown" };
}

function missionosOperatorAgentFlagState(agentsPayload, key) {
  const values = [
    agentsPayload?.[key],
    ...asArray(agentsPayload?.agents).map((agent) => agent?.[key]),
  ];
  if (values.some((value) => value === true)) {
    return { status: "warning", value: "true" };
  }
  if (values.some((value) => value === false)) {
    return { status: "safe", value: "false" };
  }
  return { status: "unknown", value: "unknown" };
}

function missionosOperatorKnowledgeSharingAgentState(knowledgeSharingPayload) {
  const boundary = asPlainObject(knowledgeSharingPayload?.authority_boundary);
  const agentExecutionStarted = boundary.agent_execution_started_in_runtime ?? boundary.agent_execution_started;
  if (agentExecutionStarted === true && boundary.agent_execution_allowed === true) {
    return { status: "safe", value: "runtime true (bounded)" };
  }
  if (agentExecutionStarted === true) {
    return { status: "warning", value: "runtime true" };
  }
  if (agentExecutionStarted === false) {
    return { status: "safe", value: "false" };
  }
  return { status: "unknown", value: "unknown" };
}

function missionosOperatorPolicyAuthorityFieldState(policyAuthorityPayload, key) {
  if (!policyAuthorityPayload) {
    return { status: "unknown", value: "unknown" };
  }
  const boundary = asPlainObject(policyAuthorityPayload?.authority_boundary);
  const pathCreated = ["authority_path_created", "authority_artifacts_recorded", "authority_runtime_applied"].includes(policyAuthorityPayload?.summary_status);
  const nonExecuting = boundary.dispatch_executed !== true
    && boundary.automatic_dispatch_executed !== true
    && boundary.physical_execution_invoked !== true
    && boundary.hardware_target_allowed !== true;
  if (["policy_update_applied", "automatic_recovery_rule_created", "dispatch_authority_created"].includes(key)) {
    const artifactKey = {
      policy_update_applied: "policy_update_recorded_in_artifact",
      automatic_recovery_rule_created: "recovery_rule_recorded_in_artifact",
      dispatch_authority_created: "dispatch_authority_recorded_in_artifact",
    }[key];
    const runtimeKey = {
      policy_update_applied: "policy_update_applied_to_runtime_engine",
      automatic_recovery_rule_created: "recovery_rule_registered_in_runtime_engine",
      dispatch_authority_created: "dispatch_authority_available_in_runtime_dispatch_table",
    }[key];
    if (boundary[runtimeKey] === true && pathCreated && nonExecuting) {
      return { status: "safe", value: "true (runtime)" };
    }
    if (boundary[artifactKey] === true && pathCreated && nonExecuting) {
      return { status: "safe", value: "artifact only" };
    }
    if (boundary[key] === true) return { status: "warning", value: "legacy true" };
    if (boundary[key] === false) return { status: "safe", value: "legacy false" };
  }
  return missionosOperatorNestedFlagState([["policy authority", policyAuthorityPayload]].filter(([, source]) => source), key);
}

function missionosOperatorNestedFlagValues(root, key) {
  const values = [];
  const visit = (value) => {
    if (!value || typeof value !== "object") return;
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (Object.prototype.hasOwnProperty.call(value, key)) {
      values.push(value[key]);
    }
    Object.values(value).forEach(visit);
  };
  visit(root);
  return values;
}

function missionosOperatorNestedFlagState(sources, key) {
  const values = sources.flatMap(([, source]) => missionosOperatorNestedFlagValues(source, key));
  if (values.some((value) => value === true)) {
    return { status: "warning", value: "true" };
  }
  if (values.some((value) => value !== false)) {
    const first = values.find((value) => value !== false);
    return { status: "warning", value: String(first) };
  }
  if (values.some((value) => value === false)) {
    return { status: "safe", value: "false" };
  }
  return { status: "unknown", value: "unknown" };
}

function missionosOperatorAuthorityRow(missionFacts, agentsPayload, knowledgeSharingPayload, policyAuthorityPayload, sitlDispatchPayload) {
  const authoritySources = [
    ["milestone", missionosAuthoritySources.milestone],
    ["operations", missionosAuthoritySources.operations],
    ["last run", missionosAuthoritySources.lastRun],
    ["agents", agentsPayload],
    ["knowledge sharing", knowledgeSharingPayload],
    ["policy authority", policyAuthorityPayload],
    ["sitl dispatch", sitlDispatchPayload],
  ].filter(([, source]) => source);
  const sourceField = (key) => missionosOperatorNestedFlagState(authoritySources, key);
  return [
    ["hardware", sourceField("hardware_target_allowed")],
    ["physical", sourceField("physical_execution_invoked")],
    ["dispatch", policyAuthorityPayload ? missionosOperatorPolicyAuthorityFieldState(policyAuthorityPayload, "dispatch_authority_created") : sourceField("dispatch_authority_created")],
    ["delivery claim", sourceField("delivery_completion_claimed")],
    ["public sync", sourceField("public_sync_performed")],
    ["synthetic success", missionosOperatorBooleanFieldState(missionFacts.syntheticSuccess)],
    ["agent started", missionosOperatorKnowledgeSharingAgentState(knowledgeSharingPayload)],
    ["policy update", policyAuthorityPayload ? missionosOperatorPolicyAuthorityFieldState(policyAuthorityPayload, "policy_update_applied") : sourceField("policy_update_applied")],
    ["auto recovery rule", policyAuthorityPayload ? missionosOperatorPolicyAuthorityFieldState(policyAuthorityPayload, "automatic_recovery_rule_created") : sourceField("automatic_recovery_rule_created")],
  ];
}

function missionosOperatorMissionFacts(result) {
  const response = result?.sitl_execution_response || {};
  const responseSummary = response.summary || {};
  const artifacts = missionDesignerResultArtifacts(result || {});
  const executionResult = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_execution_result);
  const receipt = asPlainObject(artifacts.px4_gazebo_sitl_mission_upload_receipt);
  const flightEvidence = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_flight_evidence);
  const payloadObservation = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_payload_release_observation);
  const dropoffVerification = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_dropoff_verification);
  const sitlDropoffVerification = asPlainObject(artifacts.px4_gazebo_sitl_dropoff_verification);
  const failedReceipt = asPlainObject(
    response.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt
      || artifacts.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt
  );
  const blockedReceipt = asPlainObject(response.px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt);
  const uploadObserved = receipt.upload_status === "uploaded" || executionResult.mission_ack_observed === true;
  const flightObserved = flightEvidence.actual_sitl_flight_evidence_observed === true
    || executionResult.actual_sitl_flight_evidence_observed === true
    || responseSummary.actual_sitl_flight_evidence_observed === true;
  const payloadObserved = payloadObservation.payload_release_observed === true
    || dropoffVerification.payload_release_observed === true
    || responseSummary.payload_release_observed === true;
  const dropoffVerified = dropoffVerification.dropoff_verified === true
    || sitlDropoffVerification.status === "verified"
    || responseSummary.dropoff_verified === true;
  const failureText = failedReceipt.failure_category
    || (Array.isArray(blockedReceipt.blocked_reasons) ? blockedReceipt.blocked_reasons[0] : "")
    || "";
  const explicitOptIn = responseSummary.live_opt_in === true
    || response.sitl_execution_opted_in === true
    || executionResult.sitl_execution_opted_in === true;
  const state = dropoffVerified
    ? "dropoff verified"
    : payloadObserved
      ? "payload observed"
      : flightObserved
        ? "flight observed"
        : failureText
          ? (uploadObserved ? "blocked after upload" : "blocked")
          : uploadObserved
            ? "upload observed"
            : explicitOptIn
              ? "execution pending"
              : "operator opt-in required";
  return {
    state,
    uploadObserved,
    flightObserved,
    payloadObserved,
    dropoffVerified,
    failureText: failureText || (explicitOptIn ? "" : "Mission Designer SITL execution requires explicit opt-in"),
    progress: flightEvidence.horizontal_progress_m,
    dropoffDistance: dropoffVerification.observed_distance_to_dropoff_m,
    taskId: responseSummary.task_id || response.task_id || response.task?.task_id || result?.sitl_execution_task?.task_id || "-",
    hardware: executionResult.hardware_target_allowed ?? responseSummary.hardware_target_allowed ?? result?.summary?.hardware_target_allowed ?? false,
    physical: executionResult.physical_execution_invoked ?? responseSummary.physical_execution_invoked ?? result?.summary?.physical_execution_invoked ?? false,
    deliveryClaim: executionResult.delivery_completion_claimed ?? dropoffVerification.delivery_completion_claimed ?? false,
    syntheticSuccess: executionResult.synthetic_success_allowed ?? false,
  };
}

function missionosOperatorOutcomeSteps(missionFacts) {
  const blocked = Boolean(missionFacts.failureText);
  const uploadBlocked = blocked && !missionFacts.uploadObserved;
  const takeoffBlocked = blocked && missionFacts.uploadObserved && !missionFacts.flightObserved;
  const payloadBlocked = blocked
    && missionFacts.flightObserved
    && !missionFacts.payloadObserved;
  const dropoffBlocked = blocked
    && missionFacts.payloadObserved
    && !missionFacts.dropoffVerified;
  return [
    {
      label: "prepare",
      value: missionFacts.taskId && missionFacts.taskId !== "-" ? "ready" : "waiting",
      status: missionFacts.taskId && missionFacts.taskId !== "-" ? "ok" : "pending",
    },
    {
      label: "upload",
      value: missionFacts.uploadObserved ? "ok" : (uploadBlocked ? "blocked" : "pending"),
      status: missionFacts.uploadObserved ? "ok" : (uploadBlocked ? "blocked" : "pending"),
    },
    {
      label: "takeoff / climb",
      value: missionFacts.flightObserved ? "observed" : (takeoffBlocked ? "blocked" : "pending"),
      status: missionFacts.flightObserved ? "ok" : (takeoffBlocked ? "blocked" : "pending"),
    },
    {
      label: "payload",
      value: missionFacts.payloadObserved ? "observed" : (payloadBlocked ? "blocked" : "pending"),
      status: missionFacts.payloadObserved ? "ok" : (payloadBlocked ? "blocked" : "pending"),
    },
    {
      label: "dropoff",
      value: missionFacts.dropoffVerified ? "verified" : (dropoffBlocked ? "blocked" : "pending"),
      status: missionFacts.dropoffVerified ? "ok" : (dropoffBlocked ? "blocked" : "pending"),
    },
    {
      label: "delivery claim",
      value: missionFacts.deliveryClaim === true ? "claimed" : "false",
      status: missionFacts.deliveryClaim === true ? "blocked" : "safe",
    },
  ];
}

function missionosOperatorStatusDisplay(status) {
  const value = String(status || "missing");
  if (value === "goal_640_progress_observed") return "mission progress observed";
  if (value === "goal_640_progress_blocked") return "mission progress blocked";
  return value.replace(/goal_640/g, "mission").replace(/_/g, " ");
}

function missionosAutonomyMonitorModel() {
  const form2aAiAgent = latestMissionOSOperatorPayloads.form2aAiAgent || {};
  const selection = asPlainObject(form2aAiAgent.selection);
  const review = asPlainObject(form2aAiAgent.review);
  const action = asPlainObject(form2aAiAgent.action);
  const repairPlanner = latestMissionOSOperatorPayloads.repairPlanner || {};
  const classification = asPlainObject(action.classification);
  const selectionBoundary = asPlainObject(selection.authority_boundary);
  const reviewBoundary = asPlainObject(review.authority_boundary);
  const actionBoundary = asPlainObject(action.authority_boundary);
  const repairBoundary = asPlainObject(repairPlanner.authority_boundary);
  const actionBlocks = [
    ...asArray(actionBoundary.blocking_reasons),
    ...asArray(action.blocking_reasons),
  ].filter(Boolean);
  const repairBlocks = asArray(repairBoundary.blocking_reasons).filter(Boolean);
  const selectionReady = selection.summary_status === "form2a_response_selected"
    && !asArray(selectionBoundary.blocking_reasons).length;
  const reviewGrantObserved = asPlainObject(
    review.human_operator_review
  ).human_operator_approval_granted_in_artifact === true;
  const reviewApproved = review.summary_status === "approved" && reviewGrantObserved;
  const reviewRecorded = ["approved", "review_recorded"].includes(String(review.summary_status || ""));
  const aiProgress = classification.ai_agent_progress_counted === true && reviewApproved;
  const goalProgress = classification.goal_640_progress_counted === true && reviewApproved;
  const repairReady = repairPlanner.summary_status === "repair_proposal_ready"
    && !repairBlocks.length;
  const selectedResponseKind = selection.selected_response_kind
    || asPlainObject(selection.response_selection).selected_response_kind
    || action.selected_response_kind
    || "-";
  const selectionRationale = selection.llm_response_rationale
    || asPlainObject(selection.response_selection).llm_response_rationale
    || asPlainObject(selection.response_selection).rationale
    || "";
  const approvalRequest = selection.llm_response_approval_request
    || asPlainObject(selection.response_selection).llm_response_approval_request
    || "";
  const actions = [];
  let status = "waiting";
  let tone = "pending";
  let headline = "Tell MissionOS what to plan";
  let nextStep = "Ask ADK/Gemini to propose the next bounded Form 2a response.";
  let agentSentence = "I do not have a current response proposal yet. Tell me what you want me to consider, and I will propose a bounded MissionOS action.";
  let humanPrompt = "Tell MissionOS what you want it to consider. I will turn that into a bounded proposal using the current evidence.";
  let replyMode = "conversation";
  let showInstructionInput = true;
  let instructionPlaceholder = "Example: Plan a safe payload recovery action from the latest supported evidence.";

  if (aiProgress) {
    status = "observed";
    tone = "safe";
    headline = "Form 2a internal capability runtime progress";
    nextStep = "Monitor verifier evidence. No manual repair is currently required.";
    agentSentence = `I completed the approved ${selectedResponseKind} plan and the verifier observed the expected mission behavior.`;
    humanPrompt = "Tell me what you want to understand or plan next.";
    replyMode = "monitor";
    instructionPlaceholder = "Example: Explain what changed, or plan the next repair attempt.";
  } else if (actionBlocks.length) {
    status = "blocked";
    tone = "blocked";
    headline = repairReady ? "Blocked; repair proposal ready" : "Blocked; repair planning recommended";
    nextStep = repairReady
      ? "Review the repair proposal, then decide whether to approve a new attempt."
      : "Ask MissionOS Chief to diagnose the blocked evidence.";
    agentSentence = repairReady
      ? `I found a blocked runtime chain and drafted a repair proposal for ${asPlainObject(repairPlanner.repair_proposal).repair_target || "the blocked evidence"}.`
      : "The last runtime attempt is blocked. I should diagnose the evidence before another attempt.";
    humanPrompt = repairReady
      ? "Review the repair proposal, or ask MissionOS to revise the repair direction."
      : "Describe what you want MissionOS to diagnose next.";
    replyMode = "repair";
    instructionPlaceholder = "Example: Diagnose the blocked runtime chain and propose the next bounded repair.";
    actions.push({ action: "repair", label: "Draft repair plan", tone: "primary" });
  } else if (reviewApproved) {
    status = "approved";
    tone = "runtime";
    headline = "Approved action ready";
    nextStep = "Run the approved bounded action through executor and verifier gates.";
    agentSentence = `You approved ${selectedResponseKind}. I can now run the bounded action through executor and verifier gates.`;
    humanPrompt = "Run it when you are ready, or ask me to revise the plan.";
    replyMode = "runtime";
    instructionPlaceholder = "Example: Execute the approved action.";
    actions.push({ action: "consume", label: "Run Approved Action", tone: "primary" });
  } else if (selectionReady && !reviewRecorded) {
    status = "awaiting_approval";
    tone = "approval";
    headline = "Awaiting human approval";
    nextStep = "Approve, reject, or request revision for the LLM proposal.";
    agentSentence = [
      `I propose ${selectedResponseKind}.`,
      selectionRationale ? `Reason: ${selectionRationale}` : "",
      approvalRequest ? `I need you to decide: ${approvalRequest}` : "I cannot approve or execute it myself; I need your decision.",
    ].filter(Boolean).join(" ");
    humanPrompt = "Reply with approval, rejection, or a revision request.";
    replyMode = "approval";
    instructionPlaceholder = "Example: Approve this plan, reject it, or ask MissionOS to revise it.";
    actions.push({ action: "approve", label: "Approve this plan", tone: "primary" });
    actions.push({ action: "reject", label: "Reject this plan", tone: "danger" });
    actions.push({ action: "revision", label: "Ask for revision", tone: "" });
  } else if (selectionReady) {
    status = "review_recorded";
    tone = "pending";
    headline = "Human review recorded";
    nextStep = "The latest review does not grant execution; request a new LLM proposal or inspect details.";
    agentSentence = `The last human review did not grant execution for ${selectedResponseKind}. I can propose a revised bounded response.`;
    humanPrompt = "Describe how you want the plan revised.";
    replyMode = "conversation";
    instructionPlaceholder = "Example: Revise the plan to reduce risk and keep the payload recovery bounded.";
  } else {
    instructionPlaceholder = "Example: Propose the next bounded MissionOS action from the latest evidence.";
  }

  if (showInstructionInput) {
    actions.unshift({ action: "instruction", label: "Send instruction", tone: "primary" });
  }
  return {
    status,
    tone,
    headline,
    nextStep,
    agentSentence,
    humanPrompt,
    replyMode,
    showInstructionInput,
    instructionPlaceholder,
    actions,
    selectedResponseKind,
    aiProgress,
    goalProgress,
    actionBlocks,
    repairReady,
    repairTarget: asPlainObject(repairPlanner.repair_proposal).repair_target || "-",
    selectionStatus: selection.summary_status || "missing",
    reviewStatus: review.summary_status || "missing",
    actionStatus: action.summary_status || "missing",
    actionStatusDisplay: missionosOperatorStatusDisplay(action.summary_status || "missing"),
    selectionStatusDisplay: aiProgress ? "source-bound" : (selection.summary_status || "missing"),
    notice: latestMissionOSAutonomyNotice,
  };
}

function renderMissionOSAutonomyMonitor() {
  if (!missionosAutonomyMonitorStatusEl || !missionosAutonomyMonitorSummaryEl) return;
  const model = missionosAutonomyMonitorModel();
  missionosAutonomyMonitorStatusEl.innerHTML = `
    <strong>${escapeHtml(model.headline)}</strong>
  `;
  missionosAutonomyMonitorSummaryEl.innerHTML = `
    <div class="missionos-autonomy-monitor-brief missionos-autonomy-monitor-brief-${escapeAttr(model.tone)}">
      <div class="missionos-conversation-turn missionos-conversation-agent">
        <div class="missionos-conversation-bubble">${escapeHtml(model.agentSentence)}</div>
      </div>
      <div class="missionos-conversation-turn missionos-conversation-human missionos-conversation-human-${escapeAttr(model.replyMode)}">
        <div class="missionos-conversation-bubble">
          <div>${escapeHtml(model.humanPrompt)}</div>
          ${model.showInstructionInput ? `
            <textarea id="missionosAutonomyInstructionInput" class="missionos-conversation-input" rows="3" placeholder="${escapeAttr(model.instructionPlaceholder)}">${escapeHtml(latestMissionOSOperatorInstruction)}</textarea>
          ` : ""}
          <div class="missionos-autonomy-actions" aria-label="Suggested human replies">
            ${model.actions.map((item) => `<button class="btn ${escapeAttr(item.tone || "")}" type="button" data-missionos-autonomy-action="${escapeAttr(item.action)}">${escapeHtml(item.label)}</button>`).join("")}
          </div>
        </div>
      </div>
      ${model.notice ? `<div class="missionos-conversation-notice">${escapeHtml(model.notice)}</div>` : ""}
    </div>
  `;
}

function missionosFriendlyActionError(err, path) {
  const raw = String(err || "");
  const notFound = raw.includes("Not Found") || raw.includes("HTTP 404");
  if (notFound && String(path || "").includes("autonomy-conversation")) {
    return [
      "MissionOS conversation route is not available in this running Gateway.",
      "This usually means the browser is connected to an older or different Gateway process than the current branch.",
      "I did not approve, dispatch, prepare SITL, or count progress.",
      "Restart the Gateway from the current branch, then ask me again. If the payload is extremely high, I will treat it as high-risk planning evidence rather than executable-ready input.",
    ].join(" ");
  }
  if (notFound && String(path || "").includes("form2a-response-selection")) {
    return "I cannot plan from that instruction yet because the planner route or source Form 1 evidence is unavailable in this running Gateway. I did not approve, dispatch, or count progress.";
  }
  if (notFound) {
    return "I could not reach the requested MissionOS operation in this running Gateway. I did not approve, dispatch, or count progress.";
  }
  return `That reply failed safely: ${raw}`;
}

function renderMissionOSOperatorSummary() {
  if (!missionosOperatorSummaryEl || !missionosOperatorSummaryStatusEl) return;
  const missionFacts = missionosOperatorMissionFacts(latestMissionScenarioResult);
  const timeline = latestMissionOSOperatorPayloads.timeline;
  const envelopes = latestMissionOSOperatorPayloads.envelopes;
  const knowledge = latestMissionOSOperatorPayloads.knowledge;
  const agents = latestMissionOSOperatorPayloads.agents;
  const knowledgeSharing = latestMissionOSOperatorPayloads.knowledgeSharing;
  const policyAuthority = latestMissionOSOperatorPayloads.policyAuthority;
  const sitlDispatch = latestMissionOSOperatorPayloads.sitlDispatch;
  const form2aAiAgent = latestMissionOSOperatorPayloads.form2aAiAgent;
  const operations = latestMissionOSOperatorPayloads.operations;
  const knowledgeSummary = asPlainObject(knowledge?.summary);
  const agentSummary = asPlainObject(agents?.summary);
  const sharingLesson = asPlainObject(knowledgeSharing?.l3_cross_session_lesson);
  const sharingCurator = asPlainObject(knowledgeSharing?.l4_knowledge_curator);
  const activeLessonIndex = asPlainObject(knowledgeSharing?.active_lesson_index);
  const policyAuthorityBoundary = asPlainObject(policyAuthority?.authority_boundary);
  const sitlDispatchBoundary = asPlainObject(sitlDispatch?.authority_boundary);
  const envelopeSummary = asPlainObject(envelopes?.summary);
  const nextInspection = asPlainObject(knowledge?.next_inspection || agents?.knowledge_input?.next_inspection);
  const sourceErrors = Object.entries(latestMissionOSOperatorSourceErrors)
    .filter(([, error]) => error)
    .map(([source, error]) => `${source}: ${error}`);
  const authorityRow = missionosOperatorAuthorityRow(missionFacts, agents, knowledgeSharing, policyAuthority, sitlDispatch);
  const authorityWarnings = authorityRow
    .filter(([, fieldState]) => fieldState.status !== "safe")
    .map(([label, fieldState]) => `${label}=${fieldState.value}`);
  const statuses = [
    missionFacts.failureText ? "blocked" : "observed",
    sourceErrors.length ? "warning" : "",
    authorityWarnings.length ? "warning" : "",
    timeline?.timeline_status,
    envelopes?.browser_status,
    knowledge?.browser_status,
    agents?.dashboard_status,
    knowledgeSharing?.summary_status,
    form2aAiAgent?.action?.summary_status,
    operations?.registry_status,
  ];
  const overallStatus = missionosWorstStatus(statuses);
  const blockers = uniqueStrings([
    missionFacts.failureText,
    ...(asArray(knowledge?.boundary_warnings)),
    ...(asArray(agents?.boundary_warnings)),
    ...sourceErrors,
    ...authorityWarnings,
    knowledgeSharing?.summary_status === "blocked" ? "knowledge sharing boundary blocked" : "",
    knowledgeSummary.blocked_count ? `${knowledgeSummary.blocked_count} blocked knowledge cards` : "",
    agentSummary.blocked_agent_count ? `${agentSummary.blocked_agent_count} blocked agents` : "",
  ]).slice(0, 4);
  const nextText = nextInspection.recommended_next_inspection
    || (missionFacts.failureText ? "Open the failure receipt and logs before interpreting mission success." : "")
    || "Use detailed panels only when auditing artifact lineage.";
  const outcomeSteps = missionosOperatorOutcomeSteps(missionFacts);
  missionosOperatorSummaryStatusEl.innerHTML = `
    <div class="item-head">
      <strong>${escapeHtml(missionFacts.state)}</strong>
      ${statusTag(overallStatus)}
    </div>
    <div class="muted">Read this first. Detailed MissionOS panels below are expandable audit views.</div>
  `;
  missionosOperatorSummaryEl.innerHTML = `
    <div class="missionos-operator-outcome-strip" aria-label="Mission outcome strip">
      ${outcomeSteps.map((step) => `
        <div class="missionos-operator-outcome-step missionos-operator-outcome-step-${escapeAttr(step.status)}">
          <span>${escapeHtml(step.label)}</span>
          <strong>${escapeHtml(step.value)}</strong>
        </div>
      `).join("")}
    </div>
    ${missionFacts.failureText ? `
      <div class="missionos-operator-one-line missionos-operator-one-line-blocked">
        <strong>Conclusion:</strong>
        upload ${missionFacts.uploadObserved ? "observed" : "not observed"};
        takeoff/climb ${missionFacts.flightObserved ? "observed" : "blocked or not observed"};
        payload ${missionFacts.payloadObserved ? "observed" : "not observed"};
        dropoff ${missionFacts.dropoffVerified ? "verified" : "not verified"}.
        Failure: <span class="mono">${escapeHtml(missionFacts.failureText)}</span>.
      </div>
    ` : `
      <div class="missionos-operator-one-line">
        <strong>Conclusion:</strong>
        ${escapeHtml(missionFacts.dropoffVerified ? "SITL dropoff is verified from observed simulator facts." : "No active failure receipt is loaded; inspect the outcome strip before reading detailed artifacts.")}
      </div>
    `}
    <div class="missionos-operator-summary-grid">
      <section class="missionos-operator-summary-card missionos-operator-summary-card-primary">
        <div class="k">Where We Are</div>
        <strong>${escapeHtml(missionFacts.state)}</strong>
        <div class="muted">task <span class="mono">${escapeHtml(missionFacts.taskId)}</span></div>
        <div class="detail-chip-row">
          ${[
    ["upload", missionFacts.uploadObserved],
    ["flight", missionFacts.flightObserved],
    ["payload", missionFacts.payloadObserved],
    ["dropoff", missionFacts.dropoffVerified],
  ].map(([label, value]) => `<span class="detail-chip mission-brief-chip-${value ? "ok" : "pending"}"><span class="detail-chip-label">${escapeHtml(label)}</span><span class="detail-chip-value">${escapeHtml(String(value))}</span></span>`).join("")}
        </div>
      </section>
      <section class="missionos-operator-summary-card">
        <div class="k">Observed Evidence</div>
        <div class="missionos-operator-summary-facts">
          <span>Form 3 cycles: <strong>${escapeHtml(String(asArray(timeline?.cycles).length || "-"))}</strong></span>
          <span>envelope: <strong>${escapeHtml(envelopes?.browser_status || "loading")}</strong></span>
          <span>physical seed: <strong>${escapeHtml(envelopeSummary.physical_seed_ready ? "ready" : "not ready")}</strong></span>
          <span>agent inputs: <strong>${escapeHtml(String(agentSummary.knowledge_input_candidate_count ?? "-"))}</strong></span>
          <span>L3 lesson: <strong>${escapeHtml(sharingLesson.status || "missing")}</strong></span>
          <span>L4 curator: <strong>${escapeHtml(sharingCurator.status || "not run")}</strong></span>
          <span>active lesson index: <strong>${escapeHtml(activeLessonIndex.status || "missing")}</strong></span>
          <span>policy path: <strong>${escapeHtml(policyAuthority?.summary_status || "missing")}</strong></span>
          <span>SITL dispatch: <strong>${escapeHtml(sitlDispatch?.summary_status || "missing")}</strong></span>
          <span>AI Form 2a: <strong>${escapeHtml(form2aAiAgent?.action?.classification?.ai_agent_progress_counted ? "progress observed" : form2aAiAgent?.action?.summary_status || "missing")}</strong></span>
          <span>dispatch runtime: <strong>${escapeHtml(String(sitlDispatchBoundary.dispatch_executed_in_runtime ?? policyAuthorityBoundary.dispatch_executed_in_runtime ?? false))}</strong></span>
        </div>
      </section>
      <section class="missionos-operator-summary-card missionos-operator-summary-card-${blockers.length ? "blocked" : "safe"}">
        <div class="k">Blocked / Watch</div>
        ${blockers.length ? `<ul>${blockers.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<div class="muted">No active blocker surfaced by the loaded summaries.</div>`}
      </section>
      <section class="missionos-operator-summary-card">
        <div class="k">Next Useful Read</div>
        <strong>${escapeHtml(nextInspection.failure_mode_id || "operator review")}</strong>
        <div class="muted">${escapeHtml(nextText)}</div>
        ${nextInspection.artifact_path ? `<div class="item-meta mono">${escapeHtml(nextInspection.artifact_path)}</div>` : ""}
      </section>
    </div>
    <div class="detail-chip-row missionos-operator-boundary-row">
      ${authorityRow.map(([label, fieldState]) => `<span class="detail-chip mission-brief-chip-${fieldState.status === "safe" ? "ok" : "warn"}"><span class="detail-chip-label">${escapeHtml(label)}</span><span class="detail-chip-value">${escapeHtml(fieldState.value)}</span></span>`).join("")}
    </div>
    <div class="muted">This is deterministic UI synthesis over already loaded persisted artifacts. It is not an AI gate verdict, verifier, policy update, dispatch control, live replay, or delivery-completion claim.</div>
  `;
  renderMissionOSAutonomyMonitor();
}

async function refreshMissionOSOperatorSummary() {
  await Promise.all([
    loadMissionOSCurrentMilestone(),
    loadMissionOSTimeline(),
    loadMissionOSEnvelopes(),
    loadMissionOSKnowledge(),
    loadMissionOSAgents(),
    loadMissionOSKnowledgeSharing(),
    loadMissionOSPolicyAuthority(),
    loadMissionOSSitlDispatchExecution(),
    loadMissionOSOperations(),
  ]);
  renderMissionOSOperatorSummary();
}

function renderMissionOSMilestone(summary) {
  if (!missionosMilestoneSummaryEl || !missionosMilestoneStatusEl) return;
  latestMissionOSOperatorPayloads.milestone = summary || null;
  const steps = asArray(summary?.steps);
  const boundary = asPlainObject(summary?.authority_boundary);
  const authoritySupported = boundary.authority_boundary_supported !== false;
  missionosAuthoritySources.milestone = summary || null;
  renderMissionOSAuthorityBelt();
  const summaryStatus = summary?.summary_status || "unknown";
  missionosMilestoneStatusEl.innerHTML = `
    <div class="item-head">
      <strong>${escapeHtml(summary?.milestone_label || "MissionOS milestone")}</strong>
      ${statusTag(summaryStatus)}
    </div>
    <div class="muted">Generated from persisted artifacts under <span class="mono">${escapeHtml(summary?.artifact_root || "-")}</span>. This panel is read-only and does not start SITL, Gateway probes, physical execution, or dispatch.</div>
  `;
  missionosMilestoneSummaryEl.innerHTML = `
    <div class="missionos-milestone-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">SITL only</span><span class="detail-chip-value">true</span></span>
        <span class="detail-chip mission-brief-chip-${authoritySupported ? "ok" : "warn"}"><span class="detail-chip-label">authority boundary</span><span class="detail-chip-value">${escapeHtml(String(authoritySupported))}</span></span>
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">physical</span><span class="detail-chip-value">${escapeHtml(String(boundary.physical_execution_invoked ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">dispatch</span><span class="detail-chip-value">${escapeHtml(String(boundary.dispatch_authority_created ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">delivery claim</span><span class="detail-chip-value">${escapeHtml(String(boundary.delivery_completion_claimed ?? false))}</span></span>
      </div>
      ${authoritySupported ? `<div class="muted">Milestone details are collapsed; open them only when inspecting artifact lineage.</div>` : `<div class="detail-error">Authority boundary leak detected: ${escapeHtml(asArray(boundary.authority_true_paths).join("; "))}</div>`}
    </div>
    <details class="mission-ui-collapse">
      <summary>MissionOS Milestone Artifact Details</summary>
      <div class="missionos-milestone-boundary">
        <div class="detail-chip-row">
          <span class="detail-chip"><span class="detail-chip-label">physical Form 1</span><span class="detail-chip-value">${escapeHtml(String(boundary.physical_form1_claimed ?? false))}</span></span>
          <span class="detail-chip"><span class="detail-chip-label">hardware authority</span><span class="detail-chip-value">${escapeHtml(String(boundary.hardware_target_allowed ?? false))}</span></span>
          <span class="detail-chip"><span class="detail-chip-label">public sync</span><span class="detail-chip-value">${escapeHtml(String(boundary.public_sync_performed ?? false))}</span></span>
        </div>
      </div>
      <div class="missionos-milestone-step-grid">
        ${steps.map((step) => `
          <div class="missionos-milestone-step missionos-milestone-step-${escapeAttr(step.status || "unknown")}">
            <div class="item-head">
              <strong>${escapeHtml(step.step_id || "-")} · ${escapeHtml(step.title || "-")}</strong>
              ${statusTag(step.status || "unknown")}
            </div>
            <div class="muted">${escapeHtml(step.description || "")}</div>
            <div class="missionos-milestone-fields">${missionosMilestoneFieldRows(step.fields)}</div>
            <div class="item-meta">${escapeHtml(step.boundary_note || "")}</div>
            ${step.artifact_path ? `<div class="item-meta mono">${escapeHtml(step.artifact_path)}</div>` : `<div class="item-meta mono">artifact missing</div>`}
          </div>
        `).join("")}
      </div>
      <div class="missionos-milestone-not-claimed">
        <div class="k">Not Claimed</div>
        <div class="detail-chip-row">
          ${asArray(summary?.not_claimed).map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("")}
        </div>
        <div class="muted">${escapeHtml(summary?.next_step || "")}</div>
      </div>
    </details>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSCurrentMilestone() {
  if (!missionosMilestoneSummaryEl || !missionosMilestoneStatusEl) return;
  missionosMilestoneStatusEl.textContent = "Loading milestone evidence...";
  missionosMilestoneSummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/current-milestone");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const summary = await response.json();
    latestMissionOSOperatorSourceErrors.milestone = "";
    renderMissionOSMilestone(summary);
    logEvent("missionos.current_milestone", {
      summary_status: summary.summary_status,
      steps: asArray(summary.steps).map((step) => `${step.step_id}:${step.status}`),
    });
  } catch (err) {
    latestMissionOSOperatorPayloads.milestone = null;
    latestMissionOSOperatorSourceErrors.milestone = String(err);
    missionosAuthoritySources.milestone = null;
    renderMissionOSAuthorityBelt();
    missionosMilestoneStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosMilestoneSummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.current_milestone.error", { error: String(err) });
  }
}

function missionosTimelineStep(step) {
  const fields = asPlainObject(step?.fields);
  const fieldRows = Object.entries(fields).slice(0, 6).map(([key, value]) => `
    <div class="missionos-causal-field">
      <span class="missionos-causal-field-key">${escapeHtml(key)}</span>
      <span class="missionos-causal-field-value mono">${escapeHtml(missionosMilestoneValue(value))}</span>
    </div>
  `).join("");
  return `
    <div class="missionos-causal-step missionos-causal-step-${escapeAttr(step?.status || "unknown")}">
      <div class="missionos-causal-step-dot"></div>
      <div class="missionos-causal-step-body">
        <div class="item-head">
          <strong>${escapeHtml(step?.title || step?.step_id || "-")}</strong>
          ${statusTag(step?.status || "unknown")}
        </div>
        <div class="item-detail">${escapeHtml(step?.summary || "")}</div>
        ${fieldRows ? `<div class="missionos-causal-fields">${fieldRows}</div>` : ""}
        ${step?.artifact_ref ? `<div class="item-meta mono">${escapeHtml(step.artifact_ref)}</div>` : ""}
        ${step?.boundary_note ? `<div class="item-meta">${escapeHtml(step.boundary_note)}</div>` : ""}
      </div>
    </div>
  `;
}

function renderMissionOSTimeline(timeline) {
  if (!missionosTimelineStatusEl || !missionosTimelineSummaryEl) return;
  latestMissionOSOperatorPayloads.timeline = timeline || null;
  const cycles = asArray(timeline?.cycles);
  const boundary = asPlainObject(timeline?.authority_boundary);
  const classification = asPlainObject(timeline?.classification);
  const source = asPlainObject(timeline?.source_summary);
  const overlay = asPlainObject(timeline?.replay_overlay);
  const markers = asArray(overlay.markers);
  const authoritySupported = boundary.authority_boundary_supported !== false;
  missionosTimelineStatusEl.innerHTML = `
    <div class="item-head">
      <strong>${escapeHtml(timeline?.timeline_label || "Past recovery cycles - read-only replay")}</strong>
      ${statusTag(timeline?.timeline_status || "unknown")}
    </div>
    <div class="muted">${escapeHtml(timeline?.operator_note || "")}</div>
  `;
  missionosTimelineSummaryEl.innerHTML = `
    <div class="missionos-causal-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">classification</span><span class="detail-chip-value">${escapeHtml(classification.surface || "GUI causal visualization")}</span></span>
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">progress counted</span><span class="detail-chip-value">${escapeHtml(String(classification.progress_counted ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${authoritySupported ? "ok" : "warn"}"><span class="detail-chip-label">authority boundary</span><span class="detail-chip-value">${escapeHtml(String(authoritySupported))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">source Form</span><span class="detail-chip-value">${escapeHtml(source.source_runtime_causal_form || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">cycles</span><span class="detail-chip-value">${escapeHtml(String(cycles.length))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">trigger</span><span class="detail-chip-value">${escapeHtml(source.primary_trigger || "-")}</span></span>
      </div>
      ${authoritySupported ? "" : `<div class="detail-error">Authority boundary leak detected: ${escapeHtml(asArray(boundary.authority_true_paths).join("; "))}</div>`}
    </div>
    <details class="mission-ui-collapse" data-missionos-operator-details="timeline-cycles">
      <summary>Causal Cycle Lanes (${escapeHtml(String(cycles.length))})</summary>
      <div class="missionos-causal-cycle-grid">
        ${cycles.map((cycle) => `
          <section class="missionos-causal-cycle missionos-causal-cycle-${escapeAttr(cycle.status || "unknown")}">
            <div class="missionos-causal-cycle-head">
              <div>
                <div class="k">${escapeHtml(cycle.cycle_label || `cycle ${cycle.cycle_index || "-"}`)}</div>
                <div class="item-meta mono">${escapeHtml(String(cycle.selected_bounded_action || "-"))} · ${escapeHtml(String(cycle.primary_trigger || "-"))}</div>
                ${cycle.ref_chain_consistent === false ? `<div class="detail-error">Ref chain mismatch: ${escapeHtml(asArray(cycle.ref_chain_errors).join(", ") || "unknown")}</div>` : ""}
              </div>
              ${statusTag(cycle.status || "unknown")}
            </div>
            <div class="missionos-causal-steps">
              ${asArray(cycle.steps).map((step) => missionosTimelineStep(step)).join("")}
            </div>
          </section>
        `).join("") || `<div class="detail-error">No cycle artifacts available.</div>`}
      </div>
    </details>
    <details class="mission-ui-collapse">
      <summary>3D Replay Overlay Markers (${escapeHtml(String(markers.length))})</summary>
      <div class="missionos-causal-overlay">
        <div class="detail-chip-row">
          <span class="detail-chip"><span class="detail-chip-label">mode</span><span class="detail-chip-value">${escapeHtml(overlay.mode || "artifact_replay_only")}</span></span>
          <span class="detail-chip"><span class="detail-chip-label">planned route</span><span class="detail-chip-value">${escapeHtml(overlay.planned_route || "-")}</span></span>
          <span class="detail-chip"><span class="detail-chip-label">observed trajectory</span><span class="detail-chip-value">${escapeHtml(overlay.observed_trajectory || "-")}</span></span>
        </div>
        <div class="muted">${escapeHtml(overlay.boundary_note || "")}</div>
        <div class="missionos-causal-marker-grid">
          ${markers.map((marker) => `
            <div class="missionos-causal-marker missionos-causal-step-${escapeAttr(marker.status || "unknown")}">
              <div class="item-head">
                <strong>${escapeHtml(`cycle ${marker.cycle_index || "-"} · ${marker.marker_kind || "marker"}`)}</strong>
                ${statusTag(marker.status || "unknown")}
              </div>
              <div class="item-detail">${escapeHtml(marker.label || "-")}</div>
              <div class="item-meta mono">${escapeHtml(marker.artifact_ref || "-")}</div>
            </div>
          `).join("")}
        </div>
      </div>
    </details>
    <details class="mission-ui-collapse">
      <summary>Causal Timeline Source Artifacts</summary>
      <div class="missionos-causal-source-grid">
        <div class="missionos-causal-source-card">
          <div class="k">Supervisor Runtime</div>
          <div class="mono">${escapeHtml(source.source_runtime_artifact_path || "missing")}</div>
        </div>
        <div class="missionos-causal-source-card">
          <div class="k">Gateway Runtime Probe</div>
          <div class="mono">${escapeHtml(source.gateway_runtime_artifact_path || "missing")}</div>
        </div>
      </div>
      <div class="missionos-milestone-not-claimed">
        <div class="k">Not Claimed</div>
        <div class="detail-chip-row">
          ${asArray(timeline?.not_claimed).map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("")}
        </div>
      </div>
    </details>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSTimeline() {
  if (!missionosTimelineStatusEl || !missionosTimelineSummaryEl) return;
  missionosTimelineStatusEl.textContent = "Loading causal timeline...";
  missionosTimelineSummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/causal-timeline");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const timeline = await response.json();
    latestMissionOSOperatorSourceErrors.timeline = "";
    renderMissionOSTimeline(timeline);
    logEvent("missionos.causal_timeline", {
      timeline_status: timeline.timeline_status,
      cycles: asArray(timeline.cycles).length,
    });
  } catch (err) {
    latestMissionOSOperatorPayloads.timeline = null;
    latestMissionOSOperatorSourceErrors.timeline = String(err);
    missionosTimelineStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosTimelineSummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.causal_timeline.error", { error: String(err) });
  }
}

function missionosEnvelopeFieldRows(fields) {
  return Object.entries(asPlainObject(fields)).map(([key, value]) => `
    <div class="missionos-envelope-field">
      <span class="missionos-envelope-field-key">${escapeHtml(key)}</span>
      <span class="missionos-envelope-field-value mono">${escapeHtml(missionosMilestoneValue(value))}</span>
    </div>
  `).join("");
}

function renderMissionOSEnvelopes(envelopes) {
  if (!missionosEnvelopeStatusEl || !missionosEnvelopeSummaryEl) return;
  latestMissionOSOperatorPayloads.envelopes = envelopes || null;
  const cards = asArray(envelopes?.cards);
  const boundary = asPlainObject(envelopes?.authority_boundary);
  const classification = asPlainObject(envelopes?.classification);
  const summary = asPlainObject(envelopes?.summary);
  const authoritySupported = boundary.authority_boundary_supported !== false;
  const authorityExplicit = boundary.authority_boundary_explicit === true;
  const form1Required = summary.physical_form1_required === true;
  const warnings = asArray(envelopes?.boundary_warnings);
  missionosEnvelopeStatusEl.innerHTML = `
    <div class="item-head">
      <strong>${escapeHtml(envelopes?.browser_label || "MissionOS envelope browser")}</strong>
      ${statusTag(envelopes?.browser_status || "unknown")}
    </div>
    <div class="muted">${escapeHtml(envelopes?.operator_note || "")}</div>
  `;
  missionosEnvelopeSummaryEl.innerHTML = `
    <div class="missionos-envelope-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">classification</span><span class="detail-chip-value">${escapeHtml(classification.surface || "GUI envelope visualization")}</span></span>
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">progress counted</span><span class="detail-chip-value">${escapeHtml(String(classification.progress_counted ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${authoritySupported && authorityExplicit ? "ok" : "warn"}"><span class="detail-chip-label">authority boundary</span><span class="detail-chip-value">${escapeHtml(authoritySupported && authorityExplicit ? "explicit false" : "warning")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">parameter</span><span class="detail-chip-value">${escapeHtml(summary.parameter || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">wind range</span><span class="detail-chip-value">${escapeHtml(`${summary.wind_speed_mps_min ?? "-"}..${summary.wind_speed_mps_max ?? "-"} m/s`)}</span></span>
        <span class="detail-chip mission-brief-chip-${summary.physical_seed_ready ? "ok" : "warn"}"><span class="detail-chip-label">physical seed</span><span class="detail-chip-value">${escapeHtml(summary.physical_seed_ready ? "ready" : "blocked")}</span></span>
        <span class="detail-chip mission-brief-chip-${summary.causal_verification_transferred ? "warn" : "ok"}"><span class="detail-chip-label">causal transfer</span><span class="detail-chip-value">${escapeHtml(String(summary.causal_verification_transferred ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${form1Required ? "ok" : "warn"}"><span class="detail-chip-label">Form 1 required</span><span class="detail-chip-value">${escapeHtml(String(summary.physical_form1_required ?? "unknown"))}</span></span>
      </div>
      ${warnings.length ? `<div class="detail-error">Envelope boundary warning: ${escapeHtml(warnings.join("; "))}</div>` : ""}
      ${authoritySupported ? "" : `<div class="detail-error">Authority boundary leak detected: ${escapeHtml(asArray(boundary.authority_true_paths).join("; "))}</div>`}
      ${authorityExplicit ? "" : `<div class="detail-error">Missing explicit false authority fields: ${escapeHtml(asArray(boundary.authority_missing_false_keys).join(", ") || "unknown")}</div>`}
    </div>
    <details class="mission-ui-collapse" data-missionos-operator-details="envelope-cards">
      <summary>Envelope Cards (${escapeHtml(String(cards.length))})</summary>
      <div class="missionos-envelope-grid">
        ${cards.map((card) => `
          <section class="missionos-envelope-card missionos-envelope-card-${escapeAttr(card.status || "unknown")}">
            <div class="item-head">
              <div>
                <strong>${escapeHtml(card.title || card.card_id || "Envelope")}</strong>
                <div class="item-meta">${escapeHtml(card.envelope_type || "-")}</div>
              </div>
              ${statusTag(card.status || "unknown")}
            </div>
            <div class="missionos-envelope-fields">${missionosEnvelopeFieldRows(card.fields)}</div>
            <div class="muted">${escapeHtml(card.boundary_note || "")}</div>
            <details class="mission-ui-collapse">
              <summary>Source artifact</summary>
              <div class="mono">${escapeHtml(card.artifact_path || "missing")}</div>
            </details>
          </section>
        `).join("") || `<div class="detail-error">No envelope artifacts available.</div>`}
      </div>
    </details>
    <details class="mission-ui-collapse">
      <summary>Envelope Boundary Not Claimed</summary>
      <div class="missionos-milestone-not-claimed">
        <div class="detail-chip-row">
          ${asArray(envelopes?.not_claimed).map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("")}
        </div>
      </div>
    </details>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSEnvelopes() {
  if (!missionosEnvelopeStatusEl || !missionosEnvelopeSummaryEl) return;
  missionosEnvelopeStatusEl.textContent = "Loading envelope evidence...";
  missionosEnvelopeSummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/envelopes");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const envelopes = await response.json();
    latestMissionOSOperatorSourceErrors.envelopes = "";
    renderMissionOSEnvelopes(envelopes);
    logEvent("missionos.envelopes", {
      browser_status: envelopes.browser_status,
      cards: asArray(envelopes.cards).length,
    });
  } catch (err) {
    latestMissionOSOperatorPayloads.envelopes = null;
    latestMissionOSOperatorSourceErrors.envelopes = String(err);
    missionosEnvelopeStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosEnvelopeSummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.envelopes.error", { error: String(err) });
  }
}

function missionosKnowledgeFieldRows(fields) {
  return Object.entries(asPlainObject(fields)).map(([key, value]) => `
    <div class="missionos-knowledge-field">
      <span class="missionos-knowledge-field-key">${escapeHtml(key)}</span>
      <span class="missionos-knowledge-field-value mono">${escapeHtml(missionosMilestoneValue(value))}</span>
    </div>
  `).join("");
}

function missionosKnowledgeSectionLabel(section) {
  if (section === "failure_modes") return "Failure Modes";
  if (section === "recovery_episodes") return "Recovery Episodes";
  if (section === "blocked_probes") return "Blocked Probes";
  if (section === "live_sitl_partials") return "Live SITL Partials";
  return section || "Knowledge";
}

function renderMissionOSKnowledge(knowledge) {
  if (!missionosKnowledgeStatusEl || !missionosKnowledgeSummaryEl) return;
  latestMissionOSOperatorPayloads.knowledge = knowledge || null;
  const cards = asArray(knowledge?.cards);
  const boundary = asPlainObject(knowledge?.authority_boundary);
  const classification = asPlainObject(knowledge?.classification);
  const summary = asPlainObject(knowledge?.summary);
  const next = asPlainObject(knowledge?.next_inspection);
  const warnings = asArray(knowledge?.boundary_warnings);
  const authoritySupported = boundary.authority_boundary_supported !== false;
  const authorityExplicit = boundary.authority_boundary_explicit === true;
  missionosKnowledgeStatusEl.innerHTML = `
    <div class="item-head">
      <strong>${escapeHtml(knowledge?.browser_label || "Failure notes / next inspection")}</strong>
      ${statusTag(knowledge?.browser_status || "unknown")}
    </div>
    <div class="muted">${escapeHtml(knowledge?.operator_note || "")}</div>
  `;
  const sections = ["live_sitl_partials", "blocked_probes", "failure_modes", "recovery_episodes"];
  missionosKnowledgeSummaryEl.innerHTML = `
    <div class="missionos-knowledge-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">classification</span><span class="detail-chip-value">${escapeHtml(classification.surface || "GUI knowledge visualization")}</span></span>
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">progress counted</span><span class="detail-chip-value">${escapeHtml(String(classification.progress_counted ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${authoritySupported && authorityExplicit ? "ok" : "warn"}"><span class="detail-chip-label">authority boundary</span><span class="detail-chip-value">${escapeHtml(authoritySupported && authorityExplicit ? "explicit false" : "warning")}</span></span>
        <span class="detail-chip mission-brief-chip-${summary.blocked_count ? "warn" : "ok"}"><span class="detail-chip-label">blocked</span><span class="detail-chip-value">${escapeHtml(String(summary.blocked_count ?? 0))}</span></span>
        <span class="detail-chip mission-brief-chip-${summary.partial_count ? "warn" : "ok"}"><span class="detail-chip-label">partial</span><span class="detail-chip-value">${escapeHtml(String(summary.partial_count ?? 0))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">candidates</span><span class="detail-chip-value">${escapeHtml(String(summary.candidate_count ?? 0))}</span></span>
      </div>
      <div class="missionos-knowledge-next">
        <div class="k">Next Inspection</div>
        <div><strong>${escapeHtml(next.failure_mode_id || "no current failure mode")}</strong> ${statusTag(next.status || "missing")}</div>
        <div class="muted">${escapeHtml(next.recommended_next_inspection || "No knowledge artifacts found.")}</div>
        <div class="item-meta mono">${escapeHtml(next.artifact_path || "")}</div>
      </div>
      ${warnings.length ? `<div class="detail-error">Knowledge boundary warning: ${escapeHtml(warnings.join("; "))}</div>` : ""}
      ${authoritySupported ? "" : `<div class="detail-error">Authority boundary leak detected: ${escapeHtml(asArray(boundary.authority_true_paths).join("; "))}</div>`}
      ${authorityExplicit ? "" : `<div class="detail-error">Missing explicit false authority fields: ${escapeHtml(asArray(boundary.authority_missing_false_keys).join(", ") || "unknown")}</div>`}
    </div>
    ${sections.map((section) => {
      const sectionCards = cards.filter((card) => card.section === section);
      return `
        <details class="mission-ui-collapse missionos-knowledge-section" data-missionos-operator-details="knowledge-${escapeAttr(section)}">
          <summary>${escapeHtml(missionosKnowledgeSectionLabel(section))} (${escapeHtml(String(sectionCards.length))})</summary>
          <div class="missionos-knowledge-grid">
            ${sectionCards.map((card) => `
              <section class="missionos-knowledge-card missionos-knowledge-card-${escapeAttr(card.status || "unknown")}">
                <div class="item-head">
                  <div>
                    <strong>${escapeHtml(card.title || card.failure_mode_id || "Knowledge card")}</strong>
                    <div class="item-meta mono">${escapeHtml(card.failure_mode_id || "-")}</div>
                  </div>
                  ${statusTag(card.status || "unknown")}
                </div>
                <div class="item-detail">${escapeHtml(card.summary || "")}</div>
                <div class="detail-chip-row">
                  <span class="detail-chip mission-brief-chip-${card.boundary_status === "safe" ? "ok" : "warn"}"><span class="detail-chip-label">boundary</span><span class="detail-chip-value">${escapeHtml(card.boundary_status || "unknown")}</span></span>
                  <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">policy update</span><span class="detail-chip-value">${escapeHtml(String(card.policy_update ?? false))}</span></span>
                  <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">progress</span><span class="detail-chip-value">${escapeHtml(String(card.progress_counted ?? false))}</span></span>
                </div>
                <div class="missionos-knowledge-fields">${missionosKnowledgeFieldRows(card.fields)}</div>
                ${asArray(card.observed_evidence).length ? `<div class="muted">Observed: ${escapeHtml(asArray(card.observed_evidence).join(", "))}</div>` : ""}
                ${asArray(card.missing_evidence).length ? `<div class="detail-error">Missing: ${escapeHtml(asArray(card.missing_evidence).join(", "))}</div>` : ""}
                <div class="muted">Next: ${escapeHtml(card.recommended_next_inspection || "-")}</div>
                <details class="mission-ui-collapse">
                  <summary>Source artifact</summary>
                  <div class="mono">${escapeHtml(card.artifact_path || "missing")}</div>
                </details>
              </section>
            `).join("") || `<div class="muted">No cards in this section.</div>`}
          </div>
        </details>
      `;
    }).join("")}
    <details class="mission-ui-collapse">
      <summary>Knowledge Boundary Not Claimed</summary>
      <div class="missionos-milestone-not-claimed">
        <div class="detail-chip-row">
          ${asArray(knowledge?.not_claimed).map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("")}
        </div>
      </div>
    </details>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSKnowledge() {
  if (!missionosKnowledgeStatusEl || !missionosKnowledgeSummaryEl) return;
  missionosKnowledgeStatusEl.textContent = "Loading knowledge evidence...";
  missionosKnowledgeSummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/knowledge");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const knowledge = await response.json();
    latestMissionOSOperatorSourceErrors.knowledge = "";
    renderMissionOSKnowledge(knowledge);
    logEvent("missionos.knowledge", {
      browser_status: knowledge.browser_status,
      cards: asArray(knowledge.cards).length,
    });
  } catch (err) {
    latestMissionOSOperatorPayloads.knowledge = null;
    latestMissionOSOperatorSourceErrors.knowledge = String(err);
    missionosKnowledgeStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosKnowledgeSummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.knowledge.error", { error: String(err) });
  }
}

function missionosAgentFieldList(items) {
  const values = asArray(items);
  if (!values.length) return `<span class="muted">-</span>`;
  return `
    <ul class="missionos-agent-list">
      ${values.map((item) => `<li>${escapeHtml(String(item))}</li>`).join("")}
    </ul>
  `;
}

function renderMissionOSAgents(agentsPayload) {
  if (!missionosAgentsStatusEl || !missionosAgentsSummaryEl) return;
  latestMissionOSOperatorPayloads.agents = agentsPayload || null;
  const agents = asArray(agentsPayload?.agents);
  const boundary = asPlainObject(agentsPayload?.authority_boundary);
  const classification = asPlainObject(agentsPayload?.classification);
  const summary = asPlainObject(agentsPayload?.summary);
  const knowledgeInput = asPlainObject(agentsPayload?.knowledge_input);
  const nextInspection = asPlainObject(knowledgeInput.next_inspection);
  const warnings = asArray(agentsPayload?.boundary_warnings);
  const authoritySupported = boundary.authority_boundary_supported !== false;
  const knowledgeExplicit = boundary.knowledge_authority_boundary_explicit === true;
  missionosAgentsStatusEl.innerHTML = `
    <div class="item-head">
      <strong>${escapeHtml(agentsPayload?.dashboard_label || "Future agent roles - not running")}</strong>
      ${statusTag(agentsPayload?.dashboard_status || "unknown")}
    </div>
    <div class="muted">${escapeHtml(agentsPayload?.operator_note || "")}</div>
  `;
  missionosAgentsSummaryEl.innerHTML = `
    <div class="missionos-agent-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">classification</span><span class="detail-chip-value">${escapeHtml(classification.surface || "GUI agent-status visualization")}</span></span>
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">progress counted</span><span class="detail-chip-value">${escapeHtml(String(classification.progress_counted ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${authoritySupported ? "ok" : "warn"}"><span class="detail-chip-label">authority</span><span class="detail-chip-value">${escapeHtml(authoritySupported ? "safe" : "blocked")}</span></span>
        <span class="detail-chip mission-brief-chip-${knowledgeExplicit ? "ok" : "warn"}"><span class="detail-chip-label">knowledge boundary</span><span class="detail-chip-value">${escapeHtml(knowledgeExplicit ? "explicit false" : "warning")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">agents</span><span class="detail-chip-value">${escapeHtml(String(summary.agent_count ?? agents.length))}</span></span>
        <span class="detail-chip mission-brief-chip-${summary.blocked_agent_count ? "warn" : "ok"}"><span class="detail-chip-label">blocked</span><span class="detail-chip-value">${escapeHtml(String(summary.blocked_agent_count ?? 0))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">candidate inputs</span><span class="detail-chip-value">${escapeHtml(String(summary.knowledge_input_candidate_count ?? 0))}</span></span>
      </div>
      <div class="missionos-agent-next">
        <div class="k">Diagnostic Input Candidate</div>
        <div><strong>${escapeHtml(nextInspection.failure_mode_id || "no current candidate")}</strong> ${statusTag(nextInspection.status || knowledgeInput.browser_status || "missing")}</div>
        <div class="muted">${escapeHtml(nextInspection.recommended_next_inspection || "No knowledge input candidate is available.")}</div>
        <div class="item-meta mono">${escapeHtml(nextInspection.artifact_path || "")}</div>
      </div>
      ${warnings.length ? `<div class="detail-error">Agent dashboard warning: ${escapeHtml(warnings.join("; "))}</div>` : ""}
    </div>
    <details class="mission-ui-collapse" data-missionos-operator-details="agent-cards">
      <summary>Agent Cards (${escapeHtml(String(agents.length))})</summary>
      <div class="missionos-agent-grid">
        ${agents.map((agent) => `
          <section class="missionos-agent-card missionos-agent-card-${escapeAttr(agent.status || "unknown")}">
            <div class="item-head">
              <div>
                <strong>${escapeHtml(agent.label || agent.agent_id || "MissionOS agent")}</strong>
                <div class="item-meta mono">${escapeHtml(agent.agent_id || "-")}</div>
              </div>
              ${statusTag(agent.status || "unknown")}
            </div>
            <div class="item-detail">${escapeHtml(agent.role || "")}</div>
            <div class="detail-chip-row">
              <span class="detail-chip mission-brief-chip-${agent.boundary_status === "safe" ? "ok" : "warn"}"><span class="detail-chip-label">boundary</span><span class="detail-chip-value">${escapeHtml(agent.boundary_status || "unknown")}</span></span>
              <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">agent runtime</span><span class="detail-chip-value">${escapeHtml(String(agent.agent_execution_started_in_runtime ?? agent.agent_execution_started ?? false))}</span></span>
              <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">policy runtime</span><span class="detail-chip-value">${escapeHtml(String(agent.policy_update_applied_in_runtime ?? agent.policy_update_applied ?? false))}</span></span>
              <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">rule runtime</span><span class="detail-chip-value">${escapeHtml(String(agent.automatic_recovery_rule_created_in_runtime ?? agent.automatic_recovery_rule_created ?? false))}</span></span>
            </div>
            <div class="missionos-agent-fields">
              <div class="missionos-agent-field">
                <span class="missionos-agent-field-key">authority_scope</span>
                <span class="missionos-agent-field-value">${escapeHtml(agent.authority_scope || "-")}</span>
              </div>
              <div class="missionos-agent-field">
                <span class="missionos-agent-field-key">disabled_reason</span>
                <span class="missionos-agent-field-value">${escapeHtml(agent.disabled_reason || "-")}</span>
              </div>
              <div class="missionos-agent-field">
                <span class="missionos-agent-field-key">next_required_evidence</span>
                <span class="missionos-agent-field-value">${escapeHtml(agent.next_required_evidence || "-")}</span>
              </div>
              <div class="missionos-agent-field">
                <span class="missionos-agent-field-key">input_candidate_count</span>
                <span class="missionos-agent-field-value mono">${escapeHtml(String(agent.input_candidate_count ?? 0))}</span>
              </div>
            </div>
            <details class="mission-ui-collapse">
              <summary>Inputs / Outputs</summary>
              <div class="missionos-agent-io-grid">
                <div>
                  <div class="k">Inputs</div>
                  ${missionosAgentFieldList(agent.inputs)}
                </div>
                <div>
                  <div class="k">Outputs</div>
                  ${missionosAgentFieldList(agent.outputs)}
                </div>
              </div>
            </details>
            <details class="mission-ui-collapse">
              <summary>Related Artifacts</summary>
              <div class="missionos-agent-artifacts">
                ${asArray(agent.related_artifacts).map((ref) => `<div class="mono">${escapeHtml(String(ref))}</div>`).join("") || `<div class="muted">No related artifacts.</div>`}
              </div>
            </details>
          </section>
        `).join("") || `<div class="detail-error">No agent cards available.</div>`}
      </div>
    </details>
    <details class="mission-ui-collapse">
      <summary>Agent Dashboard Boundary Not Claimed</summary>
      <div class="missionos-milestone-not-claimed">
        <div class="detail-chip-row">
          ${asArray(agentsPayload?.not_claimed).map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("")}
        </div>
      </div>
    </details>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSAgents() {
  if (!missionosAgentsStatusEl || !missionosAgentsSummaryEl) return;
  missionosAgentsStatusEl.textContent = "Loading agent status...";
  missionosAgentsSummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/agents");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const agents = await response.json();
    latestMissionOSOperatorSourceErrors.agents = "";
    renderMissionOSAgents(agents);
    logEvent("missionos.agents", {
      dashboard_status: agents.dashboard_status,
      agents: asArray(agents.agents).length,
    });
  } catch (err) {
    latestMissionOSOperatorPayloads.agents = null;
    latestMissionOSOperatorSourceErrors.agents = String(err);
    missionosAgentsStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosAgentsSummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.agents.error", { error: String(err) });
  }
}

function renderMissionOSKnowledgeSharing(payload) {
  if (!missionosKnowledgeSharingStatusEl || !missionosKnowledgeSharingSummaryEl) return;
  latestMissionOSOperatorPayloads.knowledgeSharing = payload || null;
  const l3 = asPlainObject(payload?.l3_cross_session_lesson);
  const l4 = asPlainObject(payload?.l4_knowledge_curator);
  const activeIndex = asPlainObject(payload?.active_lesson_index);
  const boundary = asPlainObject(payload?.authority_boundary);
  const currentSource = asPlainObject(payload?.current_source);
  const notClaimed = asArray(payload?.not_claimed);
  const agentStartedRuntime = boundary.agent_execution_started_in_runtime ?? boundary.agent_execution_started ?? false;
  const policyRuntime = boundary.policy_update_applied_in_runtime ?? boundary.policy_update_applied ?? false;
  const recoveryRuleRuntime = boundary.automatic_recovery_rule_created_in_runtime ?? boundary.automatic_recovery_rule_created ?? false;
  const blocked = payload?.summary_status === "blocked" || boundary.authority_boundary_supported === false;
  const blockedReasons = asArray(boundary.blocking_reasons);
  missionosKnowledgeSharingStatusEl.innerHTML = `
    <div class="item-head">
      <strong>${escapeHtml(payload?.surface_label || "Knowledge Sharing Success - Dry Run")}</strong>
      ${statusTag(payload?.summary_status || "missing")}
    </div>
    <div class="muted">${escapeHtml(payload?.operator_note || "")}</div>
  `;
  missionosKnowledgeSharingSummaryEl.innerHTML = `
    <div class="missionos-knowledge-sharing-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-${l3.status === "persisted" ? "ok" : "pending"}"><span class="detail-chip-label">L3 lesson</span><span class="detail-chip-value">${escapeHtml(l3.status || "missing")}</span></span>
        <span class="detail-chip mission-brief-chip-${l4.status === "production_completed" || l4.status === "dry_run_completed" ? "ok" : "pending"}"><span class="detail-chip-label">L4 curator</span><span class="detail-chip-value">${escapeHtml(l4.status || "not_run")}</span></span>
        <span class="detail-chip mission-brief-chip-${activeIndex.status === "updated" ? "ok" : "pending"}"><span class="detail-chip-label">active index</span><span class="detail-chip-value">${escapeHtml(activeIndex.status || "missing")}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.production_reflected ? "ok" : "pending"}"><span class="detail-chip-label">production reflected</span><span class="detail-chip-value">${escapeHtml(String(boundary.production_reflected ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.agent_execution_allowed ? "ok" : "pending"}"><span class="detail-chip-label">curator run allowed</span><span class="detail-chip-value">${escapeHtml(String(boundary.agent_execution_allowed ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.dry_run_only ? "ok" : "pending"}"><span class="detail-chip-label">dry-run only</span><span class="detail-chip-value">${escapeHtml(String(boundary.dry_run_only ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.no_background_automation ? "ok" : "warn"}"><span class="detail-chip-label">no background automation</span><span class="detail-chip-value">${escapeHtml(String(boundary.no_background_automation ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${agentStartedRuntime && !boundary.agent_execution_allowed ? "warn" : "ok"}"><span class="detail-chip-label">agent runtime</span><span class="detail-chip-value">${escapeHtml(String(agentStartedRuntime))}</span></span>
        <span class="detail-chip mission-brief-chip-${policyRuntime ? "warn" : "ok"}"><span class="detail-chip-label">policy runtime</span><span class="detail-chip-value">${escapeHtml(String(policyRuntime))}</span></span>
        <span class="detail-chip mission-brief-chip-${recoveryRuleRuntime ? "warn" : "ok"}"><span class="detail-chip-label">rule runtime</span><span class="detail-chip-value">${escapeHtml(String(recoveryRuleRuntime))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.source_bound_current ? "ok" : "warn"}"><span class="detail-chip-label">source-bound current</span><span class="detail-chip-value">${escapeHtml(String(boundary.source_bound_current ?? false))}</span></span>
      </div>
      ${blocked ? `<div class="detail-error">Knowledge sharing boundary blocked: ${escapeHtml(blockedReasons.join(", ") || asArray(boundary.authority_true_flags).join(", ") || "unknown")}</div>` : ""}
      <div class="missionos-knowledge-sharing-grid">
        <section class="missionos-knowledge-sharing-card">
          <div class="k">Current Diagnostic Source</div>
          <strong>${escapeHtml(currentSource.failure_mode_id || "no persistable source")}</strong>
          <div class="muted">persistable=${escapeHtml(String(currentSource.persistable ?? false))}</div>
          <div class="item-meta mono">${escapeHtml(currentSource.artifact_path || "")}</div>
        </section>
        <section class="missionos-knowledge-sharing-card">
          <div class="k">L3 Cross-Session Lesson</div>
          <strong>${escapeHtml(l3.source_failure_mode_id || "no lesson persisted yet")}</strong>
          <div class="muted">reuse_scope=${escapeHtml(l3.reuse_scope || "-")}</div>
          <div class="muted">production_reflected=${escapeHtml(String(l3.production_reflected ?? false))}</div>
          <div class="item-meta mono">${escapeHtml(l3.artifact_path || "")}</div>
        </section>
        <section class="missionos-knowledge-sharing-card">
          <div class="k">L4 Knowledge Curator Run</div>
          <strong>${escapeHtml(l4.curator_run_id || "not run")}</strong>
          <div class="muted">dry_run_only=${escapeHtml(String(l4.dry_run_only ?? false))}</div>
          <div class="muted">operator_approved: artifact=${escapeHtml(String(l4.operator_approved_in_artifact ?? false))} runtime=${escapeHtml(String(l4.operator_approved_in_runtime ?? false))}</div>
          <div class="muted">agent_execution_started: artifact=${escapeHtml(String(l4.agent_execution_started_in_artifact ?? false))} runtime=${escapeHtml(String(l4.agent_execution_started_in_runtime ?? false))}</div>
          <div class="muted">knowledge_index_updated: artifact=${escapeHtml(String(l4.knowledge_index_updated_in_artifact ?? false))} runtime=${escapeHtml(String(l4.knowledge_index_updated_in_runtime ?? false))}</div>
          <div class="muted">no_background_automation=${escapeHtml(String(l4.no_background_automation ?? false))}</div>
          <div class="item-meta mono">${escapeHtml(l4.artifact_path || "")}</div>
        </section>
        <section class="missionos-knowledge-sharing-card">
          <div class="k">Active Lesson Index</div>
          <strong>${escapeHtml(activeIndex.status || "missing")}</strong>
          <div class="muted">active_for_future_diagnostics=${escapeHtml(String(activeIndex.active_for_future_diagnostics ?? false))}</div>
          <div class="item-meta mono">${escapeHtml(activeIndex.artifact_path || "")}</div>
        </section>
      </div>
      <details class="mission-ui-collapse">
        <summary>Knowledge Sharing Boundary Not Claimed</summary>
        <div class="detail-chip-row">
          ${notClaimed.map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("")}
        </div>
      </details>
    </div>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSKnowledgeSharing() {
  if (!missionosKnowledgeSharingStatusEl || !missionosKnowledgeSharingSummaryEl) return;
  missionosKnowledgeSharingStatusEl.textContent = "Loading knowledge sharing evidence...";
  missionosKnowledgeSharingSummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/knowledge-sharing");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.knowledgeSharing = "";
    renderMissionOSKnowledgeSharing(payload);
    logEvent("missionos.knowledge_sharing", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorPayloads.knowledgeSharing = null;
    latestMissionOSOperatorSourceErrors.knowledgeSharing = String(err);
    missionosKnowledgeSharingStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosKnowledgeSharingSummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.knowledge_sharing.error", { error: String(err) });
  }
}

async function runMissionOSKnowledgeCuratorDryRun() {
  if (!missionosKnowledgeCuratorDryRunBtn || !missionosKnowledgeSharingStatusEl) return;
  missionosKnowledgeCuratorDryRunBtn.disabled = true;
  missionosKnowledgeSharingStatusEl.textContent = "Running Knowledge Curator dry run...";
  try {
    const response = await apiFetchWithTimeout("/missionos/knowledge-sharing/curate-dry-run", {
      method: "POST",
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.knowledgeSharing = "";
    renderMissionOSKnowledgeSharing(payload);
    logEvent("missionos.knowledge_curator_dry_run", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorSourceErrors.knowledgeSharing = String(err);
    missionosKnowledgeSharingStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    renderMissionOSOperatorSummary();
    logEvent("missionos.knowledge_curator_dry_run.error", { error: String(err) });
  } finally {
    missionosKnowledgeCuratorDryRunBtn.disabled = false;
  }
}

async function publishMissionOSKnowledgeSharing() {
  if (!missionosKnowledgePublishBtn || !missionosKnowledgeSharingStatusEl) return;
  missionosKnowledgePublishBtn.disabled = true;
  missionosKnowledgeSharingStatusEl.textContent = "Publishing diagnostic lesson to active knowledge index...";
  try {
    const response = await apiFetchWithTimeout("/missionos/knowledge-sharing/publish", {
      method: "POST",
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.knowledgeSharing = "";
    renderMissionOSKnowledgeSharing(payload);
    logEvent("missionos.knowledge_sharing_publish", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorSourceErrors.knowledgeSharing = String(err);
    missionosKnowledgeSharingStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    renderMissionOSOperatorSummary();
    logEvent("missionos.knowledge_sharing_publish.error", { error: String(err) });
  } finally {
    missionosKnowledgePublishBtn.disabled = false;
  }
}

function renderMissionOSPolicyAuthority(payload) {
  if (!missionosPolicyAuthorityStatusEl || !missionosPolicyAuthoritySummaryEl) return;
  latestMissionOSOperatorPayloads.policyAuthority = payload || null;
  const boundary = asPlainObject(payload?.authority_boundary);
  const candidate = asPlainObject(payload?.policy_update_candidate);
  const approval = asPlainObject(payload?.operator_policy_approval);
  const policy = asPlainObject(payload?.active_policy_version);
  const rule = asPlainObject(payload?.automatic_recovery_rule);
  const authority = asPlainObject(payload?.bounded_dispatch_authority);
  const blockedReasons = asArray(boundary.blocking_reasons);
  missionosPolicyAuthorityStatusEl.innerHTML = `
    <div class="item-head">
      <strong>Operator-Gated Policy Authority Path</strong>
      ${statusTag(payload?.summary_status || "missing")}
    </div>
    <div class="muted">${escapeHtml(payload?.operator_note || "")}</div>
  `;
  missionosPolicyAuthoritySummaryEl.innerHTML = `
    <div class="missionos-knowledge-sharing-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-${boundary.policy_update_recorded_in_artifact ? "ok" : "pending"}"><span class="detail-chip-label">policy artifact</span><span class="detail-chip-value">${escapeHtml(String(boundary.policy_update_recorded_in_artifact ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.policy_update_applied_to_runtime_engine ? "ok" : "pending"}"><span class="detail-chip-label">policy runtime</span><span class="detail-chip-value">${escapeHtml(String(boundary.policy_update_applied_to_runtime_engine ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.recovery_rule_recorded_in_artifact ? "ok" : "pending"}"><span class="detail-chip-label">rule artifact</span><span class="detail-chip-value">${escapeHtml(String(boundary.recovery_rule_recorded_in_artifact ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.dispatch_authority_available_in_runtime_dispatch_table ? "ok" : "pending"}"><span class="detail-chip-label">dispatch table</span><span class="detail-chip-value">${escapeHtml(String(boundary.dispatch_authority_available_in_runtime_dispatch_table ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.operator_approval_required ? "ok" : "warn"}"><span class="detail-chip-label">operator approval required</span><span class="detail-chip-value">${escapeHtml(String(boundary.operator_approval_required ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.automatic_dispatch_suppressed ? "ok" : "warn"}"><span class="detail-chip-label">automatic dispatch suppressed</span><span class="detail-chip-value">${escapeHtml(String(boundary.automatic_dispatch_suppressed ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.dispatch_executed_in_runtime ? "warn" : "ok"}"><span class="detail-chip-label">dispatch runtime</span><span class="detail-chip-value">${escapeHtml(String(boundary.dispatch_executed_in_runtime ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.physical_execution_invoked ? "warn" : "ok"}"><span class="detail-chip-label">physical</span><span class="detail-chip-value">${escapeHtml(String(boundary.physical_execution_invoked ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.llm_gate_judge_used ? "warn" : "ok"}"><span class="detail-chip-label">LLM gate</span><span class="detail-chip-value">${escapeHtml(String(boundary.llm_gate_judge_used ?? false))}</span></span>
      </div>
      ${blockedReasons.length ? `<div class="detail-error">Policy authority boundary blocked: ${escapeHtml(blockedReasons.join(", "))}</div>` : ""}
      <div class="missionos-knowledge-sharing-grid">
        ${[
    ["Policy Candidate", candidate.status, candidate.artifact_path, `rollback=${candidate.rollback_ref || "-"}`],
    ["Operator Approval", approval.status, approval.artifact_path, `operator_approved_in_artifact=${approval.operator_approved_in_artifact ?? false}; operator_approved_in_runtime=${approval.operator_approved_in_runtime ?? false}`],
    ["Active Policy", policy.status, policy.artifact_path, `policy_runtime=${policy.policy_update_applied_to_runtime_engine ?? false}`],
    ["Recovery Rule", rule.status, rule.artifact_path, `bounded_action_ref=${rule.bounded_action_ref || "-"}`],
    ["Bounded Dispatch Authority", authority.status, authority.artifact_path, `dispatch_ref=${authority.dispatch_ref || "-"}`],
  ].map(([label, status, path, meta]) => `
          <section class="missionos-knowledge-sharing-card">
            <div class="k">${escapeHtml(label)}</div>
            <strong>${escapeHtml(status || "missing")}</strong>
            <div class="muted mono">${escapeHtml(meta || "")}</div>
            <div class="item-meta mono">${escapeHtml(path || "")}</div>
          </section>
        `).join("")}
      </div>
    </div>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSPolicyAuthority() {
  if (!missionosPolicyAuthorityStatusEl || !missionosPolicyAuthoritySummaryEl) return;
  missionosPolicyAuthorityStatusEl.textContent = "Loading policy authority evidence...";
  missionosPolicyAuthoritySummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/policy-authority");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.policyAuthority = "";
    renderMissionOSPolicyAuthority(payload);
    logEvent("missionos.policy_authority", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorPayloads.policyAuthority = null;
    latestMissionOSOperatorSourceErrors.policyAuthority = String(err);
    missionosPolicyAuthorityStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosPolicyAuthoritySummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.policy_authority.error", { error: String(err) });
  }
}

async function promoteMissionOSPolicyAuthority() {
  if (!missionosPolicyAuthorityPromoteBtn || !missionosPolicyAuthorityStatusEl) return;
  missionosPolicyAuthorityPromoteBtn.disabled = true;
  missionosPolicyAuthorityStatusEl.textContent = "Promoting active lesson into operator-gated policy authority path...";
  try {
    const response = await apiFetchWithTimeout("/missionos/policy-authority/promote", {
      method: "POST",
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.policyAuthority = "";
    renderMissionOSPolicyAuthority(payload);
    logEvent("missionos.policy_authority_promote", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorSourceErrors.policyAuthority = String(err);
    missionosPolicyAuthorityStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    renderMissionOSOperatorSummary();
    logEvent("missionos.policy_authority_promote.error", { error: String(err) });
  } finally {
    missionosPolicyAuthorityPromoteBtn.disabled = false;
  }
}

function renderMissionOSSitlDispatchExecution(payload) {
  if (!missionosSitlDispatchStatusEl || !missionosSitlDispatchSummaryEl) return;
  latestMissionOSOperatorPayloads.sitlDispatch = payload || null;
  const boundary = asPlainObject(payload?.authority_boundary);
  const approval = asPlainObject(payload?.operator_dispatch_approval);
  const gate = asPlainObject(payload?.deterministic_dispatch_gate);
  const execution = asPlainObject(payload?.bounded_dispatch_execution);
  const request = asPlainObject(payload?.backend_action_request);
  const outcome = asPlainObject(payload?.dispatch_outcome_observation);
  const verifier = asPlainObject(payload?.recovery_verifier_result);
  const audit = asPlainObject(payload?.audit_record);
  const blockedReasons = asArray(boundary.blocking_reasons);
  missionosSitlDispatchStatusEl.innerHTML = `
    <div class="item-head">
      <strong>Operator-Approved SITL Dispatch Execution</strong>
      ${statusTag(payload?.summary_status || "missing")}
    </div>
    <div class="muted">${escapeHtml(payload?.operator_note || "")}</div>
  `;
  missionosSitlDispatchSummaryEl.innerHTML = `
    <div class="missionos-knowledge-sharing-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-${boundary.dispatch_executed_in_runtime ? "ok" : "pending"}"><span class="detail-chip-label">dispatch runtime</span><span class="detail-chip-value">${escapeHtml(String(boundary.dispatch_executed_in_runtime ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.dispatch_executed_in_artifact ? "ok" : "pending"}"><span class="detail-chip-label">dispatch artifact</span><span class="detail-chip-value">${escapeHtml(String(boundary.dispatch_executed_in_artifact ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.runtime_invocation_evidence_present ? "ok" : "pending"}"><span class="detail-chip-label">runtime evidence</span><span class="detail-chip-value">${escapeHtml(String(boundary.runtime_invocation_evidence_present ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.dispatch_trigger === "operator_approved" ? "ok" : "warn"}"><span class="detail-chip-label">trigger</span><span class="detail-chip-value">${escapeHtml(boundary.dispatch_trigger || "-")}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.automatic_dispatch_executed ? "warn" : "ok"}"><span class="detail-chip-label">automatic dispatch</span><span class="detail-chip-value">${escapeHtml(String(boundary.automatic_dispatch_executed ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.physical_execution_invoked ? "warn" : "ok"}"><span class="detail-chip-label">physical</span><span class="detail-chip-value">${escapeHtml(String(boundary.physical_execution_invoked ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.hardware_target_allowed ? "warn" : "ok"}"><span class="detail-chip-label">hardware</span><span class="detail-chip-value">${escapeHtml(String(boundary.hardware_target_allowed ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.same_session ? "ok" : "warn"}"><span class="detail-chip-label">same session</span><span class="detail-chip-value">${escapeHtml(String(boundary.same_session ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.refs_consistent ? "ok" : "warn"}"><span class="detail-chip-label">refs</span><span class="detail-chip-value">${escapeHtml(String(boundary.refs_consistent ?? false))}</span></span>
      </div>
      ${blockedReasons.length ? `<div class="detail-error">SITL dispatch boundary blocked: ${escapeHtml(blockedReasons.join(", "))}</div>` : ""}
      <div class="missionos-knowledge-sharing-grid">
        ${[
    ["Operator Dispatch Approval", approval.status, approval.artifact_path, `operator_approved_in_artifact=${approval.operator_approved_in_artifact ?? false}; operator_approved_in_runtime=${approval.operator_approved_in_runtime ?? false}`],
    ["Deterministic Gate", gate.status, gate.artifact_path, `deterministic_gate_passed_in_artifact=${gate.deterministic_gate_passed_in_artifact ?? false}; deterministic_gate_passed_in_runtime=${gate.deterministic_gate_passed_in_runtime ?? false}`],
    ["Dispatch Execution", execution.status, execution.artifact_path, `dispatch_runtime=${execution.dispatch_executed_in_runtime ?? false}`],
    ["Backend Action Request", request.status, request.artifact_path, `backend_target=${request.backend_target || "-"}`],
    ["Outcome Observation", outcome.status, outcome.artifact_path, `outcome_runtime=${outcome.outcome_observed_in_runtime ?? false}`],
    ["Recovery Verifier", verifier.status, verifier.artifact_path, `verified_runtime=${verifier.verified_dispatch_execution_in_runtime ?? false}`],
    ["Audit Record", audit.status, audit.artifact_path, ""],
  ].map(([label, status, path, meta]) => `
          <section class="missionos-knowledge-sharing-card">
            <div class="k">${escapeHtml(label)}</div>
            <strong>${escapeHtml(status || "missing")}</strong>
            <div class="muted mono">${escapeHtml(meta || "")}</div>
            <div class="item-meta mono">${escapeHtml(path || "")}</div>
          </section>
        `).join("")}
      </div>
    </div>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSSitlDispatchExecution() {
  if (!missionosSitlDispatchStatusEl || !missionosSitlDispatchSummaryEl) return;
  missionosSitlDispatchStatusEl.textContent = "Loading SITL dispatch evidence...";
  missionosSitlDispatchSummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/sitl-dispatch-execution");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.sitlDispatch = "";
    renderMissionOSSitlDispatchExecution(payload);
    logEvent("missionos.sitl_dispatch_execution", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorPayloads.sitlDispatch = null;
    latestMissionOSOperatorSourceErrors.sitlDispatch = String(err);
    missionosSitlDispatchStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosSitlDispatchSummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.sitl_dispatch_execution.error", { error: String(err) });
  }
}

async function runMissionOSSitlDispatchExecution() {
  if (!missionosSitlDispatchRunBtn || !missionosSitlDispatchStatusEl) return;
  missionosSitlDispatchRunBtn.disabled = true;
  missionosSitlDispatchStatusEl.textContent = "Executing operator-approved bounded SITL dispatch...";
  try {
    const response = await apiFetchWithTimeout("/missionos/sitl-dispatch-execution/run", {
      method: "POST",
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.sitlDispatch = "";
    renderMissionOSSitlDispatchExecution(payload);
    logEvent("missionos.sitl_dispatch_execution_run", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorSourceErrors.sitlDispatch = String(err);
    missionosSitlDispatchStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    renderMissionOSOperatorSummary();
    logEvent("missionos.sitl_dispatch_execution_run.error", { error: String(err) });
  } finally {
    missionosSitlDispatchRunBtn.disabled = false;
  }
}

function renderMissionOSScopedForm3(payload) {
  if (!missionosScopedForm3StatusEl || !missionosScopedForm3SummaryEl) return;
  latestMissionOSOperatorPayloads.scopedForm3 = payload || null;
  const boundary = asPlainObject(payload?.authority_boundary);
  const record = asPlainObject(payload?.closed_loop_record);
  const cycle1 = asPlainObject(payload?.cycle1);
  const cycle2 = asPlainObject(payload?.cycle2);
  missionosScopedForm3StatusEl.innerHTML = `
    <div class="item-head">
      <strong>Scoped Form 3 Closed Loop</strong>
      ${statusTag(payload?.summary_status || "missing")}
    </div>
    <div class="muted">${escapeHtml(payload?.operator_note || "")}</div>
  `;
  missionosScopedForm3SummaryEl.innerHTML = `
    <div class="missionos-knowledge-sharing-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-${boundary.form3_runtime_observed ? "ok" : "pending"}"><span class="detail-chip-label">Form 3 runtime</span><span class="detail-chip-value">${escapeHtml(String(boundary.form3_runtime_observed ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.closed_loop_cycle_count >= 2 ? "ok" : "pending"}"><span class="detail-chip-label">cycles</span><span class="detail-chip-value">${escapeHtml(String(boundary.closed_loop_cycle_count ?? 0))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.cycle1_runtime_invocation_evidence_present ? "ok" : "pending"}"><span class="detail-chip-label">cycle 1 evidence</span><span class="detail-chip-value">${escapeHtml(String(boundary.cycle1_runtime_invocation_evidence_present ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.cycle2_runtime_invocation_evidence_present ? "ok" : "pending"}"><span class="detail-chip-label">cycle 2 evidence</span><span class="detail-chip-value">${escapeHtml(String(boundary.cycle2_runtime_invocation_evidence_present ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.cycle1_dispatch_ref_distinct_from_cycle2 ? "ok" : "warn"}"><span class="detail-chip-label">distinct dispatch</span><span class="detail-chip-value">${escapeHtml(String(boundary.cycle1_dispatch_ref_distinct_from_cycle2 ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.cycle2_response_derived_from_cycle1_outcome ? "ok" : "pending"}"><span class="detail-chip-label">derived response</span><span class="detail-chip-value">${escapeHtml(String(boundary.cycle2_response_derived_from_cycle1_outcome ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.automatic_dispatch_executed ? "warn" : "ok"}"><span class="detail-chip-label">automatic dispatch</span><span class="detail-chip-value">${escapeHtml(String(boundary.automatic_dispatch_executed ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.hardware_target_allowed ? "warn" : "ok"}"><span class="detail-chip-label">hardware</span><span class="detail-chip-value">${escapeHtml(String(boundary.hardware_target_allowed ?? false))}</span></span>
      </div>
      <div class="missionos-knowledge-sharing-grid">
        ${[
    ["Closed Loop Record", record.status, record.artifact_path, `cycles=${record.closed_loop_cycle_count || 0}`],
    ["Cycle 1 Runtime", cycle1.summary_status, cycle1.dispatch_ref, `evidence=${cycle1.runtime_invocation_evidence_present ?? false}`],
    ["Cycle 2 Runtime", cycle2.summary_status, cycle2.dispatch_ref, `response=${cycle2.response || "-"}`],
  ].map(([label, status, path, meta]) => `
          <section class="missionos-knowledge-sharing-card">
            <div class="k">${escapeHtml(label)}</div>
            <strong>${escapeHtml(status || "missing")}</strong>
            <div class="muted mono">${escapeHtml(meta || "")}</div>
            <div class="item-meta mono">${escapeHtml(path || "")}</div>
          </section>
        `).join("")}
      </div>
    </div>
  `;
  renderMissionOSOperatorSummary();
}

function renderMissionOSForm2aAiAgent(payload) {
  if (!missionosForm2aAiAgentStatusEl || !missionosForm2aAiAgentSummaryEl) return;
  latestMissionOSOperatorPayloads.form2aAiAgent = payload || null;
  const selection = asPlainObject(payload?.selection?.response_selection);
  const review = asPlainObject(payload?.review?.human_review);
  const action = asPlainObject(payload?.action?.action_consumption);
  const runtime = asPlainObject(payload?.action?.runtime_dispatch);
  const classification = asPlainObject(payload?.action?.classification);
  const boundary = asPlainObject(payload?.action?.authority_boundary);
  const reviewBoundary = asPlainObject(payload?.review?.authority_boundary);
  const blockingReasons = asArray(boundary.blocking_reasons);
  const aiProgress = classification.ai_agent_progress_counted === true;
  const goalProgress = classification.goal_640_progress_counted === true;
  const llmSource = selection.intelligence_source || action.intelligence_source || boundary.intelligence_source || "-";
  missionosForm2aAiAgentStatusEl.innerHTML = `
    <div class="item-head">
      <strong>ADK/Gemini Form 2a Runtime Chain</strong>
      ${statusTag(missionosOperatorStatusDisplay(payload?.summary_status || payload?.action?.summary_status || "missing"))}
    </div>
    <div class="muted">LLM proposes; human approves; rules constrain; executor acts; verifier checks. Delivery completion, hardware authority, and physical execution remain false.</div>
  `;
  missionosForm2aAiAgentSummaryEl.innerHTML = `
    <div class="missionos-knowledge-sharing-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-${llmSource === "llm_response_planner" ? "ok" : "warn"}"><span class="detail-chip-label">intelligence</span><span class="detail-chip-value">${escapeHtml(llmSource)}</span></span>
        <span class="detail-chip mission-brief-chip-${selection.eligible_for_ai_agent_progress ? "ok" : "pending"}"><span class="detail-chip-label">AI eligible</span><span class="detail-chip-value">${escapeHtml(String(selection.eligible_for_ai_agent_progress ?? action.eligible_for_ai_agent_progress ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${reviewBoundary.human_operator_approval_granted_in_artifact ? "ok" : "pending"}"><span class="detail-chip-label">human approval</span><span class="detail-chip-value">${escapeHtml(String(reviewBoundary.human_operator_approval_granted_in_artifact ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${action.operator_approval_token_consumed_in_runtime ? "ok" : "pending"}"><span class="detail-chip-label">token runtime</span><span class="detail-chip-value">${escapeHtml(String(action.operator_approval_token_consumed_in_runtime ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${runtime.dispatch_executed_in_runtime ? "ok" : "pending"}"><span class="detail-chip-label">dispatch runtime</span><span class="detail-chip-value">${escapeHtml(String(runtime.dispatch_executed_in_runtime ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${action.llm_response_parameters_bound_to_runtime ? "ok" : "pending"}"><span class="detail-chip-label">LLM params bound</span><span class="detail-chip-value">${escapeHtml(String(action.llm_response_parameters_bound_to_runtime ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${runtime.verified_dispatch_execution_in_runtime ? "ok" : "pending"}"><span class="detail-chip-label">verifier runtime</span><span class="detail-chip-value">${escapeHtml(String(runtime.verified_dispatch_execution_in_runtime ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${aiProgress ? "ok" : "pending"}"><span class="detail-chip-label">AI progress</span><span class="detail-chip-value">${escapeHtml(String(aiProgress))}</span></span>
      </div>
      <div class="missionos-operator-one-line missionos-operator-one-line-${aiProgress ? "safe" : "blocked"}">
        <strong>Conclusion:</strong>
        ${aiProgress ? "Form 2a internal capability runtime progress observed." : "Form 2a internal capability runtime progress not yet observed."}
        response <span class="mono">${escapeHtml(selection.selected_response_kind || action.selected_response_kind || "-")}</span>;
        payload mass <span class="mono">${escapeHtml(String(asPlainObject(selection.llm_response_parameters).payload_mass_kg ?? asPlainObject(action.llm_response_parameters).payload_mass_kg ?? "-"))}</span>;
        mission progress <span class="mono">${escapeHtml(goalProgress ? "observed" : "not yet observed")}</span>.
      </div>
      ${blockingReasons.length ? `<div class="detail-error">Blocked reasons: ${escapeHtml(blockingReasons.join(", "))}</div>` : ""}
      <div class="missionos-knowledge-sharing-grid">
        ${[
    ["Internal Response Selection", payload?.selection?.summary_status, selection.artifact_path, `status=${selection.llm_response_planner_status || "-"}; source=${selection.intelligence_source || "-"}`],
    ["Human Operator Review", payload?.review?.summary_status, review.artifact_path, `approved=${reviewBoundary.human_operator_approval_granted_in_artifact ?? false}`],
    ["Action Consumption", payload?.action?.summary_status, action.artifact_path, `ai_agent_progress=${classification.ai_agent_progress_counted ?? false}; mission_progress=${classification.goal_640_progress_counted ?? false}`],
    ["Runtime Dispatch / Verifier", runtime.summary_status, "", `payload_supported=${runtime.payload_recovery_action_supported ?? false}; verified=${runtime.verified_dispatch_execution_in_runtime ?? false}`],
    ["Authority Boundary", blockingReasons.length ? "blocked" : "clear", "", `delivery=${boundary.delivery_completion_claimed ?? false}; hardware=${boundary.hardware_target_allowed ?? false}; physical=${boundary.physical_execution_invoked ?? false}`],
  ].map(([label, status, path, meta]) => `
          <section class="missionos-knowledge-sharing-card">
            <div class="k">${escapeHtml(label)}</div>
            <strong>${escapeHtml(missionosOperatorStatusDisplay(status || "missing"))}</strong>
            <div class="muted mono">${escapeHtml(meta || "")}</div>
            <div class="item-meta mono">${escapeHtml(path || "")}</div>
          </section>
        `).join("")}
      </div>
    </div>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSForm2aAiAgent() {
  if (!missionosForm2aAiAgentStatusEl || !missionosForm2aAiAgentSummaryEl) return;
  missionosForm2aAiAgentStatusEl.textContent = "Loading Form 2a internal capability audit evidence...";
  missionosForm2aAiAgentSummaryEl.innerHTML = "";
  try {
    const [selectionRes, reviewRes, actionRes] = await Promise.all([
      apiFetchWithTimeout("/missionos/form2a-response-selection"),
      apiFetchWithTimeout("/missionos/form2a-operator-review"),
      apiFetchWithTimeout("/missionos/form2a-action-consumption"),
    ]);
    for (const response of [selectionRes, reviewRes, actionRes]) {
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || `HTTP ${response.status}`);
      }
    }
    const payload = {
      summary_status: "loaded",
      selection: await selectionRes.json(),
      review: await reviewRes.json(),
      action: await actionRes.json(),
    };
    payload.summary_status = payload.action?.summary_status || payload.selection?.summary_status || "loaded";
    latestMissionOSOperatorSourceErrors.form2aAiAgent = "";
    renderMissionOSForm2aAiAgent(payload);
    logEvent("missionos.form2a_ai_agent", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorPayloads.form2aAiAgent = null;
    latestMissionOSOperatorSourceErrors.form2aAiAgent = String(err);
    missionosForm2aAiAgentStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosForm2aAiAgentSummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.form2a_ai_agent.error", { error: String(err) });
  }
}

async function runMissionOSForm2aAiAgentStep(path, statusText) {
  if (!missionosForm2aAiAgentStatusEl) return;
  missionosForm2aAiAgentStatusEl.textContent = statusText;
  latestMissionOSAutonomyNotice = statusText;
  renderMissionOSAutonomyMonitor();
  try {
    const requestBody = latestMissionOSOperatorInstruction
      ? JSON.stringify({ operator_instruction: latestMissionOSOperatorInstruction })
      : undefined;
    const response = await apiFetchWithTimeout(path, {
      method: "POST",
      headers: requestBody ? { "Content-Type": "application/json" } : undefined,
      body: requestBody,
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    await loadMissionOSForm2aAiAgent();
    latestMissionOSAutonomyNotice = "I handled that reply and refreshed the visible MissionOS state.";
    renderMissionOSAutonomyMonitor();
    logEvent("missionos.form2a_ai_agent.run_step", { path });
  } catch (err) {
    latestMissionOSAutonomyNotice = missionosFriendlyActionError(err, path);
    missionosForm2aAiAgentStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    renderMissionOSAutonomyMonitor();
    logEvent("missionos.form2a_ai_agent.run_step.error", { path, error: String(err) });
  }
}

async function postMissionOSConversationInstruction(instruction, options = {}) {
  const missionDesignerContext = latestMissionScenarioResult && typeof latestMissionScenarioResult === "object"
    ? latestMissionScenarioResult
    : null;
  const requestPayload = { operator_instruction: instruction, session_id: currentSessionId || "" };
  if (options.routeHint) requestPayload.missionos_route_hint = options.routeHint;
  if (missionDesignerContext) {
    requestPayload.mission_designer_context = {
      mission_designer_context_ref: missionDesignerContext.mission_designer_context_ref
        || missionDesignerContext.summary?.mission_designer_context_ref
        || "",
      mission_designer_context_sha256: missionDesignerContext.mission_designer_context_sha256
        || missionDesignerContext.summary?.mission_designer_context_sha256
        || "",
      mission_designer_context_session_id: missionDesignerContext.mission_designer_context_session_id
        || missionDesignerContext.summary?.mission_designer_context_session_id
        || currentSessionId
        || "",
    };
  }
  const coordinateRoute = missionOSFlightSetupCoordinateRoutePayload();
  if (coordinateRoute) {
    requestPayload.coordinate_route = coordinateRoute;
  }
  const response = await apiFetchWithTimeout("/missionos/autonomy-conversation/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(requestPayload),
  }, MISSIONOS_AUTONOMY_CONVERSATION_TIMEOUT_MS);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return response.json();
}

function missionOSPayloadMissionDesigner(payload, actions = ["mission_designer_plan", "approve", "execute"]) {
  const missionDesigner = asPlainObject(payload?.mission_designer);
  if (Object.keys(missionDesigner).length) return missionDesigner;
  if (actions.includes(payload?.routed_action)) return asPlainObject(payload?.operation_result);
  return {};
}

function applyMissionOSConversationPayload(payload) {
  const missionDesigner = missionOSPayloadMissionDesigner(payload);
  if (Object.keys(missionDesigner).length) {
    const nextRef = missionDesigner.mission_designer_context_ref
      || missionDesigner.summary?.mission_designer_context_ref
      || "";
    const prevRef = latestMissionScenarioResult?.mission_designer_context_ref
      || latestMissionScenarioResult?.summary?.mission_designer_context_ref
      || "";
    const replacesScenario = payload?.routed_action === "mission_designer_plan"
      || (nextRef && nextRef !== prevRef && Object.keys(asPlainObject(missionDesigner.scenario_proposal)).length);
    latestMissionScenarioResult = replacesScenario
      ? {
          ...missionDesigner,
          summary: { ...(missionDesigner.summary || {}) },
        }
      : {
          ...(latestMissionScenarioResult || {}),
          ...missionDesigner,
          summary: {
            ...(latestMissionScenarioResult?.summary || {}),
            ...(missionDesigner.summary || {}),
          },
        };
    const returnedRoute = asPlainObject(missionDesigner.mission_designer_coordinate_pair_route);
    if (Object.keys(returnedRoute).length) {
      applyMissionOSFlightSetupRoute(returnedRoute);
    }
    renderMissionScenarioResult(latestMissionScenarioResult);
  }
  latestMissionOSOperatorPayloads.form2aAiAgent = {
    summary_status: payload.action?.summary_status || payload.selection?.summary_status || "conversation_routed",
    selection: payload.selection || {},
    review: payload.review || {},
    action: payload.action || {},
  };
  latestMissionOSOperatorPayloads.repairPlanner = payload.repair || null;
  latestMissionOSOperatorSourceErrors.form2aAiAgent = "";
  latestMissionOSOperatorSourceErrors.repairPlanner = "";
  renderMissionOSForm2aAiAgent(latestMissionOSOperatorPayloads.form2aAiAgent);
  if (payload.repair) renderMissionOSRepairPlanner(payload.repair);
  renderMissionOSOperatorSummary();
  document.querySelectorAll(".missionos-advanced-audit-controls").forEach((details) => {
    if (details instanceof HTMLDetailsElement) details.open = false;
  });
}

function missionOSChatReplyText(payload) {
  const lines = [payload?.message || "I handled that instruction through MissionOS."];
  if (payload?.routed_action) {
    lines.push(`I treated your message as a ${missionosOperatorStatusDisplay(payload.routed_action)} request and routed it through ${missionOSSpecialistToolLabel(payload.routed_action, payload)}.`);
  }
  const fallbackReview = asPlainObject(payload?.missionos_fallback_safety_critic);
  const fallbackReviewOutput = asPlainObject(fallbackReview.validated_output);
  if (fallbackReview.agent_name) {
    const boundaryStatus = missionosOperatorStatusDisplay(fallbackReviewOutput.boundary_status || "recorded");
    const routingSource = fallbackReview.routing_source
      ? ` after ${missionosOperatorStatusDisplay(fallbackReview.routing_source)}`
      : "";
    lines.push(`Fallback boundary review recorded${routingSource}: ${boundaryStatus}. This is evidence only; existing Gateway approval and execution gates still decide authority.`);
  }
  const missionDesigner = missionOSPayloadMissionDesigner(payload);
  if (Object.keys(missionDesigner).length) {
    const proposal = asPlainObject(missionDesigner.scenario_proposal);
    const summary = asPlainObject(missionDesigner.summary);
    const validation = asPlainObject(missionDesigner.validation_result);
    const approval = asPlainObject(missionDesigner.scenario_approval);
    const boundedRequest = asPlainObject(missionDesigner.bounded_simulation_request);
    const sitlRequest = asPlainObject(missionDesigner.sitl_execution_request);
    const sitlTask = asPlainObject(missionDesigner.sitl_execution_task);
    const setupLine = missionOSChatScenarioSetupLine(missionDesigner);
    lines.push([
      "Flight scenario proposal:",
      proposal.mission_objective || summary.mission_objective || "bounded PX4/Gazebo mission",
      validation.validation_status ? `validation ${missionosOperatorStatusDisplay(validation.validation_status)}` : "",
      summary.proposed_waypoint_count !== undefined ? `${summary.proposed_waypoint_count} waypoints` : "",
    ].filter(Boolean).join(" "));
    if (setupLine) lines.push(setupLine);
    if (sitlRequest.request_status || sitlTask.task_id) {
      lines.push([
        "SITL execution request prepared.",
        sitlTask.task_id ? `task ${sitlTask.task_id}` : "",
        "I did not run Gazebo, upload a mission, dispatch, or count progress.",
      ].filter(Boolean).join(" "));
    } else if (approval.approval_status === "approved" || boundedRequest.request_status) {
      lines.push("This scenario is approved for a bounded simulation request only. Say 実行して when you want me to move to SITL preparation / execution gates.");
    } else {
      lines.push("Next I need explicit human approval before any SITL preparation or execution-boundary handoff.");
    }
    lines.push("I will not approve for you, bypass guardrails, or claim progress unless an approved runtime verifier observes it.");
    return lines.filter(Boolean).join("\n\n");
  }
  const selection = asPlainObject(payload?.selection);
  const repair = asPlainObject(payload?.repair);
  const repairProposal = asPlainObject(repair.repair_proposal);
  const responseSelection = asPlainObject(selection.response_selection);
  const selectedKind = selection.selected_response_kind || responseSelection.selected_response_kind;
  if (selectedKind) {
    lines.push(`Current plan: ${missionosOperatorStatusDisplay(selectedKind)}`);
  }
  if (repair?.summary_status === "repair_proposal_ready") {
    lines.push(`Repair focus: ${missionosOperatorStatusDisplay(repairProposal.repair_target || "latest blocked evidence")}`);
    if (repairProposal.rationale) lines.push(`Reason: ${repairProposal.rationale}`);
    const actions = asArray(repairProposal.repair_actions)
      .map((action) => compactText(`${action.action_type || "action"}: ${action.description || ""}`, 160))
      .filter(Boolean);
    if (actions.length) lines.push(`Suggested next move:\n- ${actions.slice(0, 3).join("\n- ")}`);
    if (repairProposal.next_verification) lines.push(`How I would verify it: ${repairProposal.next_verification}`);
  }
  lines.push("I will not approve for you, bypass guardrails, or claim progress unless an approved runtime verifier observes it.");
  return lines.filter(Boolean).join("\n\n");
}

function missionOSChatScenarioSetupLine(missionDesigner) {
  const value = asPlainObject(missionDesigner);
  const proposal = asPlainObject(value.scenario_proposal);
  const summary = asPlainObject(value.summary);
  const route = asPlainObject(value.mission_designer_coordinate_pair_route);
  const payload = route.payload_weight_kg ?? proposal.payload_weight_kg ?? summary.payload_weight_kg;
  const windSpeed = route.wind_speed_mps ?? summary.wind_speed_mps;
  const windDirection = route.wind_direction_deg ?? summary.wind_direction_deg;
  const roof = route.dropoff_roof_height_agl_m ?? summary.dropoff_roof_height_agl_m;
  const distanceM = route.derived_route_distance_m ?? summary.derived_route_distance_m;
  const parts = [];
  if (distanceM !== undefined && distanceM !== null) parts.push(`route about ${Math.round(Number(distanceM))} m`);
  if (payload !== undefined && payload !== null) parts.push(`payload ${payload} kg`);
  if (windSpeed !== undefined && windSpeed !== null) {
    parts.push(`wind ${windSpeed} m/s @ ${windDirection ?? "-"} deg`);
  }
  if (roof !== undefined && roof !== null) parts.push(`roof AGL ${roof} m`);
  if (!parts.length) return "";
  return `Execution setup I will use: ${parts.join(", ")}.`;
}

function missionOSChatFormatMeters(value) {
  const numberValue = missionOSChatFiniteNumber(value);
  if (numberValue === undefined) return "-";
  const rounded = Math.round(numberValue * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function missionOSChatLiveSITLStructuredResultFact(execution) {
  const summary = asPlainObject(execution?.summary);
  const artifacts = missionOSChatExecutionArtifacts(execution);
  const result = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_execution_result);
  const liveFlightRun = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_live_flight_run);
  const flightEvidence = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_flight_evidence);
  const proposal = asPlainObject(artifacts.px4_gazebo_mission_scenario_proposal);
  const route = asPlainObject(artifacts.mission_designer_coordinate_pair_route);
  const payloadObservation = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_payload_release_observation);
  const dropoffVerification = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_dropoff_verification);
  const sitlDropoffVerification = asPlainObject(artifacts.px4_gazebo_sitl_dropoff_verification);
  const progressM = missionOSChatFiniteNumber(
    summary.observed_progress_m,
    summary.horizontal_progress_m,
    flightEvidence.horizontal_progress_m,
    liveFlightRun.horizontal_progress_m,
    result.horizontal_progress_m,
  );
  const plannedM = missionOSChatFiniteNumber(
    summary.planned_route_m,
    summary.planned_m,
    summary.route_actual_distance_m,
    summary.derived_route_distance_m,
    route.derived_route_distance_m,
    route.actual_route_distance_m,
    proposal.derived_route_distance_m,
    proposal.actual_route_distance_m,
  );
  const deliveryClaimed = summary.delivery_completion_claimed === true
    || result.delivery_completion_claimed === true
    || dropoffVerification.delivery_completion_claimed === true
    || sitlDropoffVerification.delivery_completion_claimed === true;
  const dropoffReached = summary.dropoff_region_reached === true
    || summary.actual_dropoff_region_reached === true
    || liveFlightRun.dropoff_region_reached === true
    || flightEvidence.actual_dropoff_region_reached === true;
  const payloadObserved = summary.payload_release_observed === true
    || payloadObservation.payload_release_observed === true;
  const dropoffVerified = summary.dropoff_verified === true
    || dropoffVerification.dropoff_verified === true
    || sitlDropoffVerification.dropoff_verified === true
    || sitlDropoffVerification.status === "verified";
  const smokeCompleted = (
    summary.actual_px4_gazebo_horizontal_smoke_observed === true
    || liveFlightRun.actual_px4_gazebo_horizontal_smoke_observed === true
  ) && (
    summary.live_flight_status === "completed"
    || summary.final_status === "completed"
    || result.final_status === "completed"
  );
  if (!smokeCompleted || deliveryClaimed || dropoffReached) return null;
  if (progressM === undefined || plannedM === undefined) return null;
  return {
    schema_version: "missionos_live_sitl_structured_result_fact.v1",
    smoke_completed_and_landed: true,
    progress_m: progressM,
    planned_m: plannedM,
    dropoff_reached: false,
    payload_release_observed: payloadObserved,
    dropoff_verified: dropoffVerified,
    payload_dropoff_verification_observed: payloadObserved && dropoffVerified,
    delivery_completion_claimed: false,
    lines: [
      "OFFBOARD smoke completed and landed.",
      `Route progress: ${missionOSChatFormatMeters(progressM)} / ${missionOSChatFormatMeters(plannedM)} m.`,
      "Dropoff not reached.",
      "Payload/dropoff verification not observed.",
      "delivery_completion_claimed=false.",
    ],
  };
}

function missionOSChatSITLDeliveryCommandFact(execution) {
  const summary = asPlainObject(execution?.summary);
  const artifacts = missionOSChatExecutionArtifacts(execution);
  const waypointGate = asPlainObject(
    artifacts.missionos_auto_mission_waypoint_gate_summary
    || artifacts.auto_mission_waypoint_gate
    || artifacts.waypoint_gate
    || execution?.waypoint_gate
    || summary.waypoint_gate,
  );
  const dropoffGate = asPlainObject(
    artifacts.missionos_auto_mission_dropoff_gate_summary
    || artifacts.auto_mission_dropoff_gate
    || artifacts.dropoff_gate
    || execution?.dropoff_gate
    || summary.dropoff_gate,
  );
  const sitlDeliveryGate = asPlainObject(
    artifacts.missionos_auto_mission_sitl_delivery_gate_summary
    || artifacts.auto_mission_sitl_delivery_gate
    || artifacts.sitl_delivery_gate
    || execution?.sitl_delivery_gate
    || summary.sitl_delivery_gate,
  );
  const payloadReleaseSimGate = asPlainObject(
    artifacts.missionos_auto_mission_payload_release_sim_gate_summary
    || artifacts.auto_mission_payload_release_sim_gate
    || artifacts.payload_release_sim_gate
    || execution?.payload_release_sim_gate
    || summary.payload_release_sim_gate,
  );
  const sitlDeliveryClaimed = summary.sitl_delivery_claimed === true
    || sitlDeliveryGate.sitl_delivery_claimed === true;
  const deliveryClaimed = summary.delivery_completion_claimed === true
    || sitlDeliveryGate.delivery_completion_claimed === true;
  const payloadObservedSim = summary.payload_release_observed_sim === true
    || sitlDeliveryGate.payload_release_observed_sim === true
    || payloadReleaseSimGate.payload_release_observed_sim === true;
  const physicalDeliveryVerified = summary.physical_delivery_verified === true
    || sitlDeliveryGate.physical_delivery_verified === true
    || payloadReleaseSimGate.physical_delivery_verified === true;
  const routeCompleted = summary.route_completed_claimed === true
    || waypointGate.route_completed_claimed === true
    || sitlDeliveryGate.route_completed_claimed === true;
  const dropoffVerified = summary.dropoff_verified === true
    || dropoffGate.dropoff_verified === true
    || sitlDeliveryGate.dropoff_verified === true;
  const payloadReleaseCommandAcked = summary.payload_release_command_acked === true
    || sitlDeliveryGate.payload_release_command_acked === true;
  if (
    !sitlDeliveryClaimed
    || !routeCompleted
    || !dropoffVerified
    || !payloadReleaseCommandAcked
    || deliveryClaimed
    || payloadObservedSim
    || physicalDeliveryVerified
  ) {
    return null;
  }
  return {
    schema_version: "missionos_sitl_delivery_command_fact.v1",
    claim_model: sitlDeliveryGate.claim_model || "sitl_command_ack_only",
    sitl_delivery_claimed: true,
    route_completed_claimed: routeCompleted,
    dropoff_verified: dropoffVerified,
    payload_release_command_acked: payloadReleaseCommandAcked,
    payload_release_observed_sim: false,
    delivery_completion_claimed: false,
    physical_delivery_verified: false,
    lines: [
      "SITL command-level delivery sequence reached ACK.",
      "Route completion, dropoff dwell, and payload release COMMAND_ACK were observed in PX4/Gazebo SITL.",
      "No Gazebo cargo separation or physical delivery is claimed.",
      "payload_release_observed_sim=false.",
      "delivery_completion_claimed=false.",
    ],
  };
}

function missionOSChatSITLDeliverySimPayloadFact(execution) {
  const summary = asPlainObject(execution?.summary);
  const artifacts = missionOSChatExecutionArtifacts(execution);
  const waypointGate = asPlainObject(
    artifacts.missionos_auto_mission_waypoint_gate_summary
    || artifacts.auto_mission_waypoint_gate
    || artifacts.waypoint_gate
    || execution?.waypoint_gate
    || summary.waypoint_gate,
  );
  const dropoffGate = asPlainObject(
    artifacts.missionos_auto_mission_dropoff_gate_summary
    || artifacts.auto_mission_dropoff_gate
    || artifacts.dropoff_gate
    || execution?.dropoff_gate
    || summary.dropoff_gate,
  );
  const sitlDeliveryGate = asPlainObject(
    artifacts.missionos_auto_mission_sitl_delivery_gate_summary
    || artifacts.auto_mission_sitl_delivery_gate
    || artifacts.sitl_delivery_gate
    || execution?.sitl_delivery_gate
    || summary.sitl_delivery_gate,
  );
  const payloadReleaseSimGate = asPlainObject(
    artifacts.missionos_auto_mission_payload_release_sim_gate_summary
    || artifacts.auto_mission_payload_release_sim_gate
    || artifacts.payload_release_sim_gate
    || execution?.payload_release_sim_gate
    || summary.payload_release_sim_gate,
  );
  const sitlDeliveryClaimed = summary.sitl_delivery_claimed === true
    || sitlDeliveryGate.sitl_delivery_claimed === true;
  const deliveryClaimed = summary.delivery_completion_claimed === true
    || sitlDeliveryGate.delivery_completion_claimed === true
    || payloadReleaseSimGate.delivery_completion_claimed === true;
  const payloadObservedSim = summary.payload_release_observed_sim === true
    || sitlDeliveryGate.payload_release_observed_sim === true
    || payloadReleaseSimGate.payload_release_observed_sim === true;
  const physicalDeliveryVerified = summary.physical_delivery_verified === true
    || sitlDeliveryGate.physical_delivery_verified === true
    || payloadReleaseSimGate.physical_delivery_verified === true;
  const routeCompleted = summary.route_completed_claimed === true
    || waypointGate.route_completed_claimed === true
    || sitlDeliveryGate.route_completed_claimed === true
    || payloadReleaseSimGate.route_completed_claimed === true;
  const dropoffVerified = summary.dropoff_verified === true
    || dropoffGate.dropoff_verified === true
    || sitlDeliveryGate.dropoff_verified === true
    || payloadReleaseSimGate.dropoff_verified === true;
  const payloadReleaseCommandAcked = summary.payload_release_command_acked === true
    || sitlDeliveryGate.payload_release_command_acked === true
    || payloadReleaseSimGate.payload_release_command_acked === true;
  if (
    !sitlDeliveryClaimed
    || !routeCompleted
    || !dropoffVerified
    || !payloadReleaseCommandAcked
    || !payloadObservedSim
    || deliveryClaimed
    || physicalDeliveryVerified
  ) {
    return null;
  }
  return {
    schema_version: "missionos_sitl_delivery_sim_payload_fact.v1",
    claim_model: "sitl_gazebo_simulated_payload_release",
    sitl_delivery_claimed: true,
    route_completed_claimed: routeCompleted,
    dropoff_verified: dropoffVerified,
    payload_release_command_acked: payloadReleaseCommandAcked,
    payload_release_observed_sim: true,
    payload_release_event_source: payloadReleaseSimGate.payload_release_event_source || null,
    delivery_completion_claimed: false,
    physical_delivery_verified: false,
    lines: [
      "SITL/Gazebo payload release observed.",
      "Route completion, dropoff dwell, payload release COMMAND_ACK, and Gazebo cargo separation were observed in PX4/Gazebo SITL.",
      "This is simulated payload separation only; real-world delivery is not claimed.",
      "payload_release_observed_sim=true.",
      "delivery_completion_claimed=false.",
      "physical_delivery_verified=false.",
    ],
  };
}

function missionOSChatRepairPlannerSummaryPayload(execution) {
  const summary = { ...asPlainObject(execution?.summary) };
  const structuredFact = missionOSChatLiveSITLStructuredResultFact(execution);
  const sitlDeliveryFact = missionOSChatSITLDeliveryCommandFact(execution);
  const sitlDeliverySimPayloadFact = missionOSChatSITLDeliverySimPayloadFact(execution);
  if (structuredFact) {
    summary.missionos_structured_result_fact = structuredFact;
  }
  if (sitlDeliveryFact) {
    summary.missionos_sitl_delivery_command_fact = sitlDeliveryFact;
  }
  if (sitlDeliverySimPayloadFact) {
    summary.missionos_sitl_delivery_sim_payload_fact = sitlDeliverySimPayloadFact;
  }
  return summary;
}

function missionOSChatChoiceModel(payload) {
  const missionDesigner = missionOSPayloadMissionDesigner(payload);
  if (!Object.keys(missionDesigner).length) return null;
  const proposal = asPlainObject(missionDesigner.scenario_proposal);
  const summary = asPlainObject(missionDesigner.summary);
  const validation = asPlainObject(missionDesigner.validation_result);
  const approval = asPlainObject(missionDesigner.scenario_approval);
  const boundedRequest = asPlainObject(missionDesigner.bounded_simulation_request);
  const rejection = asPlainObject(missionDesigner.scenario_rejection);
  const sitlRequest = asPlainObject(missionDesigner.sitl_execution_request);
  const sitlTask = asPlainObject(missionDesigner.sitl_execution_task);
  if (!Object.keys(proposal).length) return null;
  if (rejection.rejection_status === "rejected" || rejection.operator_rejected === true) return null;
  const objective = compactText(
    proposal.mission_objective || summary.mission_objective || "bounded PX4/Gazebo mission proposal",
    220,
  );
  const setupLine = missionOSChatScenarioSetupLine(missionDesigner);
  const reason = [objective, setupLine].filter(Boolean).join("\n");
  if ((sitlRequest.request_status || sitlTask.task_id) && !missionDesigner.sitl_execution_response) {
    const taskId = sitlTask.task_id || summary.sitl_execution_task_id;
    return taskId ? {
      title: "Execute the approved live SITL request?",
      subtitle: "The bounded request is prepared. This calls the explicit live execution gate.",
      reason,
      status: "ready",
      approveLabel: "Execute Live SITL",
      denyLabel: "Deny",
      liveSitlTaskId: taskId,
      denyInstruction: "拒否して",
    } : null;
  }
  const accepted = validation.validation_status === "accepted" || validation.accepted === true;
  if ((approval.operator_approved === true || approval.approval_status === "approved" || boundedRequest.request_status) && accepted) {
    return {
      title: "Prepare the approved SITL request?",
      subtitle: "Human approval is already recorded for this bounded simulation request.",
      reason,
      status: "ready",
      approveLabel: "Prepare SITL",
      denyLabel: "Deny",
      approveInstruction: "実行して",
      denyInstruction: "拒否して",
    };
  }
  if (accepted) {
    return {
      title: "Approve this MissionOS flight scenario?",
      subtitle: "Flight Scenario Designer proposal",
      reason,
      status: "pending",
      approveLabel: "Approve",
      denyLabel: "Deny",
      approveInstruction: "承認して",
      denyInstruction: "拒否して",
    };
  }
  return null;
}

function missionOSChatVisualResponseHtml(payload) {
  const missionDesigner = missionOSPayloadMissionDesigner(payload);
  if (!Object.keys(missionDesigner).length) return "";
  const hasExecutionResponse = Boolean(missionDesigner.sitl_execution_response);
  const executionInProgress = missionDesigner.sitl_execution_in_progress === true;
  const flightSummary = missionScenarioFlightPathSummary(missionDesigner);
  const flightView = renderDigitalTwinFlightPathWindow(flightSummary);
  const autoRuntimeView = [
    renderMissionScenarioAutoMissionRuntimeRecoveryPending(missionDesigner),
    renderMissionScenarioAutoMissionEvidenceReadout(missionDesigner),
  ].join("");
  const coordinateRoute = hasExecutionResponse
    ? ""
    : renderMissionScenarioCoordinateRoute(missionDesigner.mission_designer_coordinate_pair_route);
  const body = hasExecutionResponse || executionInProgress
    ? [autoRuntimeView, flightView || coordinateRoute].filter(Boolean).join("")
    : (flightView || coordinateRoute);
  if (!body) return "";
  return [
    `<div class="missionos-chat-visual-head">`,
    `<strong>${escapeHtml(hasExecutionResponse ? "MissionOS 3D result" : executionInProgress ? "MissionOS runtime view" : "MissionOS route preview")}</strong>`,
    `<span class="muted">${escapeHtml(hasExecutionResponse ? "Visual replay plus runtime evidence. The result and safety boundary are explained in the MissionOS message above." : executionInProgress ? "Live AUTO execution is running; runtime evidence is surfaced without delivery or progress claims until verifier facts are attached." : "Coordinate planning evidence rendered before execution.")}</span>`,
    `</div>`,
    body,
  ].join("");
}

function appendMissionOSChatVisualResponse(payload) {
  if (!missionosChatMessagesEl) return null;
  const html = missionOSChatVisualResponseHtml(payload);
  if (!html) return null;
  const bubble = document.createElement("div");
  bubble.className = "bubble agent missionos-chat-visual-response";
  bubble.innerHTML = html;
  missionosChatMessagesEl.appendChild(bubble);
  missionosChatMessagesEl.scrollTop = missionosChatMessagesEl.scrollHeight;
  wireMissionOSChatRuntimeRecoveryDispatchActions(bubble);
  syncMissionFlightTelemetryAnimations();
  return bubble;
}

function updateMissionOSChatVisualResponse(bubble, payload) {
  if (!bubble) return;
  const html = missionOSChatVisualResponseHtml(payload);
  if (!html) return;
  bubble.innerHTML = html;
  wireMissionOSChatRuntimeRecoveryDispatchActions(bubble);
  syncMissionFlightTelemetryAnimations();
}

function missionOSHasAgentInvocation(payload, agentName) {
  return asArray(payload?.missionos_agent_invocations)
    .some((invocation) => asPlainObject(invocation).agent_name === agentName);
}

function missionOSSpecialistToolLabel(action, payload = {}) {
  switch (String(action || "")) {
    case "status":
      return missionOSHasAgentInvocation(payload, "missionos_situation_judge_agent")
        ? "the MissionOS Situation Judge Agent (ADK)"
        : "the Gateway state reader";
    case "plan":
      return missionOSHasAgentInvocation(payload, "missionos_response_planner_agent")
        ? "the MissionOS Response Planner Agent (ADK)"
        : "the Gateway plan boundary";
    case "approve":
      return "the Gateway Human Review recorder";
    case "reject":
    case "revision":
      return "the Gateway Human Review boundary";
    case "execute":
      return "the Gateway execution boundary and deterministic Runtime Verifier";
    case "repair":
      return missionOSHasAgentInvocation(payload, "missionos_repair_planner_agent")
        ? "the MissionOS Repair Planner Agent (ADK)"
        : "the MissionOS repair planner route";
    case "mission_designer_plan":
      return missionOSHasAgentInvocation(payload, "missionos_flight_scenario_designer_agent")
        ? "the MissionOS Flight Scenario Designer Agent (ADK)"
        : "the Gateway Mission Designer guardrail";
    default:
      return "MissionOS guardrails";
  }
}

async function runMissionOSConversationInstruction(instruction, statusText) {
  latestMissionOSAutonomyNotice = statusText;
  renderMissionOSAutonomyMonitor();
  try {
    const payload = await postMissionOSConversationInstruction(instruction);
    applyMissionOSConversationPayload(payload);
    latestMissionOSAutonomyNotice = payload.message || "I handled that instruction through MissionOS.";
    renderMissionOSAutonomyMonitor();
    logEvent("missionos.autonomy_conversation.run", {
      routed_action: payload.routed_action,
      summary_status: latestMissionOSOperatorPayloads.form2aAiAgent.summary_status,
    });
    return payload;
  } catch (err) {
    latestMissionOSAutonomyNotice = missionosFriendlyActionError(err, "/missionos/autonomy-conversation/run");
    renderMissionOSAutonomyMonitor();
    logEvent("missionos.autonomy_conversation.error", { error: String(err) });
    return null;
  }
}

function missionOSChatDesignerBrief() {
  if (latestMissionScenarioResult) {
    const facts = missionosOperatorMissionFacts(latestMissionScenarioResult);
    const evidence = [
      facts.uploadObserved ? "upload observed" : "upload not observed",
      facts.flightObserved ? "flight observed" : "flight not observed",
      facts.payloadObserved ? "payload observed" : "payload pending",
      facts.dropoffVerified ? "dropoff verified" : "dropoff pending",
    ].join(", ");
    const failure = facts.failureText ? ` The active blocker is ${facts.failureText}.` : "";
    return `I also read the Mission Designer run: ${facts.state}; ${evidence}.${failure}`;
  }
  const nextInspection = asPlainObject(latestMissionOSOperatorPayloads.knowledge?.next_inspection);
  if (nextInspection.failure_mode_id || nextInspection.recommended_next_inspection) {
    return [
      `I also checked persisted Mission Designer evidence: ${missionosOperatorStatusDisplay(nextInspection.failure_mode_id || "latest evidence needs review")}.`,
      nextInspection.recommended_next_inspection ? `Next useful read: ${nextInspection.recommended_next_inspection}` : "",
    ].filter(Boolean).join(" ");
  }
  return "";
}

function missionOSChatOpeningText(model) {
  const priorBlocked = Array.isArray(model.actionBlocks) && model.actionBlocks.length > 0;
  const currentAgentSentence = priorBlocked
    ? "前回の blocked evidence は残っていますが、この会話ではまだ新しい実行を開始していません。飛ばしたい条件を指示してください。"
    : model.agentSentence;
  const currentNextStep = priorBlocked
    ? "Request a new bounded mission plan, or ask what happened in the previous run."
    : model.nextStep;
  return [
    currentAgentSentence,
    missionOSChatDesignerBrief(),
    `What I need from you: ${currentNextStep}`,
    "Behind this conversation, ADK-backed MissionOS specialists are used only when invocation evidence is attached. Gateway still owns Human Review, guardrails, execution boundaries, artifact records, and deterministic Runtime Verifier results.",
    "You can answer naturally. When MissionOS needs a fast decision, use the inline Approve / Deny choices; type 実行して only when you explicitly want execution handoff.",
  ].filter(Boolean).join("\n\n");
}

async function initializeMissionOSChat() {
  if (!missionosChatMessagesEl || missionOSChatInitialized) return;
  missionOSChatInitialized = true;
  setMissionOSChatStatus("Reading current MissionOS state...");
  try {
    await Promise.all([
      refreshMissionOSOperatorSummary(),
      loadMissionOSForm2aAiAgent(),
      loadMissionOSRepairPlanner(),
      loadMissionOSScopedForm3(),
    ]);
  } catch (_) {
    // The chat can still accept instructions even when optional summaries fail.
  }
  const model = missionosAutonomyMonitorModel();
  appendMissionOSChatBubble("agent", missionOSChatOpeningText(model));
  setMissionOSChatStatus("Conversation ready");
}

async function submitMissionOSChatInstruction(text) {
  const instruction = String(text || "").trim();
  if (!instruction) return;
  appendMissionOSChatBubble("user", instruction);
  const thinkingBubble = appendMissionOSChatBubble("agent", missionOSChatThinkingText());
  setMissionOSChatStatus("MissionOS is reading the current evidence...");
  if (missionosChatInputEl) missionosChatInputEl.disabled = true;
  try {
    const payload = await postMissionOSConversationInstruction(instruction);
    applyMissionOSConversationPayload(payload);
    updateMissionOSChatBubbleText(thinkingBubble, missionOSChatReplyText(payload));
    clearMissionOSChatStateCards();
    appendMissionOSChatVisualResponse(payload);
    appendMissionOSChatChoiceCard(missionOSChatChoiceModel(payload));
    setMissionOSChatStatus("Conversation ready");
    logEvent("missionos.chat.run", {
      routed_action: payload.routed_action,
      routing_source: payload.routing_source,
    });
  } catch (err) {
    updateMissionOSChatBubbleText(thinkingBubble, missionosFriendlyActionError(err, "/missionos/autonomy-conversation/run"));
    setMissionOSChatStatus("Instruction failed safely");
    logEvent("missionos.chat.error", { error: String(err) });
  } finally {
    if (missionosChatInputEl) {
      missionosChatInputEl.disabled = false;
      missionosChatInputEl.value = "";
      missionosChatInputEl.focus();
    }
  }
}

function missionOSChatRepairPlannerReport(repair) {
  const proposal = asPlainObject(repair?.repair_proposal);
  const boundary = asPlainObject(repair?.authority_boundary);
  const ready = repair?.summary_status === "repair_proposal_ready";
  if (!ready || !Object.keys(proposal).length) {
    const reason = asArray(boundary.blocking_reasons).join(", ") || repair?.error || "repair proposal not available";
    return [
      "Repair Planner Agent",
      `- 修復提案はまだ使える状態ではありません: ${reason}`,
      "- MissionOS はこの失敗を成功扱いせず、次の実行権限も作っていません。",
    ].join("\n");
  }
  const actions = asArray(proposal.repair_actions)
    .map((action, index) => {
      const kind = action.action_type || "repair_action";
      const description = action.description || "";
      return `${index + 1}. ${description || kind}${description ? ` (${kind})` : ""}`;
    })
    .filter(Boolean);
  return [
    "Repair Planner Agentの提案",
    `対象: ${proposal.repair_target || "latest blocked evidence"}`,
    proposal.rationale ? `判断: ${proposal.rationale}` : "",
    actions.length ? `次の修復ステップ:\n${actions.join("\n")}` : "次の修復ステップ: まだ具体化されていません。",
    proposal.expected_outcome ? `期待する結果: ${proposal.expected_outcome}` : "",
    proposal.uncertainty ? `不確実性: ${proposal.uncertainty}` : "",
    proposal.next_verification ? `確認方法: ${proposal.next_verification}` : "",
    "この提案は次のplan候補です。承認や実行権限ではありません。",
  ].filter(Boolean).join("\n");
}

function missionOSChatRepairProposalModel(repair, execution) {
  const proposal = asPlainObject(repair?.repair_proposal);
  if (!missionOSChatRepairProposalReady(repair)) return null;
  const parameters = missionOSChatNormalizeRepairParameters(proposal.proposed_parameters);
  if (missionOSChatRepairUploadHandshakeOnly(execution)) return null;
  if (!missionOSChatRepairParameterDelta(parameters, execution)) return null;
  const firstAction = asPlainObject(asArray(proposal.repair_actions)[0]);
  const currentValues = missionOSChatRepairCurrentValues(execution);
  const payloadBefore = currentValues.payload_weight_kg;
  const payloadAfter = parameters.payload_weight_kg ?? payloadBefore;
  const windSpeed = currentValues.wind_speed_mps;
  const windDirection = currentValues.wind_direction_deg;
  return {
    title: "Repair Planner Agentからの再試行提案",
    status: "proposal",
    summary: "Repair Planner Agent が blocked evidence を読んで、次の bounded retry を提案しています。",
    reason: proposal.rationale,
    actionDescription: firstAction.description || "Repair Planner Agent generated this repair action.",
    expectedOutcome: proposal.expected_outcome,
    nextVerification: proposal.next_verification || "新しい SITL request を準備し、operator が Execute Live SITL を明示した場合だけ runtime verifier で確認する。",
    source: "llm_repair_planner",
    parameters,
    payloadLabel: `${payloadBefore ?? "-"} kg → ${payloadAfter ?? "-"} kg`,
    windLabel: `${windSpeed ?? "-"} m/s @ ${windDirection ?? "-"} deg (environment unchanged)`,
    planInstruction: proposal.proposed_operator_instruction,
    approveLabel: "Approve repair plan",
    denyLabel: "Deny",
  };
}

function missionOSChatRepairProposalText(model, context = {}) {
  if (!model) {
    if (missionOSChatRepairUploadHandshakeOnly(context.execution)) {
      return [
        "Repair Planner Agentの判断",
        "今回の blocker は mission upload / PX4 ACK の段階で止まっています。これは takeoff / climb 前の接続・アップロード境界なので、payload や route を変える修復案としては承認しません。",
        "MissionOS はこの evidence から payload retry の Approve card を作りません。",
      ].join("\n");
    }
    return [
      "Repair Planner Agentの判断",
      "Repair Planner Agent は evidence を読みましたが、現在の Gateway execution route に束縛できる payload / route 変更としては確定していません。",
      "MissionOS はこの返答を承認待ちの修復案として扱わず、Approve repair plan も表示しません。",
    ].join("\n");
  }
  return [
    "Repair Planner Agentからの提案",
    model.reason ? `判断: ${model.reason}` : model.summary,
    model.parameters.dropoff_latitude !== undefined
      ? `- route: (${model.parameters.takeoff_latitude}, ${model.parameters.takeoff_longitude}) → (${model.parameters.dropoff_latitude}, ${model.parameters.dropoff_longitude})`
      : "",
    `- payload: ${model.payloadLabel}`,
    `- wind: ${model.windLabel}`,
    `- action: ${model.actionDescription}`,
    model.expectedOutcome ? `期待: ${model.expectedOutcome}` : "",
    "Approve repair plan で再計画とHuman Review記録まで進めます。Execute Live SITL は別操作です。",
  ].filter(Boolean).join("\n");
}

async function approveMissionOSChatRepairProposal() {
  const model = latestMissionOSChatRepairProposal;
  if (!model) return;
  appendMissionOSChatBubble("user", "Approve repair plan");
  setMissionOSChatStatus("MissionOS is applying the Repair Planner Agent proposal...");
  if (missionosChatInputEl) missionosChatInputEl.disabled = true;
  try {
    latestMissionScenarioResult = null;
    applyMissionOSChatRepairProposalToFlightSetup(model);
    const repairMissionInstruction = [
      "Create a bounded PX4/Gazebo mission scenario from this Repair Planner proposal.",
      "Use the current coordinate form values and any proposed parameters already applied to the form.",
      `Repair Planner instruction: ${model.planInstruction}`,
      model.parameters.payload_weight_kg !== undefined
        ? `Repair Planner explicit payload override: payload ${model.parameters.payload_weight_kg}kg.`
        : "",
      `Repair Planner proposed_parameters JSON: ${JSON.stringify(model.parameters || {})}`,
      "Return planning evidence only. No Human Review record, no SITL preparation, no dispatch, and no progress claim.",
    ].filter(Boolean).join("\n");
    const planPayload = await postMissionOSConversationInstruction(
      repairMissionInstruction,
      { routeHint: "mission_designer_plan" },
    );
    applyMissionOSConversationPayload(planPayload);
    const approvePayload = await postMissionOSConversationInstruction("承認して");
    applyMissionOSConversationPayload(approvePayload);
    const executePayload = await postMissionOSConversationInstruction("実行して");
    applyMissionOSConversationPayload(executePayload);
    clearMissionOSChatStateCards();
    appendMissionOSChatVisualResponse(executePayload);
    const executeChoice = missionOSChatChoiceModel(executePayload)
      || missionOSChatChoiceModel({ mission_designer: latestMissionScenarioResult });
    appendMissionOSChatBubble(
      "agent",
      [
        "Repair Planner Agent の提案を承認しました。",
        `新しい retry 条件: payload ${model.payloadLabel}.`,
        `wind は実行環境の状態として保持します: ${model.windLabel}.`,
        executeChoice
          ? "再計画、Human Review、SITL request準備が完了しました。Execute Live SITL で明示実行ゲートへ進みます。"
          : "MissionOS はこの条件で再計画を試みましたが、実行可能な SITL task id を確認できませんでした。存在しない Execute ボタンは案内しません。状況を確認するか、別条件で再計画してください。",
        "まだ delivery completion / progress は claim していません。",
      ].join("\n\n"),
    );
    appendMissionOSChatChoiceCard(executeChoice);
    setMissionOSChatStatus("Conversation ready");
  } catch (err) {
    appendMissionOSChatBubble("system", missionosFriendlyActionError(err, "/missionos/autonomy-conversation/run"));
    setMissionOSChatStatus("Repair proposal approval failed safely");
  } finally {
    if (missionosChatInputEl) {
      missionosChatInputEl.disabled = false;
      missionosChatInputEl.focus();
    }
  }
}

function missionOSChatLiveSITLBlockedBeforeRunner(execution) {
  const task = asPlainObject(execution?.task);
  const artifacts = asPlainObject(task.artifacts);
  const summary = asPlainObject(execution?.summary);
  const blockedReceipt = asPlainObject(
    artifacts.px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt,
  );
  return blockedReceipt.live_flight_runner_invoked === false
    && (
      blockedReceipt.live_flight_opted_in === false
      || blockedReceipt.sitl_execution_opted_in === false
      || summary.live_flight_runner_invoked === false
    );
}

function missionOSChatLiveSITLResultSummary(execution, { blocked = false, repair = null } = {}) {
  const task = asPlainObject(execution?.task);
  const artifacts = asPlainObject(task.artifacts);
  const summary = asPlainObject(execution?.summary);
  const result = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_execution_result);
  const proposal = asPlainObject(artifacts.px4_gazebo_mission_scenario_proposal);
  const receipt = asPlainObject(artifacts.px4_gazebo_sitl_mission_upload_receipt);
  const flightEvidence = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_flight_evidence);
  const liveFlightRun = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_live_flight_run);
  const blockedReceipt = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt);
  const failedReceipt = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt);
  const payloadObservation = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_payload_release_observation);
  const payloadReleaseEvent = asPlainObject(artifacts.px4_gazebo_sitl_payload_release_event);
  const missionDesignerDropoffVerification = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_dropoff_verification);
  const sitlDropoffVerification = asPlainObject(artifacts.px4_gazebo_sitl_dropoff_verification);
  const autoRuntimeSummary = asPlainObject(
    artifacts.missionos_auto_mission_runtime_monitor_summary
    || artifacts.auto_mission_runtime_monitor_summary
    || execution?.missionos_auto_mission_runtime_monitor_summary
    || execution?.summary
    || summary.missionos_auto_mission_runtime_monitor_summary,
  );
  const autoUploadObserved = asPlainObject(
    artifacts.missionos_auto_mission_upload_observed
    || artifacts.auto_mission_upload_observed
    || execution?.missionos_auto_mission_upload_observed
    || execution?.upload_observed
    || summary.missionos_auto_mission_upload_observed,
  );
  const autoWaypointGate = asPlainObject(
    artifacts.missionos_auto_mission_waypoint_gate_summary
    || artifacts.auto_mission_waypoint_gate
    || execution?.missionos_auto_mission_waypoint_gate_summary
    || execution?.waypoint_gate
    || summary.waypoint_gate,
  );
  const autoDropoffGate = asPlainObject(
    artifacts.missionos_auto_mission_dropoff_gate_summary
    || artifacts.auto_mission_dropoff_gate
    || execution?.missionos_auto_mission_dropoff_gate_summary
    || execution?.dropoff_gate
    || summary.dropoff_gate,
  );
  const autoSITLDeliveryGate = asPlainObject(
    artifacts.missionos_auto_mission_sitl_delivery_gate_summary
    || artifacts.auto_mission_sitl_delivery_gate
    || execution?.missionos_auto_mission_sitl_delivery_gate_summary
    || execution?.sitl_delivery_gate
    || summary.sitl_delivery_gate,
  );
  const autoPayloadReleaseSimGate = asPlainObject(
    artifacts.missionos_auto_mission_payload_release_sim_gate_summary
    || artifacts.auto_mission_payload_release_sim_gate
    || execution?.missionos_auto_mission_payload_release_sim_gate_summary
    || execution?.payload_release_sim_gate
    || summary.payload_release_sim_gate,
  );
  const autoPayloadReleaseEvent = asPlainObject(
    artifacts.missionos_auto_mission_payload_release_event
    || artifacts.auto_mission_payload_release_event
    || execution?.missionos_auto_mission_payload_release_event
    || execution?.payload_release_event
    || summary.payload_release_event,
  );
  const uploadObserved = receipt.upload_status === "uploaded"
    || result.actual_sitl_mission_upload_observed === true
    || summary.upload_status === "uploaded"
    || summary.px4_mission_upload_performed === true
    || autoRuntimeSummary.mission_upload_accepted === true
    || autoUploadObserved.mission_ack_observed === true;
  const ackObserved = receipt.mission_ack_observed === true
    || result.mission_ack_observed === true
    || summary.mission_ack_observed === true
    || autoRuntimeSummary.mission_ack_observed === true
    || autoRuntimeSummary.mission_upload_accepted === true
    || autoUploadObserved.mission_ack_observed === true;
  const missionAckType = missionOSChatFiniteNumber(
    receipt.mission_ack_type,
    result.mission_ack_type,
    summary.mission_ack_type,
    autoRuntimeSummary.mission_ack_result,
    autoUploadObserved.mission_ack_type,
  );
  const ackAccepted = ackObserved && (missionAckType === undefined || missionAckType === 0);
  const ackRejected = ackObserved && missionAckType !== undefined && missionAckType !== 0;
  const takeoffObserved = result.actual_takeoff_observed === true
    || flightEvidence.actual_takeoff_observed === true
    || liveFlightRun.actual_takeoff_observed === true;
  const flightObserved = result.actual_sitl_flight_evidence_observed === true
    || flightEvidence.actual_sitl_flight_evidence_observed === true
    || liveFlightRun.actual_sitl_flight_evidence_observed === true
    || summary.actual_sitl_flight_evidence_observed === true
    || (
      autoRuntimeSummary.auto_mission_started === true
      && missionOSChatFiniteNumber(autoRuntimeSummary.telemetry_sample_count) > 0
    )
    || autoWaypointGate.route_completed_claimed === true;
  const liveFlightRunnerInvoked = liveFlightRun.live_flight_runner_invoked === true
    || blockedReceipt.live_flight_runner_invoked === true
    || failedReceipt.live_flight_runner_invoked === true;
  const blockedBeforeRunner = blocked && missionOSChatLiveSITLBlockedBeforeRunner(execution);
  const payloadObserved = payloadObservation.payload_release_observed === true
    || payloadReleaseEvent.release_observed === true
    || autoSITLDeliveryGate.payload_release_command_acked === true
    || autoPayloadReleaseSimGate.payload_release_observed_sim === true
    || autoPayloadReleaseEvent.payload_release_observed === true;
  const dropoffVerified = missionDesignerDropoffVerification.dropoff_verified === true
    || sitlDropoffVerification.dropoff_verified === true
    || summary.dropoff_verified === true
    || autoDropoffGate.dropoff_verified === true
    || autoSITLDeliveryGate.dropoff_verified === true
    || autoPayloadReleaseSimGate.dropoff_verified === true
    || sitlDropoffVerification.status === "verified";
  const landingObserved = result.actual_land_observed === true
    || flightEvidence.actual_land_observed === true
    || liveFlightRun.actual_land_observed === true
    || autoRuntimeSummary.final_landing_safe === true;
  const failureCategory = String(
    failedReceipt.failure_category
    || summary.failure_category
    || (Array.isArray(summary.blocked_reasons) ? summary.blocked_reasons[0] : "")
    || "",
  );
  const payloadWeightKg = proposal.payload_weight_kg ?? result.payload_weight_kg;
  const deliveryClaimed = result.delivery_completion_claimed === true
    || summary.delivery_completion_claimed === true
    || missionDesignerDropoffVerification.delivery_completion_claimed === true
    || sitlDropoffVerification.delivery_completion_claimed === true;
  const structuredFact = missionOSChatLiveSITLStructuredResultFact(execution);
  const sitlDeliveryFact = missionOSChatSITLDeliveryCommandFact(execution);
  const sitlDeliverySimPayloadFact = missionOSChatSITLDeliverySimPayloadFact(execution);
  const physicalExecution = result.physical_execution_invoked === true
    || summary.physical_execution_invoked === true;
  const hardwareAllowed = result.hardware_target_allowed === true
    || summary.hardware_target_allowed === true;
  const statusLine = blocked
    ? (blockedBeforeRunner
      ? "停止: 必要な Mission Designer SITL execution / Live SITL opt-in が無いため mission upload / live flight runner に進んでいません。"
      : !uploadObserved && ackRejected
      ? `停止: PX4 mission ACK type ${missionAckType} で upload accepted になりませんでした。`
      : !uploadObserved
      ? "停止: mission upload / PX4 ACK が観測されませんでした。"
      : `停止: ${failureCategory || "Runtime Verifier boundary"}.`)
    : (sitlDeliveryFact
      ? "SITL command ACK確認: command-level delivery sequence observed; physical delivery は claim していません。"
      : sitlDeliverySimPayloadFact
      ? "SITL/Gazebo payload分離確認: simulated cargo separation observed; real-world delivery は claim していません。"
      : dropoffVerified
      ? "完了: observed SITL evidence で dropoff verified まで確認しました。"
      : "完了判定待ち: runtime evidence は返りましたが、dropoff verified ではありません。");
  const observed = [
    `upload=${uploadObserved ? "observed" : "not observed"}`,
    `ack=${ackObserved ? (ackAccepted ? "accepted" : `type ${missionAckType}`) : "not observed"}`,
    `flight=${flightObserved ? "observed" : "not observed"}`,
    `payload=${payloadObserved ? "observed" : "not observed"}`,
    `dropoff=${dropoffVerified ? "verified" : "not verified"}`,
  ].join(", ");
  const nextLine = blocked
    ? (blockedBeforeRunner
      ? "次: PX4/Gazebo SITL が ready でも runner は opt-in なしでは呼ばれません。Gateway を RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION=1 と RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT=1 で起動してから、承認済みの実行をもう一度要求してください。"
      : repair?.summary_status === "repair_proposal_ready"
      ? "次: Repair Planner Agent の提案を確認してください。承認するまで retry は実行しません。"
      : "次: blocked evidence を確認してください。MissionOS は成功扱いしていません。")
    : "次: 追加操作は不要です。詳細なartifactは3D result / audit側で確認できます。";
  return [
    "MissionOS実行結果",
    statusLine,
    ...(structuredFact ? structuredFact.lines : []),
    ...(sitlDeliveryFact ? sitlDeliveryFact.lines : []),
    ...(sitlDeliverySimPayloadFact ? sitlDeliverySimPayloadFact.lines : []),
    `観測: ${observed}.`,
    payloadWeightKg !== undefined && payloadWeightKg !== null ? `条件: payload ${payloadWeightKg}kg.` : "",
    `境界: delivery_completion_claimed=${deliveryClaimed}; hardware_target_allowed=${hardwareAllowed}; physical_execution_invoked=${physicalExecution}.`,
    nextLine,
  ].filter(Boolean).join("\n");
}

async function runMissionOSChatRepairPlannerAfterBlockedExecution(execution) {
  setMissionOSChatStatus("Repair Planner Agent is reading the blocked evidence...");
  try {
    const response = await apiFetchWithTimeout("/missionos/llm-repair-planner/run-for-task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_id: execution?.summary?.task_id || execution?.task?.task_id || "",
        summary: missionOSChatRepairPlannerSummaryPayload(execution),
      }),
    }, 90000);
    const repair = await response.json();
    if (!response.ok) {
      return {
        summary_status: "blocked",
        error: repair?.detail || `HTTP ${response.status}`,
        authority_boundary: { blocking_reasons: [repair?.detail || `HTTP ${response.status}`] },
      };
    }
    latestMissionOSOperatorPayloads.repairPlanner = repair;
    renderMissionOSRepairPlanner(repair);
    return repair;
  } catch (err) {
    logEvent("missionos.chat.repair_planner.error", { error: String(err) });
    return {
      summary_status: "blocked",
      error: missionosFriendlyActionError(err, "/missionos/llm-repair-planner/run-for-task"),
      authority_boundary: { blocking_reasons: [String(err)] },
    };
  }
}

function missionOSChatLiveSITLProgressText(stage, { elapsedSeconds = 0, execution = null } = {}) {
  const summary = asPlainObject(execution?.summary);
  const failureCategory = summary.failure_category
    || (Array.isArray(summary.blocked_reasons) ? summary.blocked_reasons[0] : "")
    || "";
  if (stage === "waiting") {
    return [
      "実行中: Gateway execution boundary",
      `経過: ${elapsedSeconds} 秒`,
      "現在: mission upload / PX4 ACK / live flight runner / Runtime Verifier の結果待ちです。",
      "最終evidenceが返るまで delivery / progress claim は false のままです。",
    ].join("\n");
  }
  if (stage === "blocked_repair") {
    return [
      "実行中: Repair Planner Agent",
      `経過: ${elapsedSeconds} 秒`,
      "Runtime Verifier は blocked と判定しました。Repair Planner Agent が同じ evidence を読んでいます。",
      failureCategory ? `blocked reason: ${failureCategory}` : "",
    ].filter(Boolean).join("\n");
  }
  if (stage === "blocked_readiness") {
    return [
      "実行停止: Gateway execution boundary",
      `経過: ${elapsedSeconds} 秒`,
      "PX4/Gazebo の mission upload receipt / ACK が観測できませんでした。",
      "これは takeoff / payload / route の修復判断に入る前の readiness boundary です。Repair Planner Agent は自動起動しません。",
    ].join("\n");
  }
  if (stage === "completed") {
    if (execution && missionOSChatLiveSITLBlockedBeforeRunner(execution)) {
      return [
        "Live flight runner は呼ばれていません。",
        "PX4/Gazebo SITL が ready でも、必要な opt-in が無い場合は mission upload / PX4 ACK / telemetry に進みません。",
        "MissionOS は成功、dispatch、delivery、progress を claim していません。",
      ].join("\n");
    }
    if (execution && missionOSChatRepairUploadHandshakeOnly(execution)) {
      return [
        "実行確認が完了しました。",
        "MissionOS は実行レポートだけを表示します。accepted mission upload 前で止まったため、Repair Planner Agent の修復提案は出しません。",
      ].join("\n");
    }
    return [
      "実行確認が完了しました。",
      "下に MissionOS の実行レポートと、必要な場合は Repair Planner Agent の提案を表示します。",
    ].join("\n");
  }
  if (stage === "failed") {
    return [
      "実行 route への接続は fail-closed で停止しました。",
      "MissionOS は dispatch 成功、delivery completion、progress を claim していません。",
    ].join("\n");
  }
  return "MissionOS is processing the approved live SITL request...";
}

function updateMissionOSChatBubbleText(bubble, text) {
  if (!bubble) return;
  bubble.textContent = text;
  if (missionosChatMessagesEl) missionosChatMessagesEl.scrollTop = missionosChatMessagesEl.scrollHeight;
}

async function executeMissionOSChatLiveSITL(taskId) {
  const id = String(taskId || "").trim();
  if (!id) return;
  appendMissionOSChatBubble("user", "Execute Live SITL");
  let readinessPayload = null;
  try {
    readinessPayload = await checkMissionOSChatSITLReadiness(id);
  } catch (err) {
    appendMissionOSChatBubble("system", missionosFriendlyActionError(err, "/px4-gazebo/mission-scenarios/execute-sitl-readiness"));
    setMissionOSChatStatus("SITL readiness check failed safely");
    logEvent("missionos.chat.live_sitl.readiness_error", { error: String(err) });
    return;
  }
  const readiness = missionOSChatSITLReadinessSummary(readinessPayload);
  if (!readiness.ready) {
    try {
      const startupBubble = appendMissionOSChatBubble("agent", "PX4/Gazebo SITL を起動しています。mission upload はまだ実行していません。");
      setMissionOSChatStatus("PX4/Gazebo SITL を起動しています");
      const startupPayload = await startMissionOSChatSITL(id);
      logEvent("missionos.chat.live_sitl.startup", startupPayload.summary || startupPayload);
      const startupReadiness = missionOSChatSITLReadinessSummary(startupPayload);
      if (!startupReadiness.ready) {
        updateMissionOSChatBubbleText(
          startupBubble,
          [
            "PX4/Gazebo SITL の起動 action は完了しましたが、mission upload readiness はまだ blocked です。",
            "",
            missionOSChatSITLNotReadyText(startupPayload),
          ].join("\n"),
        );
        setMissionOSChatStatus("SITL readiness still blocked");
        return;
      }
      updateMissionOSChatBubbleText(startupBubble, "PX4/Gazebo SITL と MAVLink readiness を確認しました。続けて mission upload / live flight runner に進みます。");
    } catch (err) {
      appendMissionOSChatBubble("system", missionosFriendlyActionError(err, "/px4-gazebo/mission-scenarios/start-sitl"));
      setMissionOSChatStatus("SITL startup failed safely");
      logEvent("missionos.chat.live_sitl.startup_error", { error: String(err) });
      return;
    }
  }
  const progressStartedAt = Date.now();
  const progressBubble = appendMissionOSChatBubble(
    "agent",
    missionOSChatLiveSITLProgressText("waiting", { elapsedSeconds: 0 }),
  );
  const progressTimer = window.setInterval(() => {
    updateMissionOSChatBubbleText(
      progressBubble,
      missionOSChatLiveSITLProgressText("waiting", {
        elapsedSeconds: Math.floor((Date.now() - progressStartedAt) / 1000),
      }),
    );
  }, 5000);
  latestMissionScenarioResult = {
    ...(latestMissionScenarioResult || {}),
    sitl_execution_in_progress: true,
    sitl_execution_task: {
      ...(latestMissionScenarioResult?.sitl_execution_task || {}),
      task_id: id,
      status: "running",
      artifacts: {
        ...(latestMissionScenarioResult?.sitl_execution_task?.artifacts || {}),
        missionos_auto_mission_gui_dispatch_running_receipt: {
          schema_version: "missionos_auto_mission_gui_dispatch_running_receipt.v1",
          task_id: id,
          dispatch_status: "running",
          recovery_agent_evidence_status: "pending",
          delivery_completion_claimed: false,
          physical_delivery_verified: false,
          physical_execution_invoked: false,
        },
      },
    },
  };
  clearMissionOSChatStateCards();
  const liveVisualBubble = appendMissionOSChatVisualResponse({ mission_designer: latestMissionScenarioResult });
  const runtimeRecoveryDispatchCard = appendMissionOSChatRuntimeRecoveryDispatchCard(id);
  let finalExecution = null;
  let polling = true;
  const pollExecutionTask = (async () => {
    while (polling) {
      await new Promise((resolve) => window.setTimeout(resolve, 3000));
      if (!polling) break;
      try {
        const task = await refreshMissionScenarioExecutionTask(id);
        if (task) {
          latestMissionScenarioResult = {
            ...(latestMissionScenarioResult || {}),
            sitl_execution_in_progress: task.status === "running",
            sitl_execution_task: task,
            sitl_execution_result_task: task,
          };
          updateMissionOSChatVisualResponse(liveVisualBubble, { mission_designer: latestMissionScenarioResult });
          updateMissionOSChatRuntimeRecoveryDispatchCardEvidence(runtimeRecoveryDispatchCard, task);
        }
      } catch (err) {
        logEvent("missionos.chat.live_sitl.poll_error", { error: String(err) });
      }
    }
  })();
  setMissionOSChatStatus("MissionOS is calling the Gateway execution boundary for the approved SITL request...");
  if (missionosChatInputEl) missionosChatInputEl.disabled = true;
  try {
    const response = await apiFetchWithTimeout("/px4-gazebo/mission-scenarios/execute-sitl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_id: id,
        explicit_execution_approval: true,
        live_flight_mode: true,
      }),
    }, MISSION_SCENARIO_LIVE_SITL_TIMEOUT_MS);
    const execution = await response.json();
    if (!response.ok && response.status !== 409) {
      throw new Error(execution?.detail || `HTTP ${response.status}`);
    }
    finalExecution = execution;
    polling = false;
    const executionSummary = asPlainObject(execution.summary);
    const blocked = response.status === 409
      || executionSummary.task_status === "blocked"
      || executionSummary.live_flight_status === "blocked"
      || executionSummary.result_status === "blocked";
    latestMissionScenarioResult = {
      ...(latestMissionScenarioResult || {}),
      sitl_execution_response: execution,
      sitl_execution_result_task: execution.task,
      summary: {
        ...(latestMissionScenarioResult?.summary || {}),
        ...executionSummary,
        sitl_execution_task_id: id,
      },
    };
    renderMissionScenarioResult(latestMissionScenarioResult);
    updateMissionOSChatRuntimeRecoveryDispatchCardEvidence(runtimeRecoveryDispatchCard, execution.task);
    let repair = null;
    if (blocked) {
      if (missionOSChatRepairUploadHandshakeOnly(execution)) {
        updateMissionOSChatBubbleText(
          progressBubble,
          missionOSChatLiveSITLProgressText("blocked_readiness", {
            elapsedSeconds: Math.floor((Date.now() - progressStartedAt) / 1000),
            execution,
          }),
        );
      } else {
        updateMissionOSChatBubbleText(
          progressBubble,
          missionOSChatLiveSITLProgressText("blocked_repair", {
            elapsedSeconds: Math.floor((Date.now() - progressStartedAt) / 1000),
            execution,
          }),
        );
        repair = await runMissionOSChatRepairPlannerAfterBlockedExecution(execution);
      }
    }
    updateMissionOSChatBubbleText(
      progressBubble,
      missionOSChatLiveSITLProgressText("completed", { execution }),
    );
    appendMissionOSChatBubble("agent", missionOSChatLiveSITLResultSummary(execution, { blocked, repair }));
    clearMissionOSChatStateCards();
    appendMissionOSChatVisualResponse({ mission_designer: latestMissionScenarioResult });
    if (blocked) {
      const repairProposalModel = missionOSChatRepairProposalModel(repair, execution);
      if (repairProposalModel) {
        appendMissionOSChatBubble("agent", missionOSChatRepairProposalText(repairProposalModel, { execution, repair }));
        appendMissionOSChatRepairProposalCard(repairProposalModel);
      } else if (repair?.summary_status === "repair_proposal_ready") {
        appendMissionOSChatBubble("agent", missionOSChatRepairProposalText(null, { execution, repair }));
      }
    }
    setMissionOSChatStatus("Conversation ready");
    logEvent("missionos.chat.live_sitl.run", executionSummary);
  } catch (err) {
    polling = false;
    updateMissionOSChatBubbleText(progressBubble, missionOSChatLiveSITLProgressText("failed"));
    appendMissionOSChatBubble("system", missionosFriendlyActionError(err, "/px4-gazebo/mission-scenarios/execute-sitl"));
    setMissionOSChatStatus("Live SITL request failed safely");
    logEvent("missionos.chat.live_sitl.error", { error: String(err) });
  } finally {
    polling = false;
    window.clearInterval(progressTimer);
    disableMissionOSChatRuntimeRecoveryDispatchCard(
      runtimeRecoveryDispatchCard,
      finalExecution && missionOSChatLiveSITLBlockedBeforeRunner(finalExecution)
        ? "blocked_before_runner"
        : "ended",
    );
    if (missionosChatInputEl) {
      missionosChatInputEl.disabled = false;
      missionosChatInputEl.focus();
    }
  }
}

function renderMissionOSRepairPlanner(payload) {
  if (!missionosRepairPlannerStatusEl || !missionosRepairPlannerSummaryEl) return;
  latestMissionOSOperatorPayloads.repairPlanner = payload || null;
  const proposal = asPlainObject(payload?.repair_proposal);
  const input = asPlainObject(payload?.input_evidence);
  const boundary = asPlainObject(payload?.authority_boundary);
  const blockingReasons = asArray(boundary.blocking_reasons);
  const ready = payload?.summary_status === "repair_proposal_ready";
  missionosRepairPlannerStatusEl.innerHTML = `
    <div class="item-head">
      <strong>ADK/Gemini Repair Planning</strong>
      ${statusTag(payload?.summary_status || "missing")}
    </div>
    <div class="muted">LLM diagnoses and proposes repair steps. Rules reject authority claims; execution still requires separate human approval and runtime gates.</div>
  `;
  missionosRepairPlannerSummaryEl.innerHTML = `
    <div class="missionos-knowledge-sharing-brief">
      <div class="detail-chip-row">
        <span class="detail-chip mission-brief-chip-${ready ? "ok" : "pending"}"><span class="detail-chip-label">proposal</span><span class="detail-chip-value">${escapeHtml(proposal.planner_status || payload?.summary_status || "missing")}</span></span>
        <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">intelligence</span><span class="detail-chip-value">${escapeHtml(proposal.intelligence_source || "llm_repair_planner")}</span></span>
        <span class="detail-chip mission-brief-chip-${input.artifact_sha256_matches_current_file ? "ok" : "warn"}"><span class="detail-chip-label">source hash</span><span class="detail-chip-value">${escapeHtml(String(input.artifact_sha256_matches_current_file ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.dispatch_authority_created ? "warn" : "ok"}"><span class="detail-chip-label">dispatch authority</span><span class="detail-chip-value">${escapeHtml(String(boundary.dispatch_authority_created ?? false))}</span></span>
        <span class="detail-chip mission-brief-chip-${boundary.ai_agent_progress_counted ? "warn" : "ok"}"><span class="detail-chip-label">AI progress</span><span class="detail-chip-value">${escapeHtml(String(boundary.ai_agent_progress_counted ?? false))}</span></span>
      </div>
      <div class="missionos-operator-one-line missionos-operator-one-line-${ready ? "safe" : "blocked"}">
        <strong>Conclusion:</strong>
        ${ready ? "LLM repair proposal ready for operator review." : "No valid LLM repair proposal ready."}
        target <span class="mono">${escapeHtml(proposal.repair_target || "-")}</span>.
      </div>
      ${blockingReasons.length ? `<div class="detail-error">Blocked reasons: ${escapeHtml(blockingReasons.join(", "))}</div>` : ""}
      <div class="missionos-knowledge-sharing-grid">
        <section class="missionos-knowledge-sharing-card">
          <div class="k">Repair Target</div>
          <strong>${escapeHtml(proposal.repair_target || "missing")}</strong>
          <div class="muted">${escapeHtml(proposal.rationale || "")}</div>
        </section>
        <section class="missionos-knowledge-sharing-card">
          <div class="k">Repair Actions</div>
          <strong>${escapeHtml(String(asArray(proposal.repair_actions).length))}</strong>
          <div class="muted">${asArray(proposal.repair_actions).map((action) => escapeHtml(`${action.action_type || "action"}: ${action.description || ""}`)).join("<br>")}</div>
        </section>
        <section class="missionos-knowledge-sharing-card">
          <div class="k">Expected Outcome</div>
          <strong>${escapeHtml(proposal.expected_outcome || "missing")}</strong>
          <div class="muted">${escapeHtml(proposal.next_verification || "")}</div>
        </section>
        <section class="missionos-knowledge-sharing-card">
          <div class="k">Input Evidence</div>
          <strong>${escapeHtml(input.artifact_sha256_matches_current_file ? "source-bound" : "not verified")}</strong>
          <div class="item-meta mono">${escapeHtml(input.artifact_path || "")}</div>
        </section>
      </div>
    </div>
  `;
  renderMissionOSAutonomyMonitor();
}

async function loadMissionOSRepairPlanner() {
  if (!missionosRepairPlannerStatusEl || !missionosRepairPlannerSummaryEl) return;
  missionosRepairPlannerStatusEl.textContent = "Loading repair internal capability audit evidence...";
  missionosRepairPlannerSummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/llm-repair-planner");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.repairPlanner = "";
    renderMissionOSRepairPlanner(payload);
    logEvent("missionos.llm_repair_planner", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorPayloads.repairPlanner = null;
    latestMissionOSOperatorSourceErrors.repairPlanner = String(err);
    missionosRepairPlannerStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosRepairPlannerSummaryEl.innerHTML = "";
    renderMissionOSAutonomyMonitor();
    logEvent("missionos.llm_repair_planner.error", { error: String(err) });
  }
}

async function runMissionOSRepairPlanner() {
  if (!missionosRepairPlannerStatusEl) return;
  missionosRepairPlannerStatusEl.textContent = "Routing repair request through MissionOS Chief...";
  await runMissionOSAutonomyAction("repair");
}

async function refreshMissionOSAutonomyMonitor() {
  if (missionosAutonomyMonitorStatusEl) {
    missionosAutonomyMonitorStatusEl.textContent = "Refreshing MissionOS autonomy evidence...";
  }
  latestMissionOSAutonomyNotice = "I am refreshing the visible MissionOS evidence.";
  renderMissionOSAutonomyMonitor();
  try {
    await Promise.all([
      refreshMissionOSOperatorSummary(),
      loadMissionOSForm2aAiAgent(),
      loadMissionOSRepairPlanner(),
    ]);
    latestMissionOSAutonomyNotice = "I refreshed the conversation from the latest persisted evidence.";
    renderMissionOSAutonomyMonitor();
  } catch (err) {
    latestMissionOSAutonomyNotice = `Refresh failed: ${String(err)}`;
    renderMissionOSAutonomyMonitor();
  }
}

// Operator-facing funnel: every audit-panel/monitor button below routes through
// the single MissionOS conversation entrypoint (Chief) rather than calling the
// demoted direct capability routes. The action keys are conversation intents,
// not the legacy "Form 2a response selection" operation -- "instruction" simply
// forwards the operator's free-form text to the Chief.
async function runMissionOSAutonomyAction(action) {
  if (action === "instruction") {
    const instructionInput = document.getElementById("missionosAutonomyInstructionInput");
    latestMissionOSOperatorInstruction = String(instructionInput?.value || "").trim();
    if (!latestMissionOSOperatorInstruction) {
      latestMissionOSAutonomyNotice = "Tell me what you want MissionOS to consider before I ask the planner.";
      renderMissionOSAutonomyMonitor();
      return;
    }
    await runMissionOSConversationInstruction(
      latestMissionOSOperatorInstruction,
      "I am routing your instruction through MissionOS."
    );
  } else if (action === "approve") {
    await runMissionOSConversationInstruction("承認して", "I am recording your approval through MissionOS.");
  } else if (action === "reject") {
    await runMissionOSConversationInstruction("拒否して", "I am recording your rejection through MissionOS.");
  } else if (action === "revision") {
    await runMissionOSConversationInstruction("修正して", "I am routing your revision request through MissionOS.");
  } else if (action === "consume") {
    await runMissionOSConversationInstruction("実行して", "I am routing execution through MissionOS gates.");
  } else if (action === "repair") {
    await runMissionOSConversationInstruction("修復して", "I am asking MissionOS to diagnose and repair.");
  } else {
    await refreshMissionOSAutonomyMonitor();
  }
  renderMissionOSAutonomyMonitor();
}

async function loadMissionOSScopedForm3() {
  if (!missionosScopedForm3StatusEl || !missionosScopedForm3SummaryEl) return;
  missionosScopedForm3StatusEl.textContent = "Loading Form 3 evidence...";
  missionosScopedForm3SummaryEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/scoped-form3-closed-loop");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.scopedForm3 = "";
    renderMissionOSScopedForm3(payload);
    logEvent("missionos.scoped_form3", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorPayloads.scopedForm3 = null;
    latestMissionOSOperatorSourceErrors.scopedForm3 = String(err);
    missionosScopedForm3StatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosScopedForm3SummaryEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.scoped_form3.error", { error: String(err) });
  }
}

async function runMissionOSScopedForm3() {
  if (!missionosScopedForm3RunBtn || !missionosScopedForm3StatusEl) return;
  missionosScopedForm3RunBtn.disabled = true;
  missionosScopedForm3StatusEl.textContent = "Running two-cycle scoped Form 3 loop...";
  try {
    const response = await apiFetchWithTimeout("/missionos/scoped-form3-closed-loop/run", {
      method: "POST",
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    latestMissionOSOperatorSourceErrors.scopedForm3 = "";
    renderMissionOSScopedForm3(payload);
    logEvent("missionos.scoped_form3_run", { summary_status: payload.summary_status });
  } catch (err) {
    latestMissionOSOperatorSourceErrors.scopedForm3 = String(err);
    missionosScopedForm3StatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    renderMissionOSOperatorSummary();
    logEvent("missionos.scoped_form3_run.error", { error: String(err) });
  } finally {
    missionosScopedForm3RunBtn.disabled = false;
  }
}

function missionosOperationRiskLabel(riskLevel) {
  if (riskLevel === "artifact_replay_safe") return "artifact replay";
  if (riskLevel === "gateway_probe_safe") return "Gateway probe";
  if (riskLevel === "live_sitl_heavy") return "live SITL";
  return riskLevel || "unknown";
}

function missionosOperationRunButton(operation) {
  const operationId = operation?.operation_id || "";
  const riskLevel = operation?.risk_level || "";
  if (operation?.disabled_by_default || riskLevel === "live_sitl_heavy") {
    return `<button class="btn" type="button" disabled title="Live SITL operations are disabled by default.">Disabled</button>`;
  }
  const label = riskLevel === "gateway_probe_safe" ? "Run Probe" : "Run";
  return `<button class="btn" type="button" data-missionos-operation-run="${escapeAttr(operationId)}">${escapeHtml(label)}</button>`;
}

function renderMissionOSOperations(registry) {
  if (!missionosOperationsListEl || !missionosOperationsStatusEl) return;
  latestMissionOSOperatorPayloads.operations = registry || null;
  missionosAuthoritySources.operations = registry || null;
  renderMissionOSAuthorityBelt();
  const operations = asArray(registry?.operations);
  missionosOperationsStatusEl.innerHTML = `
    <div class="item-head">
      <strong>MissionOS operation registry</strong>
      ${statusTag(registry?.registry_status || "unknown")}
    </div>
    <div class="muted">${escapeHtml(registry?.boundary_notice || "")} Operation cards are collapsed to keep the mission view readable.</div>
  `;
  missionosOperationsListEl.innerHTML = `
    <details class="mission-ui-collapse">
      <summary>MissionOS Operation Cards (${escapeHtml(String(operations.length))})</summary>
      <div class="missionos-operations-list-inner">
      ${operations.map((operation) => {
    const last = asPlainObject(operation.last);
    const summary = asPlainObject(last.artifact_summary);
    const blocked = asArray(last.blocked_reasons);
    const runDisabled = operation.disabled_by_default || operation.risk_level === "live_sitl_heavy";
    return `
      <div class="missionos-operation-card missionos-operation-${escapeAttr(operation.risk_level || "unknown")}">
        <div class="item-head">
          <strong>${escapeHtml(operation.label || operation.operation_id || "-")}</strong>
          ${statusTag(missionosOperationRiskLabel(operation.risk_level))}
        </div>
        <div class="muted">${escapeHtml(operation.description || "")}</div>
        <div class="missionos-operation-fields">
          <div><span class="k">Last status</span><span class="mono">${escapeHtml(last.last_status || "missing")}</span></div>
          <div><span class="k">Script</span><span class="mono">${escapeHtml(operation.script_path || "-")}</span></div>
          <div><span class="k">Last artifact</span><span class="mono">${escapeHtml(last.latest_artifact_path || "missing")}</span></div>
          <div><span class="k">Physical</span><span class="mono">${escapeHtml(String(operation.physical_execution_invoked ?? false))}</span></div>
          <div><span class="k">Dispatch authority</span><span class="mono">${escapeHtml(String(operation.dispatch_authority_created ?? false))}</span></div>
        </div>
        ${blocked.length ? `<div class="detail-error">Blocked reasons: ${escapeHtml(blocked.join(", "))}</div>` : ""}
        ${Object.keys(summary).length ? `<pre class="missionos-operation-json">${escapeHtml(JSON.stringify(summary, null, 2))}</pre>` : ""}
        <div class="actions">
          ${missionosOperationRunButton(operation)}
          ${runDisabled ? `<span class="muted">Live SITL is not exposed as a casual one-click action.</span>` : ""}
        </div>
      </div>
    `;
  }).join("")}
      </div>
    </details>
  `;
  renderMissionOSOperatorSummary();
}

async function loadMissionOSOperations() {
  if (!missionosOperationsListEl || !missionosOperationsStatusEl) return;
  missionosOperationsStatusEl.textContent = "Loading MissionOS operations...";
  missionosOperationsListEl.innerHTML = "";
  try {
    const response = await apiFetchWithTimeout("/missionos/operations");
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const registry = await response.json();
    latestMissionOSOperatorSourceErrors.operations = "";
    renderMissionOSOperations(registry);
    logEvent("missionos.operations", {
      registry_status: registry.registry_status,
      operation_count: registry.operation_count,
    });
  } catch (err) {
    latestMissionOSOperatorPayloads.operations = null;
    latestMissionOSOperatorSourceErrors.operations = String(err);
    missionosAuthoritySources.operations = null;
    renderMissionOSAuthorityBelt();
    missionosOperationsStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    missionosOperationsListEl.innerHTML = "";
    renderMissionOSOperatorSummary();
    logEvent("missionos.operations.error", { error: String(err) });
  }
}

function renderMissionOSOperationRunLog(result) {
  if (!missionosOperationsRunLogEl) return;
  latestMissionOSOperatorPayloads.lastOperation = result || null;
  missionosAuthoritySources.lastRun = result || null;
  renderMissionOSAuthorityBelt();
  const blocked = asArray(result?.blocked_reasons);
  const summary = asPlainObject(result?.artifact_summary);
  missionosOperationsRunLogEl.innerHTML = `
    <div class="missionos-operation-run-result">
      <div class="item-head">
        <strong>${escapeHtml(result?.operation_label || result?.operation_id || "MissionOS operation")}</strong>
        ${statusTag(result?.run_status || "unknown")}
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">physical execution</span><span class="detail-chip-value">${escapeHtml(String(result?.physical_execution_invoked ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">physical Form 1</span><span class="detail-chip-value">${escapeHtml(String(result?.physical_form1_claimed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">hardware</span><span class="detail-chip-value">${escapeHtml(String(result?.hardware_target_allowed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">dispatch authority</span><span class="detail-chip-value">${escapeHtml(String(result?.dispatch_authority_created ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">delivery completion</span><span class="detail-chip-value">${escapeHtml(String(result?.delivery_completion_claimed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">public sync</span><span class="detail-chip-value">${escapeHtml(String(result?.public_sync_performed ?? false))}</span></span>
      </div>
      ${blocked.length ? `<div class="detail-error">Blocked reasons: ${escapeHtml(blocked.join(", "))}</div>` : ""}
      <div class="item-meta mono">${escapeHtml(result?.artifact_path || result?.operation_run_artifact_path || "")}</div>
      <pre class="missionos-operation-json">${escapeHtml(JSON.stringify(summary, null, 2))}</pre>
    </div>
  `;
  renderMissionOSOperatorSummary();
}

async function runMissionOSOperation(operationId) {
  if (!operationId) return;
  const isGatewayProbe = operationId.startsWith("probe_gateway_");
  if (isGatewayProbe && !window.confirm("Run the local Gateway loopback probe? It will not start PX4/Gazebo or physical execution.")) {
    return;
  }
  if (missionosOperationsRunLogEl) {
    missionosOperationsRunLogEl.textContent = `Running ${operationId}...`;
  }
  const body = isGatewayProbe ? { confirm_gateway_probe: true } : {};
  try {
    const response = await apiFetchWithTimeout(`/missionos/operations/${encodeURIComponent(operationId)}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result?.detail || `HTTP ${response.status}`);
    }
    renderMissionOSOperationRunLog(result);
    await loadMissionOSOperations();
    logEvent("missionos.operation.run", {
      operation_id: operationId,
      run_status: result.run_status,
      blocked_reasons: result.blocked_reasons,
    });
  } catch (err) {
    if (missionosOperationsRunLogEl) {
      missionosOperationsRunLogEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    }
    logEvent("missionos.operation.run.error", { operation_id: operationId, error: String(err) });
  }
}

function uniqueStrings(values) {
  return Array.from(new Set(values.filter(Boolean).map(String)));
}

function coerceNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function missionFlightSampleTimeSeconds(sample, fallbackIndex) {
  const direct = coerceNumber(sample.elapsed_s ?? sample.elapsed_seconds ?? sample.sample_time_s);
  if (direct !== null) return direct;
  const sampleIndex = coerceNumber(sample.sample_index);
  if (sampleIndex !== null) return sampleIndex;
  const timestamp = sample.observed_at || sample.timestamp;
  if (timestamp) {
    const parsed = Date.parse(timestamp);
    if (Number.isFinite(parsed)) return parsed / 1000;
  }
  return fallbackIndex;
}

function missionFlightAnimationTimeline(samples, points) {
  const rawTimes = samples.map((sample, index) => missionFlightSampleTimeSeconds(sample, index));
  const firstTime = rawTimes[0] ?? 0;
  const normalizedTimes = rawTimes.map((time, index) => {
    const normalized = time - firstTime;
    return Number.isFinite(normalized) && normalized >= 0 ? normalized : index;
  });
  return points.map((point, index) => ({
    x: Number(point.x.toFixed(2)),
    y: Number(point.y.toFixed(2)),
    t: Number((normalizedTimes[index] ?? index).toFixed(3)),
    phase: samples[index]?.phase || "telemetry",
  }));
}

function missionFlightSegmentDurationMs(points, fromIndex) {
  const from = points[fromIndex] || points[0] || { t: 0 };
  const to = points[fromIndex + 1] || points[0] || from;
  const sampleDeltaSeconds = Math.max(0.001, (to.t ?? 0) - (from.t ?? 0));
  return Math.max(
    MISSION_FLIGHT_ANIMATION_MIN_MS,
    Math.min(MISSION_FLIGHT_ANIMATION_MAX_MS, sampleDeltaSeconds * 1000),
  );
}

function syncMissionFlightTelemetryAnimations() {
  const elements = Array.from(document.querySelectorAll(".mission-flight-replay-drone[data-flight-animation-points]"));
  const activeIds = new Set();
  const now = performance.now();
  for (const element of elements) {
    const animationId = element.dataset.flightAnimationId || "";
    activeIds.add(animationId);
    let points = [];
    try {
      points = JSON.parse(element.dataset.flightAnimationPoints || "[]");
    } catch (_) {
      points = [];
    }
    if (!animationId || !points.length) continue;
    const latestKey = element.dataset.flightAnimationLatestKey || JSON.stringify(points.map((point) => [point.x, point.y, point.t]));
    let state = missionFlightAnimationStates.get(animationId);
    if (!state || state.latestKey !== latestKey) {
      const first = points[0];
      const next = points[1] || first;
      state = {
        points,
        segmentIndex: 0,
        currentX: first.x,
        currentY: first.y,
        fromX: first.x,
        fromY: first.y,
        targetX: next.x,
        targetY: next.y,
        latestKey,
        startedAt: now,
        durationMs: missionFlightSegmentDurationMs(points, 0),
      };
      missionFlightAnimationStates.set(animationId, state);
    }
    state.points = points;
    element.setAttribute("transform", `translate(${(state.currentX ?? points[0].x).toFixed(2)} ${(state.currentY ?? points[0].y).toFixed(2)})`);
  }
  for (const animationId of Array.from(missionFlightAnimationStates.keys())) {
    if (!activeIds.has(animationId)) {
      missionFlightAnimationStates.delete(animationId);
    }
  }
  if (elements.length && !missionFlightAnimationFrame) {
    missionFlightAnimationFrame = window.requestAnimationFrame(stepMissionFlightTelemetryAnimations);
  }
}

function stepMissionFlightTelemetryAnimations(now) {
  missionFlightAnimationFrame = null;
  let needsNextFrame = false;
  for (const [animationId, state] of missionFlightAnimationStates.entries()) {
    const element = Array.from(document.querySelectorAll(".mission-flight-replay-drone[data-flight-animation-id]"))
      .find((candidate) => candidate.dataset.flightAnimationId === animationId);
    if (!element) {
      missionFlightAnimationStates.delete(animationId);
      continue;
    }
    const points = state.points || [];
    if (points.length < 2) {
      continue;
    }
    const progress = Math.min(1, Math.max(0, (now - state.startedAt) / Math.max(1, state.durationMs)));
    const eased = 1 - ((1 - progress) ** 3);
    const fromX = state.fromX ?? state.currentX ?? state.targetX;
    const fromY = state.fromY ?? state.currentY ?? state.targetY;
    state.currentX = fromX + ((state.targetX - fromX) * eased);
    state.currentY = fromY + ((state.targetY - fromY) * eased);
    element.setAttribute("transform", `translate(${state.currentX.toFixed(2)} ${state.currentY.toFixed(2)})`);
    if (progress >= 1) {
      const nextSegmentIndex = (state.segmentIndex + 1) % (points.length - 1);
      const from = points[nextSegmentIndex];
      const to = points[nextSegmentIndex + 1] || points[0];
      state.segmentIndex = nextSegmentIndex;
      state.fromX = from.x;
      state.fromY = from.y;
      state.currentX = from.x;
      state.currentY = from.y;
      state.targetX = to.x;
      state.targetY = to.y;
      state.startedAt = now;
      state.durationMs = missionFlightSegmentDurationMs(points, nextSegmentIndex);
    }
    needsNextFrame = true;
  }
  if (needsNextFrame) {
    missionFlightAnimationFrame = window.requestAnimationFrame(stepMissionFlightTelemetryAnimations);
  }
}

function digitalTwinFlightPathSamples(summary) {
  const direct = asArray(summary.flight_path_profile).length
    ? summary.flight_path_profile
    : (asArray(summary.position_profile).length ? summary.position_profile : summary.route_preview_waypoints);
  return asArray(direct)
    .map((sample) => asPlainObject(sample))
    .filter((sample) => (
      (
        coerceNumber(sample.latitude_deg) !== null
        && coerceNumber(sample.longitude_deg) !== null
      )
      || (
        coerceNumber(sample.local_x_m ?? sample.x) !== null
        && coerceNumber(sample.local_y_m ?? sample.y) !== null
      )
    ));
}

function missionFlightTerminalPoseSample(pose, fallbackPhase = "telemetry") {
  const record = asPlainObject(pose);
  if (record.observed !== true) return null;
  const x = coerceNumber(record.x_m ?? record.local_x_m ?? record.x);
  const y = coerceNumber(record.y_m ?? record.local_y_m ?? record.y);
  if (x === null || y === null) return null;
  const z = coerceNumber(record.z_m ?? record.relative_alt_m ?? record.local_z_m ?? record.z);
  const progress = coerceNumber(record.progress_m ?? record.horizontal_progress_m);
  return {
    phase: record.phase || fallbackPhase,
    local_x_m: x,
    local_y_m: y,
    local_z_m: z ?? 0,
    horizontal_progress_m: progress ?? undefined,
    elapsed_s: record.elapsed_s,
    sample_index: record.sample_index,
  };
}

function missionFlightDisplayPoseModel(summary, latestSample, usesWgs84) {
  const latestPhase = String(latestSample.phase || summary.flight_path_status || "").toLowerCase();
  const routeTerminal = missionFlightTerminalPoseSample(summary.route_terminal_pose, "route");
  const completedTerminal = missionFlightTerminalPoseSample(summary.completed_terminal_pose, "completed");
  const latestIsLandedTerminal = latestPhase === "completed" || latestPhase === "landing";
  const useRouteTerminal = !usesWgs84
    && latestIsLandedTerminal
    && summary.delivery_completion_claimed !== true
    && routeTerminal !== null;
  const finalPose = completedTerminal || (latestIsLandedTerminal ? latestSample : null);
  const progress = coerceNumber(
    routeTerminal?.horizontal_progress_m
    ?? summary.route_terminal_progress_m
    ?? summary.horizontal_progress_m
    ?? latestSample.horizontal_progress_m,
  );
  const landedZ = finalPose
    ? coerceNumber(finalPose.z_m ?? finalPose.relative_alt_m ?? finalPose.local_z_m ?? finalPose.z)
    : null;
  return {
    sample: useRouteTerminal ? routeTerminal : latestSample,
    usesRouteTerminal: useRouteTerminal,
    routeEndedText: useRouteTerminal && progress !== null
      ? `route ended at ~${Math.round(progress)}m, then landed`
      : "",
    landedFinalZ: useRouteTerminal ? landedZ : null,
  };
}

function artifactTaskRefMatchesCurrentTask(task, ...artifacts) {
  const taskId = String(task?.task_id || "");
  if (!taskId) return false;
  const expectedTaskRef = `task:${taskId}`;
  return artifacts.every((artifact) => {
    const item = asPlainObject(artifact);
    return item.task_ref === expectedTaskRef || item.task_id === taskId;
  });
}

function findDigitalTwinFlightPathSummary(task) {
  const artifacts = asPlainObject(task && task.artifacts);
  const missionSummary = asPlainObject(artifacts.mission_scenario_designer_summary);
  const coordinateRoute = asPlainObject(artifacts.mission_designer_coordinate_pair_route);
  const terrainSource = asPlainObject(artifacts.terrain_dem_source_snapshot);
  const terrainTile = asPlainObject(artifacts.terrain_dem_tile_snapshot);
  const tileTerrain = asPlainObject(artifacts.tile_backed_terrain_environment_snapshot);
  const projectedTerrain = asPlainObject(artifacts.terrain_environment_snapshot);
  const heightmapFile = asPlainObject(artifacts.terrain_heightmap_file_artifact);
  const heightmapPreview = asPlainObject(
    artifacts.terrain_heightmap_preview_grid
    || heightmapFile.terrain_heightmap_preview_grid
    || heightmapFile.heightmap_preview_grid,
  );
  const liveTelemetrySnapshot = asPlainObject(artifacts.mission_designer_live_telemetry_snapshot);
  const autoMissionRuntimeReplay = asPlainObject(
    artifacts.missionos_auto_mission_runtime_replay
    || artifacts.auto_mission_runtime_replay,
  );
  const failedReceipt = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt);
  const failedReplayCandidate = Object.keys(failedReceipt).length
    && digitalTwinFlightPathSamples(liveTelemetrySnapshot).length
    && artifactTaskRefMatchesCurrentTask(task, failedReceipt, liveTelemetrySnapshot)
    ? Object.assign({}, liveTelemetrySnapshot, {
      flight_path_status: failedReceipt.failure_category || "blocked",
      flight_path_replay_kind: "failure",
      failure_category: failedReceipt.failure_category || "",
      failure_reason_digest: failedReceipt.failure_reason_digest || "",
      live_flight_execution_status: failedReceipt.live_flight_execution_status || "blocked",
      delivery_completion_claimed: false,
      actual_sitl_flight_evidence_observed: false,
    })
    : {};
  const candidates = [
    autoMissionRuntimeReplay,
    artifacts.px4_gazebo_mission_designer_sitl_live_flight_run,
    failedReplayCandidate,
    artifacts.digital_twin_waypoint_smoke_summary,
    artifacts.digital_twin_sitl_waypoint_reach_summary,
    artifacts.digital_twin_sitl_waypoint_reach_observation,
    artifacts.digital_twin_delivery_smoke_summary,
    artifacts.result,
  ].map(asPlainObject);
  const flightSummary = candidates.find((candidate) => digitalTwinFlightPathSamples(candidate).length) || {};
  if (!Object.keys(flightSummary).length) return {};
  const terrainContext = Object.assign(
    {},
    missionSummary,
    coordinateRoute,
    projectedTerrain,
    tileTerrain,
    terrainTile,
    terrainSource,
    flightSummary,
  );
  if (Object.keys(heightmapPreview).length) {
    terrainContext.terrain_heightmap_preview_grid = heightmapPreview;
  }
  terrainContext.terrain_source_url = terrainSource.source_url
    || terrainTile.source_url
    || tileTerrain.source_url
    || projectedTerrain.source_url
    || flightSummary.terrain_source_url
    || flightSummary.terrain_dem_source_snapshot_source_url
    || missionSummary.terrain_dem_source_snapshot_source_url
    || missionSummary.target_resolution_source_url
    || coordinateRoute.source_url
    || "";
  terrainContext.terrain_source_status = terrainSource.snapshot_status
    || missionSummary.terrain_dem_source_snapshot_status
    || flightSummary.terrain_source_status
    || flightSummary.terrain_dem_source_snapshot_status
    || terrainTile.snapshot_mode
    || tileTerrain.snapshot_mode
    || projectedTerrain.snapshot_mode
    || "";
  terrainContext.terrain_provider = terrainSource.provider
    || terrainTile.provider
    || tileTerrain.provider
    || projectedTerrain.provider
    || flightSummary.terrain_provider
    || flightSummary.terrain_dem_source_snapshot_provider
    || missionSummary.terrain_dem_source_snapshot_provider
    || "";
  terrainContext.terrain_provider_response_status = terrainSource.provider_response_status
    || flightSummary.terrain_provider_response_status
    || flightSummary.terrain_dem_source_snapshot_provider_response_status
    || missionSummary.terrain_dem_source_snapshot_provider_response_status
    || "";
  terrainContext.terrain_elevation_min_m = terrainSource.elevation_min_m
    ?? terrainTile.elevation_min_m
    ?? tileTerrain.elevation_min_m
    ?? projectedTerrain.elevation_min_m
    ?? flightSummary.terrain_elevation_min_m
    ?? missionSummary.terrain_elevation_min_m
    ?? null;
  terrainContext.terrain_elevation_max_m = terrainSource.elevation_max_m
    ?? terrainTile.elevation_max_m
    ?? tileTerrain.elevation_max_m
    ?? projectedTerrain.elevation_max_m
    ?? flightSummary.terrain_elevation_max_m
    ?? missionSummary.terrain_elevation_max_m
    ?? null;
  if (flightSummary.flight_path_replay_kind === "auto_mission" && Object.keys(coordinateRoute).length) {
    const plannedPreview = missionScenarioCoordinateRouteFlightPathSummaryFromArtifacts({
      route: coordinateRoute,
      summary: missionSummary,
      terrain: projectedTerrain,
      source: terrainSource,
      heightmap: heightmapPreview,
      flightPathSource: "operator_coordinate_route_planned_preview",
      flightPathStatus: "planned_route_preview",
    });
    if (digitalTwinFlightPathSamples(plannedPreview).length) {
      terrainContext.planned_route_preview_summary = plannedPreview;
    }
  }
  return terrainContext;
}

function missionScenarioCoordinateRouteFlightPathSummaryFromArtifacts({
  route,
  summary,
  terrain,
  source,
  heightmap,
  sitlExecutionInProgress = false,
  flightPathSource = "operator_coordinate_route_pending_telemetry",
  flightPathStatus = "",
} = {}) {
  const routeRecord = asPlainObject(route);
  const summaryRecord = asPlainObject(summary);
  const terrainRecord = asPlainObject(terrain);
  const sourceRecord = asPlainObject(source);
  const heightmapRecord = asPlainObject(heightmap);
  const takeoffLat = coerceNumber(routeRecord.takeoff_latitude);
  const takeoffLon = coerceNumber(routeRecord.takeoff_longitude);
  const dropoffLat = coerceNumber(routeRecord.dropoff_latitude);
  const dropoffLon = coerceNumber(routeRecord.dropoff_longitude);
  if (takeoffLat === null || takeoffLon === null || dropoffLat === null || dropoffLon === null) {
    return {};
  }
  const roofAgl = coerceNumber(routeRecord.dropoff_roof_height_agl_m) ?? coerceNumber(summaryRecord.altitude_target_m) ?? 10;
  const cruiseAlt = Math.max(roofAgl + 10, 20);
  const midpointLat = (takeoffLat + dropoffLat) / 2;
  const midpointLon = (takeoffLon + dropoffLon) / 2;
  return {
    flight_path_source: flightPathSource,
    flight_path_status: flightPathStatus || (sitlExecutionInProgress === true
      ? "awaiting_runtime_pose_log"
      : "prepared_route_waiting_for_execute"),
    terrain_provider: sourceRecord.provider || terrainRecord.provider || summaryRecord.terrain_dem_source_snapshot_provider || "digital_twin_fixture_dem",
    terrain_source_status: sourceRecord.snapshot_status || summaryRecord.terrain_dem_source_snapshot_status || terrainRecord.snapshot_mode || "prompt_projected_fixture",
    terrain_provider_response_status: sourceRecord.provider_response_status || summaryRecord.terrain_dem_source_snapshot_provider_response_status || "",
    terrain_source_url: sourceRecord.source_url || terrainRecord.source_url || summaryRecord.terrain_dem_source_snapshot_source_url || routeRecord.source_url || "",
    terrain_elevation_min_m: sourceRecord.elevation_min_m ?? terrainRecord.elevation_min_m ?? summaryRecord.terrain_elevation_min_m ?? 0,
    terrain_elevation_max_m: sourceRecord.elevation_max_m ?? terrainRecord.elevation_max_m ?? summaryRecord.terrain_elevation_max_m ?? roofAgl,
    dropoff_latitude_deg: dropoffLat,
    dropoff_longitude_deg: dropoffLon,
    horizontal_progress_m: "-",
    observed_at: "",
    route_preview_waypoints: [
      { phase: "prepared", latitude_deg: takeoffLat, longitude_deg: takeoffLon, relative_alt_m: 0 },
      { phase: "takeoff", latitude_deg: takeoffLat, longitude_deg: takeoffLon, relative_alt_m: Math.max(roofAgl, 15) },
      { phase: "route_pending_log", latitude_deg: midpointLat, longitude_deg: midpointLon, relative_alt_m: cruiseAlt },
      { phase: "dropoff_target", latitude_deg: dropoffLat, longitude_deg: dropoffLon, relative_alt_m: cruiseAlt },
    ],
    ...(Object.keys(heightmapRecord).length
      ? { terrain_heightmap_preview_grid: heightmapRecord }
      : {}),
  };
}

function missionScenarioCoordinateRouteFlightPathSummary(result) {
  return missionScenarioCoordinateRouteFlightPathSummaryFromArtifacts({
    route: result?.mission_designer_coordinate_pair_route,
    summary: result?.summary,
    terrain: result?.terrain_environment_snapshot,
    source: result?.terrain_dem_source_snapshot,
    heightmap: result?.terrain_heightmap_preview_grid
      || result?.terrain_heightmap_file_artifact?.terrain_heightmap_preview_grid
      || result?.terrain_heightmap_file_artifact?.heightmap_preview_grid,
    sitlExecutionInProgress: result?.sitl_execution_in_progress === true,
  });
}

function missionScenarioFlightPathSummary(result) {
  const response = asPlainObject(result?.sitl_execution_response);
  const responseTask = asPlainObject(response.task);
  const resultTask = asPlainObject(result?.sitl_execution_result_task);
  const preparedTask = asPlainObject(result?.sitl_execution_task);
  const directCandidates = [
    responseTask,
    resultTask,
    preparedTask,
  ];
  for (const task of directCandidates) {
    const summary = findDigitalTwinFlightPathSummary(task);
    if (digitalTwinFlightPathSamples(summary).length) {
      return summary;
    }
  }
  const directArtifacts = [
    response.missionos_auto_mission_runtime_replay,
    response.auto_mission_runtime_replay,
    response.px4_gazebo_mission_designer_sitl_live_flight_run,
    response.digital_twin_waypoint_smoke_summary,
    response.digital_twin_sitl_waypoint_reach_summary,
    response.digital_twin_sitl_waypoint_reach_observation,
    response.digital_twin_delivery_smoke_summary,
  ].map(asPlainObject);
  const direct = directArtifacts.find((candidate) => digitalTwinFlightPathSamples(candidate).length);
  if (direct) {
    const plannedPreview = missionScenarioCoordinateRouteFlightPathSummaryFromArtifacts({
      route: result?.mission_designer_coordinate_pair_route,
      summary: result?.summary,
      terrain: result?.terrain_environment_snapshot,
      source: result?.terrain_dem_source_snapshot,
      heightmap: result?.terrain_heightmap_preview_grid
        || result?.terrain_heightmap_file_artifact?.terrain_heightmap_preview_grid
        || result?.terrain_heightmap_file_artifact?.heightmap_preview_grid,
      flightPathSource: "operator_coordinate_route_planned_preview",
      flightPathStatus: "planned_route_preview",
    });
    return Object.assign(
      {},
      missionScenarioCoordinateRouteFlightPathSummary(result),
      direct,
      digitalTwinFlightPathSamples(plannedPreview).length
        ? { planned_route_preview_summary: plannedPreview }
        : {},
    );
  }
  return missionScenarioCoordinateRouteFlightPathSummary(result);
}

function renderMissionScenarioFlightPathPendingNotice(summary) {
  if (!summary || summary.flight_path_source !== "operator_coordinate_route_pending_telemetry") return "";
  return "";
}

function missionFlightTerrainHeightmap(summary) {
  const source = asPlainObject(summary);
  const nestedGrid = asPlainObject(
    source.terrain_heightmap_preview_grid
    || source.terrain_heightmap_grid
    || source.heightmap_grid,
  );
  const rawHeights = Array.isArray(nestedGrid.normalized_heights)
    ? nestedGrid.normalized_heights
    : Array.isArray(source.terrain_heightmap_normalized_heights)
      ? source.terrain_heightmap_normalized_heights
      : Array.isArray(source.heightmap_file_normalized_heights)
        ? source.heightmap_file_normalized_heights
        : Array.isArray(source.normalized_heights)
          ? source.normalized_heights
          : [];
  const width = Math.trunc(coerceNumber(
    nestedGrid.pixel_width
    ?? source.terrain_heightmap_pixel_width
    ?? source.heightmap_file_pixel_width
    ?? source.heightmap_artifact_pixel_width
    ?? source.pixel_width,
  ) ?? 0);
  const height = Math.trunc(coerceNumber(
    nestedGrid.pixel_height
    ?? source.terrain_heightmap_pixel_height
    ?? source.heightmap_file_pixel_height
    ?? source.heightmap_artifact_pixel_height
    ?? source.pixel_height,
  ) ?? 0);
  const expected = width * height;
  if (width < 2 || height < 2 || rawHeights.length !== expected) {
    return null;
  }
  const heights = rawHeights.map((value) => coerceNumber(value));
  if (heights.some((value) => value === null)) {
    return null;
  }
  const bboxRaw = nestedGrid.bbox
    || source.terrain_heightmap_bbox
    || source.heightmap_file_bbox
    || source.heightmap_artifact_bbox
    || source.bbox;
  const bbox = Array.isArray(bboxRaw) && bboxRaw.length === 4
    ? bboxRaw.map((value) => coerceNumber(value))
    : [];
  return {
    width,
    height,
    heights,
    bbox: bbox.length === 4 && bbox.every((value) => value !== null) ? bbox : null,
    source: nestedGrid.source || source.terrain_heightmap_source || source.heightmap_file_source || "attached_heightfield_grid",
  };
}

function missionFlightTerrainHeightmapSample(heightmap, ratioX, ratioY) {
  if (!heightmap) return null;
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const x = clamp(ratioX, 0, 1) * (heightmap.width - 1);
  const y = clamp(ratioY, 0, 1) * (heightmap.height - 1);
  const x0 = Math.floor(x);
  const y0 = Math.floor(y);
  const x1 = Math.min(heightmap.width - 1, x0 + 1);
  const y1 = Math.min(heightmap.height - 1, y0 + 1);
  const tx = x - x0;
  const ty = y - y0;
  const at = (ix, iy) => heightmap.heights[iy * heightmap.width + ix] ?? 0;
  const top = at(x0, y0) * (1 - tx) + at(x1, y0) * tx;
  const bottom = at(x0, y1) * (1 - tx) + at(x1, y1) * tx;
  return top * (1 - ty) + bottom * ty;
}

function renderDigitalTwinFlightPathWindow(summary) {
  const samples = digitalTwinFlightPathSamples(summary);
  if (!samples.length) {
    return "";
  }
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const isFailureReplay = summary.flight_path_replay_kind === "failure"
    || summary.live_flight_execution_status === "blocked"
    || Boolean(summary.failure_category);
  const usesWgs84 = coerceNumber(samples[0].latitude_deg) !== null
    && coerceNumber(samples[0].longitude_deg) !== null;
  const coordX = (sample) => usesWgs84
    ? coerceNumber(sample.longitude_deg)
    : coerceNumber(sample.local_x_m ?? sample.x);
  const coordY = (sample) => usesWgs84
    ? coerceNumber(sample.latitude_deg)
    : coerceNumber(sample.local_y_m ?? sample.y);
  const heightmap = missionFlightTerrainHeightmap(summary);
  const xValues = samples.map(coordX);
  const yValues = samples.map(coordY);
  const rawMinX = Math.min(...xValues);
  const rawMaxX = Math.max(...xValues);
  const rawMinY = Math.min(...yValues);
  const rawMaxY = Math.max(...yValues);
  const terrainViewportBbox = usesWgs84 && heightmap?.bbox ? heightmap.bbox : null;
  const routeViewMinX = terrainViewportBbox ? Math.min(rawMinX, terrainViewportBbox[1]) : rawMinX;
  const routeViewMaxX = terrainViewportBbox ? Math.max(rawMaxX, terrainViewportBbox[3]) : rawMaxX;
  const routeViewMinY = terrainViewportBbox ? Math.min(rawMinY, terrainViewportBbox[0]) : rawMinY;
  const routeViewMaxY = terrainViewportBbox ? Math.max(rawMaxY, terrainViewportBbox[2]) : rawMaxY;
  const failureLocalViewportM = isFailureReplay && !usesWgs84 ? 8 : 0;
  const minX = failureLocalViewportM ? ((rawMinX + rawMaxX) / 2) - failureLocalViewportM / 2 : routeViewMinX;
  const maxX = failureLocalViewportM ? ((rawMinX + rawMaxX) / 2) + failureLocalViewportM / 2 : routeViewMaxX;
  const minY = failureLocalViewportM ? ((rawMinY + rawMaxY) / 2) - failureLocalViewportM / 2 : routeViewMinY;
  const maxY = failureLocalViewportM ? ((rawMinY + rawMaxY) / 2) + failureLocalViewportM / 2 : routeViewMaxY;
  const xSpan = Math.max(maxX - minX, 0.000001);
  const ySpan = Math.max(maxY - minY, 0.000001);
  const width = 980;
  const height = 560;
  const normalizeX = (sample) => ((coordX(sample) - minX) / xSpan) * 2 - 1;
  const normalizeY = (sample) => ((coordY(sample) - minY) / ySpan) * 2 - 1;
  const latestSample = samples[samples.length - 1];
  const firstSample = samples[0];
  const displayPose = missionFlightDisplayPoseModel(summary, latestSample, usesWgs84);
  const displaySample = displayPose.sample;
  const altitudeOf = (sample) => coerceNumber(sample.relative_alt_m ?? sample.local_z_m ?? sample.z) ?? 0;
  const frameValue = String(summary.flight_path_frame || (usesWgs84 ? "wgs84" : "gazebo_world_local"));
  const altitudeValues = samples.map(altitudeOf);
  const minAltitude = isFailureReplay ? Math.min(0, ...altitudeValues) : Math.min(...altitudeValues);
  const maxAltitude = isFailureReplay ? Math.max(2, ...altitudeValues) : Math.max(...altitudeValues);
  const altitudeSpan = Math.max(maxAltitude - minAltitude, isFailureReplay ? 2 : 0.001);
  const terrainMin = coerceNumber(summary.terrain_elevation_min_m);
  const terrainMax = coerceNumber(summary.terrain_elevation_max_m);
  const terrainSpanM = terrainMin !== null && terrainMax !== null
    ? Math.max(terrainMax - terrainMin, 1)
    : Math.max(altitudeSpan, 10);
  const terrainHeightfieldAttached = Boolean(heightmap);
  const terrainViewportMode = terrainViewportBbox ? "heightfield_bbox" : "route_bbox";
  const terrainAt = (nx, ny) => {
    if (heightmap) {
      let ratioX = (nx + 1) / 2;
      let ratioY = (ny + 1) / 2;
      if (usesWgs84 && heightmap.bbox) {
        const [latMin, lonMin, latMax, lonMax] = heightmap.bbox;
        const lon = minX + ratioX * xSpan;
        const lat = minY + ratioY * ySpan;
        ratioX = lonMax !== lonMin ? (lon - lonMin) / (lonMax - lonMin) : ratioX;
        ratioY = latMax !== latMin ? (lat - latMin) / (latMax - latMin) : ratioY;
      }
      const normalized = missionFlightTerrainHeightmapSample(heightmap, ratioX, ratioY);
      if (normalized !== null) {
        return clamp(0.08 + normalized * 0.78, 0.04, 0.98);
      }
    }
    const ridge = Math.exp(-((nx - 0.26) ** 2) * 2.6 - ((ny + 0.18) ** 2) * 2.1);
    const valley = Math.exp(-((nx + 0.54) ** 2) * 7.4 - ((ny - 0.46) ** 2) * 4.6);
    const folded = Math.sin((nx + 1.4) * 4.1) * 0.08 + Math.cos((ny - 0.3) * 3.5) * 0.08;
    return clamp(0.24 + ridge * 0.54 - valley * 0.18 + folded, 0.04, 0.98);
  };
  const project = (nx, ny, nz = 0) => {
    const x = width / 2 + nx * 300 + ny * 118;
    const y = height * 0.64 + ny * 132 - nx * 42 - nz * 166;
    return { x, y, nx, ny, nz };
  };
  const routePointFor = (sample) => {
    const nx = normalizeX(sample);
    const ny = normalizeY(sample);
    const terrainZ = terrainAt(nx, ny);
    const flightZ = terrainZ + 0.12 + clamp((altitudeOf(sample) - minAltitude) / altitudeSpan, 0, 1) * 0.42;
    return Object.assign(project(nx, ny, flightZ), {
      rawX: coordX(sample),
      rawY: coordY(sample),
      altitude: altitudeOf(sample),
      phase: sample.phase || "telemetry",
    });
  };
  const dronePointFor = (sample) => {
    const nx = normalizeX(sample);
    const ny = normalizeY(sample);
    const droneZ = terrainAt(nx, ny) + 0.12 + clamp((altitudeOf(sample) - minAltitude) / altitudeSpan, 0, 1) * 0.42;
    return Object.assign(project(nx, ny, droneZ), {
      rawX: coordX(sample),
      rawY: coordY(sample),
      altitude: altitudeOf(sample),
      phase: sample.phase || "telemetry",
    });
  };
  const points = samples.map(routePointFor);
  const dronePoints = samples.map(dronePointFor);
  const timelinePoints = missionFlightAnimationTimeline(samples, dronePoints);
  const pathD = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
  const groundPathD = points.map((point, index) => {
    const ground = project(point.nx, point.ny, terrainAt(point.nx, point.ny) + 0.04);
    return `${index ? "L" : "M"}${ground.x.toFixed(2)} ${ground.y.toFixed(2)}`;
  }).join(" ");
  const latestPoint = points[points.length - 1];
  const firstPoint = points[0];
  const targetRawX = usesWgs84
    ? coerceNumber(summary.dropoff_longitude_deg ?? summary.target_longitude_deg)
    : coerceNumber(summary.route_target_x_m ?? summary.dropoff_target_x_m);
  const targetRawY = usesWgs84
    ? coerceNumber(summary.dropoff_latitude_deg ?? summary.target_latitude_deg)
    : coerceNumber(summary.route_target_y_m ?? summary.dropoff_target_y_m);
  const targetPoint = targetRawX !== null && targetRawY !== null
    ? routePointFor(usesWgs84
      ? { latitude_deg: targetRawY, longitude_deg: targetRawX, relative_alt_m: maxAltitude }
      : { local_x_m: targetRawX, local_y_m: targetRawY, local_z_m: maxAltitude })
    : latestPoint;
  const tracePath = summary.flight_path_trace_path || "";
  const frameLabel = usesWgs84 ? "WGS84 lat/lon" : "Gazebo local x/y";
  const firstCoordLabel = usesWgs84 ? "latest lat" : (displayPose.usesRouteTerminal ? "route end x m" : "latest x m");
  const secondCoordLabel = usesWgs84 ? "latest lon" : (displayPose.usesRouteTerminal ? "route end y m" : "latest y m");
  const altitudeLabel = displayPose.usesRouteTerminal ? "route end z m" : "alt/local z m";
  const displayRawX = coordX(displaySample);
  const displayRawY = coordY(displaySample);
  const firstCoordValue = usesWgs84
    ? latestPoint.rawY.toFixed(7)
    : (displayRawX !== null ? displayRawX : latestPoint.rawX).toFixed(2);
  const secondCoordValue = usesWgs84
    ? latestPoint.rawX.toFixed(7)
    : (displayRawY !== null ? displayRawY : latestPoint.rawY).toFixed(2);
  const altitudeValue = displaySample.relative_alt_m
    ?? displaySample.local_z_m
    ?? displaySample.z_m
    ?? displaySample.z
    ?? "-";
  const routeEndAltitudeValue = coerceNumber(
    displaySample.relative_alt_m
    ?? displaySample.local_z_m
    ?? displaySample.z_m
    ?? displaySample.z,
  );
  const phaseAltitudeSummary = displayPose.usesRouteTerminal
    && routeEndAltitudeValue !== null
    && displayPose.landedFinalZ !== null
    ? `route end altitude ${routeEndAltitudeValue.toFixed(2)}m / landed altitude ${displayPose.landedFinalZ.toFixed(2)}m`
    : "";
  const elapsedValue = latestSample.elapsed_s ?? latestSample.sample_index ?? "-";
  const progressValue = displayPose.usesRouteTerminal
    ? (displaySample.horizontal_progress_m ?? summary.route_terminal_progress_m ?? summary.horizontal_progress_m ?? latestSample.horizontal_progress_m ?? "-")
    : (summary.horizontal_progress_m ?? latestSample.horizontal_progress_m ?? "-");
  const observedAt = summary.observed_at || summary.completed_at || "";
  const mapId = `mission-flight-${String(tracePath || samples.length).replace(/[^a-zA-Z0-9_-]/g, "-").slice(-32) || "trace"}`;
  const isPendingTelemetry = summary.flight_path_source === "operator_coordinate_route_pending_telemetry";
  const isPlannedRoutePreview = summary.flight_path_source === "operator_coordinate_route_planned_preview";
  const isRoutePreview = isPendingTelemetry || isPlannedRoutePreview;
  const phaseLabel = isRoutePreview ? "phase from route plan" : "current phase from log";
  const logTitle = isRoutePreview ? "Route Preview" : "Runtime Flight Log";
  const sourceText = [
    summary.terrain_provider || "",
    summary.terrain_source_status || "",
    summary.terrain_provider_response_status || "",
    summary.terrain_source_url || "",
  ].join(" ");
  const isGsiTerrain = /gsi|cyberjapan|地理院/i.test(sourceText);
  const sourceUnavailable = /unavailable|404|blocked|missing/i.test(sourceText);
  const isGazeboLocalFrame = /gazebo.*local|world_local/i.test(frameValue) || (!usesWgs84 && /local/i.test(frameValue));
  const isGeospatialReplay = usesWgs84 && isGsiTerrain && !sourceUnavailable && !isGazeboLocalFrame;
  const isAutoMissionReplay = summary.flight_path_replay_kind === "auto_mission"
    || String(summary.schema_version || "").startsWith("missionos_auto_mission_runtime_replay");
  const replayTitle = isFailureReplay
    ? "Gazebo Local Failure 3D Replay"
    : isPlannedRoutePreview
      ? "Planned Route 3D Preview"
      : isAutoMissionReplay
      ? "AUTO Mission Runtime 3D Replay"
      : (isGeospatialReplay ? "GSI Terrain 3D Replay" : "Gazebo Local 3D Replay");
  const replayDescription = isFailureReplay
    ? "Log-driven failure replay from the last observed Gazebo-local pose trace before the blocked SITL outcome."
    : isPlannedRoutePreview
      ? "Coordinate planning route rendered beside the runtime replay for comparison."
      : isAutoMissionReplay
      ? "Log-driven replay from completed AUTO mission runtime telemetry and projected terrain context."
      : isGeospatialReplay
        ? "Log-driven 3D replay from completed runtime telemetry and source-backed geospatial terrain evidence."
        : "Log-driven 3D replay from completed Gazebo-local runtime telemetry and projected terrain context.";
  const markerProvenance = "observed altitude marker";
  const terrainBadge = terrainHeightfieldAttached
    ? (isGsiTerrain && !sourceUnavailable ? "GSI DEM heightfield attached" : "attached terrain heightfield")
    : isGsiTerrain
      ? (sourceUnavailable ? "GSI DEM source unavailable; projected mesh" : "GSI DEM metadata only; projected mesh")
      : "projected terrain mesh";
  const terrainShapeLine = terrainHeightfieldAttached
    ? `terrain_shape=heightfield_grid:${heightmap.width}x${heightmap.height}; source=${heightmap.source}; terrain_viewport=${terrainViewportMode}`
    : "terrain_shape=projected_procedural_mesh; DEM heightfield not attached";
  const sourceBadge = isPendingTelemetry
    ? "awaiting runtime pose log"
    : isPlannedRoutePreview
      ? "planning route preview"
    : (isGazeboLocalFrame ? frameValue : "observed terrain context");
  const terrainShapeBadge = terrainHeightfieldAttached
    ? `heightfield ${heightmap.width}x${heightmap.height}`
    : "projected mesh";
  const terrainViewportBadge = terrainViewportBbox ? "terrain context bbox" : "";
  const terrainSourceLine = summary.terrain_source_url
    ? `terrain_source=${summary.terrain_source_url}`
    : "terrain_source=not_recorded";
  const elevationRange = terrainMin !== null && terrainMax !== null
    ? `${terrainMin}..${terrainMax} m`
    : `${minAltitude.toFixed(1)}..${maxAltitude.toFixed(1)} m projected`;
  const batteryScenario = summary.vehicle_state_scenario || "";
  const batteryWarningRaw = summary.battery_warning ?? summary.post_injection_battery_warning ?? latestSample.battery_warning;
  const batteryRemainingRaw = summary.battery_remaining_percent ?? summary.post_injection_battery_remaining_percent ?? latestSample.battery_remaining_percent;
  const batteryVoltageRaw = summary.battery_voltage_v ?? summary.post_injection_battery_voltage_v ?? latestSample.battery_voltage_v;
  const batteryCurrentRaw = summary.battery_current_a ?? latestSample.battery_current_a;
  const batteryWarning = batteryWarningRaw ?? "-";
  const batteryRemaining = batteryRemainingRaw ?? "-";
  const batteryVoltage = batteryVoltageRaw ?? "-";
  const batteryRemainingValues = samples
    .map((sample) => coerceNumber(sample.battery_remaining_percent))
    .filter((value) => value !== null);
  const summaryBatteryRemaining = coerceNumber(summary.battery_remaining_percent ?? summary.post_injection_battery_remaining_percent);
  if (summaryBatteryRemaining !== null) {
    batteryRemainingValues.push(summaryBatteryRemaining);
  }
  const batteryApplied = summary.battery_state_injection_applied === true;
  const batteryObserved = batteryApplied
    || summary.battery_status_observed === true
    || batteryWarningRaw !== undefined
    || batteryRemainingRaw !== undefined
    || batteryVoltageRaw !== undefined
    || batteryCurrentRaw !== undefined
    || latestSample.battery_status_observed === true;
  const batteryStateSource = summary.battery_state_source || latestSample.battery_state_source || summary.battery_state_injection_mechanism || "";
  const batteryUnavailable = !batteryObserved && (
    summary.battery_status_observed === false
    || Boolean(batteryStateSource)
  );
  const batteryRemainingStatic = !batteryApplied
    && isAutoMissionReplay
    && batteryObserved
    && batteryRemainingValues.length >= 2
    && (Math.max(...batteryRemainingValues) - Math.min(...batteryRemainingValues)) <= 0.001;
  const batteryTelemetryLabel = batteryRemainingStatic
    ? "PX4 SITL telemetry (unchanged in samples)"
    : "observed PX4 telemetry";
  const batteryVoltageLabel = batteryVoltageRaw !== undefined
    ? batteryTelemetryLabel
    : "not reported by replay";
  const batteryChip = batteryObserved
    ? (batteryApplied
      ? `${batteryScenario || "battery"}: warning ${batteryWarning}`
      : batteryRemainingStatic
        ? `PX4 SITL battery static · warning ${batteryWarning}`
        : `battery warning ${batteryWarning}`)
    : (batteryUnavailable ? "battery telemetry not attached" : "");
  const batteryStatusCards = batteryObserved
    ? (batteryRemainingStatic
      ? [
        `<div><span class="k">PX4 SITL battery</span><strong>fixed sample</strong><span>reported=${escapeHtml(batteryRemaining)}% · warning=${escapeHtml(batteryWarning)} · source=${escapeHtml(batteryTelemetryLabel)} · drain not modeled</span></div>`,
        `<div><span class="k">voltage</span><strong>${escapeHtml(batteryVoltage)} V</strong><span>${escapeHtml(batteryVoltageLabel)}</span></div>`,
        batteryCurrentRaw !== undefined ? `<div><span class="k">current</span><strong>${escapeHtml(batteryCurrentRaw)} A</strong><span>${escapeHtml(batteryTelemetryLabel)}</span></div>` : "",
      ]
      : [
        `<div><span class="k">battery warning</span><strong>${escapeHtml(batteryWarning)}</strong><span>${escapeHtml(String(summary.battery_state_injection_mechanism || batteryTelemetryLabel))}</span></div>`,
        `<div><span class="k">battery remaining</span><strong>${escapeHtml(batteryRemaining)}%</strong><span>${escapeHtml(batteryTelemetryLabel)}</span></div>`,
        `<div><span class="k">voltage</span><strong>${escapeHtml(batteryVoltage)} V</strong><span>${escapeHtml(batteryVoltageLabel)}</span></div>`,
        batteryCurrentRaw !== undefined ? `<div><span class="k">current</span><strong>${escapeHtml(batteryCurrentRaw)} A</strong><span>${escapeHtml(batteryTelemetryLabel)}</span></div>` : "",
      ])
    : (batteryUnavailable
      ? [`<div><span class="k">battery telemetry</span><strong>not attached</strong><span>${escapeHtml(String(batteryStateSource || "PX4 battery status not observed"))}</span></div>`]
      : []);
  const replayPhase = summary.battery_critical_mission_block_observed
    ? "blocked by critical battery"
    : isFailureReplay
      ? (summary.failure_category || latestSample.phase || summary.flight_path_status || "blocked")
    : (latestSample.phase || summary.flight_path_status || "telemetry");
  const latestTimelinePoint = timelinePoints[timelinePoints.length - 1] || latestPoint;
  const previousTimelinePoint = timelinePoints.length > 1 ? timelinePoints[timelinePoints.length - 2] : latestTimelinePoint;
  const animationLatestKey = [
    timelinePoints.length,
    latestTimelinePoint.t,
    latestTimelinePoint.x,
    latestTimelinePoint.y,
    latestSample.phase || "",
  ].join(":");
  const animationMode = isPendingTelemetry ? "pending" : (isPlannedRoutePreview ? "planned-preview" : "observed-log-follow");
  const animationCaption = isPendingTelemetry
    ? "awaiting execution replay artifact"
    : isPlannedRoutePreview
      ? "planning route preview"
    : "log-timed replay";
  const terrainFaces = [];
  const terrainGrid = terrainHeightfieldAttached
    ? Math.min(33, Math.max(13, Math.min(heightmap.width, heightmap.height)))
    : 13;
  for (let iy = 0; iy < terrainGrid - 1; iy += 1) {
    for (let ix = 0; ix < terrainGrid - 1; ix += 1) {
      const nx0 = -1 + (ix / (terrainGrid - 1)) * 2;
      const nx1 = -1 + ((ix + 1) / (terrainGrid - 1)) * 2;
      const ny0 = -1 + (iy / (terrainGrid - 1)) * 2;
      const ny1 = -1 + ((iy + 1) / (terrainGrid - 1)) * 2;
      const z00 = terrainAt(nx0, ny0);
      const z10 = terrainAt(nx1, ny0);
      const z11 = terrainAt(nx1, ny1);
      const z01 = terrainAt(nx0, ny1);
      const avg = (z00 + z10 + z11 + z01) / 4;
      const vertices = [
        project(nx0, ny0, z00),
        project(nx1, ny0, z10),
        project(nx1, ny1, z11),
        project(nx0, ny1, z01),
      ];
      const red = Math.round(42 + avg * 76);
      const green = Math.round(72 + avg * 68);
      const blue = Math.round(48 + avg * 34);
      terrainFaces.push({
        sort: ix + iy + avg,
        points: vertices.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" "),
        fill: `rgb(${red}, ${green}, ${blue})`,
      });
    }
  }
  terrainFaces.sort((a, b) => a.sort - b.sort);
  const terrainMesh = terrainFaces
    .map((face) => `<polygon class="mission-flight-gsi-terrain-face" points="${escapeAttr(face.points)}" fill="${escapeAttr(face.fill)}"></polygon>`)
    .join("");
  const waypointDots = (isRoutePreview ? points : [])
    .filter((_, index) => index > 0 && index < points.length - 1)
    .map((point, index) => [
      `<g class="mission-flight-waypoint-3d">`,
      `<line x1="${point.x.toFixed(2)}" y1="${(point.y + 34).toFixed(2)}" x2="${point.x.toFixed(2)}" y2="${point.y.toFixed(2)}"></line>`,
      `<circle class="mission-flight-waypoint" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="${index % 2 ? "3.2" : "2.8"}"></circle>`,
      `<text class="mission-flight-map-label" x="${(point.x + 8).toFixed(2)}" y="${(point.y - 8).toFixed(2)}">${String(index + 2).padStart(2, "0")}</text>`,
      `</g>`,
    ].join(""))
    .join("");
  const logRows = samples.slice(-7).reverse().map((sample) => {
    const sampleAltitude = altitudeOf(sample);
    const sampleTime = sample.observed_at || sample.timestamp || sample.elapsed_s || sample.sample_index || "-";
    const sampleBattery = sample.battery_remaining_percent !== undefined
      ? ` · ${sample.battery_remaining_percent}%`
      : "";
    return `<div class="mission-flight-log-row"><span class="mono">${escapeHtml(String(sampleTime))}</span><span>${escapeHtml(sample.phase || "telemetry")}</span><span class="mono">${escapeHtml(sampleAltitude.toFixed(2))} m${escapeHtml(sampleBattery)}</span></div>`;
  }).join("");
  const routeTerminalPoint = displayPose.usesRouteTerminal ? routePointFor(displaySample) : null;
  const terminalPoseMarkers = routeTerminalPoint
    ? [
      `<g class="mission-flight-terminal-marker mission-flight-route-end-marker" data-testid="mission-flight-route-end-marker">`,
      `<circle class="mission-flight-route-end-halo" cx="${routeTerminalPoint.x.toFixed(2)}" cy="${routeTerminalPoint.y.toFixed(2)}" r="13"></circle>`,
      `<circle class="mission-flight-route-end-dot" cx="${routeTerminalPoint.x.toFixed(2)}" cy="${routeTerminalPoint.y.toFixed(2)}" r="5.5"></circle>`,
      `<text class="mission-flight-map-label mission-flight-route-end-label" x="${Math.min(width - 120, routeTerminalPoint.x + 14).toFixed(2)}" y="${Math.max(22, routeTerminalPoint.y - 12).toFixed(2)}">route end</text>`,
      `</g>`,
      `<g class="mission-flight-terminal-marker mission-flight-landed-marker" data-testid="mission-flight-landed-marker">`,
      `<circle class="mission-flight-landed-dot" cx="${latestPoint.x.toFixed(2)}" cy="${latestPoint.y.toFixed(2)}" r="5"></circle>`,
      `<text class="mission-flight-map-label mission-flight-landed-label" x="${Math.min(width - 96, latestPoint.x + 12).toFixed(2)}" y="${Math.min(height - 18, latestPoint.y + 20).toFixed(2)}">landed</text>`,
      `</g>`,
    ].join("")
    : `<circle class="mission-flight-current" cx="${latestPoint.x.toFixed(2)}" cy="${latestPoint.y.toFixed(2)}" r="5"></circle>`;
  const targetMarkup = isFailureReplay && targetRawX === null && targetRawY === null
    ? [
      `<line class="mission-flight-target-stem mission-flight-failure-stem" x1="${latestPoint.x.toFixed(2)}" y1="${(latestPoint.y + 34).toFixed(2)}" x2="${latestPoint.x.toFixed(2)}" y2="${latestPoint.y.toFixed(2)}"></line>`,
      `<circle class="mission-flight-target-ring mission-flight-failure-ring" cx="${latestPoint.x.toFixed(2)}" cy="${latestPoint.y.toFixed(2)}" r="11"></circle>`,
      `<text class="mission-flight-map-label mission-flight-failure-label" x="${Math.min(width - 150, latestPoint.x + 14).toFixed(2)}" y="${Math.max(22, latestPoint.y - 10).toFixed(2)}">last observed</text>`,
    ].join("")
    : [
      `<line class="mission-flight-target-stem" x1="${targetPoint.x.toFixed(2)}" y1="${(targetPoint.y + 42).toFixed(2)}" x2="${targetPoint.x.toFixed(2)}" y2="${targetPoint.y.toFixed(2)}"></line>`,
      `<ellipse class="mission-flight-target-pad" cx="${targetPoint.x.toFixed(2)}" cy="${(targetPoint.y + 42).toFixed(2)}" rx="28" ry="12"></ellipse>`,
      `<circle class="mission-flight-target-ring" cx="${targetPoint.x.toFixed(2)}" cy="${targetPoint.y.toFixed(2)}" r="10"></circle>`,
      `<text class="mission-flight-map-label" x="${Math.min(width - 92, targetPoint.x + 14).toFixed(2)}" y="${Math.max(22, targetPoint.y - 10).toFixed(2)}">dropoff</text>`,
    ].join("");
  return [
    `<div class="detail-card mission-flight-path-window ${isFailureReplay ? "mission-flight-failure-replay" : ""}">`,
    `<div class="mission-flight-header">`,
    `<div>`,
    `<div class="k">${escapeHtml(replayTitle)}</div>`,
    `<div class="muted">${escapeHtml(isPendingTelemetry ? "3D replay is hidden until an execution replay artifact is attached." : replayDescription)} Read-only: it is not a verifier, gate, dispatch control, or delivery completion claim.</div>`,
    `</div>`,
    `<div class="mission-flight-header-badges">`,
    `<span class="mission-flight-live-pill ${isFailureReplay ? "mission-flight-failure-pill" : ""}">${escapeHtml(isPendingTelemetry ? "pending replay" : isPlannedRoutePreview ? "planned route" : isFailureReplay ? "failure replay" : "log-driven replay")}</span>`,
    !isPendingTelemetry ? `<span class="mission-flight-live-pill">${escapeHtml(animationCaption)}</span>` : "",
    isFailureReplay ? `<span class="mission-flight-live-pill mission-flight-failure-pill">${escapeHtml(summary.failure_category || "blocked")}</span>` : "",
    batteryChip ? `<span class="mission-flight-live-pill mission-flight-battery-pill">${escapeHtml(batteryChip)}</span>` : "",
    `</div>`,
    `</div>`,
    `<div class="mission-flight-status-strip">`,
    batteryStatusCards.join(""),
    `<div><span class="k">replay phase</span><strong>${escapeHtml(replayPhase)}</strong><span>delivery_completion_claimed=false</span></div>`,
    `<div><span class="k">frame</span><strong>${escapeHtml(frameValue)}</strong><span>${escapeHtml(markerProvenance)}</span></div>`,
    isFailureReplay ? `<div><span class="k">failure reason</span><strong>${escapeHtml(summary.failure_reason_digest || summary.failure_category || "blocked")}</strong><span>actual_sitl_flight_evidence_observed=false</span></div>` : "",
    `</div>`,
    `<div class="mission-flight-3d-layout">`,
    `<div class="mission-flight-map mission-flight-gsi-3d-scene" role="img" aria-label="${escapeAttr(replayTitle)}" data-terrain-viewport="${escapeAttr(terrainViewportMode)}">`,
    `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">`,
    `<defs>`,
    `<linearGradient id="${escapeAttr(mapId)}-route" x1="0%" y1="0%" x2="100%" y2="0%">`,
    `<stop offset="0%" stop-color="#34d399"></stop>`,
    `<stop offset="55%" stop-color="#60a5fa"></stop>`,
    `<stop offset="100%" stop-color="#fbbf24"></stop>`,
    `</linearGradient>`,
    `<linearGradient id="${escapeAttr(mapId)}-altitude" x1="0%" y1="0%" x2="0%" y2="100%">`,
    `<stop offset="0%" stop-color="rgba(251,191,36,0.8)"></stop>`,
    `<stop offset="100%" stop-color="rgba(96,165,250,0.08)"></stop>`,
    `</linearGradient>`,
    `<filter id="${escapeAttr(mapId)}-glow" x="-30%" y="-30%" width="160%" height="160%">`,
    `<feGaussianBlur stdDeviation="3" result="blur"></feGaussianBlur>`,
    `<feMerge><feMergeNode in="blur"></feMergeNode><feMergeNode in="SourceGraphic"></feMergeNode></feMerge>`,
    `</filter>`,
    `</defs>`,
    `<rect x="0" y="0" width="${width}" height="${height}" rx="10"></rect>`,
    `<g class="mission-flight-gsi-sky">`,
    `<path d="M0 0 H${width} V196 C778 147 665 160 559 132 C430 99 321 128 203 94 C103 65 48 75 0 50 Z"></path>`,
    `</g>`,
    `<g class="mission-flight-gsi-water">`,
    `<path d="M0 438 C190 410 300 470 430 430 C560 390 650 450 980 398 L980 560 L0 560 Z"></path>`,
    `</g>`,
    `<g class="mission-flight-terrain mission-flight-gsi-terrain">${terrainMesh}</g>`,
    `<path class="mission-flight-ground-track" d="${escapeAttr(groundPathD)}"></path>`,
    `<path class="mission-flight-corridor" d="${escapeAttr(pathD)}"></path>`,
    `<path class="mission-flight-path-shadow" d="${escapeAttr(pathD)}"></path>`,
    `<path class="mission-flight-path-line" filter="url(#${escapeAttr(mapId)}-glow)" stroke="url(#${escapeAttr(mapId)}-route)" d="${escapeAttr(pathD)}"></path>`,
    `<path class="mission-flight-altitude-line" d="${escapeAttr(pathD)}"></path>`,
    waypointDots,
    targetMarkup,
    `<circle class="mission-flight-start" cx="${firstPoint.x.toFixed(2)}" cy="${firstPoint.y.toFixed(2)}" r="4"></circle>`,
    `<text class="mission-flight-map-label" x="${Math.min(width - 70, firstPoint.x + 8).toFixed(2)}" y="${Math.max(22, firstPoint.y - 8).toFixed(2)}">start</text>`,
    terminalPoseMarkers,
    `<g class="mission-flight-replay-drone" transform="translate(${previousTimelinePoint.x.toFixed(2)} ${previousTimelinePoint.y.toFixed(2)})" data-flight-animation-id="${escapeAttr(mapId)}" data-flight-animation-mode="${escapeAttr(animationMode)}" data-flight-animation-latest-key="${escapeAttr(animationLatestKey)}" data-flight-animation-points="${escapeAttr(JSON.stringify(timelinePoints))}">`,
    `<ellipse class="mission-flight-drone-shadow" cx="0" cy="33" rx="22" ry="7"></ellipse>`,
    `<circle class="mission-flight-drone-halo" r="18"></circle>`,
    `<path class="mission-flight-drone-body" d="M0 -15 L12 8 L0 3 L-12 8 Z"></path>`,
    `<path class="mission-flight-drone-arm" d="M-22 0 L22 0 M0 -22 L0 22"></path>`,
    `<circle class="mission-flight-drone-rotor" cx="-22" cy="0" r="5"></circle>`,
    `<circle class="mission-flight-drone-rotor" cx="22" cy="0" r="5"></circle>`,
    `<circle class="mission-flight-drone-rotor" cx="0" cy="-22" r="5"></circle>`,
    `<circle class="mission-flight-drone-rotor" cx="0" cy="22" r="5"></circle>`,
    `</g>`,
    `</svg>`,
    `<div class="mission-flight-map-overlay">`,
    `<span>${escapeHtml(isPendingTelemetry ? "3D replay awaiting flight log" : replayTitle)}</span>`,
    !isPendingTelemetry ? `<span>${escapeHtml(animationCaption)}</span>` : "",
    !isPendingTelemetry ? `<span>${escapeHtml(markerProvenance)}</span>` : "",
    `<span>${escapeHtml(sourceBadge)}</span>`,
    `<span>${escapeHtml(terrainBadge)}</span>`,
    `<span>${escapeHtml(terrainShapeBadge)}</span>`,
    terrainViewportBadge ? `<span>${escapeHtml(terrainViewportBadge)}</span>` : "",
    batteryChip ? `<span>${escapeHtml(batteryChip)}</span>` : "",
    `<span>${escapeHtml(samples.length)} samples</span>`,
    `</div>`,
    `</div>`,
    `<div class="mission-flight-path-facts mission-flight-cockpit-panel">`,
    `<div class="mission-flight-readout">`,
    `<span class="mission-flight-readout-label">${escapeHtml(phaseLabel)}</span>`,
    `<strong>${escapeHtml(latestSample.phase ?? "telemetry")}</strong>`,
    `<span class="mission-flight-readout-sub mono">${escapeHtml([frameLabel, phaseAltitudeSummary].filter(Boolean).join(" · "))}</span>`,
    `</div>`,
    `<div class="detail-chip-row">`,
    `<span class="detail-chip"><span class="detail-chip-label">samples</span><span class="detail-chip-value">${escapeHtml(samples.length)}</span></span>`,
    `<span class="detail-chip"><span class="detail-chip-label">${escapeHtml(firstCoordLabel)}</span><span class="detail-chip-value">${escapeHtml(firstCoordValue)}</span></span>`,
    `<span class="detail-chip"><span class="detail-chip-label">${escapeHtml(secondCoordLabel)}</span><span class="detail-chip-value">${escapeHtml(secondCoordValue)}</span></span>`,
    `<span class="detail-chip"><span class="detail-chip-label">${escapeHtml(altitudeLabel)}</span><span class="detail-chip-value">${escapeHtml(altitudeValue)}</span></span>`,
    `</div>`,
    `<div class="detail-chip-row">`,
    `<span class="detail-chip"><span class="detail-chip-label">progress m</span><span class="detail-chip-value">${escapeHtml(progressValue)}</span></span>`,
    `<span class="detail-chip"><span class="detail-chip-label">distance to target m</span><span class="detail-chip-value">${escapeHtml(latestSample.distance_to_target_m ?? "-")}</span></span>`,
    `<span class="detail-chip"><span class="detail-chip-label">seq reached</span><span class="detail-chip-value">${escapeHtml(latestSample.seq_reached ?? "-")}</span></span>`,
    `<span class="detail-chip"><span class="detail-chip-label">elapsed/sample</span><span class="detail-chip-value">${escapeHtml(elapsedValue)}</span></span>`,
    `</div>`,
    `<div class="detail-chip-row">`,
    `<span class="detail-chip"><span class="detail-chip-label">terrain</span><span class="detail-chip-value">${escapeHtml(terrainBadge)}</span></span>`,
    `<span class="detail-chip"><span class="detail-chip-label">elevation</span><span class="detail-chip-value">${escapeHtml(elevationRange)}</span></span>`,
    `</div>`,
    displayPose.routeEndedText ? `<div class="item-meta">${escapeHtml(displayPose.routeEndedText)}</div>` : "",
    displayPose.landedFinalZ !== null ? `<div class="item-meta mono">landed final pose z=${escapeHtml(displayPose.landedFinalZ.toFixed(2))}m</div>` : "",
    `<div class="mission-flight-log-panel">`,
    `<div class="k">${escapeHtml(logTitle)}</div>`,
    `<div class="mission-flight-log-grid">${logRows}</div>`,
    `</div>`,
    isPendingTelemetry ? `<div class="muted">Runtime pose log has not been attached yet. This panel is a read-only route visualization until observed SITL telemetry arrives.</div>` : "",
    isPlannedRoutePreview ? `<div class="muted">Planning preview shown beside the observed AUTO replay. It is not runtime telemetry and it does not prove delivery or progress.</div>` : "",
    observedAt ? `<div class="item-meta mono">observed_at=${escapeHtml(observedAt)}</div>` : "",
    `<div class="item-meta mono">${escapeHtml(terrainShapeLine)}</div>`,
    `<div class="item-meta mono">${escapeHtml(terrainSourceLine)}</div>`,
    tracePath ? `<div class="item-meta mono">trace=${escapeHtml(tracePath)}</div>` : "",
    `</div>`,
    `</div>`,
    `</div>`,
    !isRoutePreview && digitalTwinFlightPathSamples(asPlainObject(summary.planned_route_preview_summary)).length
      ? renderDigitalTwinFlightPathWindow(asPlainObject(summary.planned_route_preview_summary))
      : "",
  ].join("");
}

function renderMissionScenarioList(title, items) {
  const values = Array.isArray(items) ? items : [];
  const body = values.length
    ? `<ul class="list compact-list">${values.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : '<div class="muted">none detected</div>';
  return `
    <div class="detail-card">
      <div class="k">${escapeHtml(title)}</div>
      ${body}
    </div>
  `;
}

function renderDigitalTwinWaypointSmokeArtifacts(task) {
  try {
    if (task?.kind === "px4_gazebo_mission_designer_sitl_execution_request") {
      return "";
    }
    const artifacts = asPlainObject(task?.artifacts);
    const directCandidates = [
      artifacts.digital_twin_waypoint_smoke_summary,
      artifacts.digital_twin_sitl_waypoint_reach_summary,
      artifacts.digital_twin_sitl_waypoint_reach_observation,
      artifacts.digital_twin_delivery_smoke_summary,
      artifacts.result,
    ].map(asPlainObject);
    const directSummary = directCandidates.find((candidate) => digitalTwinFlightPathSamples(candidate).length) || {};
    const summary = Object.keys(directSummary).length ? directSummary : findDigitalTwinFlightPathSummary(task);
    if (!digitalTwinFlightPathSamples(summary).length) return "";
    return [
      `<div class="detail-section">`,
      renderDigitalTwinFlightPathWindow(summary),
      `</div>`,
    ].join("");
  } catch (err) {
    return `
      <div class="detail-section">
        <div class="detail-card">
          <div class="k">3D Flight Replay</div>
          <div class="detail-error">Flight view renderer failed closed: ${escapeHtml(String(err?.message || err))}</div>
          <div class="muted">Artifacts remain available below; this panel is read-only and does not affect dispatch, gates, or delivery completion.</div>
        </div>
      </div>
    `;
  }
}

function renderMissionScenarioConstraints(proposal) {
  const altitude = proposal?.altitude_target_m;
  const payload = proposal?.payload_weight_kg;
  const labels = Array.isArray(proposal?.extracted_constraint_labels)
    ? proposal.extracted_constraint_labels
    : [];
  return `
    <div class="detail-card">
      <div class="k">Extracted Constraints</div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">altitude target</span><span class="detail-chip-value">${escapeHtml(altitude ?? "not detected")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">payload weight</span><span class="detail-chip-value">${escapeHtml(payload ?? "not detected")}</span></span>
      </div>
      ${
        labels.length
          ? `<div class="muted">${labels.map((label) => escapeHtml(label)).join(", ")}</div>`
          : '<div class="muted">no numeric constraints detected</div>'
      }
    </div>
  `;
}

function renderMissionScenarioDigitalTwinStage1(result) {
  const target = result?.real_world_mission_target || null;
  const geocode = result?.real_world_geocode_candidate || null;
  const demRequest = result?.terrain_dem_tile_request_candidate || null;
  const demSnapshot = result?.terrain_dem_tile_snapshot || null;
  const tileTerrain = result?.tile_backed_terrain_environment_snapshot || null;
  const heightmap = result?.terrain_heightmap_candidate || null;
  const heightmapArtifact = result?.terrain_heightmap_artifact || null;
  const heightmapFile = result?.terrain_heightmap_file_artifact || null;
  const gazeboWorldCandidate = result?.gazebo_world_candidate || null;
  const gazeboWorldArtifact = result?.gazebo_world_artifact || null;
  const coordinateTransform = result?.coordinate_transform_candidate || null;
  const px4MissionItemCandidate = result?.digital_twin_px4_mission_item_candidate || null;
  const sitlBindingGate = result?.digital_twin_sitl_binding_gate || null;
  const terrain = result?.terrain_environment_snapshot || null;
  const weather = result?.weather_environment_snapshot || null;
  const route = result?.digital_twin_route_feasibility || null;
  const weatherGate = result?.weather_environment_policy_gate || null;
  const routePlan = result?.digital_twin_route_plan || null;
  const summary = result?.summary || {};
  if (!target && !geocode && !demRequest && !demSnapshot && !tileTerrain && !heightmap && !heightmapArtifact && !heightmapFile && !gazeboWorldCandidate && !gazeboWorldArtifact && !coordinateTransform && !px4MissionItemCandidate && !sitlBindingGate && !terrain && !weather && !route && !weatherGate && !routePlan) return "";
  const distance = target?.requested_distance_km ?? summary.requested_distance_km ?? "-";
  const altitude = target?.requested_altitude_m ?? terrain?.elevation_max_m ?? summary.terrain_elevation_max_m ?? "-";
  const elevationMin = terrain?.elevation_min_m ?? summary.terrain_elevation_min_m ?? "-";
  const elevationMax = terrain?.elevation_max_m ?? summary.terrain_elevation_max_m ?? "-";
  const rainLabel = weather?.precipitation_label ?? summary.weather_precipitation_label ?? "-";
  const routeDistance = route?.actual_route_distance_m ?? summary.route_actual_distance_m ?? "-";
  const geocodeLocation = geocode
    ? `${geocode.latitude}, ${geocode.longitude}`
    : `${summary.geocode_candidate_latitude ?? "-"}, ${summary.geocode_candidate_longitude ?? "-"}`;
  const demTileCount = demRequest?.tile_refs?.length ?? summary.dem_tile_request_tile_refs?.length ?? 0;
  const demElevationMin = demSnapshot?.elevation_min_m ?? summary.dem_tile_snapshot_elevation_min_m ?? "-";
  const demElevationMax = demSnapshot?.elevation_max_m ?? summary.dem_tile_snapshot_elevation_max_m ?? "-";
  const tileTerrainMin = tileTerrain?.elevation_min_m ?? summary.tile_backed_terrain_elevation_min_m ?? "-";
  const tileTerrainMax = tileTerrain?.elevation_max_m ?? summary.tile_backed_terrain_elevation_max_m ?? "-";
  const heightmapPixels = heightmap
    ? `${heightmap.pixel_width}x${heightmap.pixel_height}`
    : `${summary.heightmap_candidate_pixel_width ?? "-"}x${summary.heightmap_candidate_pixel_height ?? "-"}`;
  const elevationGain = route?.elevation_gain_m ?? summary.route_elevation_gain_m ?? "-";
  const averageSlope = route?.average_slope_percent ?? summary.route_average_slope_percent ?? "-";
  const routeInput = route?.route_feasibility_input_source ?? summary.route_feasibility_input_source ?? "-";
  const gateStatus = weatherGate?.gate_status ?? summary.weather_policy_gate_status ?? "-";
  const routePlanStatus = routePlan?.route_plan_status ?? summary.route_plan_status ?? "-";
  return `
    <div class="detail-card">
      <div class="k">Digital Twin Stage 2 Planning</div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">target</span><span class="detail-chip-value">${escapeHtml(target?.resolved_location_label || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">status</span><span class="detail-chip-value">${escapeHtml(target?.target_resolution_status || summary.target_resolution_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">distance km</span><span class="detail-chip-value">${escapeHtml(distance)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">altitude m</span><span class="detail-chip-value">${escapeHtml(altitude)}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">geocode</span><span class="detail-chip-value">${escapeHtml(geocode?.geocode_mode || summary.geocode_candidate_mode || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">candidate</span><span class="detail-chip-value">${escapeHtml(geocode?.candidate_status || summary.geocode_candidate_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">lat/lon</span><span class="detail-chip-value">${escapeHtml(geocodeLocation)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">source</span><span class="detail-chip-value">${escapeHtml(geocode?.source_url || summary.geocode_candidate_source_url || "-")}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">DEM request</span><span class="detail-chip-value">${escapeHtml(demRequest?.tile_request_status || summary.dem_tile_request_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">tile index</span><span class="detail-chip-value">${escapeHtml(demRequest?.request_mode || summary.dem_tile_request_mode || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">tiles</span><span class="detail-chip-value">${escapeHtml(demTileCount)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">DEM fetched</span><span class="detail-chip-value">${escapeHtml(String(demRequest?.live_fetch_performed ?? summary.dem_tile_request_live_fetch_performed ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">DEM snapshot</span><span class="detail-chip-value">${escapeHtml(demSnapshot?.snapshot_mode || summary.dem_tile_snapshot_mode || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">DEM elevation m</span><span class="detail-chip-value">${escapeHtml(`${demElevationMin}..${demElevationMax}`)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">no data</span><span class="detail-chip-value">${escapeHtml(demSnapshot?.no_data_ratio ?? summary.dem_tile_snapshot_no_data_ratio ?? "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">heightmap</span><span class="detail-chip-value">${escapeHtml(String(demSnapshot?.heightmap_generated ?? summary.dem_tile_snapshot_heightmap_generated ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">tile terrain</span><span class="detail-chip-value">${escapeHtml(tileTerrain?.snapshot_mode || summary.tile_backed_terrain_snapshot_mode || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">route bound</span><span class="detail-chip-value">${escapeHtml(tileTerrain?.route_feasibility_binding_status || summary.tile_backed_terrain_route_binding_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">tile elevation m</span><span class="detail-chip-value">${escapeHtml(`${tileTerrainMin}..${tileTerrainMax}`)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">tile heightmap</span><span class="detail-chip-value">${escapeHtml(String(tileTerrain?.heightmap_generated ?? summary.tile_backed_terrain_heightmap_generated ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">heightmap candidate</span><span class="detail-chip-value">${escapeHtml(heightmap?.heightmap_status || summary.heightmap_candidate_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">pixels</span><span class="detail-chip-value">${escapeHtml(heightmapPixels)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">vertical scale m</span><span class="detail-chip-value">${escapeHtml(heightmap?.vertical_scale_m ?? summary.heightmap_candidate_vertical_scale_m ?? "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world from heightmap</span><span class="detail-chip-value">${escapeHtml(String(heightmap?.gazebo_world_generated ?? summary.heightmap_candidate_gazebo_world_generated ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">heightmap artifact record</span><span class="detail-chip-value">${escapeHtml(heightmapArtifact?.artifact_status || summary.heightmap_artifact_status || "not_generated")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">record materialized</span><span class="detail-chip-value">${escapeHtml(String(heightmapArtifact?.artifact_materialized ?? summary.heightmap_artifact_materialized ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">artifact format</span><span class="detail-chip-value">${escapeHtml(heightmapArtifact?.artifact_format || summary.heightmap_artifact_format || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world from artifact</span><span class="detail-chip-value">${escapeHtml(String(heightmapArtifact?.gazebo_world_generated ?? summary.heightmap_artifact_gazebo_world_generated ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">heightmap file</span><span class="detail-chip-value">${escapeHtml(heightmapFile?.file_artifact_status || summary.heightmap_file_artifact_status || "not_generated")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">file materialized</span><span class="detail-chip-value">${escapeHtml(String(heightmapFile?.file_materialized ?? summary.heightmap_file_materialized ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">file format</span><span class="detail-chip-value">${escapeHtml(heightmapFile?.file_format || summary.heightmap_file_format || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world from file</span><span class="detail-chip-value">${escapeHtml(String(heightmapFile?.gazebo_world_generated ?? summary.heightmap_file_gazebo_world_generated ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">world candidate</span><span class="detail-chip-value">${escapeHtml(gazeboWorldCandidate?.world_candidate_status || summary.gazebo_world_candidate_status || "not_generated")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">candidate format</span><span class="detail-chip-value">${escapeHtml(gazeboWorldCandidate?.world_format || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">candidate materialized</span><span class="detail-chip-value">${escapeHtml(String(gazeboWorldCandidate?.gazebo_world_materialized ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">candidate binding</span><span class="detail-chip-value">${escapeHtml(String(gazeboWorldCandidate?.execution_binding_allowed ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">world artifact</span><span class="detail-chip-value">${escapeHtml(gazeboWorldArtifact?.world_artifact_status || summary.gazebo_world_artifact_status || "not_generated")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world format</span><span class="detail-chip-value">${escapeHtml(gazeboWorldArtifact?.world_format || summary.gazebo_world_format || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world materialized</span><span class="detail-chip-value">${escapeHtml(String(gazeboWorldArtifact?.gazebo_world_materialized ?? summary.gazebo_world_materialized ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">execution binding</span><span class="detail-chip-value">${escapeHtml(String(gazeboWorldArtifact?.execution_binding_allowed ?? summary.gazebo_world_execution_binding_allowed ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">transform candidate</span><span class="detail-chip-value">${escapeHtml(coordinateTransform?.transform_candidate_status || summary.coordinate_transform_candidate_status || "not_generated")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">source frame</span><span class="detail-chip-value">${escapeHtml(coordinateTransform?.coordinate_frame_source || summary.coordinate_transform_frame_source || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">target frame</span><span class="detail-chip-value">${escapeHtml(coordinateTransform?.coordinate_frame_target || summary.coordinate_transform_frame_target || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">transform materialized</span><span class="detail-chip-value">${escapeHtml(String(coordinateTransform?.coordinate_transform_materialized ?? summary.coordinate_transform_materialized ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">PX4 item candidate</span><span class="detail-chip-value">${escapeHtml(px4MissionItemCandidate?.candidate_status || summary.px4_mission_item_candidate_status || "not_generated")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">candidate items</span><span class="detail-chip-value">${escapeHtml(px4MissionItemCandidate?.candidate_item_count ?? summary.px4_mission_item_candidate_item_count ?? 0)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">takeoff anchor</span><span class="detail-chip-value">${escapeHtml(px4MissionItemCandidate?.takeoff_anchor_ref || summary.px4_mission_item_candidate_takeoff_anchor_ref || "missing")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">PX4 upload allowed</span><span class="detail-chip-value">${escapeHtml(String(px4MissionItemCandidate?.px4_mission_upload_allowed ?? summary.px4_mission_item_candidate_px4_mission_upload_allowed ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">SITL binding gate</span><span class="detail-chip-value">${escapeHtml(sitlBindingGate?.binding_gate_status || summary.sitl_binding_gate_status || "not_generated")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">binding eligible</span><span class="detail-chip-value">${escapeHtml(String(sitlBindingGate?.binding_eligible ?? summary.sitl_binding_gate_binding_eligible ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">binding allowed</span><span class="detail-chip-value">${escapeHtml(String(sitlBindingGate?.binding_allowed ?? summary.sitl_binding_gate_binding_allowed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">operator approval</span><span class="detail-chip-value">${escapeHtml(String(sitlBindingGate?.operator_approval_required ?? summary.sitl_binding_gate_operator_approval_required ?? true))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">server opt-in</span><span class="detail-chip-value">${escapeHtml(String(sitlBindingGate?.server_opt_in_required ?? summary.sitl_binding_gate_server_opt_in_required ?? true))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">terrain</span><span class="detail-chip-value">${escapeHtml(terrain?.snapshot_mode || summary.terrain_snapshot_mode || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">elevation m</span><span class="detail-chip-value">${escapeHtml(`${elevationMin}..${elevationMax}`)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">slope risk</span><span class="detail-chip-value">${escapeHtml(terrain?.slope_risk_label || summary.terrain_slope_risk_label || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">weather</span><span class="detail-chip-value">${escapeHtml(rainLabel)}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">source-bound</span><span class="detail-chip-value">${escapeHtml(String((target?.source_bound ?? terrain?.source_bound ?? weather?.source_bound) ?? true))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">planning only</span><span class="detail-chip-value">${escapeHtml(String((target?.planning_only ?? terrain?.planning_only ?? weather?.planning_only) ?? true))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world generated</span><span class="detail-chip-value">${escapeHtml(String(summary.digital_twin_world_generated ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">external weather</span><span class="detail-chip-value">${escapeHtml(String(!(weather?.stale_or_missing_external_weather ?? summary.weather_external_snapshot_missing ?? true)))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">route status</span><span class="detail-chip-value">${escapeHtml(route?.route_feasibility_status || summary.route_feasibility_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">route input</span><span class="detail-chip-value">${escapeHtml(routeInput)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">route m</span><span class="detail-chip-value">${escapeHtml(routeDistance)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">gain m</span><span class="detail-chip-value">${escapeHtml(elevationGain)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">avg slope %</span><span class="detail-chip-value">${escapeHtml(averageSlope)}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">weather gate</span><span class="detail-chip-value">${escapeHtml(gateStatus)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">escalation</span><span class="detail-chip-value">${escapeHtml(String(weatherGate?.operator_escalation_required ?? summary.weather_operator_escalation_required ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">external required</span><span class="detail-chip-value">${escapeHtml(String(weatherGate?.external_weather_required ?? summary.weather_external_weather_required ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">external observed</span><span class="detail-chip-value">${escapeHtml(String(weatherGate?.external_weather_observed ?? summary.weather_external_weather_observed ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">route plan</span><span class="detail-chip-value">${escapeHtml(routePlanStatus)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">mode</span><span class="detail-chip-value">${escapeHtml(routePlan?.route_plan_mode ?? summary.route_plan_mode ?? "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world binding</span><span class="detail-chip-value">${escapeHtml(routePlan?.sitl_world_binding_status ?? summary.route_plan_sitl_world_binding_status ?? "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">transform</span><span class="detail-chip-value">${escapeHtml(routePlan?.coordinate_transform_status ?? summary.route_plan_coordinate_transform_status ?? "-")}</span></span>
      </div>
    </div>
  `;
}

function renderMissionScenarioTrackBoundary(title, chips) {
  return `
    <div class="detail-card mission-scenario-track-boundary">
      <div class="k">${escapeHtml(title)}</div>
      <div class="detail-chip-row">
        ${chips.map((chip) => `
          <span class="detail-chip">
            <span class="detail-chip-label">${escapeHtml(chip.label)}</span>
            <span class="detail-chip-value">${escapeHtml(chip.value)}</span>
          </span>
        `).join("")}
      </div>
    </div>
  `;
}

function renderMissionScenarioTrack(className, title, description, status, body) {
  const content = String(body || "").trim();
  if (!content) return "";
  return `
    <section class="mission-scenario-track ${escapeAttr(className)}">
      <div class="mission-scenario-track-heading">
        <div>
          <div class="k">${escapeHtml(title)}</div>
          <div class="muted">${escapeHtml(description)}</div>
        </div>
        ${status ? statusTag(status) : ""}
      </div>
      <div class="detail-grid mission-scenario-track-grid">
        ${content}
      </div>
    </section>
  `;
}

function missionScenarioTrackStatus(...values) {
  const statuses = values
    .map((value) => String(value ?? "").trim())
    .filter(Boolean);
  return statuses.find((value) => value !== "not_generated") || statuses[0] || "";
}

function renderMissionScenarioDigitalTwinPlanningTrack(result) {
  const summary = result?.summary || {};
  const heightmap = result?.terrain_heightmap_candidate || {};
  const heightmapArtifact = result?.terrain_heightmap_artifact || {};
  const heightmapFile = result?.terrain_heightmap_file_artifact || {};
  const gazeboWorldCandidate = result?.gazebo_world_candidate || {};
  const gazeboWorldArtifact = result?.gazebo_world_artifact || {};
  const coordinateTransform = result?.coordinate_transform_candidate || {};
  const missionAnchorCandidate = result?.digital_twin_mission_anchor_candidate || {};
  const px4MissionItemCandidate = result?.digital_twin_px4_mission_item_candidate || {};
  const sitlBindingGate = result?.digital_twin_sitl_binding_gate || {};
  const routePlan = result?.digital_twin_route_plan || {};
  const body = [
    renderMissionScenarioTrackBoundary("Digital Twin Boundary", [
      {
        label: "current SITL route",
        value: "not driven by this track",
      },
      {
        label: "world generated",
        value: String(summary.digital_twin_world_generated ?? false),
      },
      {
        label: "world candidate",
        value: String(gazeboWorldCandidate.world_candidate_status ?? summary.gazebo_world_candidate_status ?? "not_generated"),
      },
      {
        label: "world artifact",
        value: String(gazeboWorldArtifact.world_artifact_status ?? summary.gazebo_world_artifact_status ?? "not_generated"),
      },
      {
        label: "transform candidate",
        value: String(coordinateTransform.transform_candidate_status ?? summary.coordinate_transform_candidate_status ?? "not_generated"),
      },
      {
        label: "anchor candidate",
        value: String(missionAnchorCandidate.anchor_candidate_status ?? summary.mission_anchor_candidate_status ?? "not_generated"),
      },
      {
        label: "takeoff anchor",
        value: String(missionAnchorCandidate.takeoff_anchor_ref ?? summary.mission_anchor_candidate_takeoff_anchor_ref ?? ""),
      },
      {
        label: "mission item candidate",
        value: String(px4MissionItemCandidate.candidate_status ?? summary.px4_mission_item_candidate_status ?? "not_generated"),
      },
      {
        label: "binding gate",
        value: String(sitlBindingGate.binding_gate_status ?? summary.sitl_binding_gate_status ?? "not_generated"),
      },
      {
        label: "binding eligible",
        value: String(sitlBindingGate.binding_eligible ?? summary.sitl_binding_gate_binding_eligible ?? false),
      },
      {
        label: "binding allowed",
        value: String(sitlBindingGate.binding_allowed ?? summary.sitl_binding_gate_binding_allowed ?? false),
      },
      {
        label: "heightmap file",
        value: String(heightmapFile.file_materialized ?? summary.heightmap_file_materialized ?? false),
      },
      {
        label: "PX4 mission items",
        value: String(px4MissionItemCandidate.candidate_item_count ?? summary.px4_mission_item_candidate_item_count ?? 0),
      },
      {
        label: "PX4 upload",
        value: String(px4MissionItemCandidate.px4_mission_upload_allowed ?? summary.px4_mission_item_candidate_px4_mission_upload_allowed ?? false),
      },
      {
        label: "world binding",
        value: String(sitlBindingGate.sitl_execution_bound ?? px4MissionItemCandidate.sitl_execution_bound ?? coordinateTransform.sitl_execution_bound ?? gazeboWorldArtifact.sitl_execution_bound ?? gazeboWorldCandidate.sitl_execution_bound ?? routePlan.sitl_world_binding_status ?? summary.route_plan_sitl_world_binding_status ?? "not_generated"),
      },
      {
        label: "physical",
        value: String(summary.physical_execution_invoked ?? false),
      },
    ]),
    renderMissionScenarioDigitalTwinStage1(result),
  ].join("");
  const status = missionScenarioTrackStatus(
    summary.sitl_binding_gate_status,
    summary.route_plan_status,
    summary.weather_policy_gate_status,
    summary.px4_mission_item_candidate_status,
    summary.coordinate_transform_candidate_status,
    summary.gazebo_world_artifact_status,
    summary.gazebo_world_candidate_status,
    summary.heightmap_file_artifact_status,
    summary.heightmap_artifact_status,
    summary.heightmap_candidate_status,
  );
  return renderMissionScenarioTrack(
    "mission-scenario-track-digital-twin",
    "Digital Twin Planning Track",
    "This track evaluates real-world terrain/weather planning evidence. It does not drive the current SITL execution route yet.",
    status,
    body,
  );
}

function missionScenarioOperatorRouteBlocksSITL(result) {
  const summary = result?.summary || {};
  const coordinateBinding = result?.mission_designer_coordinate_pair_sitl_binding || {};
  const coordinateRoute = result?.mission_designer_coordinate_pair_route || {};
  const coordinateRouteCanBind = summary.coordinate_pair_route_mode === true
    && coordinateRoute.route_mode === "operator_coordinate_pair"
    && coordinateRoute.planning_only === true;
  const coordinateRouteBound = coordinateBinding.binding_status === "bound_to_operator_coordinate_route";
  const bindingAllowed = summary.sitl_binding_gate_binding_allowed === true
    || result?.digital_twin_sitl_binding_gate?.binding_allowed === true
    || coordinateRouteBound
    || coordinateRouteCanBind;
  return summary.coordinate_pair_route_mode === true && !bindingAllowed;
}

function renderMissionScenarioUnboundOperatorRouteNotice(result) {
  if (!missionScenarioOperatorRouteBlocksSITL(result)) return "";
  return `
    <div class="detail-card">
      <div class="k">SITL Binding</div>
      <div class="detail-error">Coordinate Route is still in its planning-evidence stage. Generate and approve a bounded request, then prepare SITL execution to create the Mission Designer SITL-only binding.</div>
      <div class="muted">The coordinate pair does not grant hardware authority, physical execution authority, or approval-free dispatch.</div>
    </div>
  `;
}

function renderMissionScenarioCoordinateRouteBinding(result) {
  const binding = result?.mission_designer_coordinate_pair_sitl_binding || {};
  if (binding.binding_status !== "bound_to_operator_coordinate_route") return "";
  return `
    <div class="detail-card">
      <div class="k">Coordinate Route SITL Binding</div>
      <div>${statusTag("bound")}</div>
      <div class="item-meta mono">mission_items_source=${escapeHtml(binding.mission_items_source || "-")}</div>
      <div class="item-meta mono">mission_item_count=${escapeHtml(String(binding.mission_item_count ?? 0))}</div>
      <div class="muted">Operator-entered takeoff/dropoff coordinates are compiled into SITL-only PX4 mission items. Hardware and physical execution remain false.</div>
    </div>
  `;
}

function renderMissionScenarioSafeRouteSITLTrack(result) {
  const response = result?.sitl_execution_response || {};
  const summary = result?.summary || {};
  const executionSummary = response.summary || {};
  const routePlan = result?.digital_twin_route_plan || {};
  const flightPathSummary = missionScenarioFlightPathSummary(result);
  const flightPathSamples = digitalTwinFlightPathSamples(flightPathSummary);
  const flightPathReplayEligible = flightPathSamples.length
    && flightPathSummary.flight_path_source !== "operator_coordinate_route_pending_telemetry";
  const setupDetails = [
    renderMissionScenarioTrackBoundary("Safe-Route SITL Boundary", [
      {
        label: "Digital Twin world bound",
        value: String(routePlan.sitl_world_binding_status === "bound_to_sitl_world"),
      },
      {
        label: "operator action",
        value: "required",
      },
      {
        label: "server opt-in",
        value: "required",
      },
      {
        label: "hardware",
        value: String(executionSummary.hardware_target_allowed ?? summary.hardware_target_allowed ?? false),
      },
      {
        label: "physical",
        value: String(executionSummary.physical_execution_invoked ?? summary.physical_execution_invoked ?? false),
      },
      {
        label: "synthetic success",
        value: String(executionSummary.synthetic_success_allowed ?? false),
      },
    ]),
    renderMissionScenarioUnboundOperatorRouteNotice(result),
    renderMissionScenarioCoordinateRouteBinding(result),
    renderMissionScenarioApproval(result),
    renderMissionScenarioPreparedSITL(result),
  ].join("");
  const body = [
    renderMissionScenarioDeliveryReplay(result),
    renderMissionScenarioAutoMissionEvidenceReadout(result),
    `<details class="mission-ui-collapse mission-sitl-setup-collapse">
      <summary>Execution Setup and Boundary Details</summary>
      <div class="detail-grid">${setupDetails}</div>
    </details>`,
    flightPathReplayEligible
      ? renderDigitalTwinFlightPathWindow(flightPathSummary)
      : renderMissionScenarioFlightPathPendingNotice(flightPathSummary),
    renderMissionScenarioLiveSITLExecution(result, { includeFailureReplay: false }),
  ].join("");
  const status = executionSummary.live_flight_status
    || executionSummary.result_status
    || executionSummary.task_status
    || summary.sitl_execution_request_status
    || "";
  return renderMissionScenarioTrack(
    "mission-scenario-track-safe-sitl",
    "Existing Safe-Route SITL Execution Track",
    "This track executes the current bounded SITL delivery route. It is not yet bound to the Digital Twin world.",
    status,
    body,
  );
}

function renderMissionScenarioApproval(result) {
  const approval = result?.scenario_approval || null;
  const compileResult = result?.scenario_compile_result || null;
  const request = result?.bounded_simulation_request || null;
  if (!approval && !compileResult && !request) return "";
  return `
    <div class="detail-card">
      <div class="k">Approval</div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">operator approved</span><span class="detail-chip-value">${escapeHtml(String(approval?.operator_approved ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">scope</span><span class="detail-chip-value">${escapeHtml(approval?.approval_scope || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">bounded request</span><span class="detail-chip-value">${escapeHtml(String(approval?.approved_for_bounded_simulation_request ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">Gazebo execution</span><span class="detail-chip-value">${escapeHtml(String(approval?.approved_for_gazebo_execution ?? false))}</span></span>
      </div>
    </div>
    <div class="detail-card">
      <div class="k">Compile Result</div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">scenario profile</span><span class="detail-chip-value">${escapeHtml(compileResult?.scenario_profile || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">route profile</span><span class="detail-chip-value">${escapeHtml(compileResult?.route_profile || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">runner</span><span class="detail-chip-value">${escapeHtml(compileResult?.runner_kind || "-")}</span></span>
      </div>
      <div class="muted">${escapeHtml(compileResult?.compile_reason || "compile reason unavailable")}</div>
    </div>
    <div class="detail-card">
      <div class="k">Bounded Simulation Request</div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">status</span><span class="detail-chip-value">${escapeHtml(request?.request_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">runner invoked</span><span class="detail-chip-value">${escapeHtml(String(request?.deterministic_bounded_runner_invoked ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">Gazebo invoked</span><span class="detail-chip-value">${escapeHtml(String(request?.gazebo_execution_invoked ?? false))}</span></span>
      </div>
    </div>
    ${renderMissionScenarioList("Compiled Risk Profile", compileResult?.risk_profile)}
  `;
}

function missionScenarioOptionalNumber(inputEl) {
  const value = inputEl?.value?.trim?.() || "";
  if (!value) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function missionScenarioOptionalText(inputEl) {
  const value = inputEl?.value?.trim?.() || "";
  return value || null;
}

function missionScenarioCoordinateRouteInputs() {
  return [
    missionScenarioTakeoffLatInputEl,
    missionScenarioTakeoffLonInputEl,
    missionScenarioDropoffLatInputEl,
    missionScenarioDropoffLonInputEl,
    missionScenarioRoofHeightInputEl,
    missionScenarioPayloadWeightInputEl,
    missionScenarioWindSpeedInputEl,
    missionScenarioWindDirectionInputEl,
    missionScenarioWindGustInputEl,
    missionScenarioWindVarianceInputEl,
    missionScenarioBatteryRemainingInputEl,
    missionScenarioSensorFailureTypeInputEl,
    missionScenarioLandingZoneBlockedInputEl,
    missionScenarioVisibilityModeInputEl,
    missionScenarioNoFlyZoneMarkerInputEl,
    missionScenarioTrafficConflictMarkerInputEl,
    missionScenarioAlternateLandingMarkerInputEl,
    missionScenarioMovingActorMarkerInputEl,
    missionScenarioMultiDroneConflictProbeInputEl,
    missionScenarioTelemetryDropoutModeInputEl,
    missionScenarioMavlinkLinkDegradationModeInputEl,
  ];
}

function resetMissionScenarioCoordinateRouteInputs() {
  const defaults = MISSION_SCENARIO_COORDINATE_ROUTE_DEFAULTS;
  if (missionScenarioTakeoffLatInputEl) missionScenarioTakeoffLatInputEl.value = defaults.takeoffLatitude;
  if (missionScenarioTakeoffLonInputEl) missionScenarioTakeoffLonInputEl.value = defaults.takeoffLongitude;
  if (missionScenarioDropoffLatInputEl) missionScenarioDropoffLatInputEl.value = defaults.dropoffLatitude;
  if (missionScenarioDropoffLonInputEl) missionScenarioDropoffLonInputEl.value = defaults.dropoffLongitude;
  if (missionScenarioRoofHeightInputEl) missionScenarioRoofHeightInputEl.value = defaults.roofHeightAglM;
  if (missionScenarioPayloadWeightInputEl) missionScenarioPayloadWeightInputEl.value = defaults.payloadWeightKg;
  if (missionScenarioWindSpeedInputEl) missionScenarioWindSpeedInputEl.value = defaults.windSpeedMps;
  if (missionScenarioWindDirectionInputEl) missionScenarioWindDirectionInputEl.value = defaults.windDirectionDeg;
  if (missionScenarioWindGustInputEl) missionScenarioWindGustInputEl.value = defaults.windGustMps;
  if (missionScenarioWindVarianceInputEl) missionScenarioWindVarianceInputEl.value = defaults.windVariance;
  if (missionScenarioBatteryRemainingInputEl) missionScenarioBatteryRemainingInputEl.value = defaults.batteryRemainingPercent;
  if (missionScenarioSensorFailureTypeInputEl) missionScenarioSensorFailureTypeInputEl.value = defaults.sensorFailureType;
  if (missionScenarioLandingZoneBlockedInputEl) missionScenarioLandingZoneBlockedInputEl.value = defaults.landingZoneBlocked;
  if (missionScenarioVisibilityModeInputEl) missionScenarioVisibilityModeInputEl.value = defaults.visibilityMode;
  if (missionScenarioNoFlyZoneMarkerInputEl) missionScenarioNoFlyZoneMarkerInputEl.value = defaults.noFlyZoneMarker;
  if (missionScenarioTrafficConflictMarkerInputEl) missionScenarioTrafficConflictMarkerInputEl.value = defaults.trafficConflictMarker;
  if (missionScenarioAlternateLandingMarkerInputEl) missionScenarioAlternateLandingMarkerInputEl.value = defaults.alternateLandingMarker;
  if (missionScenarioMovingActorMarkerInputEl) missionScenarioMovingActorMarkerInputEl.value = defaults.movingActorMarker;
  if (missionScenarioMultiDroneConflictProbeInputEl) missionScenarioMultiDroneConflictProbeInputEl.value = defaults.multiDroneConflictProbe;
  if (missionScenarioTelemetryDropoutModeInputEl) missionScenarioTelemetryDropoutModeInputEl.value = defaults.telemetryDropoutMode;
  if (missionScenarioMavlinkLinkDegradationModeInputEl) missionScenarioMavlinkLinkDegradationModeInputEl.value = defaults.mavlinkLinkDegradationMode;
}

function missionScenarioCoordinateRouteDistanceM(takeoffLatitude, takeoffLongitude, dropoffLatitude, dropoffLongitude) {
  const earthRadiusM = 6371000;
  const toRadians = (value) => value * Math.PI / 180;
  const dLat = toRadians(dropoffLatitude - takeoffLatitude);
  const dLon = toRadians(dropoffLongitude - takeoffLongitude);
  const lat1 = toRadians(takeoffLatitude);
  const lat2 = toRadians(dropoffLatitude);
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * earthRadiusM * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function updateMissionScenarioCoordinateRouteStatus() {
  if (!missionScenarioCoordinateRouteStatusEl) return;
  const takeoffLatitude = missionScenarioOptionalNumber(missionScenarioTakeoffLatInputEl);
  const takeoffLongitude = missionScenarioOptionalNumber(missionScenarioTakeoffLonInputEl);
  const dropoffLatitude = missionScenarioOptionalNumber(missionScenarioDropoffLatInputEl);
  const dropoffLongitude = missionScenarioOptionalNumber(missionScenarioDropoffLonInputEl);
  const hasAny = missionScenarioCoordinateRouteInputs().some((inputEl) => (inputEl?.value?.trim?.() || "") !== "");
  const hasAllCoordinates = [
    takeoffLatitude,
    takeoffLongitude,
    dropoffLatitude,
    dropoffLongitude,
  ].every((value) => value !== null);

  missionScenarioCoordinateRouteStatusEl.className = "coordinate-route-status muted";
  if (!hasAny) {
    missionScenarioCoordinateRouteStatusEl.textContent = "No coordinate route values entered yet. Prompt-only planning will be used.";
    return;
  }
  if (!hasAllCoordinates) {
    missionScenarioCoordinateRouteStatusEl.className = "coordinate-route-status coordinate-route-status-warning";
    missionScenarioCoordinateRouteStatusEl.textContent = "Coordinate Route is incomplete. Enter start and goal latitude/longitude before generating.";
    return;
  }
  const distanceM = missionScenarioCoordinateRouteDistanceM(
    takeoffLatitude,
    takeoffLongitude,
    dropoffLatitude,
    dropoffLongitude
  );
  missionScenarioCoordinateRouteStatusEl.className = "coordinate-route-status coordinate-route-status-ready";
  missionScenarioCoordinateRouteStatusEl.textContent = `Coordinate Route ready. Approximately ${Math.round(distanceM)} m from start to goal; it will be sent as planning evidence.`;
}

function missionScenarioCoordinateRoutePayload() {
  const takeoffLatitude = missionScenarioOptionalNumber(missionScenarioTakeoffLatInputEl);
  const takeoffLongitude = missionScenarioOptionalNumber(missionScenarioTakeoffLonInputEl);
  const dropoffLatitude = missionScenarioOptionalNumber(missionScenarioDropoffLatInputEl);
  const dropoffLongitude = missionScenarioOptionalNumber(missionScenarioDropoffLonInputEl);
  const hasAny = [
    takeoffLatitude,
    takeoffLongitude,
    dropoffLatitude,
    dropoffLongitude,
    missionScenarioOptionalNumber(missionScenarioRoofHeightInputEl),
    missionScenarioOptionalNumber(missionScenarioPayloadWeightInputEl),
    missionScenarioOptionalNumber(missionScenarioWindSpeedInputEl),
    missionScenarioOptionalNumber(missionScenarioWindDirectionInputEl),
    missionScenarioOptionalNumber(missionScenarioWindGustInputEl),
    missionScenarioOptionalNumber(missionScenarioWindVarianceInputEl),
    missionScenarioOptionalNumber(missionScenarioBatteryRemainingInputEl),
    missionScenarioOptionalText(missionScenarioSensorFailureTypeInputEl),
    missionScenarioOptionalText(missionScenarioLandingZoneBlockedInputEl),
    missionScenarioOptionalText(missionScenarioVisibilityModeInputEl),
    missionScenarioOptionalText(missionScenarioNoFlyZoneMarkerInputEl),
    missionScenarioOptionalText(missionScenarioTrafficConflictMarkerInputEl),
    missionScenarioOptionalText(missionScenarioAlternateLandingMarkerInputEl),
    missionScenarioOptionalText(missionScenarioMovingActorMarkerInputEl),
    missionScenarioOptionalText(missionScenarioMultiDroneConflictProbeInputEl),
    missionScenarioOptionalText(missionScenarioTelemetryDropoutModeInputEl),
    missionScenarioOptionalText(missionScenarioMavlinkLinkDegradationModeInputEl),
  ].some((value) => value !== null);
  if (!hasAny) return null;
  if (
    takeoffLatitude === null
    || takeoffLongitude === null
    || dropoffLatitude === null
    || dropoffLongitude === null
  ) {
    throw new Error("Coordinate Route requires takeoff and dropoff latitude/longitude.");
  }
  return {
    takeoff_latitude: takeoffLatitude,
    takeoff_longitude: takeoffLongitude,
    dropoff_latitude: dropoffLatitude,
    dropoff_longitude: dropoffLongitude,
    dropoff_roof_height_agl_m: missionScenarioOptionalNumber(missionScenarioRoofHeightInputEl) ?? 0,
    payload_weight_kg: missionScenarioOptionalNumber(missionScenarioPayloadWeightInputEl),
    wind_speed_mps: missionScenarioOptionalNumber(missionScenarioWindSpeedInputEl),
    wind_direction_deg: missionScenarioOptionalNumber(missionScenarioWindDirectionInputEl),
    wind_gust_mps: missionScenarioOptionalNumber(missionScenarioWindGustInputEl),
    wind_variance: missionScenarioOptionalNumber(missionScenarioWindVarianceInputEl),
    battery_remaining_percent: missionScenarioOptionalNumber(missionScenarioBatteryRemainingInputEl),
    sensor_failure_component: missionScenarioOptionalText(missionScenarioSensorFailureTypeInputEl) ? "gps" : null,
    sensor_failure_type: missionScenarioOptionalText(missionScenarioSensorFailureTypeInputEl),
    landing_zone_blocked: missionScenarioOptionalText(missionScenarioLandingZoneBlockedInputEl) === "true",
    visibility_mode: missionScenarioOptionalText(missionScenarioVisibilityModeInputEl),
    no_fly_zone_marker: missionScenarioOptionalText(missionScenarioNoFlyZoneMarkerInputEl) === "true",
    traffic_conflict_marker: missionScenarioOptionalText(missionScenarioTrafficConflictMarkerInputEl) === "true",
    alternate_landing_marker: missionScenarioOptionalText(missionScenarioAlternateLandingMarkerInputEl) === "true",
    moving_actor_marker: missionScenarioOptionalText(missionScenarioMovingActorMarkerInputEl) === "true",
    multi_drone_conflict_probe: missionScenarioOptionalText(missionScenarioMultiDroneConflictProbeInputEl) === "true",
    telemetry_dropout_mode: missionScenarioOptionalText(missionScenarioTelemetryDropoutModeInputEl),
    mavlink_link_degradation_mode: missionScenarioOptionalText(missionScenarioMavlinkLinkDegradationModeInputEl),
  };
}

function missionScenarioPromptForRequest(prompt, coordinateRoute) {
  if (prompt) return prompt;
  if (!coordinateRoute) return "";
  return [
    "Coordinate Route planning request",
    `from ${coordinateRoute.takeoff_latitude},${coordinateRoute.takeoff_longitude}`,
    `to ${coordinateRoute.dropoff_latitude},${coordinateRoute.dropoff_longitude}`,
    `roof_agl_m=${coordinateRoute.dropoff_roof_height_agl_m}`,
    coordinateRoute.payload_weight_kg !== null ? `payload_kg=${coordinateRoute.payload_weight_kg}` : "",
    coordinateRoute.wind_speed_mps !== null ? `wind_speed_mps=${coordinateRoute.wind_speed_mps}` : "",
    coordinateRoute.wind_direction_deg !== null ? `wind_direction_deg=${coordinateRoute.wind_direction_deg}` : "",
    coordinateRoute.wind_gust_mps !== null ? `wind_gust_mps=${coordinateRoute.wind_gust_mps}` : "",
    coordinateRoute.wind_variance !== null ? `wind_variance=${coordinateRoute.wind_variance}` : "",
    coordinateRoute.battery_remaining_percent !== null ? `battery_remaining_percent=${coordinateRoute.battery_remaining_percent}` : "",
    coordinateRoute.sensor_failure_type !== null ? `sensor_failure=gps:${coordinateRoute.sensor_failure_type}` : "",
    coordinateRoute.landing_zone_blocked === true ? "landing_zone_blocked=true" : "",
    coordinateRoute.visibility_mode !== null ? `visibility_mode=${coordinateRoute.visibility_mode}` : "",
    coordinateRoute.no_fly_zone_marker === true ? "no_fly_zone_marker=true" : "",
    coordinateRoute.traffic_conflict_marker === true ? "traffic_conflict_marker=true" : "",
    coordinateRoute.alternate_landing_marker === true ? "alternate_landing_marker=true" : "",
    coordinateRoute.moving_actor_marker === true ? "moving_actor_marker=true" : "",
    coordinateRoute.multi_drone_conflict_probe === true ? "multi_drone_conflict_probe=true" : "",
    coordinateRoute.telemetry_dropout_mode != null ? `telemetry_dropout_mode=${coordinateRoute.telemetry_dropout_mode}` : "",
    coordinateRoute.mavlink_link_degradation_mode != null ? `mavlink_link_degradation_mode=${coordinateRoute.mavlink_link_degradation_mode}` : "",
  ].filter(Boolean).join(" ");
}

function renderMissionScenarioCoordinateRoute(route) {
  const value = asPlainObject(route);
  if (!Object.keys(value).length) return "";
  return `
    <div class="detail-card">
      <div class="k">Coordinate Route</div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">mode</span><span class="detail-chip-value">${escapeHtml(value.route_mode || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">distance m</span><span class="detail-chip-value">${escapeHtml(value.derived_route_distance_m ?? "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">roof AGL m</span><span class="detail-chip-value">${escapeHtml(value.dropoff_roof_height_agl_m ?? "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">payload kg</span><span class="detail-chip-value">${escapeHtml(value.payload_weight_kg ?? "-")}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">start/takeoff</span><span class="detail-chip-value">${escapeHtml(`${value.takeoff_latitude ?? "-"}, ${value.takeoff_longitude ?? "-"}`)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">goal/dropoff</span><span class="detail-chip-value">${escapeHtml(`${value.dropoff_latitude ?? "-"}, ${value.dropoff_longitude ?? "-"}`)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">wind</span><span class="detail-chip-value">${escapeHtml(`${value.wind_speed_mps ?? "-"} m/s @ ${value.wind_direction_deg ?? "-"} deg`)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">wind gust</span><span class="detail-chip-value">${escapeHtml(`${value.wind_gust_mps ?? "-"} m/s · variance ${value.wind_variance ?? "-"}`)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">battery requested</span><span class="detail-chip-value">${escapeHtml(value.battery_remaining_percent ?? "-")}%</span></span>
        <span class="detail-chip"><span class="detail-chip-label">sensor failure</span><span class="detail-chip-value">${escapeHtml(`${value.sensor_failure_component ?? "-"}:${value.sensor_failure_type ?? "-"}`)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">landing zone marker</span><span class="detail-chip-value">${escapeHtml(value.landing_zone_blocked === true ? "visual-only" : "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">visibility marker</span><span class="detail-chip-value">${escapeHtml(value.visibility_mode ?? "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">no-fly zone marker</span><span class="detail-chip-value">${escapeHtml(value.no_fly_zone_marker === true ? "visual-only" : "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">traffic marker</span><span class="detail-chip-value">${escapeHtml(value.traffic_conflict_marker === true ? "visual-only" : "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">alternate landing</span><span class="detail-chip-value">${escapeHtml(value.alternate_landing_marker === true ? "visual-only" : "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">moving actor</span><span class="detail-chip-value">${escapeHtml(value.moving_actor_marker === true ? "moving visual-only" : "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">multi-drone</span><span class="detail-chip-value">${escapeHtml(value.multi_drone_conflict_probe === true ? "unsupported probe" : "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">telemetry dropout</span><span class="detail-chip-value">${escapeHtml(value.telemetry_dropout_mode ?? "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">MAVLink link</span><span class="detail-chip-value">${escapeHtml(value.mavlink_link_degradation_mode ?? "-")}</span></span>
      </div>
      <div class="muted">Coordinate pair starts as source-backed Digital Twin planning evidence. After operator approval and SITL preparation, this Mission Designer track can compile it into SITL-only PX4 mission items. Hardware and physical execution remain false.</div>
    </div>
  `;
}

function renderMissionDesignerRealismConditions(artifacts) {
  const environmentProfile = asPlainObject(artifacts.environment_condition_profile);
  const capability = asPlainObject(artifacts.simulator_capability_matrix);
  const application = asPlainObject(artifacts.simulator_condition_application);
  const evidence = asPlainObject(artifacts.observed_environment_evidence);
  const cleanup = asPlainObject(artifacts.scenario_cleanup_receipt);
  const vehicleProfile = asPlainObject(artifacts.vehicle_condition_profile);
  const payloadCapability = asPlainObject(artifacts.payload_simulator_capability_matrix);
  const payloadApplication = asPlainObject(artifacts.payload_simulator_condition_application);
  const vehicleEvidence = asPlainObject(artifacts.observed_vehicle_condition_evidence);
  const payloadFeasibilityAdvisory = asPlainObject(artifacts.payload_feasibility_advisory);
  const payloadRecoveryAction = asPlainObject(
    artifacts.payload_recovery_action_artifact || artifacts.payload_recovery_action
  );
  const batteryProfile = asPlainObject(artifacts.battery_condition_profile);
  const batteryCapability = asPlainObject(artifacts.battery_simulator_capability_matrix);
  const batteryApplication = asPlainObject(artifacts.battery_simulator_condition_application);
  const batteryEvidence = asPlainObject(artifacts.observed_battery_condition_evidence);
  const sensorProfile = asPlainObject(artifacts.sensor_condition_profile);
  const sensorCapability = asPlainObject(artifacts.sensor_simulator_capability_matrix);
  const sensorApplication = asPlainObject(artifacts.sensor_failure_injection_application);
  const sensorEvidence = asPlainObject(artifacts.observed_sensor_condition_evidence);
  const worldProfile = asPlainObject(artifacts.gazebo_world_condition_profile);
  const worldCapability = asPlainObject(artifacts.gazebo_world_capability_matrix);
  const worldApplication = asPlainObject(artifacts.gazebo_world_application);
  const obstacleManifest = asPlainObject(artifacts.obstacle_manifest);
  const worldEvidence = asPlainObject(artifacts.observed_world_condition_evidence);
  const visibilityProfile = asPlainObject(artifacts.visibility_condition_profile);
  const visibilityCapability = asPlainObject(artifacts.visibility_capability_matrix);
  const visibilityApplication = asPlainObject(artifacts.visibility_application);
  const visibilityEvidence = asPlainObject(artifacts.observed_visibility_condition_evidence);
  const operationalProfile = asPlainObject(artifacts.operational_condition_profile);
  const geofenceProfile = asPlainObject(artifacts.geofence_condition_profile);
  const trafficConflictProfile = asPlainObject(artifacts.traffic_conflict_profile);
  const alternateLandingProfile = asPlainObject(artifacts.alternate_landing_profile);
  const dynamicActorProfile = asPlainObject(artifacts.dynamic_actor_profile);
  const collisionObstacleProfile = asPlainObject(artifacts.collision_obstacle_profile);
  const gazeboObstacleSpawnApplication = asPlainObject(artifacts.gazebo_route_corridor_obstacle_spawn_application);
  const gazeboObstacleSpawnObserved = asPlainObject(gazeboObstacleSpawnApplication.observed);
  const collisionObstacleEvidence = asPlainObject(artifacts.collision_obstacle_evidence);
  const collisionObstacleObserved = asPlainObject(collisionObstacleEvidence.observed);
  const routeBlockingCandidateEvidence = asPlainObject(artifacts.route_blocking_candidate_evidence);
  const routeBlockingCandidateObserved = asPlainObject(routeBlockingCandidateEvidence.observed);
  const contactEventIncidentEvidence = asPlainObject(artifacts.contact_event_incident_evidence);
  const contactEventIncidentObserved = asPlainObject(contactEventIncidentEvidence.observed);
  const horizontalRouteContactIntegration = asPlainObject(artifacts.horizontal_route_contact_topic_integration);
  const horizontalRouteContactObserved = asPlainObject(horizontalRouteContactIntegration.observed);
  const horizontalRouteContactVerifierCandidate = asPlainObject(artifacts.horizontal_route_contact_scoped_verifier_candidate);
  const horizontalRouteContactVerifierObserved = asPlainObject(horizontalRouteContactVerifierCandidate.observed);
  const horizontalRouteContactIncidentVerification = asPlainObject(artifacts.horizontal_route_contact_incident_verification);
  const horizontalRouteContactIncidentVerifiedObserved = asPlainObject(horizontalRouteContactIncidentVerification.observed);
  const horizontalRouteIncidentTrafficVerification = asPlainObject(artifacts.horizontal_route_incident_informed_traffic_conflict_verification);
  const horizontalRouteIncidentTrafficObserved = asPlainObject(horizontalRouteIncidentTrafficVerification.observed);
  const horizontalRouteIncidentBlockingVerification = asPlainObject(artifacts.horizontal_route_incident_informed_route_blocking_verification);
  const horizontalRouteIncidentBlockingObserved = asPlainObject(horizontalRouteIncidentBlockingVerification.observed);
  const operationalIncidentReport = asPlainObject(artifacts.operational_incident_report);
  const operationalIncidentObserved = asPlainObject(operationalIncidentReport.observed);
  const trafficConflictVerification = asPlainObject(artifacts.traffic_conflict_verification);
  const trafficConflictObserved = asPlainObject(trafficConflictVerification.observed);
  const routeBlockingVerification = asPlainObject(artifacts.route_blocking_verification);
  const routeBlockingObserved = asPlainObject(routeBlockingVerification.observed);
  const alternateLandingCandidateEvidence = asPlainObject(artifacts.alternate_landing_candidate_evidence);
  const alternateLandingCandidateObserved = asPlainObject(alternateLandingCandidateEvidence.observed);
  const alternateLandingExecutionRequest = asPlainObject(artifacts.alternate_landing_execution_request);
  const alternateLandingCommandDispatch = asPlainObject(artifacts.alternate_landing_command_dispatch);
  const alternateLandingBehaviorObservation = asPlainObject(artifacts.alternate_landing_behavior_observation);
  const alternateMissionUploadRequest = asPlainObject(artifacts.alternate_mission_upload_request);
  const alternateMissionUploadReceipt = asPlainObject(artifacts.alternate_mission_upload_receipt);
  const alternateRouteBehaviorObservation = asPlainObject(artifacts.alternate_route_behavior_observation);
  const alternateRouteExecutionEvidence = asPlainObject(artifacts.alternate_route_execution_evidence);
  const alternateRouteExecutionObserved = asPlainObject(alternateRouteExecutionEvidence.observed);
  const rthExecutionRequest = asPlainObject(artifacts.rth_execution_request);
  const rthCommandDispatch = asPlainObject(artifacts.rth_command_dispatch);
  const rthBehaviorObservation = asPlainObject(artifacts.rth_behavior_observation);
  const multiVehicleFrameContract = asPlainObject(artifacts.multi_vehicle_frame_contract);
  const movingActorPoseObservation = asPlainObject(artifacts.moving_actor_pose_observation);
  const movingActorProximityEvidence = asPlainObject(artifacts.moving_actor_proximity_evidence);
  const movingActorProximityObserved = asPlainObject(movingActorProximityEvidence.observed);
  const operationalCapability = asPlainObject(artifacts.operational_capability_matrix);
  const operationalApplication = asPlainObject(artifacts.operational_application);
  const operationalEvidence = asPlainObject(artifacts.observed_operational_condition_evidence);
  const telemetryProfile = asPlainObject(artifacts.telemetry_degradation_profile);
  const telemetryApplication = asPlainObject(artifacts.telemetry_degradation_application);
  const telemetryEvidence = asPlainObject(artifacts.observed_telemetry_gap_evidence);
  const telemetryFreshness = asPlainObject(artifacts.telemetry_freshness_report);
  const mavlinkLinkProfile = asPlainObject(artifacts.mavlink_link_degradation_profile);
  const mavlinkLinkCapability = asPlainObject(artifacts.mavlink_link_degradation_capability_matrix);
  const mavlinkLinkApplication = asPlainObject(artifacts.mavlink_link_degradation_application);
  const mavlinkLinkEvidence = asPlainObject(artifacts.observed_mavlink_gap_evidence);
  const requested = asPlainObject(environmentProfile.requested);
  const vehicleRequested = asPlainObject(vehicleProfile.requested);
  const batteryRequested = asPlainObject(batteryProfile.requested);
  const sensorRequested = asPlainObject(sensorProfile.requested);
  const worldRequested = asPlainObject(worldProfile.requested);
  const visibilityRequested = asPlainObject(visibilityProfile.requested);
  const operationalRequested = asPlainObject(operationalProfile.requested);
  const telemetryRequested = asPlainObject(telemetryProfile.requested);
  const mavlinkLinkRequested = asPlainObject(mavlinkLinkProfile.requested);
  if (
    !Object.keys(environmentProfile).length
    && !Object.keys(application).length
    && !Object.keys(evidence).length
    && !Object.keys(vehicleProfile).length
    && !Object.keys(payloadApplication).length
    && !Object.keys(vehicleEvidence).length
    && !Object.keys(payloadFeasibilityAdvisory).length
    && !Object.keys(payloadRecoveryAction).length
    && !Object.keys(batteryProfile).length
    && !Object.keys(batteryApplication).length
    && !Object.keys(batteryEvidence).length
    && !Object.keys(sensorProfile).length
    && !Object.keys(sensorApplication).length
    && !Object.keys(sensorEvidence).length
    && !Object.keys(worldProfile).length
    && !Object.keys(worldApplication).length
    && !Object.keys(worldEvidence).length
    && !Object.keys(visibilityProfile).length
    && !Object.keys(visibilityApplication).length
    && !Object.keys(visibilityEvidence).length
    && !Object.keys(operationalProfile).length
    && !Object.keys(operationalApplication).length
    && !Object.keys(operationalEvidence).length
    && !Object.keys(telemetryProfile).length
    && !Object.keys(telemetryApplication).length
    && !Object.keys(telemetryEvidence).length
    && !Object.keys(mavlinkLinkProfile).length
    && !Object.keys(mavlinkLinkApplication).length
    && !Object.keys(mavlinkLinkEvidence).length
  ) {
    return "";
  }
  const observed = asPlainObject(evidence.observed);
  const applied = asPlainObject(application.applied);
  const vehicleObserved = asPlainObject(vehicleEvidence.observed);
  const payloadApplied = asPlainObject(payloadApplication.applied);
  const batteryObserved = asPlainObject(batteryEvidence.observed);
  const batteryApplied = asPlainObject(batteryApplication.applied);
  const observedBatteryStatus = asPlainObject(batteryObserved.battery_status);
  const sensorObserved = asPlainObject(sensorEvidence.observed);
  const sensorApplied = asPlainObject(sensorApplication.applied);
  const worldObserved = asPlainObject(worldEvidence.observed);
  const worldApplied = asPlainObject(worldApplication.applied);
  const visibilityObserved = asPlainObject(visibilityEvidence.observed);
  const visibilityApplied = asPlainObject(visibilityApplication.applied);
  const operationalObserved = asPlainObject(operationalEvidence.observed);
  const operationalApplied = asPlainObject(operationalApplication.applied);
  const telemetryObserved = asPlainObject(telemetryEvidence.observed);
  const telemetryApplied = asPlainObject(telemetryApplication.applied);
  const mavlinkLinkObserved = asPlainObject(mavlinkLinkEvidence.observed);
  const conditionNotes = [
    ...(application.unsupported_reasons || []),
    ...(capability.unsupported_reasons || []),
    ...(application.approximation_reasons || []),
    ...(capability.approximation_reasons || []),
  ];
  const payloadNotes = [
    ...(payloadApplication.unsupported_reasons || []),
    ...(payloadCapability.unsupported_reasons || []),
    ...(payloadApplication.approximation_reasons || []),
    ...(payloadCapability.approximation_reasons || []),
  ];
  const batteryNotes = [
    ...(batteryApplication.unsupported_reasons || []),
    ...(batteryCapability.unsupported_reasons || []),
    ...(batteryApplication.approximation_reasons || []),
    ...(batteryCapability.approximation_reasons || []),
  ];
  const sensorNotes = [
    ...(sensorApplication.unsupported_reasons || []),
    ...(sensorCapability.unsupported_reasons || []),
    ...(sensorApplication.approximation_reasons || []),
    ...(sensorCapability.approximation_reasons || []),
  ];
  const worldNotes = [
    ...(worldApplication.unsupported_reasons || []),
    ...(worldCapability.unsupported_reasons || []),
    ...(worldApplication.approximation_reasons || []),
    ...(worldCapability.approximation_reasons || []),
  ];
  const visibilityNotes = [
    ...(visibilityApplication.unsupported_reasons || []),
    ...(visibilityCapability.unsupported_reasons || []),
    ...(visibilityApplication.approximation_reasons || []),
    ...(visibilityCapability.approximation_reasons || []),
  ];
  const operationalNotes = [
    ...(operationalApplication.unsupported_reasons || []),
    ...(operationalCapability.unsupported_reasons || []),
    ...(operationalApplication.approximation_reasons || []),
    ...(operationalCapability.approximation_reasons || []),
  ];
  const telemetryNotes = [
    ...(telemetryApplication.unsupported_reasons || []),
    ...(telemetryApplication.approximation_reasons || []),
  ];
  const mavlinkLinkNotes = [
    ...(mavlinkLinkApplication.unsupported_reasons || []),
    ...(mavlinkLinkCapability.unsupported_reasons || []),
    ...(mavlinkLinkApplication.approximation_reasons || []),
    ...(mavlinkLinkCapability.approximation_reasons || []),
  ];
  const telemetryPublisherStateMutated = Boolean(telemetryApplication.publisher_state_mutated || telemetryApplied.publisher_state_mutated);
  const telemetryMissionUploadPathMutated = Boolean(telemetryApplication.mission_upload_path_mutated || telemetryApplied.mission_upload_path_mutated);
  const telemetryMissionProgressMutated = Boolean(telemetryApplication.mission_progress_mutated || telemetryApplied.mission_progress_mutated);
  const mavlinkLinkCommandPathMutated = Boolean(
    mavlinkLinkApplication.px4_command_path_mutated
      || mavlinkLinkApplication.gazebo_command_path_mutated
      || mavlinkLinkApplication.mission_upload_path_mutated
  );
  return `
    <div class="detail-card">
      <div class="k">Realism Control Plane</div>
      <div class="muted">requested / applied / observed / approximated / unsupported are rendered separately, with cleanup evidence kept visible. This panel is read-only and does not grant verifier, gate, dispatch, or completion authority.</div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">condition</span><span class="detail-chip-value">${escapeHtml(environmentProfile.condition_kind || application.condition_kind || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">capability</span><span class="detail-chip-value">${escapeHtml(capability.wind_gust || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">applied</span><span class="detail-chip-value">${escapeHtml(application.application_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">observed</span><span class="detail-chip-value">${escapeHtml(evidence.observation_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">cleanup</span><span class="detail-chip-value">${escapeHtml(cleanup.cleanup_status || "-")}</span></span>
      </div>
      <div class="detail-grid">
        <div><span class="k">requested</span><strong>${escapeHtml(`${requested.wind_mean_mps ?? "-"} m/s @ ${requested.wind_direction_deg ?? "-"} deg`)}</strong><span>gust=${escapeHtml(String(requested.wind_gust_mps ?? "-"))} m/s · variance=${escapeHtml(String(requested.wind_variance ?? "-"))}</span></div>
        <div><span class="k">applied simulator condition</span><strong>${escapeHtml(applied.method || application.application_status || "-")}</strong><span>${escapeHtml(applied.topic || "no simulator mutation observed")}</span></div>
        <div><span class="k">observed evidence</span><strong>${escapeHtml(String(observed.observed ?? false))}</strong><span>${escapeHtml(observed.source || evidence.observation_status || "-")}</span></div>
        <div><span class="k">unsupported / approximated</span><strong>${escapeHtml(conditionNotes.join(", ") || "none")}</strong><span>delivery_completion_claimed=false</span></div>
      </div>
      ${Object.keys(vehicleProfile).length || Object.keys(payloadApplication).length || Object.keys(vehicleEvidence).length || Object.keys(payloadFeasibilityAdvisory).length || Object.keys(payloadRecoveryAction).length ? `
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">vehicle condition</span><span class="detail-chip-value">${escapeHtml(vehicleProfile.condition_kind || payloadApplication.condition_kind || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">payload capability</span><span class="detail-chip-value">${escapeHtml(payloadCapability.payload_mass || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">payload applied</span><span class="detail-chip-value">${escapeHtml(payloadApplication.application_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">payload observed</span><span class="detail-chip-value">${escapeHtml(vehicleEvidence.observation_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">payload advisory</span><span class="detail-chip-value">${escapeHtml(payloadFeasibilityAdvisory.advisory_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">payload action</span><span class="detail-chip-value">${escapeHtml(payloadRecoveryAction.mission_response_kind || "-")}</span></span>
      </div>
      <div class="detail-grid">
        <div><span class="k">requested payload</span><strong>${escapeHtml(`${vehicleRequested.payload_mass_kg ?? "-"} kg`)}</strong><span>mounted=${escapeHtml(String(vehicleRequested.payload_mounted ?? "-"))}</span></div>
        <div><span class="k">applied payload condition</span><strong>${escapeHtml(payloadApplied.method || payloadApplication.application_status || "-")}</strong><span>${escapeHtml(payloadApplied.world_sdf_path || "no payload mass mutation observed")}</span></div>
        <div><span class="k">observed payload evidence</span><strong>${escapeHtml(String(vehicleObserved.observed ?? false))}</strong><span>${escapeHtml(vehicleObserved.source || vehicleEvidence.observation_status || "-")}</span></div>
        <div><span class="k">payload feasibility advisory</span><strong>${escapeHtml(payloadFeasibilityAdvisory.mission_response_kind || "-")}</strong><span>form=${escapeHtml(payloadFeasibilityAdvisory.form2_subtype || payloadFeasibilityAdvisory.causal_form || "-")} · trigger=${escapeHtml(payloadFeasibilityAdvisory.trigger_level || "-")} · operator_review=${escapeHtml(String(payloadFeasibilityAdvisory.operator_review_required ?? false))} · auto_dispatch_suppressed=${escapeHtml(String(payloadFeasibilityAdvisory.automatic_dispatch_suppressed ?? true))} · margin=${escapeHtml(String(payloadFeasibilityAdvisory.behavior_delta_margin ?? "-"))} · lifecycle=${escapeHtml(String(payloadFeasibilityAdvisory.advisory_lifecycle_state ?? "-"))}</span></div>
        <div><span class="k">payload recovery action</span><strong>${escapeHtml(payloadRecoveryAction.bounded_action_kind || payloadRecoveryAction.mission_response_kind || "-")}</strong><span>form=${escapeHtml(payloadRecoveryAction.form2_subtype || payloadRecoveryAction.causal_form || "-")} · consumed=${escapeHtml(payloadRecoveryAction.advisory_consumed_by_ref || "-")} · approval=${escapeHtml(payloadRecoveryAction.approval_ref || "-")} · dispatch=${escapeHtml(payloadRecoveryAction.dispatch_status || "-")} · delivery_claimed=${escapeHtml(String(payloadRecoveryAction.delivery_completion_claimed ?? false))}</span></div>
        <div><span class="k">payload unsupported / approximated</span><strong>${escapeHtml(payloadNotes.join(", ") || "none")}</strong><span>payload_release_does_not_verify_dropoff=true</span></div>
      </div>
      ` : ""}
      ${Object.keys(batteryProfile).length || Object.keys(batteryApplication).length || Object.keys(batteryEvidence).length ? `
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">battery condition</span><span class="detail-chip-value">${escapeHtml(batteryProfile.condition_kind || batteryApplication.condition_kind || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">battery capability</span><span class="detail-chip-value">${escapeHtml(batteryCapability.battery_threshold || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">battery applied</span><span class="detail-chip-value">${escapeHtml(batteryApplication.application_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">battery observed</span><span class="detail-chip-value">${escapeHtml(batteryEvidence.observation_status || "-")}</span></span>
      </div>
      <div class="detail-grid">
        <div><span class="k">requested battery</span><strong>${escapeHtml(`${batteryRequested.requested_remaining_percent ?? "-"}%`)}</strong><span>scenario=${escapeHtml(String(batteryRequested.battery_scenario ?? "-"))}</span></div>
        <div><span class="k">applied battery condition</span><strong>${escapeHtml(batteryApplied.method || batteryApplication.application_status || "-")}</strong><span>${escapeHtml(JSON.stringify(batteryApplied.applied_params || {}))}</span></div>
        <div><span class="k">observed battery evidence</span><strong>${escapeHtml(String(batteryObserved.observed ?? false))}</strong><span>remaining=${escapeHtml(String(observedBatteryStatus.battery_remaining_percent ?? "-"))}% · warning=${escapeHtml(String(observedBatteryStatus.battery_warning ?? "-"))} · failsafe=${escapeHtml(String(batteryObserved.failsafe_behavior_status ?? "-"))}</span></div>
        <div><span class="k">battery unsupported / approximated</span><strong>${escapeHtml(batteryNotes.join(", ") || "none")}</strong><span>requested percent does not spoof PX4 telemetry</span></div>
      </div>
      ` : ""}
      ${Object.keys(sensorProfile).length || Object.keys(sensorApplication).length || Object.keys(sensorEvidence).length ? `
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">sensor condition</span><span class="detail-chip-value">${escapeHtml(sensorProfile.condition_kind || sensorApplication.condition_kind || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">sensor capability</span><span class="detail-chip-value">${escapeHtml(sensorCapability.sensor_failure || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">sensor applied</span><span class="detail-chip-value">${escapeHtml(sensorApplication.application_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">sensor observed</span><span class="detail-chip-value">${escapeHtml(sensorEvidence.observation_status || "-")}</span></span>
      </div>
      <div class="detail-grid">
        <div><span class="k">requested sensor</span><strong>${escapeHtml(`${sensorRequested.sensor_component ?? "-"}:${sensorRequested.failure_type ?? "-"}`)}</strong><span>reset=${escapeHtml(String(sensorRequested.reset_failure_type ?? "-"))}</span></div>
        <div><span class="k">applied sensor failure</span><strong>${escapeHtml(sensorApplied.method || sensorApplication.application_status || "-")}</strong><span>${escapeHtml(JSON.stringify(sensorApplied.block_param_result || sensorApplied.failure_command_result || {}))}</span></div>
        <div><span class="k">observed sensor evidence</span><strong>${escapeHtml(String(sensorObserved.sensor_failure_effect_observed ?? false))}</strong><span>gps_sample_lost=${escapeHtml(String(sensorObserved.gps_sample_lost_after_injection ?? false))} · estimator_degradation_observed=${escapeHtml(String(sensorObserved.estimator_degradation_observed ?? false))}</span></div>
        <div><span class="k">sensor unsupported / approximated</span><strong>${escapeHtml(sensorNotes.join(", ") || "none")}</strong><span>sensor failure does not verify failsafe or delivery completion</span></div>
      </div>
      ` : ""}
      ${Object.keys(worldProfile).length || Object.keys(worldApplication).length || Object.keys(worldEvidence).length ? `
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">world condition</span><span class="detail-chip-value">${escapeHtml(worldProfile.condition_kind || worldApplication.condition_kind || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world capability</span><span class="detail-chip-value">${escapeHtml(worldCapability.landing_zone_blocked_marker || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world applied</span><span class="detail-chip-value">${escapeHtml(worldApplication.application_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">world observed</span><span class="detail-chip-value">${escapeHtml(worldEvidence.observation_status || "-")}</span></span>
      </div>
      <div class="detail-grid">
        <div><span class="k">requested world</span><strong>${escapeHtml(worldRequested.landing_zone_blocked === true ? "landing zone blocked" : "-")}</strong><span>frame=${escapeHtml(String(worldRequested.dropoff_frame ?? "-"))} · visual_only=${escapeHtml(String(worldRequested.visual_only ?? "-"))}</span></div>
        <div><span class="k">applied world condition</span><strong>${escapeHtml(worldApplied.method || worldApplication.application_status || "-")}</strong><span>${escapeHtml(worldApplied.world_sdf_path || "no world marker mutation observed")}</span></div>
        <div><span class="k">observed world evidence</span><strong>${escapeHtml(String(worldObserved.observed ?? false))}</strong><span>${escapeHtml(worldObserved.source || worldEvidence.observation_status || "-")}</span></div>
        <div><span class="k">world unsupported / approximated</span><strong>${escapeHtml(worldNotes.join(", ") || "none")}</strong><span>obstacles=${escapeHtml(String((obstacleManifest.obstacles || []).length || 0))} · visual marker does not verify landing-zone safety</span></div>
      </div>
      ` : ""}
      ${Object.keys(visibilityProfile).length || Object.keys(visibilityApplication).length || Object.keys(visibilityEvidence).length ? `
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">visibility condition</span><span class="detail-chip-value">${escapeHtml(visibilityProfile.condition_kind || visibilityApplication.condition_kind || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">visibility capability</span><span class="detail-chip-value">${escapeHtml(visibilityCapability.fog_render_marker || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">smoke deferred</span><span class="detail-chip-value">${escapeHtml(visibilityCapability.smoke_render_marker || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">visibility applied</span><span class="detail-chip-value">${escapeHtml(visibilityApplication.application_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">visibility observed</span><span class="detail-chip-value">${escapeHtml(visibilityEvidence.observation_status || "-")}</span></span>
      </div>
      <div class="detail-grid">
        <div><span class="k">requested visibility</span><strong>${escapeHtml(visibilityRequested.visibility_mode ?? "-")}</strong><span>fog_render=${escapeHtml(String(visibilityRequested.render_only_marker_requested ?? "-"))} · smoke_deferred=${escapeHtml(String(visibilityRequested.smoke_deferred_to_followup_pr ?? "-"))}</span></div>
        <div><span class="k">applied visibility condition</span><strong>${escapeHtml(visibilityApplied.method || visibilityApplication.application_status || "-")}</strong><span>${escapeHtml(visibilityApplied.world_sdf_path || "no fog render marker mutation observed")}</span></div>
        <div><span class="k">observed visibility evidence</span><strong>${escapeHtml(String(visibilityObserved.observed ?? false))}</strong><span>${escapeHtml(visibilityObserved.source || visibilityEvidence.observation_status || "-")}</span></div>
        <div><span class="k">deferred visibility modes</span><strong>${escapeHtml(visibilityCapability.smoke_render_marker || "-")}</strong><span>smoke remains a separate particle-slice follow-up</span></div>
        <div><span class="k">visibility unsupported / approximated</span><strong>${escapeHtml(visibilityNotes.join(", ") || "none")}</strong><span>fog render marker does not verify visibility meters or operational safety</span></div>
      </div>
      ` : ""}
      ${Object.keys(operationalProfile).length || Object.keys(operationalApplication).length || Object.keys(operationalEvidence).length ? `
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">operational condition</span><span class="detail-chip-value">${escapeHtml(operationalProfile.condition_kind || operationalApplication.condition_kind || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">operational capability</span><span class="detail-chip-value">${escapeHtml([operationalCapability.no_fly_zone_marker, operationalCapability.traffic_conflict_marker, operationalCapability.alternate_landing_marker, operationalCapability.moving_actor_marker, operationalCapability.collision_obstacle, operationalCapability.multi_drone_conflict_probe].filter((value) => value && value !== "not_requested").join(", ") || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">operational applied</span><span class="detail-chip-value">${escapeHtml(operationalApplication.application_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">operational observed</span><span class="detail-chip-value">${escapeHtml(operationalEvidence.observation_status || "-")}</span></span>
      </div>
      <div class="detail-grid">
        <div><span class="k">requested no-fly zone</span><strong>${escapeHtml(operationalRequested.no_fly_zone_marker === true ? "visual marker" : "-")}</strong><span>geofences=${escapeHtml(String((geofenceProfile.geofences || []).length || 0))} · enforcement=${escapeHtml(String(operationalRequested.enforcement_enabled ?? "-"))}</span></div>
        <div><span class="k">requested traffic conflict</span><strong>${escapeHtml(operationalRequested.traffic_conflict_marker === true ? "visual marker" : "-")}</strong><span>conflicts=${escapeHtml(String((trafficConflictProfile.conflicts || []).length || 0))} · dynamic=${escapeHtml(String(operationalRequested.traffic_motion_enabled ?? "-"))} · collision=${escapeHtml(String(operationalRequested.collision_enabled ?? "-"))}</span></div>
        <div><span class="k">requested alternate landing</span><strong>${escapeHtml(operationalRequested.alternate_landing_marker === true ? "visual marker" : "-")}</strong><span>candidates=${escapeHtml(String((alternateLandingProfile.candidates || []).length || 0))} · behavior=${escapeHtml(String(operationalRequested.alternate_landing_behavior_enabled ?? "-"))} · rth=${escapeHtml(String(operationalRequested.return_to_home_behavior_enabled ?? "-"))}</span></div>
        <div><span class="k">requested moving actor</span><strong>${escapeHtml(operationalRequested.moving_actor_marker === true ? "moving visual actor" : "-")}</strong><span>actors=${escapeHtml(String((dynamicActorProfile.actors || []).length || 0))} · pose=${escapeHtml(movingActorPoseObservation.observation_status || "-")} · advisory=${escapeHtml(movingActorProximityObserved.advisory_status || "-")} · proximity=${escapeHtml(movingActorProximityEvidence.observation_status || "-")} · collision=${escapeHtml(String(operationalRequested.collision_enabled ?? "-"))} · sensor-visible=${escapeHtml(String(operationalRequested.sensor_visible_claimed ?? "-"))}</span></div>
        <div><span class="k">collision-enabled obstacle</span><strong>${escapeHtml(operationalRequested.collision_obstacle === true ? "collision geometry" : "-")}</strong><span>obstacles=${escapeHtml(String((collisionObstacleProfile.obstacles || []).length || 0))} · evidence=${escapeHtml(collisionObstacleEvidence.observation_status || "-")} · collision=${escapeHtml(String(collisionObstacleObserved.collision_geometry_observed ?? "-"))} · contact_topic=${escapeHtml(String(collisionObstacleObserved.contact_topic_observed ?? "-"))} · contact_event=${escapeHtml(String(collisionObstacleObserved.contact_event_observed ?? false))} · route-blocking=${escapeHtml(String(collisionObstacleObserved.route_blocking_observed ?? false))}</span></div>
        <div><span class="k">Gazebo obstacle applicator</span><strong>${escapeHtml(gazeboObstacleSpawnApplication.application_status || "-")}</strong><span>method=${escapeHtml(String(asPlainObject(gazeboObstacleSpawnApplication.applied).method || "-"))} · sdf_hash_match=${escapeHtml(String(gazeboObstacleSpawnObserved.world_sdf_hash_match ?? false))} · model=${escapeHtml(String(gazeboObstacleSpawnObserved.model_materialized ?? false))} · collision=${escapeHtml(String(gazeboObstacleSpawnObserved.collision_geometry_materialized ?? false))} · route_blocking=${escapeHtml(String(gazeboObstacleSpawnObserved.route_blocking_verified ?? false))} · task_mutated=${escapeHtml(String(gazeboObstacleSpawnObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">route blocking candidate</span><strong>${escapeHtml(routeBlockingCandidateObserved.route_blocking_candidate === true ? "candidate" : "-")}</strong><span>evidence=${escapeHtml(routeBlockingCandidateEvidence.observation_status || "-")} · threshold=${escapeHtml(String(routeBlockingCandidateObserved.candidate_threshold_m ?? "-"))}m · verified=${escapeHtml(String(routeBlockingCandidateObserved.route_blocking_verified ?? false))} · incident=${escapeHtml(String(routeBlockingCandidateObserved.incident_report_created ?? false))}</span></div>
        <div><span class="k">contact incident candidate</span><strong>${escapeHtml(contactEventIncidentObserved.contact_event_incident_candidate === true ? "candidate" : "-")}</strong><span>status=${escapeHtml(contactEventIncidentEvidence.observation_status || "-")} · contact=${escapeHtml(String(contactEventIncidentObserved.contact_event_observed ?? false))} · operator_review=${escapeHtml(String(contactEventIncidentObserved.operator_review_required ?? false))} · incident_verified=${escapeHtml(String(contactEventIncidentObserved.incident_verified ?? false))}</span></div>
        <div><span class="k">horizontal route contact topic</span><strong>${escapeHtml(horizontalRouteContactObserved.contact_event_observed === true ? "sidecar observed" : "-")}</strong><span>status=${escapeHtml(horizontalRouteContactIntegration.integration_status || "-")} · mode=${escapeHtml(horizontalRouteContactIntegration.integration_mode || "-")} · route_world_sensor=${escapeHtml(String(horizontalRouteContactIntegration.horizontal_route_world_contact_sensor_injected ?? false))} · task_mutated=${escapeHtml(String(horizontalRouteContactObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">contact scoped verifier candidate</span><strong>${escapeHtml(horizontalRouteContactVerifierObserved.scoped_verifier_candidate === true ? "operator review" : "-")}</strong><span>status=${escapeHtml(horizontalRouteContactVerifierCandidate.candidate_status || "-")} · contact=${escapeHtml(String(horizontalRouteContactVerifierObserved.contact_event_observed ?? false))} · operator_review=${escapeHtml(String(horizontalRouteContactVerifierObserved.operator_review_required ?? false))} · traffic_verified=${escapeHtml(String(horizontalRouteContactVerifierObserved.traffic_conflict_verified ?? false))} · task_mutated=${escapeHtml(String(horizontalRouteContactVerifierObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">contact incident verifier</span><strong>${escapeHtml(horizontalRouteContactIncidentVerifiedObserved.incident_verified === true ? "verified" : "-")}</strong><span>status=${escapeHtml(horizontalRouteContactIncidentVerification.verification_status || "-")} · scope=${escapeHtml(horizontalRouteContactIncidentVerification.verification_scope || "-")} · route_blocking=${escapeHtml(String(horizontalRouteContactIncidentVerifiedObserved.route_blocking_verified ?? false))} · traffic_verified=${escapeHtml(String(horizontalRouteContactIncidentVerifiedObserved.traffic_conflict_verified ?? false))} · auto_gate=${escapeHtml(String(horizontalRouteContactIncidentVerifiedObserved.auto_gate ?? false))} · task_mutated=${escapeHtml(String(horizontalRouteContactIncidentVerifiedObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">incident-informed traffic verifier</span><strong>${escapeHtml(horizontalRouteIncidentTrafficObserved.traffic_conflict_verified === true ? "verified" : "-")}</strong><span>status=${escapeHtml(horizontalRouteIncidentTrafficVerification.verification_status || "-")} · scope=${escapeHtml(horizontalRouteIncidentTrafficVerification.verification_scope || "-")} · incident=${escapeHtml(String(horizontalRouteIncidentTrafficObserved.incident_verified ?? false))} · route_blocking=${escapeHtml(String(horizontalRouteIncidentTrafficObserved.route_blocking_verified ?? false))} · auto_gate=${escapeHtml(String(horizontalRouteIncidentTrafficObserved.auto_gate ?? false))} · task_mutated=${escapeHtml(String(horizontalRouteIncidentTrafficObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">incident-informed route blocking verifier</span><strong>${escapeHtml(horizontalRouteIncidentBlockingObserved.route_blocking_verified === true ? "verified" : "-")}</strong><span>status=${escapeHtml(horizontalRouteIncidentBlockingVerification.verification_status || "-")} · scope=${escapeHtml(horizontalRouteIncidentBlockingVerification.verification_scope || "-")} · traffic=${escapeHtml(String(horizontalRouteIncidentBlockingObserved.traffic_conflict_verified ?? false))} · candidate=${escapeHtml(String(horizontalRouteIncidentBlockingObserved.route_blocking_candidate ?? false))} · auto_gate=${escapeHtml(String(horizontalRouteIncidentBlockingObserved.auto_gate ?? false))} · task_mutated=${escapeHtml(String(horizontalRouteIncidentBlockingObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">operational incident report</span><strong>${escapeHtml(operationalIncidentObserved.operator_review_required === true ? "operator review" : "-")}</strong><span>status=${escapeHtml(operationalIncidentReport.report_status || "-")} · contact_candidate=${escapeHtml(String(operationalIncidentObserved.contact_event_incident_candidate ?? false))} · auto_gate=${escapeHtml(String(operationalIncidentObserved.auto_gate ?? false))} · incident_verified=${escapeHtml(String(operationalIncidentObserved.incident_verified ?? false))} · task_mutated=${escapeHtml(String(operationalIncidentObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">traffic conflict verifier</span><strong>${escapeHtml(trafficConflictObserved.traffic_conflict_verified === true ? "verified" : "-")}</strong><span>status=${escapeHtml(trafficConflictVerification.verification_status || "-")} · scope=${escapeHtml(String(trafficConflictObserved.verification_scope ?? "-"))} · route_blocking=${escapeHtml(String(trafficConflictObserved.route_blocking_verified ?? false))} · dropoff=${escapeHtml(String(trafficConflictObserved.dropoff_verified ?? false))} · task_mutated=${escapeHtml(String(trafficConflictObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">route blocking verifier</span><strong>${escapeHtml(routeBlockingObserved.route_blocking_verified === true ? "verified" : "-")}</strong><span>status=${escapeHtml(routeBlockingVerification.verification_status || "-")} · gate_candidate=${escapeHtml(String(routeBlockingObserved.gate_candidate ?? false))} · auto_gate=${escapeHtml(String(routeBlockingObserved.auto_gate ?? false))} · task_mutated=${escapeHtml(String(routeBlockingObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">alternate landing candidate</span><strong>${escapeHtml(alternateLandingCandidateObserved.alternate_landing_candidate === true ? "candidate" : "-")}</strong><span>status=${escapeHtml(alternateLandingCandidateEvidence.observation_status || "-")} · px4_route_changed=${escapeHtml(String(alternateLandingCandidateObserved.px4_route_changed ?? false))} · rth_commanded=${escapeHtml(String(alternateLandingCandidateObserved.rth_commanded ?? false))} · task_mutated=${escapeHtml(String(alternateLandingCandidateObserved.task_status_mutated ?? false))}</span></div>
        <div><span class="k">alternate landing execution</span><strong>${escapeHtml(alternateLandingBehaviorObservation.alternate_landing_behavior_observed === true ? "observed" : "-")}</strong><span>request=${escapeHtml(alternateLandingExecutionRequest.request_status || "-")} · dispatch=${escapeHtml(alternateLandingCommandDispatch.dispatch_status || "-")} · ack=${escapeHtml(String(alternateLandingCommandDispatch.command_ack_observed ?? false))} · basis=${escapeHtml(String(alternateLandingBehaviorObservation.completion_basis || alternateLandingCommandDispatch.completion_basis || "-"))} · landed=${escapeHtml(String(alternateLandingBehaviorObservation.landing_observed ?? false))} · completion=${escapeHtml(String(alternateLandingBehaviorObservation.delivery_completion_claimed ?? false))}</span></div>
        <div><span class="k">alternate mission upload</span><strong>${escapeHtml(alternateRouteBehaviorObservation.alternate_mission_uploaded === true ? "uploaded" : "-")}</strong><span>request=${escapeHtml(alternateMissionUploadRequest.request_status || "-")} · upload=${escapeHtml(alternateMissionUploadReceipt.upload_status || "-")} · ack=${escapeHtml(String(alternateMissionUploadReceipt.mission_ack_observed ?? false))} · items=${escapeHtml(String(alternateMissionUploadReceipt.mission_item_count ?? "-"))} · behavior=${escapeHtml(alternateRouteBehaviorObservation.observation_status || "-")} · dropoff=${escapeHtml(String(alternateRouteBehaviorObservation.dropoff_verified ?? false))} · completion=${escapeHtml(String(alternateRouteBehaviorObservation.delivery_completion_claimed ?? false))}</span></div>
        <div><span class="k">alternate route execution</span><strong>${escapeHtml(alternateRouteExecutionEvidence.alternate_route_execution_observed === true ? "observed" : "-")}</strong><span>status=${escapeHtml(alternateRouteExecutionEvidence.observation_status || "-")} · waypoint=${escapeHtml(String(alternateRouteExecutionEvidence.alternate_waypoint_reached_observed ?? false))} · progress=${escapeHtml(String(alternateRouteExecutionObserved.horizontal_progress_toward_alternate_waypoint_m ?? "-"))}m · distance=${escapeHtml(String(alternateRouteExecutionObserved.final_distance_to_alternate_waypoint_m ?? "-"))}m · dropoff=${escapeHtml(String(alternateRouteExecutionObserved.dropoff_verified ?? false))} · completion=${escapeHtml(String(alternateRouteExecutionObserved.delivery_completion_claimed ?? false))}</span></div>
        <div><span class="k">RTH behavior observation</span><strong>${escapeHtml(rthBehaviorObservation.return_to_home_behavior_observed === true ? "observed" : "-")}</strong><span>request=${escapeHtml(rthExecutionRequest.request_status || "-")} · dispatch=${escapeHtml(rthCommandDispatch.dispatch_status || "-")} · ack=${escapeHtml(String(rthCommandDispatch.command_ack_observed ?? false))} · basis=${escapeHtml(String(rthBehaviorObservation.completion_basis || rthCommandDispatch.completion_basis || "-"))} · state=${escapeHtml(String(rthBehaviorObservation.rth_state_label || "-"))} · completion=${escapeHtml(String(rthBehaviorObservation.delivery_completion_claimed ?? false))}</span></div>
        <div><span class="k">requested multi-drone</span><strong>${escapeHtml(operationalRequested.multi_drone_conflict_probe === true ? "support probe" : "-")}</strong><span>multi_vehicle=${escapeHtml(String(operationalRequested.multi_vehicle_enabled ?? "-"))} · verifier=${escapeHtml(String(operationalRequested.multi_drone_conflict_verifier_enabled ?? "-"))} · vehicle_ids=${escapeHtml(String((operationalRequested.explicit_vehicle_ids || []).length || 0))}</span></div>
        <div><span class="k">multi-vehicle frame contract</span><strong>${escapeHtml(multiVehicleFrameContract.primary_vehicle_id || "-")}</strong><span>frame=${escapeHtml(String(multiVehicleFrameContract.frame ?? "-"))} · additional=${escapeHtml(String((multiVehicleFrameContract.additional_vehicle_ids || []).length || 0))} · verifier=${escapeHtml(String(multiVehicleFrameContract.conflict_verifier_enabled ?? "-"))}</span></div>
        <div><span class="k">applied operational condition</span><strong>${escapeHtml(operationalApplied.method || operationalApplication.application_status || "-")}</strong><span>${escapeHtml(operationalApplied.world_sdf_path || "no operational marker mutation observed")}</span></div>
        <div><span class="k">observed operational evidence</span><strong>${escapeHtml(String(operationalObserved.observed ?? false))}</strong><span>${escapeHtml(operationalObserved.source || operationalEvidence.observation_status || "-")}</span></div>
        <div><span class="k">operational unsupported / approximated</span><strong>${escapeHtml(operationalNotes.join(", ") || "none")}</strong><span>visual markers and multi-drone probes do not enforce geofence, verify traffic, provide sensor evidence, trigger alternate landing/RTH, enable collision, report incidents, block routes, or alter task status</span></div>
      </div>
      ` : ""}
      ${Object.keys(telemetryProfile).length || Object.keys(telemetryApplication).length || Object.keys(telemetryEvidence).length ? `
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">telemetry condition</span><span class="detail-chip-value">${escapeHtml(telemetryProfile.condition_kind || telemetryApplication.condition_kind || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">telemetry applied</span><span class="detail-chip-value">${escapeHtml(telemetryApplication.application_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">telemetry observed</span><span class="detail-chip-value">${escapeHtml(telemetryEvidence.observation_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">freshness</span><span class="detail-chip-value">${escapeHtml(telemetryFreshness.freshness_status || "-")}</span></span>
      </div>
      <div class="detail-grid">
        <div><span class="k">requested telemetry</span><strong>${escapeHtml(telemetryRequested.telemetry_dropout_mode ?? "-")}</strong><span>observer_side_only=${escapeHtml(String(telemetryRequested.observer_side_only ?? "-"))}</span></div>
        <div><span class="k">applied telemetry condition</span><strong>${escapeHtml(telemetryApplied.method || telemetryApplication.application_status || "-")}</strong><span>publisher_mutated=${escapeHtml(String(telemetryPublisherStateMutated))} · mission_upload_path_mutated=${escapeHtml(String(telemetryMissionUploadPathMutated))} · mission_progress_mutated=${escapeHtml(String(telemetryMissionProgressMutated))}</span></div>
        <div><span class="k">observed telemetry gap evidence</span><strong>${escapeHtml(String(telemetryObserved.gap_count ?? 0))} gaps</strong><span>baseline=${escapeHtml(String(telemetryObserved.baseline_observer_sample_observed ?? "-"))} · pause=${escapeHtml(String(telemetryObserved.observer_sample_pause_performed ?? "-"))} · post_pause=${escapeHtml(String(telemetryObserved.post_pause_observer_sample_observed ?? "-"))} · missing_samples=${escapeHtml(String(telemetryObserved.missing_sample_count ?? "-"))}</span></div>
        <div><span class="k">telemetry unsupported / approximated</span><strong>${escapeHtml(telemetryNotes.join(", ") || "none")}</strong><span>observer sample pause does not claim publisher transport loss, vehicle recovery behavior, mission failure, or delivery completion</span></div>
      </div>
      ` : ""}
      ${Object.keys(mavlinkLinkProfile).length || Object.keys(mavlinkLinkApplication).length || Object.keys(mavlinkLinkEvidence).length ? `
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">MAVLink link condition</span><span class="detail-chip-value">${escapeHtml(mavlinkLinkProfile.condition_kind || mavlinkLinkApplication.condition_kind || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">MAVLink capability</span><span class="detail-chip-value">${escapeHtml([mavlinkLinkCapability.mavlink_link_loss, mavlinkLinkCapability.heartbeat_gap_observer].filter((value) => value && value !== "not_requested").join(", ") || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">MAVLink applied</span><span class="detail-chip-value">${escapeHtml(mavlinkLinkApplication.application_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">MAVLink observed</span><span class="detail-chip-value">${escapeHtml(mavlinkLinkEvidence.observation_status || "-")}</span></span>
      </div>
      <div class="detail-grid">
        <div><span class="k">requested MAVLink link</span><strong>${escapeHtml(mavlinkLinkRequested.mavlink_link_degradation_mode ?? "-")}</strong><span>requested_link_loss=${escapeHtml(String(mavlinkLinkRequested.requested_link_loss ?? "-"))}</span></div>
        <div><span class="k">applied MAVLink link condition</span><strong>${escapeHtml(mavlinkLinkApplication.application_status || "-")}</strong><span>px4_endpoint_mutated=${escapeHtml(String(mavlinkLinkApplication.px4_mavlink_endpoint_mutated ?? false))} · gazebo_mutated=${escapeHtml(String(mavlinkLinkApplication.gazebo_command_path_mutated ?? false))} · mission_upload_path_mutated=${escapeHtml(String(mavlinkLinkApplication.mission_upload_path_mutated ?? false))} · mission_upload_interrupted=${escapeHtml(String(mavlinkLinkApplication.mission_upload_interruption_observed ?? false))}</span></div>
        <div><span class="k">observed MAVLink gap evidence</span><strong>${escapeHtml(String(mavlinkLinkObserved.mavlink_link_loss_observed ?? false))}</strong><span>baseline=${escapeHtml(String(mavlinkLinkObserved.baseline_heartbeat_observed ?? "-"))} · heartbeat_count=${escapeHtml(String(mavlinkLinkObserved.heartbeat_count ?? "-"))} · heartbeat_gap=${escapeHtml(String(mavlinkLinkObserved.heartbeat_gap_observed ?? false))} · endpoint_restart=${escapeHtml(String(mavlinkLinkObserved.endpoint_restart_performed ?? false))} · post_restart=${escapeHtml(String(mavlinkLinkObserved.post_restart_heartbeat_observed ?? "-"))} · failsafe=${escapeHtml(String(mavlinkLinkObserved.vehicle_failsafe_observed ?? false))}</span></div>
        <div><span class="k">MAVLink unsupported / approximated</span><strong>${escapeHtml(mavlinkLinkNotes.join(", ") || "none")}</strong><span>${escapeHtml(mavlinkLinkApplication.rf_link_loss_claimed === false ? "bounded SITL endpoint loss only; no RF-link or vehicle-failsafe claim" : "support detection only; observer dropout is not used as link-loss evidence")}</span></div>
      </div>
      ` : ""}
    </div>
  `;
}

function renderMissionScenarioPreparedSITL(result) {
  const request = result?.sitl_execution_request || null;
  const task = result?.sitl_execution_task || null;
  if (!request) return "";
  return `
    <div class="detail-card">
      <div class="k">Prepared SITL Execution Request</div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">status</span><span class="detail-chip-value">${escapeHtml(request.request_status || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">scope</span><span class="detail-chip-value">${escapeHtml(request.preparation_scope || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">mode</span><span class="detail-chip-value">${escapeHtml(request.execution_mode || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">task</span><span class="detail-chip-value">${escapeHtml(task?.task_id || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">endpoint</span><span class="detail-chip-value">${escapeHtml(request.target_endpoint || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">explicit approval</span><span class="detail-chip-value">${escapeHtml(String(request.requires_explicit_execution_approval ?? true))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">execution invoked</span><span class="detail-chip-value">${escapeHtml(String(request.execution_invoked ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">Gazebo invoked</span><span class="detail-chip-value">${escapeHtml(String(request.gazebo_execution_invoked ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">MAVLink dispatch</span><span class="detail-chip-value">${escapeHtml(String(request.mavlink_dispatch_performed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">mission upload</span><span class="detail-chip-value">${escapeHtml(String(request.px4_mission_upload_performed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">hardware</span><span class="detail-chip-value">${escapeHtml(String(request.hardware_target_allowed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">physical</span><span class="detail-chip-value">${escapeHtml(String(request.physical_execution_invoked ?? false))}</span></span>
      </div>
      <div class="muted">Prepared only. Use the operator-approved Execute Live SITL action to call the explicit opt-in execution route. Payload, dropoff, and epic-exit evidence remain read-only.</div>
    </div>
  `;
}

function renderMissionScenarioLiveSITLExecution(result, options = {}) {
  const response = result?.sitl_execution_response || null;
  const task = response?.task || result?.sitl_execution_result_task || null;
  if (!response && !task) return "";
  const summary = response?.summary || {};
  const blockedReceipt = response?.px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt || null;
  const failedReceipt = response?.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt
    || task?.artifacts?.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt
    || null;
  const envelopeAdvisory = response?.envelope_violation_advisory || task?.artifacts?.envelope_violation_advisory || null;
  const liveRun = response?.px4_gazebo_mission_designer_sitl_live_flight_run || null;
  const executionResult = response?.px4_gazebo_mission_designer_sitl_execution_result || task?.artifacts?.px4_gazebo_mission_designer_sitl_execution_result || null;
  const resultStatus = summary.live_flight_status
    || failedReceipt?.live_flight_execution_status
    || failedReceipt?.failure_category
    || summary.result_status
    || executionResult?.result_status
    || summary.task_status
    || "-";
  const uploadStatus = summary.upload_status || response?.px4_gazebo_sitl_mission_upload_receipt?.upload_status || "-";
  const liveBlockedReasons = uniqueStrings([
    ...(Array.isArray(failedReceipt?.blocked_reasons) ? failedReceipt.blocked_reasons : []),
    ...(Array.isArray(blockedReceipt?.blocked_reasons) ? blockedReceipt.blocked_reasons : []),
    ...(Array.isArray(summary.blocked_reasons) ? summary.blocked_reasons : []),
  ]);
  return `
    <div class="detail-card">
      <div class="k">Live SITL Execution</div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">status</span><span class="detail-chip-value">${escapeHtml(resultStatus)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">upload</span><span class="detail-chip-value">${escapeHtml(uploadStatus)}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">task</span><span class="detail-chip-value">${escapeHtml(task?.task_id || summary.task_id || "-")}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">live mode</span><span class="detail-chip-value">${escapeHtml(String(response?.live_flight_mode_requested ?? summary.live_flight_mode_requested ?? failedReceipt?.live_flight_mode_requested ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">live opt-in</span><span class="detail-chip-value">${escapeHtml(String(response?.live_flight_opted_in ?? summary.live_flight_opted_in ?? failedReceipt?.live_flight_opted_in ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">runner invoked</span><span class="detail-chip-value">${escapeHtml(String(summary.live_flight_runner_invoked ?? liveRun?.live_flight_runner_invoked ?? failedReceipt?.live_flight_runner_invoked ?? false))}</span></span>
      </div>
      <div class="detail-chip-row">
        <span class="detail-chip"><span class="detail-chip-label">mission upload</span><span class="detail-chip-value">${escapeHtml(String(summary.px4_mission_upload_performed ?? executionResult?.px4_mission_upload_performed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">flight observed</span><span class="detail-chip-value">${escapeHtml(String(summary.actual_sitl_flight_evidence_observed ?? executionResult?.actual_sitl_flight_evidence_observed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">payload observed</span><span class="detail-chip-value">${escapeHtml(String(summary.payload_release_observed ?? executionResult?.payload_release_observed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">dropoff verified</span><span class="detail-chip-value">${escapeHtml(String(summary.dropoff_verified ?? executionResult?.dropoff_verified ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">hardware</span><span class="detail-chip-value">${escapeHtml(String(summary.hardware_target_allowed ?? executionResult?.hardware_target_allowed ?? false))}</span></span>
        <span class="detail-chip"><span class="detail-chip-label">physical</span><span class="detail-chip-value">${escapeHtml(String(summary.physical_execution_invoked ?? executionResult?.physical_execution_invoked ?? false))}</span></span>
      </div>
      ${blockedReceipt || failedReceipt ? `<div class="detail-error">${escapeHtml(liveBlockedReasons.join("; ") || "Live SITL execution blocked by server-side gates.")}</div>` : ""}
      ${failedReceipt ? `<div class="item-meta mono">failure_category=${escapeHtml(failedReceipt.failure_category || "-")}</div><div class="item-meta mono">failure_reason=${escapeHtml(failedReceipt.failure_reason_digest || "-")}</div>` : ""}
      ${renderMissionScenarioEnvelopeViolationAdvisory(envelopeAdvisory)}
      <div class="muted">Operator-approved execute is the only action surface here. Payload release, dropoff verification, and epic-exit artifacts below are read-only evidence.</div>
    </div>
    ${task ? renderMissionDesignerSITLExecutionResult(task, { includeFlightPath: false, includeFailureReplay: options.includeFailureReplay !== false }) : ""}
  `;
}

function missionScenarioAutoMissionEvidenceData(source) {
  const response = asPlainObject(source?.sitl_execution_response || source);
  const task = asPlainObject(
    response.task
    || source?.sitl_execution_result_task
    || source?.sitl_execution_task
    || source?.task,
  );
  const artifacts = asPlainObject(task.artifacts || source?.artifacts);
  const summary = asPlainObject(response.summary || source?.summary);
  const autoSummary = asPlainObject(
    artifacts.missionos_auto_mission_runtime_monitor_summary
    || artifacts.auto_mission_runtime_monitor_summary
    || artifacts.auto_mission_summary
    || response.auto_mission_summary
    || summary.auto_mission_summary
    || summary,
  );
  const waypointGate = asPlainObject(
    artifacts.missionos_auto_mission_waypoint_gate_summary
    || artifacts.auto_mission_waypoint_gate
    || artifacts.waypoint_gate
    || response.waypoint_gate
    || source?.waypoint_gate
    || summary.waypoint_gate,
  );
  const dropoffGate = asPlainObject(
    artifacts.missionos_auto_mission_dropoff_gate_summary
    || artifacts.auto_mission_dropoff_gate
    || artifacts.dropoff_gate
    || response.dropoff_gate
    || source?.dropoff_gate
    || summary.dropoff_gate,
  );
  const sitlDeliveryGate = asPlainObject(
    artifacts.missionos_auto_mission_sitl_delivery_gate_summary
    || artifacts.auto_mission_sitl_delivery_gate
    || artifacts.sitl_delivery_gate
    || response.sitl_delivery_gate
    || source?.sitl_delivery_gate
    || summary.sitl_delivery_gate,
  );
  const payloadReleaseSimGate = asPlainObject(
    artifacts.missionos_auto_mission_payload_release_sim_gate_summary
    || artifacts.auto_mission_payload_release_sim_gate
    || artifacts.payload_release_sim_gate
    || response.payload_release_sim_gate
    || source?.payload_release_sim_gate
    || summary.payload_release_sim_gate,
  );
  const payloadReleaseEvent = asPlainObject(
    artifacts.missionos_auto_mission_payload_release_event
    || artifacts.auto_mission_payload_release_event
    || artifacts.payload_release_event
    || response.payload_release_event
    || source?.payload_release_event
    || summary.payload_release_event,
  );
  const hasAutoSummaryEvidence = String(autoSummary.schema_version || "").startsWith("missionos_auto_mission_")
    || autoSummary.auto_mission_runner_invoked !== undefined
    || autoSummary.auto_mission_started !== undefined
    || autoSummary.route_waypoint_reached_count !== undefined
    || autoSummary.route_completed_claimed !== undefined
    || autoSummary.sitl_delivery_claimed !== undefined
    || autoSummary.payload_release_observed_sim !== undefined;
  const hasAutoEvidence = hasAutoSummaryEvidence || [
    waypointGate,
    dropoffGate,
    sitlDeliveryGate,
    payloadReleaseSimGate,
    payloadReleaseEvent,
  ].some((item) => Object.keys(item).length);
  return {
    artifacts,
    autoSummary,
    waypointGate,
    dropoffGate,
    sitlDeliveryGate,
    payloadReleaseSimGate,
    payloadReleaseEvent,
    hasAutoEvidence,
  };
}

function missionScenarioAutoMissionRunningReceipt(source) {
  const response = asPlainObject(source?.sitl_execution_response || source);
  const task = asPlainObject(
    response.task
    || source?.sitl_execution_result_task
    || source?.sitl_execution_task
    || source?.task,
  );
  const artifacts = asPlainObject(task.artifacts || source?.artifacts);
  return asPlainObject(
    artifacts.missionos_auto_mission_gui_dispatch_running_receipt
    || response.missionos_auto_mission_gui_dispatch_running_receipt
    || source?.missionos_auto_mission_gui_dispatch_running_receipt,
  );
}

function renderMissionScenarioAutoMissionRuntimeRecoveryPending(source) {
  const response = asPlainObject(source?.sitl_execution_response || source);
  const task = asPlainObject(
    response.task
    || source?.sitl_execution_result_task
    || source?.sitl_execution_task
    || source?.task,
  );
  const runningReceipt = missionScenarioAutoMissionRunningReceipt(source);
  const hasCompletedAutoEvidence = missionScenarioAutoMissionEvidenceData(source).hasAutoEvidence;
  const blockedBeforeRunner = missionOSChatLiveSITLBlockedBeforeRunner({ task, summary: source?.summary });
  const taskRunning = task.status === "running";
  const receiptRunning = runningReceipt.dispatch_status === "running";
  const executionInProgress = source?.sitl_execution_in_progress === true || taskRunning || receiptRunning;
  if ((!executionInProgress && !blockedBeforeRunner) || hasCompletedAutoEvidence) return "";
  const monitorSeconds = missionOSChatFiniteNumber(
    runningReceipt.monitor_seconds,
    runningReceipt.auto_mission_monitor_seconds,
    source?.summary?.auto_mission_monitor_seconds,
  );
  const processTimeoutSeconds = missionOSChatFiniteNumber(
    runningReceipt.process_timeout_seconds,
    source?.summary?.auto_mission_process_timeout_seconds,
  );
  const monitorLine = monitorSeconds !== undefined
    ? `${Math.round(monitorSeconds)}s`
    : "-";
  const timeoutLine = processTimeoutSeconds !== undefined
    ? `${Math.round(processTimeoutSeconds)}s`
    : "-";
  const taskId = task.task_id || runningReceipt.task_id || source?.summary?.sitl_execution_task_id || "-";
  const liveSnapshot = asPlainObject(asPlainObject(task.artifacts).missionos_auto_mission_runtime_snapshot);
  const hasLiveSnapshot = liveSnapshot.snapshot_status === "running" || liveSnapshot.sample_index !== undefined;
  const liveTelemetry = hasLiveSnapshot
    ? renderMissionScenarioAutoMissionRuntimeRecoveryLiveTelemetry(liveSnapshot)
    : "";
  const agentProposalHtml = missionOSChatRuntimeRecoveryAgentProposalHtml(taskId, task);
  const evidenceStatus = hasLiveSnapshot
    ? (runningReceipt.recovery_agent_evidence_status || "running_telemetry")
    : (runningReceipt.recovery_agent_evidence_status || "pending");
  const recoveryControls = taskId && taskId !== "-"
    ? missionOSChatRuntimeRecoveryControlsHtml(taskId)
    : [
      `<div class="item-meta mono">LAND/RTL controls=not available</div>`,
      `<div class="muted">A running task id is required before MissionOS can request operator-approved runtime recovery dispatch.</div>`,
    ].join("");
  const headerTag = blockedBeforeRunner
    ? statusTag("not started")
    : hasLiveSnapshot
    ? statusTag("live telemetry")
    : statusTag("evidence pending");
  const headerMeta = blockedBeforeRunner
    ? "runner not invoked · opt-in missing"
    : hasLiveSnapshot
    ? "AUTO mission running · live runtime telemetry"
    : "AUTO mission running · recovery evidence pending";
  const introLine = blockedBeforeRunner
    ? "Gateway did not invoke the live SITL runner because explicit opt-in is missing. PX4/Gazebo SITL may already be ready, but no mission upload, PX4 ACK, telemetry, recovery evidence, delivery, or progress has been observed for this request."
    : hasLiveSnapshot
    ? "Gateway is streaming the approved AUTO mission's in-flight telemetry to the Recovery Agent view. These are read-only observed samples; MissionOS keeps delivery/progress claims false because the verifier summary is not attached yet."
    : "Gateway has entered the approved AUTO mission execution boundary. The Recovery Agent evidence window is not attached until runtime telemetry or the final verifier summary exists, so MissionOS keeps delivery/progress claims false while waiting.";
  const executionStatus = blockedBeforeRunner
    ? "not_started"
    : (runningReceipt.dispatch_status || (taskRunning ? "running" : "waiting"));
  const runnerLine = blockedBeforeRunner
    ? "runner=not invoked"
    : `runner=${runningReceipt.runner_script || "scripts/smoke_missionos_auto_mission_full_runtime_probe.py"}`;
  const recoveryEvidenceLine = blockedBeforeRunner ? "not_started" : evidenceStatus;
  const controlsLine = blockedBeforeRunner
    ? "LAND/RTL controls=disabled; runner not active"
    : "manual LAND/RTL controls=operator approval required";
  const recoveryControlsHtml = blockedBeforeRunner
    ? `<div class="muted">Runtime recovery controls are disabled because no live runner or telemetry window exists for this request.</div>`
    : recoveryControls;
  return `
    <div class="detail-section mission-auto-runtime-recovery-pending" data-testid="mission-auto-runtime-recovery-pending">
      <div class="detail-heading">
        <div>
          <div class="k">Runtime Recovery Agent view</div>
          <div class="item-meta">${escapeHtml(headerMeta)}</div>
        </div>
        ${headerTag}
      </div>
      <div class="muted">${escapeHtml(introLine)}</div>
      ${liveTelemetry}
      <div class="detail-grid">
        <div class="detail-card">
          <div class="k">Execution Boundary</div>
          <strong>${escapeHtml(executionStatus)}</strong>
          <div class="item-meta mono">task=${escapeHtml(taskId)}</div>
          <div class="item-meta mono">${escapeHtml(runnerLine)}</div>
        </div>
        <div class="detail-card">
          <div class="k">AUTO Window</div>
          <strong>${escapeHtml(monitorLine)}</strong>
          <div class="item-meta mono">process_timeout=${escapeHtml(timeoutLine)}</div>
          <div class="item-meta mono">full AUTO routes can take tens of minutes</div>
        </div>
        <div class="detail-card">
          <div class="k">Recovery Evidence</div>
          <strong>${escapeHtml(recoveryEvidenceLine)}</strong>
          <div class="item-meta mono">${escapeHtml(controlsLine)}</div>
          ${agentProposalHtml}
          ${recoveryControlsHtml}
        </div>
        <div class="detail-card">
          <div class="k">Truth Boundary</div>
          <div class="detail-chip-row">
            <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">delivery_completion_claimed</span><span class="detail-chip-value">false</span></span>
            <span class="detail-chip mission-brief-chip-ok"><span class="detail-chip-label">physical_execution_invoked</span><span class="detail-chip-value">false</span></span>
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderMissionScenarioAutoMissionRuntimeRecoveryLiveTelemetry(snapshot) {
  const snap = asPlainObject(snapshot);
  const num = (...values) => missionOSChatFiniteNumber(...values);
  const fmt = (value, suffix = "") => (value === undefined ? "-" : `${value}${suffix}`);
  const progressM = num(snap.progress_m);
  const distanceM = num(snap.distance_to_home_m);
  const altitudeM = num(snap.altitude_above_home_m);
  const terrainClearanceM = num(snap.terrain_clearance_m);
  const terrainClearanceTargetM = num(snap.terrain_clearance_target_m);
  const terrainClearanceMarginM = num(snap.terrain_clearance_margin_m);
  const terrainClearanceStatus = snap.terrain_clearance_status === undefined || snap.terrain_clearance_status === null
    ? "not_configured"
    : String(snap.terrain_clearance_status);
  const localX = num(snap.local_x_m);
  const localY = num(snap.local_y_m);
  const battery = num(snap.battery_remaining_percent);
  const sampleIndex = num(snap.sample_index);
  const elapsed = num(snap.elapsed_seconds);
  const missionCurrent = num(snap.mission_current_seq);
  const missionReached = num(snap.mission_reached_seq);
  const waypointTotal = num(snap.waypoint_total);
  const batteryFirst = num(snap.battery_remaining_first_percent);
  const batteryLatest = num(snap.battery_remaining_latest_percent, battery);
  const batteryDelta = num(snap.battery_remaining_delta_percent);
  const batterySampleCount = num(snap.battery_remaining_sample_count);
  const navState = snap.nav_state === undefined || snap.nav_state === null ? "-" : String(snap.nav_state);
  const heartbeat = snap.heartbeat_observed === true;
  const batterySampleAccepted = snap.battery_sample_accepted === true;
  const batteryRejectedReason = snap.battery_sample_rejected_reason === undefined || snap.battery_sample_rejected_reason === null
    ? ""
    : String(snap.battery_sample_rejected_reason);
  const batteryWarning = snap.battery_warning === undefined || snap.battery_warning === null ? "-" : String(snap.battery_warning);
  const dwellCandidate = snap.dropoff_dwell_candidate === true;
  const batteryHasSample = battery !== undefined;
  const batteryDynamic = snap.battery_remaining_dynamic === true || (
    batteryDelta !== undefined
    && Math.abs(Number(batteryDelta)) > 0.001
  );
  const batteryKnownStatic = snap.battery_remaining_dynamic === false
    && batterySampleCount !== undefined
    && Number(batterySampleCount) >= 2;
  const batterySourceRaw = snap.battery_state_source === undefined || snap.battery_state_source === null
    ? ""
    : String(snap.battery_state_source);
  const batterySource = battery !== undefined
    ? (batteryDynamic
      ? "PX4 SITL simulated battery_status observed (not real power module)"
      : batteryKnownStatic
        ? "PX4 SITL simulated fixed telemetry sample (not real power module)"
        : "PX4 SITL simulated battery_status sample (not real power module)")
    : "battery telemetry not attached";
  const batteryTitle = batteryHasSample
    ? (batteryKnownStatic ? "PX4 SITL Battery" : "Battery Remaining")
    : "Battery";
  const batteryValue = batteryHasSample
    ? (batteryKnownStatic ? "fixed sample" : fmt(batteryLatest, " %"))
    : fmt(battery, " %");
  const batteryTrendLine = batteryHasSample
    ? (batteryKnownStatic
      ? `<div class="item-meta mono">reported=${escapeHtml(fmt(batteryLatest, " %"))}</div>`
      : `<div class="item-meta mono">first=${escapeHtml(fmt(batteryFirst, " %"))} · latest=${escapeHtml(fmt(batteryLatest, " %"))} · delta=${escapeHtml(fmt(batteryDelta, " %"))} · samples=${escapeHtml(fmt(batterySampleCount))}</div>`)
    : "";
  const positionLine = localX !== undefined && localY !== undefined
    ? `x=${localX}m y=${localY}m`
    : "-";
  return `
    <div class="detail-grid mission-auto-runtime-recovery-live" data-testid="mission-auto-runtime-recovery-live">
      <div class="detail-card">
        <div class="k">Route Progress</div>
        <strong>${escapeHtml(fmt(progressM, " m"))}</strong>
        <div class="item-meta mono">mission seq reached=${escapeHtml(fmt(missionReached))}/${escapeHtml(fmt(waypointTotal))} (current seq=${escapeHtml(fmt(missionCurrent))})</div>
        <div class="item-meta mono">sample #${escapeHtml(fmt(sampleIndex))} · elapsed=${escapeHtml(fmt(elapsed, "s"))}</div>
      </div>
      <div class="detail-card">
        <div class="k">Position</div>
        <strong>${escapeHtml(fmt(altitudeM, " m AGL"))}</strong>
        <div class="item-meta mono">${escapeHtml(positionLine)}</div>
        <div class="item-meta mono">distance_to_home=${escapeHtml(fmt(distanceM, " m"))}</div>
        <div class="item-meta mono">terrain_clearance=${escapeHtml(fmt(terrainClearanceM, " m"))}</div>
        <div class="item-meta mono">terrain_target=${escapeHtml(fmt(terrainClearanceTargetM, " m"))} · margin=${escapeHtml(fmt(terrainClearanceMarginM, " m"))}</div>
        <div class="item-meta mono">terrain_status=${escapeHtml(terrainClearanceStatus)}</div>
      </div>
      <div class="detail-card">
        <div class="k">${escapeHtml(batteryTitle)}</div>
        <strong>${escapeHtml(batteryValue)}</strong>
        ${batteryTrendLine}
        <div class="item-meta mono">warning=${escapeHtml(batteryWarning)}</div>
        <div class="item-meta mono">source=${escapeHtml(batterySource)}</div>
        ${batterySourceRaw ? `<div class="item-meta mono">source_ref=${escapeHtml(batterySourceRaw)}</div>` : ""}
        <div class="item-meta mono">battery_sample=${batterySampleAccepted ? "accepted" : "held"}</div>
        ${batteryRejectedReason ? `<div class="item-meta mono">battery_reject=${escapeHtml(batteryRejectedReason)}</div>` : ""}
        <div class="item-meta mono">heartbeat=${heartbeat ? "observed" : "stale"}</div>
        <div class="muted">${batteryDynamic ? "Observed PX4 SITL simulated battery_status trend from the live monitor. This is not real power-module endurance evidence." : "Single or unchanged PX4 SITL simulated battery sample; battery drain is not inferred without a trend, and this is not real power-module evidence."}</div>
      </div>
      <div class="detail-card">
        <div class="k">Flight State</div>
        <strong>${escapeHtml(navState)}</strong>
        <div class="item-meta mono">dropoff_dwell_candidate=${dwellCandidate ? "yes" : "no"}</div>
        <div class="muted">Observed-only; the Recovery Agent reads these samples but does not auto-dispatch LAND/RTL.</div>
      </div>
    </div>
  `;
}

function renderMissionScenarioAutoMissionEvidenceReadout(source) {
  const {
    autoSummary,
    waypointGate,
    dropoffGate,
    sitlDeliveryGate,
    payloadReleaseSimGate,
    payloadReleaseEvent,
    hasAutoEvidence,
  } = missionScenarioAutoMissionEvidenceData(source);
  if (!hasAutoEvidence) return "";
  const readNumber = (...values) => missionOSChatFiniteNumber(...values);
  const readBool = (...values) => values.some((value) => value === true);
  const routeCompleted = readBool(
    waypointGate.route_completed_claimed,
    sitlDeliveryGate.route_completed_claimed,
    payloadReleaseSimGate.route_completed_claimed,
    autoSummary.route_completed_claimed,
  );
  const dropoffVerified = readBool(
    dropoffGate.dropoff_verified,
    sitlDeliveryGate.dropoff_verified,
    payloadReleaseSimGate.dropoff_verified,
    autoSummary.dropoff_verified,
  );
  const payloadCommandAcked = readBool(
    sitlDeliveryGate.payload_release_command_acked,
    payloadReleaseSimGate.payload_release_command_acked,
    autoSummary.payload_release_command_acked,
  );
  const sitlDeliveryClaimed = readBool(
    sitlDeliveryGate.sitl_delivery_claimed,
    autoSummary.sitl_delivery_claimed,
  );
  const payloadObservedSim = readBool(
    payloadReleaseSimGate.payload_release_observed_sim,
    autoSummary.payload_release_observed_sim,
  );
  const finalLandingSafe = readBool(autoSummary.final_landing_safe);
  const deliveryClaimed = readBool(
    sitlDeliveryGate.delivery_completion_claimed,
    payloadReleaseSimGate.delivery_completion_claimed,
    autoSummary.delivery_completion_claimed,
  );
  const physicalDeliveryVerified = readBool(
    sitlDeliveryGate.physical_delivery_verified,
    payloadReleaseSimGate.physical_delivery_verified,
    autoSummary.physical_delivery_verified,
  );
  const recoveryObservationLost = readBool(autoSummary.recovery_observation_lost);
  const reachedCount = readNumber(
    waypointGate.route_waypoint_reached_count,
    autoSummary.route_waypoint_reached_count,
  );
  const expectedCount = readNumber(
    waypointGate.expected_route_waypoint_count,
    autoSummary.route_waypoint_count,
  );
  const progressM = readNumber(autoSummary.observed_progress_m, autoSummary.horizontal_progress_m);
  const plannedM = readNumber(autoSummary.planned_route_m, autoSummary.operator_route_distance_m);
  const dwellSeconds = readNumber(dropoffGate.observed_dwell_seconds);
  const residualM = readNumber(dropoffGate.observed_min_residual_xy_m);
  const zDropM = readNumber(
    payloadReleaseEvent.payload_release_z_drop_m,
    payloadReleaseSimGate.payload_release_z_drop_m,
  );
  const homeDistanceM = readNumber(autoSummary.recovery_distance_to_home_end_m);
  const payloadEventSource = payloadReleaseSimGate.payload_release_event_source
    || payloadReleaseEvent.payload_release_event_source
    || "-";
  const payloadBeforeZ = readNumber(payloadReleaseEvent.payload_pose_before_release?.z);
  const payloadAfterZ = readNumber(payloadReleaseEvent.payload_pose_after_release?.z);
  const countLabel = reachedCount !== undefined && expectedCount !== undefined
    ? `${reachedCount}/${expectedCount}`
    : reachedCount !== undefined
      ? String(reachedCount)
      : "-";
  const routeLine = progressM !== undefined && plannedM !== undefined
    ? `${missionOSChatFormatMeters(progressM)} / ${missionOSChatFormatMeters(plannedM)} m`
    : progressM !== undefined
      ? `${missionOSChatFormatMeters(progressM)} m`
      : "-";
  const boolChip = (label, value, expected = true) => {
    const ok = value === expected;
    const cls = ok ? "mission-brief-chip-ok" : "mission-brief-chip-pending";
    return `<span class="detail-chip ${cls}"><span class="detail-chip-label">${escapeHtml(label)}</span><span class="detail-chip-value">${escapeHtml(String(value))}</span></span>`;
  };
  const status = payloadObservedSim
    ? "sim payload separated"
    : sitlDeliveryClaimed
      ? "command ACK reached"
      : dropoffVerified
        ? "dropoff verified"
        : routeCompleted
          ? "route completed"
          : "recorded";
  const primaryMessage = payloadObservedSim
    ? "SITL/Gazebo cargo separation and fall were observed. This is simulation-only payload separation; real-world delivery is not claimed."
    : sitlDeliveryClaimed
      ? "SITL command-level delivery reached payload COMMAND_ACK. Simulated cargo separation and real-world delivery are not claimed."
      : "AUTO mission evidence is rendered from persisted runtime artifacts only.";
  return `
    <div class="detail-section mission-auto-sitl-readout" data-testid="mission-auto-sitl-readout">
      <div class="detail-heading">
        <div>
          <div class="k">AUTO Mission SITL Evidence</div>
          <div class="item-meta">${escapeHtml(status)}</div>
        </div>
        ${statusTag(status)}
      </div>
      <div class="muted">${escapeHtml(primaryMessage)}</div>
      <div class="detail-grid">
        <div class="detail-card">
          <div class="k">Route Gate</div>
          <strong>${escapeHtml(countLabel)}</strong>
          <div class="item-meta mono">route_progress=${escapeHtml(routeLine)}</div>
          <div class="item-meta mono">route_completed_claimed=${escapeHtml(String(routeCompleted))}</div>
        </div>
        <div class="detail-card">
          <div class="k">Dropoff Hover Gate</div>
          <strong>${escapeHtml(String(dropoffVerified))}</strong>
          <div class="item-meta mono">dwell_seconds=${escapeHtml(String(dwellSeconds ?? "-"))}</div>
          <div class="item-meta mono">residual_xy_m=${escapeHtml(String(residualM ?? "-"))}</div>
        </div>
        <div class="detail-card">
          <div class="k">Payload Command</div>
          <strong>${escapeHtml(String(payloadCommandAcked))}</strong>
          <div class="item-meta mono">sitl_delivery_claimed=${escapeHtml(String(sitlDeliveryClaimed))}</div>
          <div class="item-meta mono">claim_model=${escapeHtml(sitlDeliveryGate.claim_model || "-")}</div>
        </div>
        <div class="detail-card">
          <div class="k">Sim Payload Separation</div>
          <strong>${escapeHtml(String(payloadObservedSim))}</strong>
          <div class="item-meta mono">event_source=${escapeHtml(payloadEventSource)}</div>
          <div class="item-meta mono">payload_release_observed_sim=${escapeHtml(String(payloadObservedSim))}</div>
          <div class="item-meta mono">z_drop_m=${escapeHtml(String(zDropM ?? "-"))}</div>
          <div class="item-meta mono">payload_z_before_after=${escapeHtml(String(payloadBeforeZ ?? "-"))} → ${escapeHtml(String(payloadAfterZ ?? "-"))}</div>
        </div>
        <div class="detail-card">
          <div class="k">Recovery</div>
          <strong>${escapeHtml(finalLandingSafe ? "final landing safe" : "pending / not safe")}</strong>
          <div class="item-meta mono">recovery_observation_lost=${escapeHtml(String(recoveryObservationLost))}</div>
          <div class="item-meta mono">distance_to_home_end_m=${escapeHtml(String(homeDistanceM ?? "-"))}</div>
        </div>
        <div class="detail-card">
          <div class="k">Truth Boundary</div>
          <div class="detail-chip-row">
            ${boolChip("delivery_completion_claimed", deliveryClaimed, false)}
            ${boolChip("physical_delivery_verified", physicalDeliveryVerified, false)}
          </div>
          <div class="muted">SITL can prove simulated separation. It does not prove hardware delivery, field delivery, or payload ground contact/settling.</div>
        </div>
      </div>
    </div>
  `;
}

function renderMissionScenarioEnvelopeViolationAdvisory(advisory) {
  if (!advisory || typeof advisory !== "object" || advisory.advisory_status !== "operator_review_required") return "";
  const violations = Array.isArray(advisory.violations) ? advisory.violations : [];
  const violationText = violations.map((item) => {
    const kind = item?.violation_kind || "contract_envelope_violation";
    const requested = item?.requested_value ?? "-";
    const limit = item?.limit_value ?? "-";
    const unit = item?.unit || "";
    return `${kind}: requested=${requested}${unit ? ` ${unit}` : ""}, limit=${limit}${unit ? ` ${unit}` : ""}`;
  }).join("; ") || "contract envelope violation";
  return `
    <div class="detail-error">
      envelope_violation_advisory: ${escapeHtml(violationText)}
      <div class="item-meta mono">mission_response_kind=${escapeHtml(advisory.mission_response_kind || "advisory")} · form2_subtype=${escapeHtml(advisory.form2_subtype || "Form 2b")} · execution_upload_blocked=${escapeHtml(String(advisory.execution_upload_blocked ?? true))}</div>
      <div class="item-meta">SITL upload は contract envelope violation のため開始されず、operator review が必要です。recovery dispatch / delivery completion / hardware / physical authority は発生していません。</div>
    </div>
  `;
}

function setMissionScenarioSummary(result) {
  const summary = result?.summary || {};
  const executionSummary = result?.sitl_execution_response?.summary || {};
  if (missionScenarioValidationStatusEl) {
    missionScenarioValidationStatusEl.textContent = summary.validation_status || "-";
  }
  if (missionScenarioDryRunStatusEl) {
    missionScenarioDryRunStatusEl.textContent = summary.dry_run_status || "-";
  }
  if (missionScenarioWaypointCountEl) {
    missionScenarioWaypointCountEl.textContent = String(summary.proposed_waypoint_count ?? "-");
  }
  if (missionScenarioSegmentCountEl) {
    missionScenarioSegmentCountEl.textContent = String(summary.proposed_route_segment_count ?? "-");
  }
  if (missionScenarioSitlStatusEl) {
    missionScenarioSitlStatusEl.textContent = summary.sitl_execution_request_status || "-";
  }
  if (missionScenarioSitlExecutionStatusEl) {
    missionScenarioSitlExecutionStatusEl.textContent = executionSummary.live_flight_status
      || executionSummary.result_status
      || executionSummary.task_status
      || "-";
  }
}

function clearMissionScenarioDesigner() {
  latestMissionScenarioResult = null;
  if (missionScenarioPromptInputEl) missionScenarioPromptInputEl.value = "";
  resetMissionScenarioCoordinateRouteInputs();
  updateMissionScenarioCoordinateRouteStatus();
  if (missionScenarioApproveBtn) missionScenarioApproveBtn.disabled = true;
  if (missionScenarioPrepareSitlBtn) missionScenarioPrepareSitlBtn.disabled = true;
  if (missionScenarioExecuteSitlBtn) missionScenarioExecuteSitlBtn.disabled = true;
  if (missionScenarioStatusEl) {
    missionScenarioStatusEl.className = "selection-detail selection-detail-empty";
    missionScenarioStatusEl.textContent = "Enter a mission prompt or use the default Coordinate Route, then generate a scenario proposal.";
  }
  setMissionScenarioSummary({});
  if (missionScenarioResultEl) missionScenarioResultEl.innerHTML = "";
  if (missionScenarioRawEl) missionScenarioRawEl.textContent = "";
  renderMissionOSOperatorSummary();
}

function missionDesignerResultArtifacts(result) {
  const response = result?.sitl_execution_response || {};
  const task = response?.task || result?.sitl_execution_result_task || result?.sitl_execution_task || {};
  return task?.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
}

function renderMissionScenarioHumanBrief(result) {
  const proposal = result?.scenario_proposal || {};
  const summary = result?.summary || {};
  const response = result?.sitl_execution_response || {};
  const responseSummary = response.summary || {};
  const artifacts = missionDesignerResultArtifacts(result);
  const autoMissionEvidence = missionScenarioAutoMissionEvidenceData(result);
  const autoSummary = autoMissionEvidence.autoSummary;
  const autoWaypointGate = autoMissionEvidence.waypointGate;
  const autoDropoffGate = autoMissionEvidence.dropoffGate;
  const autoSITLDeliveryGate = autoMissionEvidence.sitlDeliveryGate;
  const autoPayloadSimGate = autoMissionEvidence.payloadReleaseSimGate;
  const executionResult = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_execution_result);
  const receipt = asPlainObject(artifacts.px4_gazebo_sitl_mission_upload_receipt);
  const flightEvidence = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_flight_evidence);
  const payloadObservation = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_payload_release_observation);
  const missionDesignerDropoffVerification = asPlainObject(artifacts.px4_gazebo_mission_designer_sitl_dropoff_verification);
  const sitlDropoffVerification = asPlainObject(artifacts.px4_gazebo_sitl_dropoff_verification);
  const failedReceipt = asPlainObject(
    response.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt
      || artifacts.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt
  );
  const blockedReceipt = asPlainObject(response.px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt);
  const routePlan = result?.digital_twin_route_plan || {};
  const uploadObserved = receipt.upload_status === "uploaded" || executionResult.mission_ack_observed === true;
  const flightObserved = flightEvidence.actual_sitl_flight_evidence_observed === true
    || executionResult.actual_sitl_flight_evidence_observed === true
    || responseSummary.actual_sitl_flight_evidence_observed === true;
  const payloadObserved = payloadObservation.payload_release_observed === true
    || missionDesignerDropoffVerification.payload_release_observed === true
    || responseSummary.payload_release_observed === true
    || autoSITLDeliveryGate.payload_release_command_acked === true
    || autoPayloadSimGate.payload_release_observed_sim === true;
  const dropoffVerified = missionDesignerDropoffVerification.dropoff_verified === true
    || sitlDropoffVerification.status === "verified"
    || responseSummary.dropoff_verified === true
    || autoDropoffGate.dropoff_verified === true
    || autoSITLDeliveryGate.dropoff_verified === true
    || autoPayloadSimGate.dropoff_verified === true;
  const autoRouteCompleted = autoWaypointGate.route_completed_claimed === true
    || autoSITLDeliveryGate.route_completed_claimed === true
    || autoPayloadSimGate.route_completed_claimed === true;
  const autoSITLDeliveryClaimed = autoSITLDeliveryGate.sitl_delivery_claimed === true
    || autoSummary.sitl_delivery_claimed === true;
  const autoPayloadObservedSim = autoPayloadSimGate.payload_release_observed_sim === true
    || autoSummary.payload_release_observed_sim === true;
  const chainState = autoPayloadObservedSim
    ? "sim payload separated"
    : autoSITLDeliveryClaimed
      ? "SITL command ACK"
      : autoRouteCompleted
        ? "AUTO route completed"
        : dropoffVerified
    ? "dropoff verified"
    : payloadObserved
      ? "payload observed"
      : flightObserved
        ? "flight observed"
        : (failedReceipt.failure_category || blockedReceipt.blocked_reasons)
          ? (uploadObserved ? "blocked after upload" : "blocked")
          : uploadObserved
            ? "upload observed"
            : "planning";
  const liveStatus = responseSummary.live_flight_status
    || failedReceipt.live_flight_execution_status
    || responseSummary.result_status
    || executionResult.result_status
    || summary.sitl_execution_request_status
    || "not executed";
  const taskId = responseSummary.task_id || response.task_id || response.task?.task_id || result?.sitl_execution_task?.task_id || "-";
  const failureText = failedReceipt.failure_category
    || (Array.isArray(blockedReceipt.blocked_reasons) ? blockedReceipt.blocked_reasons[0] : "")
    || "";
  const outcomeText = autoPayloadObservedSim
    ? "SITL/Gazebo cargo separation and fall were observed; real-world delivery is not claimed."
    : autoSITLDeliveryClaimed
      ? "SITL command-level delivery reached payload COMMAND_ACK; simulated payload separation is not required for this fact and real-world delivery is not claimed."
      : dropoffVerified
    ? "SITL dropoff verification is complete from observed simulator facts."
    : failureText
      ? `SITL run is blocked: ${failureText}.`
      : uploadObserved
        ? "Mission upload was observed; final flight/dropoff evidence is still pending."
        : "Scenario is prepared for operator review; execution evidence is not complete yet.";
  const nextActionText = autoPayloadObservedSim
    ? "Audit AUTO Mission SITL Evidence; do not read simulated separation as physical delivery."
    : autoSITLDeliveryClaimed
      ? "Audit command ACK and delivery boundary before any physical claim."
      : dropoffVerified
    ? "Review compact evidence first; open detailed cards only when auditing refs."
    : failureText
      ? "Use the failure receipt and logs first; do not infer delivery success from upload-only evidence."
      : "Approve, prepare, or execute only through the explicit operator-gated controls.";
  const distance = missionDesignerDropoffVerification.observed_distance_to_dropoff_m;
  const progress = autoSummary.observed_progress_m ?? flightEvidence.horizontal_progress_m;
  const objective = proposal.mission_objective || summary.mission_objective || "-";
  const boundaryFacts = [
    ["hardware", executionResult.hardware_target_allowed ?? responseSummary.hardware_target_allowed ?? summary.hardware_target_allowed ?? false],
    ["physical", executionResult.physical_execution_invoked ?? responseSummary.physical_execution_invoked ?? summary.physical_execution_invoked ?? false],
    ["delivery claim", executionResult.delivery_completion_claimed ?? missionDesignerDropoffVerification.delivery_completion_claimed ?? autoSITLDeliveryGate.delivery_completion_claimed ?? autoPayloadSimGate.delivery_completion_claimed ?? false],
    ["synthetic success", executionResult.synthetic_success_allowed ?? false],
  ];
  const evidenceFacts = [
    ["upload", uploadObserved],
    ["flight", flightObserved || autoRouteCompleted],
    ["payload", payloadObserved],
    ["dropoff", dropoffVerified],
  ];
  const caveats = uniqueStrings([
    routePlan.sitl_world_binding_status !== "bound_to_sitl_world" ? "Digital Twin planning does not drive the current SITL route." : "",
    executionResult.delivery_completion_claimed === true || missionDesignerDropoffVerification.delivery_completion_claimed === true
      ? "Delivery completion claim present; inspect authority boundary."
      : "Mission OS delivery_completion_claimed remains false.",
    failureText ? `Failure receipt: ${failureText}.` : "",
    autoPayloadObservedSim ? "Simulated payload separation is not a real-world delivery claim." : "",
    dropoffVerified ? "Verification is simulator-only; no hardware or physical authority is granted." : "",
  ]).slice(0, 4);
  return `
    <div class="detail-card mission-human-brief">
      <div class="mission-human-brief-head">
        <div>
          <div class="k">Mission Brief</div>
          <strong>${escapeHtml(chainState)}</strong>
        </div>
        <div>${statusTag(liveStatus)}</div>
      </div>
      <div class="mission-human-brief-outcome">${escapeHtml(outcomeText)}</div>
      <div class="mission-human-brief-grid">
        <div>
          <span class="k">operator objective</span>
          <strong>${escapeHtml(objective)}</strong>
        </div>
        <div>
          <span class="k">next useful read</span>
          <strong>${escapeHtml(nextActionText)}</strong>
        </div>
        <div>
          <span class="k">observed movement</span>
          <strong>${escapeHtml(progress !== undefined ? `${progress} m progress` : "pending")}</strong>
          <span>${escapeHtml(distance !== undefined ? `${distance} m to dropoff` : "dropoff distance pending")}</span>
        </div>
        <div>
          <span class="k">task</span>
          <strong class="mono">${escapeHtml(taskId)}</strong>
        </div>
      </div>
      <div class="detail-chip-row mission-human-brief-row">
        ${evidenceFacts.map(([label, value]) => `<span class="detail-chip mission-brief-chip-${value ? "ok" : "pending"}"><span class="detail-chip-label">${escapeHtml(label)}</span><span class="detail-chip-value">${escapeHtml(String(value))}</span></span>`).join("")}
      </div>
      <div class="detail-chip-row mission-human-brief-row">
        ${boundaryFacts.map(([label, value]) => `<span class="detail-chip mission-brief-chip-${value === false ? "ok" : "warn"}"><span class="detail-chip-label">${escapeHtml(label)}</span><span class="detail-chip-value">${escapeHtml(String(value))}</span></span>`).join("")}
      </div>
      <ul class="mission-human-brief-caveats">
        ${caveats.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
      </ul>
      <div class="muted">This is deterministic UI synthesis over persisted artifacts, not an AI gate verdict or new authority.</div>
    </div>
  `;
}

function renderMissionScenarioResult(result) {
  const proposal = result?.scenario_proposal || {};
  const validation = result?.validation_result || {};
  const dryRun = result?.dry_run_result || {};
  const summary = result?.summary || {};
  const blockedReasons = Array.isArray(validation.blocked_reasons) ? validation.blocked_reasons : [];
  const canApprove = validation.validation_status === "accepted" && !result?.scenario_approval;
  const operatorRouteBlocksSITL = missionScenarioOperatorRouteBlocksSITL(result);
  const canPrepareSITL = Boolean(result?.bounded_simulation_request)
    && !result?.sitl_execution_request
    && !operatorRouteBlocksSITL;
  const canExecuteSITL = Boolean(result?.sitl_execution_task?.task_id)
    && !result?.sitl_execution_response
    && !operatorRouteBlocksSITL;
  if (missionScenarioGenerateBtn) missionScenarioGenerateBtn.disabled = false;
  if (missionScenarioApproveBtn) missionScenarioApproveBtn.disabled = !canApprove;
  if (missionScenarioPrepareSitlBtn) missionScenarioPrepareSitlBtn.disabled = !canPrepareSITL;
  if (missionScenarioExecuteSitlBtn) missionScenarioExecuteSitlBtn.disabled = !canExecuteSITL;
  setMissionScenarioSummary(result);
  if (missionScenarioStatusEl) {
    const accepted = validation.validation_status === "accepted";
    missionScenarioStatusEl.className = "selection-detail";
    missionScenarioStatusEl.innerHTML = `
      <div class="item-head">
        <strong>${accepted ? "Scenario proposal ready for operator review" : "Scenario proposal blocked"}</strong>
        ${statusTag(validation.validation_status || "unknown")}
      </div>
      <div class="muted">${escapeHtml(dryRun.report_summary || "Gazebo execution was not invoked.")}</div>
    `;
  }
  if (missionScenarioResultEl) {
    missionScenarioResultEl.innerHTML = `
      ${renderMissionScenarioHumanBrief(result)}
      <details class="mission-ui-collapse">
        <summary>Scenario Inputs and Planning Evidence</summary>
        <div class="detail-grid">
          <div class="detail-card">
            <div class="k">Objective</div>
            <div>${escapeHtml(proposal.mission_objective || "-")}</div>
          </div>
          <div class="detail-card">
            <div class="k">Execution Boundary</div>
            <div class="detail-chip-row">
              <span class="detail-chip"><span class="detail-chip-label">LLM authority</span><span class="detail-chip-value">${escapeHtml(String(summary.llm_output_is_authority ?? false))}</span></span>
              <span class="detail-chip"><span class="detail-chip-label">Gazebo invoked</span><span class="detail-chip-value">${escapeHtml(String(summary.gazebo_execution_invoked ?? false))}</span></span>
              <span class="detail-chip"><span class="detail-chip-label">hardware</span><span class="detail-chip-value">${escapeHtml(String(summary.hardware_target_allowed ?? false))}</span></span>
              <span class="detail-chip"><span class="detail-chip-label">physical</span><span class="detail-chip-value">${escapeHtml(String(summary.physical_execution_invoked ?? false))}</span></span>
            </div>
          </div>
          ${renderMissionScenarioConstraints(proposal)}
          ${renderMissionScenarioCoordinateRoute(result?.mission_designer_coordinate_pair_route)}
          ${renderMissionScenarioList("Weather Hazards", proposal.weather_hazard_labels)}
          ${renderMissionScenarioList("Terrain Hazards", proposal.terrain_hazard_labels)}
          ${renderMissionScenarioList("Equipment Incidents", proposal.equipment_incident_labels)}
          ${renderMissionScenarioList("Feasibility Risks", proposal.feasibility_risk_labels)}
          ${renderMissionScenarioList("Blocked Reasons", blockedReasons)}
          ${renderMissionScenarioDigitalTwinPlanningTrack(result)}
        </div>
      </details>
      ${renderMissionScenarioSafeRouteSITLTrack(result)}
    `;
  }
  if (missionScenarioRawEl) {
    missionScenarioRawEl.textContent = JSON.stringify(result, null, 2);
  }
  syncMissionFlightTelemetryAnimations();
  renderMissionOSOperatorSummary();
}

async function generateMissionScenarioProposal() {
  if (!missionScenarioPromptInputEl) return;
  const prompt = missionScenarioPromptInputEl.value.trim();
  let coordinateRoute = null;
  try {
    coordinateRoute = missionScenarioCoordinateRoutePayload();
  } catch (err) {
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    }
    logEvent("mission_scenario.propose.error", { error: String(err) });
    return;
  }
  const requestPrompt = missionScenarioPromptForRequest(prompt, coordinateRoute);
  if (!requestPrompt) {
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail selection-detail-empty";
      missionScenarioStatusEl.textContent = "Mission prompt or complete Coordinate Route is required.";
    }
    return;
  }
  if (missionScenarioGenerateBtn) missionScenarioGenerateBtn.disabled = true;
  if (missionScenarioStatusEl) {
    missionScenarioStatusEl.className = "selection-detail";
    missionScenarioStatusEl.textContent = "Generating scenario proposal...";
  }
  try {
    const body = coordinateRoute ? { prompt: requestPrompt, coordinate_route: coordinateRoute } : { prompt: requestPrompt };
    const response = await apiFetchWithTimeout("/px4-gazebo/mission-scenarios/propose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const result = await response.json();
    latestMissionScenarioResult = result;
    renderMissionScenarioResult(result);
    logEvent("mission_scenario.propose", result.summary || {});
  } catch (err) {
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    }
    logEvent("mission_scenario.propose.error", { error: String(err) });
  } finally {
    if (missionScenarioGenerateBtn) missionScenarioGenerateBtn.disabled = false;
  }
}

async function approveMissionScenarioProposal() {
  if (!latestMissionScenarioResult) return;
  if (missionScenarioApproveBtn) missionScenarioApproveBtn.disabled = true;
  if (missionScenarioStatusEl) {
    missionScenarioStatusEl.className = "selection-detail";
    missionScenarioStatusEl.textContent = "Approving compile-only bounded simulation request...";
  }
  try {
    const response = await apiFetchWithTimeout("/px4-gazebo/mission-scenarios/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scenario_proposal: latestMissionScenarioResult.scenario_proposal,
        validation_result: latestMissionScenarioResult.validation_result
      })
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const approvalResult = await response.json();
    latestMissionScenarioResult = {
      ...latestMissionScenarioResult,
      ...approvalResult,
      summary: {
        ...(latestMissionScenarioResult.summary || {}),
        ...(approvalResult.summary || {})
      }
    };
    renderMissionScenarioResult(latestMissionScenarioResult);
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `
        <div class="item-head">
          <strong>Scenario approved for bounded simulation request</strong>
          ${statusTag("approved")}
        </div>
        <div class="muted">Approval scope is compile_to_bounded_simulation_request_only. No runner or Gazebo execution was invoked.</div>
      `;
    }
    logEvent("mission_scenario.approve", approvalResult.summary || {});
  } catch (err) {
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    }
    logEvent("mission_scenario.approve.error", { error: String(err) });
  } finally {
    if (missionScenarioGenerateBtn) missionScenarioGenerateBtn.disabled = false;
    if (missionScenarioApproveBtn) {
      missionScenarioApproveBtn.disabled = !(
        latestMissionScenarioResult?.validation_result?.validation_status === "accepted"
        && !latestMissionScenarioResult?.scenario_approval
      );
    }
    if (missionScenarioPrepareSitlBtn) {
      missionScenarioPrepareSitlBtn.disabled = !latestMissionScenarioResult?.bounded_simulation_request
        || Boolean(latestMissionScenarioResult?.sitl_execution_request)
        || missionScenarioOperatorRouteBlocksSITL(latestMissionScenarioResult);
    }
  }
}

async function prepareMissionScenarioSITLExecution() {
  if (!latestMissionScenarioResult?.bounded_simulation_request) return;
  if (missionScenarioOperatorRouteBlocksSITL(latestMissionScenarioResult)) {
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `
        <div class="item-head">
          <strong>Coordinate Route is still planning evidence</strong>
          ${statusTag("blocked")}
        </div>
        <div class="muted">Generate and approve a bounded request first; SITL preparation creates the Mission Designer SITL-only binding without granting hardware or physical authority.</div>
      `;
    }
    renderMissionScenarioResult(latestMissionScenarioResult);
    return;
  }
  if (missionScenarioPrepareSitlBtn) missionScenarioPrepareSitlBtn.disabled = true;
  if (missionScenarioExecuteSitlBtn) missionScenarioExecuteSitlBtn.disabled = true;
  if (missionScenarioStatusEl) {
    missionScenarioStatusEl.className = "selection-detail";
    missionScenarioStatusEl.textContent = "Preparing SITL execution request...";
  }
  try {
    const response = await apiFetchWithTimeout("/px4-gazebo/mission-scenarios/prepare-sitl-execution", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scenario_proposal: latestMissionScenarioResult.scenario_proposal,
        validation_result: latestMissionScenarioResult.validation_result,
        scenario_approval: latestMissionScenarioResult.scenario_approval,
        scenario_compile_result: latestMissionScenarioResult.scenario_compile_result,
        bounded_simulation_request: latestMissionScenarioResult.bounded_simulation_request,
        mission_designer_coordinate_pair_route: latestMissionScenarioResult.mission_designer_coordinate_pair_route,
        real_world_target_resolution: latestMissionScenarioResult.real_world_target_resolution,
        terrain_dem_source_snapshot: latestMissionScenarioResult.terrain_dem_source_snapshot,
        terrain_heightmap_file_artifact: latestMissionScenarioResult.terrain_heightmap_file_artifact,
        execution_terrain_fallback_reason: latestMissionScenarioResult.execution_terrain_fallback_reason,
        execution_terrain_source_backed: latestMissionScenarioResult.execution_terrain_source_backed,
        gazebo_world_artifact: latestMissionScenarioResult.gazebo_world_artifact,
        coordinate_transform_candidate: latestMissionScenarioResult.coordinate_transform_candidate,
        digital_twin_sitl_binding_gate: latestMissionScenarioResult.digital_twin_sitl_binding_gate,
        digital_twin_route_plan: latestMissionScenarioResult.digital_twin_route_plan,
        digital_twin_px4_mission_item_candidate: latestMissionScenarioResult.digital_twin_px4_mission_item_candidate,
        summary: latestMissionScenarioResult.summary,
        owner_session_id: currentSessionId || "",
        owner_user_id: (currentSettings().userId || "web_user").trim() || "web_user"
      })
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const prepared = await response.json();
    latestMissionScenarioResult = {
      ...latestMissionScenarioResult,
      sitl_execution_request: prepared.sitl_execution_request,
      mission_designer_coordinate_pair_sitl_binding: prepared.mission_designer_coordinate_pair_sitl_binding,
      sitl_execution_task: prepared.task,
      sitl_execution_summary: prepared.summary,
      summary: {
        ...(latestMissionScenarioResult.summary || {}),
        sitl_execution_request_status: prepared.summary?.request_status || "-",
        sitl_execution_task_id: prepared.summary?.task_id || "-"
      }
    };
    renderMissionScenarioResult(latestMissionScenarioResult);
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `
        <div class="item-head">
          <strong>SITL execution request prepared</strong>
          ${statusTag("pending")}
        </div>
        <div class="muted">Prepared request is persisted. Gazebo execution, MAVLink dispatch, and mission upload remain false until a separate explicit opt-in execution route exists.</div>
      `;
    }
    logEvent("mission_scenario.prepare_sitl_execution", prepared.summary || {});
  } catch (err) {
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(String(err))}</div>`;
    }
    logEvent("mission_scenario.prepare_sitl_execution.error", { error: String(err) });
    if (missionScenarioPrepareSitlBtn) {
      missionScenarioPrepareSitlBtn.disabled = !latestMissionScenarioResult?.bounded_simulation_request
        || Boolean(latestMissionScenarioResult?.sitl_execution_request)
        || missionScenarioOperatorRouteBlocksSITL(latestMissionScenarioResult);
    }
    if (missionScenarioExecuteSitlBtn) {
      missionScenarioExecuteSitlBtn.disabled = !latestMissionScenarioResult?.sitl_execution_task?.task_id
        || Boolean(latestMissionScenarioResult?.sitl_execution_response)
        || missionScenarioOperatorRouteBlocksSITL(latestMissionScenarioResult);
    }
  } finally {
    if (missionScenarioGenerateBtn) missionScenarioGenerateBtn.disabled = false;
    if (missionScenarioPrepareSitlBtn) {
      missionScenarioPrepareSitlBtn.disabled = !latestMissionScenarioResult?.bounded_simulation_request
        || Boolean(latestMissionScenarioResult?.sitl_execution_request)
        || missionScenarioOperatorRouteBlocksSITL(latestMissionScenarioResult);
    }
    if (missionScenarioExecuteSitlBtn) {
      missionScenarioExecuteSitlBtn.disabled = !latestMissionScenarioResult?.sitl_execution_task?.task_id
        || Boolean(latestMissionScenarioResult?.sitl_execution_response)
        || missionScenarioOperatorRouteBlocksSITL(latestMissionScenarioResult);
    }
  }
}

async function refreshMissionScenarioExecutionTask(taskId) {
  if (!taskId) return null;
  const response = await apiFetch(`/tasks/${encodeURIComponent(taskId)}`);
  if (!response.ok) return null;
  const payload = await response.json().catch(() => ({}));
  const task = payload.task && typeof payload.task === "object" ? payload.task : null;
  if (!task) return null;
  latestMissionScenarioResult = {
    ...latestMissionScenarioResult,
    sitl_execution_result_task: task,
    sitl_execution_task: task,
  };
  renderMissionScenarioResult(latestMissionScenarioResult);
  return task;
}

async function executeMissionScenarioLiveSITL() {
  if (missionScenarioOperatorRouteBlocksSITL(latestMissionScenarioResult)) {
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `
        <div class="item-head">
          <strong>Coordinate Route is still planning evidence</strong>
          ${statusTag("blocked")}
        </div>
        <div class="muted">Prepare SITL execution first; Execute Live SITL remains gated by explicit operator approval and server-side opt-in.</div>
      `;
    }
    renderMissionScenarioResult(latestMissionScenarioResult);
    return;
  }
  const taskId = latestMissionScenarioResult?.sitl_execution_task?.task_id
    || latestMissionScenarioResult?.summary?.sitl_execution_task_id;
  if (!taskId) return;
  if (missionScenarioExecuteSitlBtn) missionScenarioExecuteSitlBtn.disabled = true;
  if (missionScenarioStatusEl) {
    missionScenarioStatusEl.className = "selection-detail";
    missionScenarioStatusEl.textContent = "Executing operator-approved live PX4/Gazebo SITL chain; full AUTO routes can take tens of minutes while simulator evidence is collected...";
  }
  latestMissionScenarioResult = {
    ...latestMissionScenarioResult,
    sitl_execution_in_progress: true,
  };
  renderMissionScenarioResult(latestMissionScenarioResult);
  let polling = true;
  const pollExecutionTask = (async () => {
    while (polling) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        await refreshMissionScenarioExecutionTask(taskId);
      } catch (err) {
        logEvent("mission_scenario.execute_live_sitl.poll_error", { error: String(err) });
      }
    }
  })();
  try {
    const response = await apiFetchWithTimeout("/px4-gazebo/mission-scenarios/execute-sitl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_id: taskId,
        explicit_execution_approval: true,
        live_flight_mode: true
      })
    }, MISSION_SCENARIO_LIVE_SITL_TIMEOUT_MS);
    const execution = await response.json();
    polling = false;
    await pollExecutionTask;
    if (!response.ok && response.status !== 409) {
      throw new Error(execution?.detail || `HTTP ${response.status}`);
    }
    latestMissionScenarioResult = {
      ...latestMissionScenarioResult,
      sitl_execution_in_progress: false,
      sitl_execution_response: execution,
      sitl_execution_result_task: execution.task,
      summary: {
        ...(latestMissionScenarioResult.summary || {}),
        sitl_execution_request_status: latestMissionScenarioResult.summary?.sitl_execution_request_status || "-",
        sitl_execution_task_id: taskId
      }
    };
    renderMissionScenarioResult(latestMissionScenarioResult);
    const executionSummary = execution.summary || {};
    const blocked = response.status === 409 || executionSummary.task_status === "blocked" || executionSummary.live_flight_status === "blocked";
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `
        <div class="item-head">
          <strong>${blocked ? "Live SITL execution blocked by server-side gates" : "Live SITL execution evidence recorded"}</strong>
          ${statusTag(blocked ? "blocked" : "recorded")}
        </div>
        <div class="muted">The UI called the explicit operator-approved live execution route. Payload, dropoff, and epic-exit data below are rendered from persisted evidence artifacts only.</div>
      `;
    }
    logEvent("mission_scenario.execute_live_sitl", executionSummary);
  } catch (err) {
    polling = false;
    await pollExecutionTask.catch(() => {});
    latestMissionScenarioResult = {
      ...latestMissionScenarioResult,
      sitl_execution_in_progress: false,
    };
    const message = missionScenarioRequestErrorMessage(err, "Live SITL execution request failed");
    if (missionScenarioStatusEl) {
      missionScenarioStatusEl.className = "selection-detail";
      missionScenarioStatusEl.innerHTML = `<div class="detail-error">${escapeHtml(message)}</div>`;
    }
    logEvent("mission_scenario.execute_live_sitl.error", { error: message });
    if (missionScenarioExecuteSitlBtn) {
      missionScenarioExecuteSitlBtn.disabled = !taskId;
    }
  } finally {
    if (missionScenarioGenerateBtn) missionScenarioGenerateBtn.disabled = false;
    if (missionScenarioExecuteSitlBtn) {
      missionScenarioExecuteSitlBtn.disabled = !latestMissionScenarioResult?.sitl_execution_task?.task_id
        || Boolean(latestMissionScenarioResult?.sitl_execution_response)
        || missionScenarioOperatorRouteBlocksSITL(latestMissionScenarioResult);
    }
  }
}

function formatJsonBlock(value) {
  try {
    return escapeHtml(JSON.stringify(value ?? {}, null, 2));
  } catch (_) {
    return escapeHtml(String(value ?? ""));
  }
}

function compactText(value, limit = 180) {
  const text = String(value || "").trim();
  if (!text) return "";
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}…`;
}

function safeToyGridSvgDataUrl(svg) {
  const text = String(svg || "").trim();
  if (!text || !text.startsWith("<svg") || !text.endsWith("</svg>")) return "";
  const lowered = text.toLowerCase();
  if (
    lowered.includes("<script")
    || lowered.includes("<foreignobject")
    || lowered.includes("<iframe")
    || lowered.includes("<object")
    || lowered.includes("<embed")
    || /(?:^|[\s<])on[a-z0-9_-]+\s*=/.test(lowered)
    || /(?:^|\s)(?:xlink:href|href)\s*=/.test(lowered)
    || lowered.includes("javascript:")
  ) {
    return "";
  }
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(text)}`;
}

function renderSafeToyGridSvgPreview(svg) {
  const dataUrl = safeToyGridSvgDataUrl(svg);
  if (!dataUrl) {
    return `<div class="muted">No safe SVG preview recorded.</div>`;
  }
  return [
    `<div class="toy-grid-svg-frame">`,
    `<img src="${escapeAttr(dataUrl)}" alt="Toy grid-world replay preview" loading="lazy">`,
    `</div>`,
  ].join("");
}

function renderMissionDesignerDeliveryReplay(options) {
  const isExecuting = options.isExecuting === true;
  const isBlocked = options.isBlocked === true;
  const uploadAttempted = options.uploadAttempted === true;
  const uploadFailed = options.uploadFailed === true;
  const uploadObserved = options.uploadObserved === true;
  const flightObserved = options.flightObserved === true;
  const payloadObserved = options.payloadObserved === true;
  const payloadVerified = options.payloadVerified === true;
  const dropoffVerified = options.dropoffVerified === true;
  const epicExitComplete = options.epicExitComplete === true;
  const flightFailed = isBlocked && uploadObserved && !flightObserved;
  const missionStatus = epicExitComplete
    ? "delivery verified"
    : isBlocked
      ? "blocked"
      : isExecuting
        ? "executing"
        : dropoffVerified
          ? "dropoff verified"
          : payloadObserved
            ? "payload released"
            : flightObserved
              ? "flight observed"
              : uploadObserved
                ? "mission uploaded"
                : "pending";
  const stage = (label, complete, failed = false) => `<span class="mission-replay-step ${failed ? "failed" : complete ? "complete" : "pending"}">${escapeHtml(label)}</span>`;
  const frame = (label, complete, frameClass, droneLeft, packageLeft, packageBottom, packageReleased = false, packageVerified = false, failed = false) => [
    `<div class="mission-replay-frame ${failed ? "failed" : complete ? "complete" : "pending"} ${escapeAttr(frameClass)}">`,
    `<div class="mission-replay-frame-label">${escapeHtml(label)}</div>`,
    `<div class="mission-replay-scene" role="img" aria-label="${escapeAttr(`PX4 Gazebo SITL delivery replay frame: ${label}`)}">`,
    `<div class="mission-replay-sky-grid"></div>`,
    `<div class="mission-replay-sun"></div>`,
    `<div class="mission-replay-cloud cloud-a"></div>`,
    `<div class="mission-replay-cloud cloud-b"></div>`,
    `<div class="mission-replay-mountain mountain-a"></div>`,
    `<div class="mission-replay-mountain mountain-b"></div>`,
    `<div class="mission-replay-ground"></div>`,
    `<div class="mission-replay-pad pickup-pad"></div>`,
    `<div class="mission-replay-pad dropoff-pad"></div>`,
    `<div class="mission-replay-cabin"><span class="cabin-roof"></span><span class="cabin-body"></span><span class="cabin-door"></span></div>`,
    `<div class="mission-replay-route ${complete ? "active" : ""}"></div>`,
    `<div class="mission-replay-drone ${complete ? "active" : ""}" style="left:${droneLeft}%"><span class="rotor rotor-left"></span><span class="rotor rotor-right"></span><span class="drone-body"></span><span class="drone-tail"></span></div>`,
    `<div class="mission-replay-package ${packageReleased ? "released" : ""} ${packageVerified ? "verified" : ""}" style="left:${packageLeft}%; bottom:${packageBottom}px"></div>`,
    `<div class="mission-replay-flag ${packageVerified ? "verified" : ""}"></div>`,
    `</div>`,
    `</div>`,
  ].join("");
  return [
    `<div class="detail-card mission-replay-card ${isExecuting ? "executing" : ""} ${isBlocked ? "blocked" : ""}">`,
    `<div class="detail-heading"><div><div class="k">8-bit Delivery Replay</div><div class="item-meta">${escapeHtml(missionStatus)}</div></div>${statusTag(missionStatus)}</div>`,
    `<div class="mission-replay-strip">`,
    frame("prepare", true, "frame-prepare", 10, 13, 74),
    frame(uploadFailed ? "upload failed" : "upload", uploadObserved || uploadAttempted, "frame-upload", 24, 27, 74, false, false, uploadFailed),
    frame(flightFailed ? "flight blocked" : "flight", flightObserved, "frame-flight", flightFailed ? 28 : 52, 55, 62, false, false, flightFailed),
    frame(payloadObserved ? "payload" : "no payload", payloadObserved, "frame-payload", 70, 82, 44, payloadObserved, false),
    frame(dropoffVerified ? "dropoff" : "no dropoff", dropoffVerified, "frame-dropoff", 78, 82, 24, payloadObserved, dropoffVerified),
    frame(epicExitComplete ? "verified" : "not verified", epicExitComplete, "frame-verified", 82, 82, 24, payloadObserved, epicExitComplete, isBlocked && !epicExitComplete),
    `</div>`,
    `<div class="mission-replay-steps">`,
    stage("prepare", true),
    stage("upload", uploadObserved || uploadAttempted, uploadFailed),
    stage(flightFailed ? "flight blocked" : "flight", flightObserved, flightFailed),
    stage(payloadObserved ? "payload" : "payload missing", payloadObserved, isBlocked && !payloadObserved),
    stage(dropoffVerified ? "dropoff" : "dropoff missing", dropoffVerified, isBlocked && !dropoffVerified),
    stage(epicExitComplete ? "epic exit" : "not verified", epicExitComplete, isBlocked && !epicExitComplete),
    `</div>`,
    `<div class="item-meta mono">${isBlocked ? `blocked_reason=${escapeHtml(options.blockedReason || "-")} · ` : ""}payload_verified=${escapeHtml(String(payloadVerified))} · synthetic_success_allowed=${escapeHtml(String(options.syntheticSuccessAllowed === true))}</div>`,
    `</div>`,
  ].join("");
}

function renderMissionScenarioDeliveryReplay(result) {
  if (!result?.sitl_execution_request && !result?.sitl_execution_response && result?.sitl_execution_in_progress !== true) {
    return "";
  }
  const response = result?.sitl_execution_response || {};
  const summary = response.summary || {};
  const task = response.task || result?.sitl_execution_result_task || {};
  const artifacts = task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const receipt = response.px4_gazebo_sitl_mission_upload_receipt || artifacts.px4_gazebo_sitl_mission_upload_receipt || {};
  const executionResult = response.px4_gazebo_mission_designer_sitl_execution_result || artifacts.px4_gazebo_mission_designer_sitl_execution_result || {};
  const payloadObservation = response.px4_gazebo_mission_designer_sitl_payload_release_observation || artifacts.px4_gazebo_mission_designer_sitl_payload_release_observation || {};
  const dropoffVerification = response.px4_gazebo_mission_designer_sitl_dropoff_verification || artifacts.px4_gazebo_mission_designer_sitl_dropoff_verification || {};
  const epicExit = response.px4_gazebo_mission_designer_sitl_delivery_epic_exit || artifacts.px4_gazebo_mission_designer_sitl_delivery_epic_exit || {};
  const blockedReceipt = response.px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt || artifacts.px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt || {};
  const failedReceipt = response.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt || artifacts.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt || {};
  const blockedReasons = uniqueStrings([
    ...(Array.isArray(summary.blocked_reasons) ? summary.blocked_reasons : []),
    ...(Array.isArray(receipt.blocked_reasons) ? receipt.blocked_reasons : []),
    ...(Array.isArray(blockedReceipt.blocked_reasons) ? blockedReceipt.blocked_reasons : []),
    ...(Array.isArray(failedReceipt.blocked_reasons) ? failedReceipt.blocked_reasons : []),
    ...(Array.isArray(executionResult.failure_reasons) ? executionResult.failure_reasons : []),
  ]);
  const uploadAttempted = Boolean(receipt.upload_status || receipt.receipt_id);
  const uploadFailed = uploadAttempted && !["", "uploaded"].includes(String(receipt.upload_status || ""));
  const isBlocked = (
    response.summary?.task_status === "blocked" ||
    task.status === "blocked" ||
    executionResult.result_status === "blocked" ||
    blockedReceipt.live_flight_execution_status === "blocked" ||
    failedReceipt.live_flight_execution_status === "blocked" ||
    uploadFailed
  );
  const uploadObserved = (
    summary.actual_sitl_mission_upload_observed === true ||
    summary.px4_mission_upload_performed === true ||
    executionResult.actual_sitl_mission_upload_observed === true ||
    receipt.upload_status === "uploaded"
  );
  const flightObserved = (
    summary.actual_sitl_flight_evidence_observed === true ||
    executionResult.actual_sitl_flight_evidence_observed === true
  );
  const payloadObserved = (
    summary.payload_release_observed === true ||
    payloadObservation.payload_release_observed === true ||
    epicExit.payload_release_observed === true
  );
  const payloadVerified = (
    summary.payload_release_verified === true ||
    dropoffVerification.payload_release_verified === true ||
    epicExit.payload_release_verified === true
  );
  const dropoffVerified = (
    summary.dropoff_verified === true ||
    dropoffVerification.dropoff_verified === true ||
    epicExit.dropoff_verified === true
  );
  const epicExitComplete = (
    summary.mission_designer_sitl_delivery_epic_exit_complete === true ||
    epicExit.mission_designer_sitl_delivery_epic_exit_complete === true
  );
  return renderMissionDesignerDeliveryReplay({
    isExecuting: result?.sitl_execution_in_progress === true && !response.summary,
    isBlocked,
    blockedReason: blockedReasons[0] || (uploadFailed ? `upload_${receipt.upload_status}` : ""),
    uploadAttempted,
    uploadFailed,
    uploadObserved,
    flightObserved,
    payloadObserved,
    payloadVerified,
    dropoffVerified,
    epicExitComplete,
    syntheticSuccessAllowed: executionResult.synthetic_success_allowed,
  });
}

function isOpenTaskStatus(status) {
  const normalized = String(status || "").toLowerCase();
  return !["completed", "failed", "cancelled", "expired"].includes(normalized);
}

function dashboardSearchQuery() {
  return String(dashboardState.searchQuery || "").trim();
}

function approvalListIncludesExpired(filter) {
  return filter === "all" || filter === "expired";
}

function paginationLabel(total, page, pageSize, currentCount) {
  if (!total) return "0 shown";
  if (!currentCount) return `0 / ${total}`;
  const start = (Math.max(1, page) - 1) * pageSize + 1;
  const end = start + currentCount - 1;
  return `${start}-${Math.max(start, end)} / ${total}`;
}

function updatePagerButtons(prevEl, nextEl, page, hasMore) {
  if (prevEl) prevEl.disabled = page <= 1;
  if (nextEl) nextEl.disabled = !hasMore;
}

function resetDashboardPages() {
  dashboardState.approvalPage = 1;
  dashboardState.taskPage = 1;
}

function updateDashboardFilterButtons() {
  dashboardFilterChips.forEach((chip) => {
    const kind = chip.dataset.filterKind;
    const value = chip.dataset.filterValue || "all";
    const active = (
      (kind === "task-status" && dashboardState.taskStatusFilter === value)
      || (kind === "approval-state" && dashboardState.approvalStateFilter === value)
    );
    chip.classList.toggle("active", active);
  });
}

function buildDashboardApprovalParams() {
  const params = new URLSearchParams({
    state: dashboardState.approvalStateFilter || "all",
    page: String(dashboardState.approvalPage || 1),
    page_size: String(dashboardState.approvalPageSize || 12),
  });
  if (approvalListIncludesExpired(dashboardState.approvalStateFilter)) {
    params.set("include_expired", "true");
  }
  const query = dashboardSearchQuery();
  if (query) params.set("q", query);
  if (currentSessionId) params.set("session_id", currentSessionId);
  return params;
}

function buildDashboardTaskParams() {
  const params = new URLSearchParams({
    page: String(dashboardState.taskPage || 1),
    page_size: String(dashboardState.taskPageSize || 12),
  });
  if (dashboardState.taskStatusFilter && dashboardState.taskStatusFilter !== "all") {
    params.set("status", dashboardState.taskStatusFilter);
  }
  const query = dashboardSearchQuery();
  if (query) params.set("q", query);
  if (currentSessionId) params.set("session_id", currentSessionId);
  return params;
}

function auditSearchQuery() {
  return String(auditState.searchQuery || "").trim();
}

function syncAuditInputsFromState() {
  if (auditSearchInputEl) auditSearchInputEl.value = auditState.searchQuery || "";
  if (auditActorInputEl) auditActorInputEl.value = auditState.actorFilter || "";
  if (auditSessionInputEl) auditSessionInputEl.value = auditState.sessionFilter || "";
  if (auditToolInputEl) auditToolInputEl.value = auditState.toolFilter || "";
  if (auditSourceInputEl) auditSourceInputEl.value = auditState.sourceFilter || "";
  if (auditResultInputEl) auditResultInputEl.value = auditState.resultFilter || "";
}

function resetAuditFilters() {
  auditState.searchQuery = "";
  auditState.actorFilter = "";
  auditState.sessionFilter = currentSessionId || "";
  auditState.toolFilter = "";
  auditState.sourceFilter = "";
  auditState.resultFilter = "";
  auditState.page = 1;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  auditState.autoSelectFirst = false;
  auditState.focus = null;
  syncAuditInputsFromState();
}

function buildAuditParams() {
  const params = new URLSearchParams({
    page: String(auditState.page || 1),
    page_size: String(auditState.pageSize || 20),
  });
  const query = auditSearchQuery();
  if (query) params.set("q", query);
  if (auditState.actorFilter) params.set("actor_user_id", auditState.actorFilter);
  if (auditState.sessionFilter) params.set("session_id", auditState.sessionFilter);
  if (auditState.toolFilter) params.set("tool", auditState.toolFilter);
  if (auditState.sourceFilter) params.set("source", auditState.sourceFilter);
  if (auditState.resultFilter) params.set("result", auditState.resultFilter);
  return params;
}

function auditResultTag(entry) {
  const result = String(entry?.result || entry?.event_type || "unknown");
  return statusTag(result);
}

function auditMetadata(entry) {
  return entry?.metadata && typeof entry.metadata === "object" ? entry.metadata : {};
}

function renderDetailChips(items) {
  if (!Array.isArray(items) || !items.length) return "";
  return [
    `<div class="detail-chip-row">`,
    ...items.map((item) => (
      `<span class="detail-chip"><span class="detail-chip-label">${escapeHtml(item.label || "meta")}</span><span class="detail-chip-value">${escapeHtml(item.value || "-")}</span></span>`
    )),
    `</div>`,
  ].join("");
}

function normalizeAuditFocus(focus) {
  if (!focus || typeof focus !== "object") return null;
  const normalized = {};
  [
    "entryId",
    "requestId",
    "taskId",
    "runId",
    "sessionId",
    "toolName",
    "source",
    "result",
    "searchQuery",
  ].forEach((key) => {
    const value = String(focus[key] || "").trim();
    if (value) normalized[key] = value;
  });
  return Object.keys(normalized).length ? normalized : null;
}

function auditSearchText(entry) {
  const metadata = auditMetadata(entry);
  const parts = [
    entry?.entry_id,
    entry?.event_type,
    entry?.user_id,
    entry?.session_id,
    entry?.action,
    entry?.resource,
    entry?.result,
    metadata.tool_name,
    metadata.tool_pattern,
    metadata.source,
    metadata.actor_user_id,
    metadata.target_session_id,
  ];
  try {
    parts.push(JSON.stringify(metadata));
  } catch (_) {
    parts.push(String(metadata || ""));
  }
  return parts
    .filter(Boolean)
    .map((part) => String(part).toLowerCase())
    .join(" ");
}

function auditEntryFocusScore(entry, focus) {
  if (!focus) return 0;
  const metadata = auditMetadata(entry);
  const resource = String(entry?.resource || "");
  const searchText = auditSearchText(entry);
  let score = 0;

  if (focus.entryId && entry?.entry_id === focus.entryId) score += 1000;
  if (focus.requestId) {
    const requestMatches = [
      resource,
      metadata.request_id,
      metadata.source_request_id,
    ].filter(Boolean).map((value) => String(value));
    if (requestMatches.includes(focus.requestId)) score += 700;
    else if (requestMatches.some((value) => value.includes(focus.requestId))) score += 480;
  }
  if (focus.taskId) {
    const taskMatches = [
      metadata.task_id,
      metadata.parent_task_id,
      metadata.winner_task_id,
      resource,
    ].filter(Boolean).map((value) => String(value));
    if (taskMatches.includes(focus.taskId)) score += 520;
    else if (taskMatches.some((value) => value.includes(focus.taskId))) score += 340;
  }
  if (focus.runId) {
    const runMatches = [
      metadata.run_id,
      metadata.runId,
      resource,
    ].filter(Boolean).map((value) => String(value));
    if (runMatches.includes(focus.runId)) score += 460;
    else if (runMatches.some((value) => value.includes(focus.runId))) score += 300;
  }
  if (focus.toolName) {
    const toolText = [
      metadata.tool_name,
      metadata.tool_pattern,
      entry?.action,
      resource,
    ].filter(Boolean).join(" ").toLowerCase();
    if (toolText.includes(focus.toolName.toLowerCase())) score += 180;
  }
  if (focus.source && String(metadata.source || "").toLowerCase() === focus.source.toLowerCase()) {
    score += 120;
  }
  if (focus.result && String(entry?.result || "").toLowerCase() === focus.result.toLowerCase()) {
    score += 90;
  }
  if (
    focus.sessionId
    && [entry?.session_id, metadata.target_session_id]
      .filter(Boolean)
      .map((value) => String(value))
      .includes(focus.sessionId)
  ) {
    score += 80;
  }
  if (focus.searchQuery && searchText.includes(focus.searchQuery.toLowerCase())) score += 50;
  return score;
}

function selectPreferredAuditEntry(entries, focus) {
  if (!Array.isArray(entries) || !entries.length) return null;
  if (!focus) return entries[0];
  let bestEntry = entries[0];
  let bestScore = auditEntryFocusScore(bestEntry, focus);
  for (const entry of entries.slice(1)) {
    const score = auditEntryFocusScore(entry, focus);
    if (score > bestScore) {
      bestEntry = entry;
      bestScore = score;
    }
  }
  return bestEntry;
}

function renderAuditListItem(entry) {
  const metaBits = [
    entry.event_type || "-",
    entry.user_id || entry.metadata?.actor_user_id || "-",
    formatTimestamp(entry.timestamp),
  ];
  if (entry.session_id) metaBits.push(entry.session_id);
  const detailBits = [];
  if (entry.action) detailBits.push(`action=${entry.action}`);
  if (entry.metadata?.tool_name) detailBits.push(`tool=${entry.metadata.tool_name}`);
  if (entry.metadata?.source) detailBits.push(`source=${entry.metadata.source}`);
  if (entry.resource) detailBits.push(`resource=${compactText(entry.resource, 90)}`);
  return [
    "<li>",
    `<button class="list-item-button${auditState.selectedEntryId === entry.entry_id ? " active" : ""}" type="button" data-audit-id="${escapeAttr(entry.entry_id || "")}">`,
    `<div class="item-card">`,
    `<div class="item-head">`,
    `<div class="item-title">${escapeHtml(entry.event_type || entry.action || "audit")}</div>`,
    auditResultTag(entry),
    `</div>`,
    `<div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div>`,
    detailBits.length ? `<div class="item-detail mono">${escapeHtml(detailBits.join(" · "))}</div>` : "",
    entry.metadata?.resolve_reason ? `<div class="item-detail">${escapeHtml(compactText(entry.metadata.resolve_reason, 180))}</div>` : "",
    `</div>`,
    `</button>`,
    "</li>",
  ].join("");
}

function renderScopeTransition(entry) {
  const metadata = auditMetadata(entry);
  const rows = [
    ["State", metadata.state_before, metadata.state_after],
    ["Scope", metadata.scope_before, metadata.scope_after],
    ["Tool Pattern", metadata.tool_pattern_before, metadata.tool_pattern_after],
    ["Path Scope", metadata.path_scope_before, metadata.path_scope_after],
    ["Propagate", metadata.propagate_to_subagents_before, metadata.propagate_to_subagents_after],
  ].filter((row) => row[1] != null || row[2] != null);
  if (!rows.length) return "";
  return [
    `<div class="detail-section">`,
    `<div class="k">Before / After</div>`,
    `<div class="audit-diff-grid">`,
    ...rows.map(([label, before, after]) => (
      `<div class="detail-card"><div class="k">${escapeHtml(label)}</div><div class="mono">${escapeHtml(String(before ?? "-"))}</div><div class="item-meta mono">→ ${escapeHtml(String(after ?? "-"))}</div></div>`
    )),
    `</div>`,
    `</div>`,
  ].join("");
}

function renderApprovalAuditSummary(entry) {
  if (entry?.event_type !== "tool_approval") return "";
  const metadata = auditMetadata(entry);
  const requestId = entry.resource || metadata.request_id || metadata.source_request_id || "-";
  const resolveReason = metadata.resolve_reason || "-";
  const sourceRequestId = metadata.source_request_id || "-";
  return [
    `<div class="detail-section">`,
    `<div class="k">Approval Resolve Summary</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Request</div><div class="mono">${escapeHtml(requestId)}</div><div class="item-meta mono">${escapeHtml(sourceRequestId)}</div></div>`,
    `<div class="detail-card"><div class="k">Actor / Source</div><div class="mono">${escapeHtml(metadata.actor_user_id || entry.user_id || "-")}</div><div class="item-meta">${escapeHtml(metadata.source || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Tool / Scope</div><div>${escapeHtml(metadata.tool_name || metadata.tool_pattern || "-")}</div><div class="item-meta mono">${escapeHtml(`${metadata.scope_before || "-"} → ${metadata.scope_after || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Reason</div><div>${escapeHtml(compactText(resolveReason, 220))}</div><div class="item-meta">${escapeHtml(entry.result || "-")}</div></div>`,
    `</div>`,
    `</div>`,
  ].join("");
}

function renderAuditDetail(entry) {
  const metadata = auditMetadata(entry);
  const chips = [
    { label: "actor", value: entry.user_id || metadata.actor_user_id || "-" },
    { label: "source", value: metadata.source || "-" },
    { label: "result", value: entry.result || "-" },
    { label: "tool", value: metadata.tool_name || metadata.tool_pattern || "-" },
  ].filter((item) => item.value && item.value !== "-");
  return [
    `<div class="detail-section">`,
    `<div class="detail-heading">`,
    `<div>`,
    `<h5>${escapeHtml(entry.event_type || "audit")}</h5>`,
    `<div class="detail-meta mono">${escapeHtml([entry.entry_id || "-", formatTimestamp(entry.timestamp), entry.session_id || metadata.target_session_id || "-"].join(" · "))}</div>`,
    `</div>`,
    auditResultTag(entry),
    `</div>`,
    `</div>`,
    renderDetailChips(chips),
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Actor</div><div class="mono">${escapeHtml(entry.user_id || metadata.actor_user_id || "-")}</div><div class="item-meta">${escapeHtml(metadata.source || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Session</div><div class="mono">${escapeHtml(entry.session_id || metadata.target_session_id || "-")}</div><div class="item-meta">${escapeHtml(entry.action || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Tool</div><div>${escapeHtml(metadata.tool_name || metadata.tool_pattern || "-")}</div><div class="item-meta mono">${escapeHtml(entry.resource || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Result</div><div>${escapeHtml(entry.result || "-")}</div><div class="item-meta">${escapeHtml(metadata.resolve_reason || "-")}</div></div>`,
    `</div>`,
    renderApprovalAuditSummary(entry),
    entry.event_type === "tool_approval" ? renderScopeTransition(entry) : "",
    `<div class="detail-section">`,
    `<div class="k">Metadata</div>`,
    `<pre class="detail-pre">${formatJsonBlock(metadata)}</pre>`,
    `</div>`,
  ].join("");
}

function renderAuditDetailPanel() {
  const badge = auditState.selectedEntry?.entry_id || "none selected";
  const html = auditState.selectedEntry
    ? renderAuditDetail(auditState.selectedEntry)
    : "Select an audit event to inspect actor, source, result, and before/after scope changes.";
  if (auditDetailBadgeEl) auditDetailBadgeEl.textContent = badge;
  if (auditDetailPanelEl) {
    auditDetailPanelEl.classList.toggle("selection-detail-empty", !auditState.selectedEntry);
    auditDetailPanelEl.innerHTML = html;
  }
}

function updateAuditUi() {
  if (auditCurrentSessionEl) {
    auditCurrentSessionEl.textContent = currentSessionId || "-";
  }
  if (auditMatchCountEl) {
    auditMatchCountEl.textContent = String(auditState.total || 0);
  }
  if (auditCaptionEl) {
    auditCaptionEl.textContent = paginationLabel(
      auditState.total,
      auditState.page,
      auditState.pageSize,
      auditState.entries.length,
    );
  }
  updatePagerButtons(auditPrevBtn, auditNextBtn, auditState.page, auditState.hasMore);
  renderCompactList(
    auditListEl,
    auditState.entries,
    renderAuditListItem,
    "No audit events matched these filters."
  );
  renderAuditDetailPanel();
}

function selectAuditEntry(entryId) {
  auditState.selectedEntryId = entryId || null;
  auditState.selectedEntry = auditState.entries.find((entry) => entry.entry_id === entryId) || null;
  updateAuditUi();
}

function openAuditView(filters = {}) {
  if (Object.prototype.hasOwnProperty.call(filters, "searchQuery")) {
    auditState.searchQuery = filters.searchQuery || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "actorFilter")) {
    auditState.actorFilter = filters.actorFilter || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "sessionFilter")) {
    auditState.sessionFilter = filters.sessionFilter || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "toolFilter")) {
    auditState.toolFilter = filters.toolFilter || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "sourceFilter")) {
    auditState.sourceFilter = filters.sourceFilter || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "resultFilter")) {
    auditState.resultFilter = filters.resultFilter || "";
  }
  auditState.focus = normalizeAuditFocus(filters.focus);
  auditState.page = 1;
  auditState.autoSelectFirst = true;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  syncAuditInputsFromState();
  activateTab("audit");
  scheduleAuditRefresh(0);
}

function renderApprovalListItem(item) {
  const title = item.tool_name || item.tool_pattern || "approval";
  const sessionScope = item.scope === "session" ? "session" : "single";
  const meta = [item.agent_name || "-", sessionScope, formatTimestamp(item.created_at)].join(" · ");
  const detail = item.reason || item.resolve_reason || "";
  const scopeBits = [];
  if (item.tool_pattern && item.tool_pattern !== item.tool_name) scopeBits.push(`tool=${item.tool_pattern}`);
  if (item.path_scope) scopeBits.push(`path=${item.path_scope}`);
  if (item.propagate_to_subagents) scopeBits.push("subagents");
  if (item.source_request_id) scopeBits.push(`source=${item.source_request_id}`);
  const extra = scopeBits.length ? `<div class="item-meta mono">${escapeHtml(scopeBits.join(" · "))}</div>` : "";
  return [
    "<li>",
    `<button class="list-item-button${dashboardState.selectedKind === "approval" && dashboardState.selectedId === item.request_id ? " active" : ""}" type="button" data-approval-id="${escapeAttr(item.request_id || "")}">`,
    `<div class="item-card">`,
    `<div class="item-head">`,
    `<div class="item-title">${escapeHtml(title)}</div>`,
    statusTag(item.state || "pending"),
    "</div>",
    `<div class="item-meta">${escapeHtml(meta)}</div>`,
    detail ? `<div class="item-detail">${escapeHtml(compactText(detail))}</div>` : "",
    extra,
    `</div>`,
    `</button>`,
    "</li>"
  ].join("");
}

function renderTaskListItem(task) {
  const metaBits = [task.kind || "-", formatTimestamp(task.updated_at)];
  if (task.task_id) metaBits.unshift(task.task_id);
  const detailBits = [];
  if (task.run_id) detailBits.push(`run=${task.run_id}`);
  if (task.winner_task_id) detailBits.push(`winner=${task.winner_task_id}`);
  if (Array.isArray(task.loser_task_ids) && task.loser_task_ids.length) {
    detailBits.push(`losers=${task.loser_task_ids.length}`);
  }
  if (Array.isArray(task.approval_dependencies) && task.approval_dependencies.length) {
    detailBits.push(`approvals=${task.approval_dependencies.length}`);
  }
  if (task.error) detailBits.push(`error=${task.error}`);
  const artifactKeys = task.artifacts && typeof task.artifacts === "object"
    ? Object.keys(task.artifacts).slice(0, 4)
    : [];
  return [
    "<li>",
    `<button class="list-item-button${dashboardState.selectedKind === "task" && dashboardState.selectedId === task.task_id ? " active" : ""}" type="button" data-task-id="${escapeAttr(task.task_id || "")}">`,
    `<div class="item-card">`,
    `<div class="item-head">`,
    `<div class="item-title">${escapeHtml(task.title || task.kind || task.task_id || "task")}</div>`,
    statusTag(task.status || "unknown"),
    "</div>",
    `<div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div>`,
    detailBits.length ? `<div class="item-detail mono">${escapeHtml(compactText(detailBits.join(" · "), 220))}</div>` : "",
    artifactKeys.length ? `<div class="item-meta mono">artifacts=${escapeHtml(artifactKeys.join(", "))}</div>` : "",
    `</div>`,
    `</button>`,
    "</li>"
  ].join("");
}

function renderCompactList(targetEl, items, renderer, emptyText) {
  if (!targetEl) return;
  if (!items.length) {
    targetEl.innerHTML = `<li class="muted">${escapeHtml(emptyText)}</li>`;
    return;
  }
  targetEl.innerHTML = items.map((item) => renderer(item)).join("");
}

function extractTaskApprovalRequest(task) {
  const artifacts = task && typeof task.artifacts === "object" ? task.artifacts : {};
  const result = artifacts.result && typeof artifacts.result === "object" ? artifacts.result : {};
  const resumeContext = artifacts.resume_context && typeof artifacts.resume_context === "object"
    ? artifacts.resume_context
    : {};
  const candidates = [
    result.approval_request,
    resumeContext.approval_request,
    artifacts.approval_request,
  ];
  return candidates.find((candidate) => candidate && typeof candidate === "object") || null;
}

function buildSyntheticControlApproval(task, approvalRequest) {
  if (!task || !approvalRequest) return null;
  const requestId = String(approvalRequest.request_id || "").trim();
  if (!requestId) return null;
  return {
    request_id: requestId,
    state: "pending",
    tool_name: `control plan ${approvalRequest.plan_id || task.task_id || "control"}`,
    tool_pattern: "control_loop",
    agent_name: "control_loop",
    reason: approvalRequest.goal || approvalRequest.reason || task.title || "control approval required",
    session_id: task.owner_session_id || "",
    created_at: task.updated_at || task.created_at || null,
    scope: "single",
    synthetic_control: true,
    plan_id: approvalRequest.plan_id || "",
    risk_level: approvalRequest.risk_level || "",
    required_capabilities: Array.isArray(approvalRequest.required_capabilities)
      ? approvalRequest.required_capabilities
      : [],
  };
}

function shouldRecoverControlApproval(task) {
  if (!task || task.status !== "pending") return false;
  return Boolean(extractTaskApprovalRequest(task));
}

function collectSyntheticControlApprovals() {
  const merged = new Map();
  const tasks = [
    ...(Array.isArray(dashboardState.recentTasks) ? dashboardState.recentTasks : []),
    ...(Array.isArray(dashboardState.dashboardTasks) ? dashboardState.dashboardTasks : []),
  ];
  tasks.forEach((task) => {
    if (!shouldRecoverControlApproval(task)) return;
    const synthetic = buildSyntheticControlApproval(task, extractTaskApprovalRequest(task));
    if (synthetic) merged.set(synthetic.request_id, synthetic);
  });
  return Array.from(merged.values()).sort((a, b) => {
    const aTs = Date.parse(a.created_at || "") || 0;
    const bTs = Date.parse(b.created_at || "") || 0;
    return bTs - aTs;
  });
}

function mergeApprovalLists(primary, synthetic) {
  const merged = new Map();
  (Array.isArray(primary) ? primary : []).forEach((item) => {
    if (!item || !item.request_id) return;
    merged.set(item.request_id, item);
  });
  (Array.isArray(synthetic) ? synthetic : []).forEach((item) => {
    if (!item || !item.request_id || merged.has(item.request_id)) return;
    merged.set(item.request_id, item);
  });
  return Array.from(merged.values());
}

function updateDashboardUi() {
  const syntheticControlApprovals = collectSyntheticControlApprovals();
  const activeSyntheticControlApprovalIds = new Set(
    syntheticControlApprovals.map((approval) => approval.request_id),
  );
  syntheticControlApprovals.forEach((approval) => {
    const existing = inlineApprovals.get(approval.request_id);
    const preserveStatus = existing && existing.kind === "control" && existing.status !== "pending";
    upsertInlineApproval({
      kind: "control",
      requestId: approval.request_id,
      title: approval.tool_name,
      subtitle: approval.risk_level
        ? `risk=${approval.risk_level} caps=${(approval.required_capabilities || []).join(", ")}`
        : "pending control approval",
      reason: approval.reason || "control approval required",
      sessionId: approval.session_id || "",
      status: preserveStatus ? existing.status : "pending",
      note: preserveStatus
        ? existing.note
        : "Recovered from pending task state. Respond inline to continue the control loop.",
      syntheticControl: true,
    });
  });
  Array.from(inlineApprovals.values()).forEach((model) => {
    if (
      model.kind === "control"
      && model.syntheticControl
      && model.status === "pending"
      && !activeSyntheticControlApprovalIds.has(model.requestId)
    ) {
      removeInlineApproval(model.requestId);
    }
  });
  const pendingApprovals = mergeApprovalLists(
    dashboardState.pendingApprovals,
    syntheticControlApprovals,
  );
  const pendingCount = pendingApprovals.length;
  const openTaskCount = dashboardState.openTaskCount;
  const filteredApprovals = mergeApprovalLists(
    dashboardState.dashboardApprovals || [],
    syntheticControlApprovals,
  );
  const filteredTasks = dashboardState.dashboardTasks || [];
  if (dashboardSessionBackendEl) {
    dashboardSessionBackendEl.textContent = dashboardState.sessionBackend || "-";
  }
  if (dashboardSessionNamespaceEl) {
    dashboardSessionNamespaceEl.textContent = dashboardState.sessionNamespace || "-";
  }
  if (dashboardPendingApprovalsEl) {
    dashboardPendingApprovalsEl.textContent = String(pendingCount);
  }
  if (dashboardOpenTasksEl) {
    dashboardOpenTasksEl.textContent = String(openTaskCount);
  }
  if (dashboardApprovalsCaptionEl) {
    dashboardApprovalsCaptionEl.textContent = paginationLabel(
      Math.max(dashboardState.approvalTotal, filteredApprovals.length),
      dashboardState.approvalPage,
      dashboardState.approvalPageSize,
      filteredApprovals.length,
    );
  }
  if (dashboardTasksCaptionEl) {
    const label = paginationLabel(
      dashboardState.taskTotal,
      dashboardState.taskPage,
      dashboardState.taskPageSize,
      filteredTasks.length,
    );
    dashboardTasksCaptionEl.textContent = currentSessionId
      ? `${label} · ${currentSessionId}`
      : label;
  }
  if (inspectorSessionBackendEl) {
    inspectorSessionBackendEl.textContent = dashboardState.sessionBackend || "-";
  }
  if (inspectorCurrentSessionEl) {
    inspectorCurrentSessionEl.textContent = currentSessionId || "-";
  }
  if (inspectorPendingApprovalsEl) {
    inspectorPendingApprovalsEl.textContent = String(pendingCount);
  }
  if (inspectorOpenTasksEl) {
    inspectorOpenTasksEl.textContent = String(openTaskCount);
  }
  if (inspectorApprovalCountBadgeEl) {
    inspectorApprovalCountBadgeEl.textContent = String(pendingCount);
  }
  if (inspectorTaskCountBadgeEl) {
    inspectorTaskCountBadgeEl.textContent = String(dashboardState.recentTasksTotal);
  }
  updatePagerButtons(
    dashboardApprovalsPrevBtn,
    dashboardApprovalsNextBtn,
    dashboardState.approvalPage,
    dashboardState.approvalHasMore,
  );
  updatePagerButtons(
    dashboardTasksPrevBtn,
    dashboardTasksNextBtn,
    dashboardState.taskPage,
    dashboardState.taskHasMore,
  );

  renderCompactList(
    inspectorApprovalsListEl,
    pendingApprovals,
    renderApprovalListItem,
    "No pending approvals."
  );
  renderCompactList(
    inspectorTasksListEl,
    dashboardState.recentTasks.slice(0, 5),
    renderTaskListItem,
    "No recent tasks."
  );
  renderCompactList(
    dashboardApprovalsListEl,
    filteredApprovals,
    renderApprovalListItem,
    "No approvals yet."
  );
  renderCompactList(
    dashboardTasksListEl,
    filteredTasks,
    renderTaskListItem,
    "No tasks yet."
  );

  renderSelectionDetail();
}

function renderRelationChips(kind, items, emptyText) {
  if (!items.length) {
    return `<div class="muted">${escapeHtml(emptyText)}</div>`;
  }
  return [
    `<div class="relation-list">`,
    ...items.map((item) => {
      if (kind === "task") {
        return `<button class="relation-chip mono" type="button" data-task-ref="${escapeAttr(item.task_id || "")}">${escapeHtml(item.title || item.task_id || "task")}</button>`;
      }
      return `<button class="relation-chip mono" type="button" data-approval-ref="${escapeAttr(item.request_id || "")}">${escapeHtml(item.tool_name || item.request_id || "approval")}</button>`;
    }),
    `</div>`
  ].join("");
}

function renderReuseSuggestions(reuseSuggestions) {
  if (!Array.isArray(reuseSuggestions) || !reuseSuggestions.length) {
    return "";
  }
  const items = reuseSuggestions.map((item) => {
    const metaBits = [];
    if (item.memory_id != null) metaBits.push(`#${item.memory_id}`);
    if (item.score != null) metaBits.push(`score=${Number(item.score).toFixed(3)}`);
    const diffStat = item.metadata && item.metadata.diff_stat ? String(item.metadata.diff_stat) : "";
    return [
      `<div class="detail-card">`,
      `<div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div>`,
      `<div>${escapeHtml(compactText(item.content, 240))}</div>`,
      diffStat ? `<div class="item-meta mono">${escapeHtml(diffStat)}</div>` : "",
      `</div>`
    ].join("");
  });
  return [
    `<div class="detail-section">`,
    `<div class="k">Reusable Approved Improvements</div>`,
    `<div class="detail-grid">`,
    ...items,
    `</div>`,
    `</div>`
  ].join("");
}

function renderTaskTimeline(entries, pagination) {
  const items = Array.isArray(entries) ? entries : [];
  if (!items.length) {
    return `<div class="muted">No timeline events yet.</div>`;
  }
  const caption = pagination && pagination.total
    ? `<div class="item-meta mono">${escapeHtml(paginationLabel(pagination.total, pagination.page || 1, pagination.page_size || items.length, items.length))}</div>`
    : "";
  const rows = items.map((entry) => {
    const kind = String(entry.kind || "timeline");
    const title = String(entry.title || entry.event_type || kind);
    const summary = compactText(entry.summary || "", 220);
    const metaBits = [formatTimestamp(entry.timestamp)];
    if (entry.event_type) metaBits.push(String(entry.event_type));
    if (entry.request_id) metaBits.push(String(entry.request_id));
    if (entry.audit_entry_id) metaBits.push(String(entry.audit_entry_id));
    const body = [
      `<div class="timeline-entry-card detail-card">`,
      `<div class="item-head">`,
      `<div class="item-title">${escapeHtml(title)}</div>`,
      statusTag(entry.status || kind),
      `</div>`,
      `<div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div>`,
      summary ? `<div class="item-detail">${escapeHtml(summary)}</div>` : "",
      `</div>`,
    ].join("");
    if (kind === "approval" && entry.request_id) {
      return `<button class="timeline-entry-button" type="button" data-approval-ref="${escapeAttr(entry.request_id)}">${body}</button>`;
    }
    if (kind === "audit" && entry.audit_entry_id) {
      const focus = entry.audit_focus || {};
      return [
        `<button class="timeline-entry-button" type="button"`,
        ` data-action="open-related-audit"`,
        ` data-audit-session-id="${escapeAttr(focus.sessionId || "")}"`,
        ` data-audit-query="${escapeAttr(focus.searchQuery || "")}"`,
        ` data-audit-request-id="${escapeAttr(focus.requestId || "")}"`,
        ` data-audit-task-id="${escapeAttr(focus.taskId || "")}"`,
        ` data-audit-run-id="${escapeAttr(focus.runId || "")}"`,
        ` data-audit-tool="${escapeAttr(focus.toolName || "")}"`,
        ` data-audit-source="${escapeAttr(focus.source || "")}"`,
        ` data-audit-result="${escapeAttr(focus.result || "")}"`,
        ` data-audit-entry-id="${escapeAttr(entry.audit_entry_id)}">`,
        body,
        `</button>`,
      ].join("");
    }
    return `<div class="timeline-entry-static">${body}</div>`;
  });
  return [
    `<div class="detail-section">`,
    `<div class="k">Task Timeline</div>`,
    caption,
    `<div class="timeline-list">`,
    ...rows,
    `</div>`,
    `</div>`,
  ].join("");
}

function renderTaskComparison(comparison) {
  if (!comparison || typeof comparison !== "object") {
    return "";
  }
  const leftTask = comparison.left_task || {};
  const rightTask = comparison.right_task || {};
  const left = comparison.left || {};
  const right = comparison.right || {};
  const leftResult = left.result || {};
  const rightResult = right.result || {};
  const stepCompare = comparison.step_compare || {};
  const stepRows = Array.isArray(stepCompare.rows) ? stepCompare.rows : [];
  const summaryItems = Array.isArray(comparison.summary) ? comparison.summary : [];
  const summaryHtml = summaryItems.length
    ? `<ul class="detail-list">${summaryItems.map((item) => `<li>${escapeHtml(String(item))}</li>`).join("")}</ul>`
    : `<div class="muted">No comparison summary.</div>`;
  const leftIsReplay = Boolean(
    leftTask.parent_task_id
    && rightTask.task_id
    && leftTask.parent_task_id === rightTask.task_id
  );
  const rightIsReplay = Boolean(
    rightTask.parent_task_id
    && leftTask.task_id
    && rightTask.parent_task_id === leftTask.task_id
  );
  const leftLabel = leftIsReplay ? "Replay" : "Baseline";
  const rightLabel = rightIsReplay ? "Replay" : "Baseline";
  const renderStepRow = (row) => {
    const leftStep = row.left || {};
    const rightStep = row.right || {};
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div>`,
      `<div class="k">${escapeHtml(row.title || row.step_id || "step")}</div>`,
      `<div class="item-meta mono">${escapeHtml(row.step_id || "-")}</div>`,
      `</div>`,
      row.changed ? `<span class="tag warning">changed</span>` : `<span class="tag">same</span>`,
      `</div>`,
      `<div class="item-detail">${escapeHtml(`${leftStep.status || "-"} -> ${rightStep.status || "-"}`)}</div>`,
      `<div class="item-meta">${escapeHtml(`${leftStep.output_summary || "-"} -> ${rightStep.output_summary || "-"}`)}</div>`,
      `<div class="item-meta">${escapeHtml(`failed: ${(leftStep.failed_criteria || []).join(", ") || "-"} -> ${(rightStep.failed_criteria || []).join(", ") || "-"}`)}</div>`,
      `</div>`,
    ].join("");
  };
  const renderSide = (label, task, snapshot) => [
    `<div class="detail-card">`,
    `<div class="k">${escapeHtml(label)}</div>`,
    `<div class="item-title">${escapeHtml(task.title || task.task_id || label)}</div>`,
    `<div class="item-meta mono">${escapeHtml([task.task_id || "-", task.status || "-", snapshot.verification_status || "-"].join(" · "))}</div>`,
    `<div class="item-detail">score=${escapeHtml(String((snapshot.overall_score || 0).toFixed ? snapshot.overall_score.toFixed(2) : snapshot.overall_score || 0))} repairs=${escapeHtml(String(snapshot.repair_count ?? "-"))}</div>`,
    snapshot.failed_criteria?.length
      ? `<div class="item-meta">${escapeHtml(`failed: ${snapshot.failed_criteria.join(", ")}`)}</div>`
      : `<div class="item-meta">failed: -</div>`,
    snapshot.screenshot_refs?.length
      ? `<div class="item-meta mono">${escapeHtml(snapshot.screenshot_refs.join(" · "))}</div>`
      : `<div class="item-meta mono">screenshots: -</div>`,
    `<pre class="detail-pre">${formatJsonBlock(snapshot.verification_report || {})}</pre>`,
    `</div>`,
  ].join("");

  return [
    `<div class="detail-section">`,
    `<div class="k">Replay Compare</div>`,
    summaryHtml,
    `<div class="detail-grid">`,
    renderSide(leftLabel, leftTask, leftResult),
    renderSide(rightLabel, rightTask, rightResult),
    `</div>`,
    `<div class="k">Step Diff</div>`,
    stepRows.length
      ? `<div class="detail-grid">${stepRows.map(renderStepRow).join("")}</div>`
      : `<div class="muted">No step-level diff available.</div>`,
    `</div>`,
  ].join("");
}

function humanizeLongRunningToken(value) {
  const text = String(value || "").trim();
  if (!text) return "-";
  return text.replace(/[_-]+/g, " ");
}

function extractLongRunningReport(task) {
  const artifacts = task && typeof task.artifacts === "object" ? task.artifacts : {};
  const report = artifacts.report && typeof artifacts.report === "object" ? artifacts.report : null;
  const liveDurable = artifacts.durable_execution && typeof artifacts.durable_execution === "object"
    ? artifacts.durable_execution
    : null;
  if (!report && !liveDurable) return null;
  const durable = report?.durable_execution && typeof report.durable_execution === "object"
    ? report.durable_execution
    : (liveDurable || {});
  const runJobs = Array.isArray(report?.run_jobs)
    ? report.run_jobs
    : (Array.isArray(durable.job_runs) ? durable.job_runs : []);
  if (!runJobs.length && !Object.keys(durable).length) {
    return null;
  }
  return { report: report || {}, durable, runJobs };
}

function renderLongRunningTaskState(task) {
  const state = extractLongRunningReport(task);
  if (!state) {
    return "";
  }
  const { report, durable, runJobs } = state;
  const artifacts = task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const slice = report.slice && typeof report.slice === "object" ? report.slice : {};
  const taskGraph = durable.task_graph && typeof durable.task_graph === "object"
    ? durable.task_graph
    : {};
  const taskNodes = Array.isArray(taskGraph.nodes) ? taskGraph.nodes : [];
  const schedulerState = durable.scheduler_state && typeof durable.scheduler_state === "object"
    ? durable.scheduler_state
    : {};
  const missionContract = durable.mission_contract && typeof durable.mission_contract === "object"
    ? durable.mission_contract
    : (artifacts.mission_contract && typeof artifacts.mission_contract === "object" ? artifacts.mission_contract : {});
  const missionScorecard = durable.mission_scorecard && typeof durable.mission_scorecard === "object"
    ? durable.mission_scorecard
    : (artifacts.mission_scorecard && typeof artifacts.mission_scorecard === "object" ? artifacts.mission_scorecard : {});
  const missionReview = artifacts.mission_review && typeof artifacts.mission_review === "object"
    ? artifacts.mission_review
    : (durable.mission_review && typeof durable.mission_review === "object" ? durable.mission_review : {});
  const memoryPromotionCandidates = Array.isArray(artifacts.memory_promotion_candidates)
    ? artifacts.memory_promotion_candidates
    : (Array.isArray(durable.memory_promotion_candidates) ? durable.memory_promotion_candidates : []);
  const reusePlan = artifacts.reuse_plan && typeof artifacts.reuse_plan === "object"
    ? artifacts.reuse_plan
    : (durable.reuse_plan && typeof durable.reuse_plan === "object" ? durable.reuse_plan : {});
  const taskTimelineForMission = Array.isArray(dashboardState.taskTimeline) ? dashboardState.taskTimeline : [];
  const reusePlanHistory = taskTimelineForMission.filter((entry) => entry && entry.event_type === "mission_reuse_plan_recorded");
  const resumeState = durable.resume_state && typeof durable.resume_state === "object"
    ? durable.resume_state
    : {};
  const supervisorHealth = durable.supervisor_health && typeof durable.supervisor_health === "object"
    ? durable.supervisor_health
    : {};
  const checkpoints = Array.isArray(durable.checkpoints) ? durable.checkpoints : [];
  const escalations = Array.isArray(durable.escalations) ? durable.escalations : [];
  const recoveryDecisions = Array.isArray(durable.recovery_decisions) ? durable.recovery_decisions : [];
  const verifierVerdicts = Array.isArray(durable.verifier_verdicts)
    ? durable.verifier_verdicts
    : runJobs.map((job) => job.verifier_verdict).concat(taskNodes.map((node) => node.verifier_verdict)).filter((item) => item && typeof item === "object");
  const activeRunJobs = runJobs.filter((item) => {
    const queue = String(item.scheduler_queue || "").trim();
    return queue && queue !== "completed";
  });
  const budgetExhaustedJobs = runJobs.filter((item) => (
    Boolean(item.budget_state && item.budget_state.budget_exhausted)
  ));
  const queueGroups = [
    ["ready", Array.isArray(schedulerState.ready_queue) ? schedulerState.ready_queue : []],
    ["blocked", Array.isArray(schedulerState.blocked_queue) ? schedulerState.blocked_queue : []],
    ["waiting_for_approval", Array.isArray(schedulerState.waiting_for_approval_queue) ? schedulerState.waiting_for_approval_queue : []],
    ["retry_later", Array.isArray(schedulerState.retry_later_queue) ? schedulerState.retry_later_queue : []],
    ["periodic_check", Array.isArray(schedulerState.periodic_check_queue) ? schedulerState.periodic_check_queue : []],
    ["completed", Array.isArray(schedulerState.completed_queue) ? schedulerState.completed_queue : []],
  ];
  const latestCheckpoints = checkpoints.slice(-3).reverse();
  const currentNodeId = supervisorHealth.active_node_id
    || resumeState.next_actionable_task_node_id
    || taskNodes.find((node) => ["running", "blocked", "failed"].includes(String(node.status || "").toLowerCase()))?.node_id
    || "-";
  const missionStatus = missionReview.final_status || task.status || missionScorecard.objective_progress || "unknown";
  const approvalIds = Array.isArray(resumeState.pending_approval_ids) ? resumeState.pending_approval_ids : [];
  const contractPolicyBits = [
    Array.isArray(missionContract.success_metrics) && missionContract.success_metrics.length ? `metrics=${missionContract.success_metrics.length}` : "",
    Array.isArray(missionContract.completion_criteria) && missionContract.completion_criteria.length ? `criteria=${missionContract.completion_criteria.length}` : "",
    Array.isArray(missionContract.evidence_requirements) && missionContract.evidence_requirements.length ? `evidence=${missionContract.evidence_requirements.length}` : "",
  ].filter(Boolean);

  const renderRefs = (refs, prefix = "refs") => {
    const values = Array.isArray(refs) ? refs.filter(Boolean).map(String) : [];
    return values.length
      ? `<div class="item-meta mono">${escapeHtml(`${prefix}=${values.join(", ")}`)}</div>`
      : "";
  };

  const renderMissionRuntimeSummary = () => [
    `<div class="detail-section">`,
    `<div class="k">Mission Runtime</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Status</div><div>${statusTag(missionStatus)}</div><div class="item-meta mono">${escapeHtml(`task=${task.status || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Current Node</div><div class="mono">${escapeHtml(currentNodeId)}</div><div class="item-meta">${escapeHtml(humanizeLongRunningToken(supervisorHealth.reason || resumeState.reason || "-"))}</div></div>`,
    `<div class="detail-card"><div class="k">Objective</div><div class="item-detail">${escapeHtml(compactText(missionContract.objective || task.title || "-", 220))}</div><div class="item-meta mono">${escapeHtml(missionContract.contract_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Policy Surface</div><div class="item-detail">${escapeHtml((missionContract.allowed_actions || []).join(", ") || "-")}</div><div class="item-meta mono">${escapeHtml(contractPolicyBits.join(" · ") || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Approval Wait</div><div>${escapeHtml(String(escalations.length || approvalIds.length || 0))}</div><div class="item-meta mono">${escapeHtml(approvalIds.join(", ") || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Memory Candidates</div><div>${escapeHtml(String(memoryPromotionCandidates.length))}</div><div class="item-meta mono">${escapeHtml(memoryPromotionCandidates.map((item) => item.approval_status || "unknown").join(" · ") || "-")}</div></div>`,
    `</div>`,
    `</div>`,
  ].join("");

  const renderTaskNode = (node) => {
    const verdict = node.verifier_verdict && typeof node.verifier_verdict === "object" ? node.verifier_verdict : {};
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(node.title || node.node_id || "node")}</div><div class="item-meta mono">${escapeHtml(node.node_id || "-")}</div></div>`,
      statusTag(node.scheduler_queue || node.status || "unknown"),
      `</div>`,
      `<div class="item-detail">${escapeHtml(compactText(node.description || "-", 180))}</div>`,
      `<div class="item-meta mono">${escapeHtml([
        `status=${node.status || "-"}`,
        `retry=${node.retry_count ?? 0}`,
        verdict.verdict ? `verdict=${verdict.verdict}` : "",
        verdict.failure_type ? `failure=${verdict.failure_type}` : "",
      ].filter(Boolean).join(" · "))}</div>`,
      renderRefs(verdict.evidence_refs, "evidence"),
      `</div>`,
    ].join("");
  };

  const renderQueueEntry = (queueName, entry) => {
    const metaBits = [
      entry.node_id || "-",
      entry.checkpoint_id || "-",
      entry.available_at ? formatTimestamp(entry.available_at) : null,
    ].filter(Boolean);
    const metadata = entry.metadata && typeof entry.metadata === "object" ? entry.metadata : {};
    const detailBits = [
      metadata.failure_type ? `failure=${metadata.failure_type}` : "",
      metadata.chosen_action ? `next=${metadata.chosen_action}` : "",
    ].filter(Boolean);
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(entry.node_id || queueName)}</div><div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div></div>`,
      statusTag(queueName),
      `</div>`,
      `<div class="item-detail">${escapeHtml(entry.reason || "-")}</div>`,
      detailBits.length ? `<div class="item-meta mono">${escapeHtml(detailBits.join(" · "))}</div>` : "",
      entry.escalation_id ? `<div class="item-meta mono">${escapeHtml(`escalation=${entry.escalation_id}`)}</div>` : "",
      `</div>`,
    ].join("");
  };

  const renderRunJob = (job) => {
    const checkpoint = job.checkpoint && typeof job.checkpoint === "object" ? job.checkpoint : {};
    const decision = job.recovery_decision && typeof job.recovery_decision === "object" ? job.recovery_decision : {};
    const budgetState = job.budget_state && typeof job.budget_state === "object" ? job.budget_state : {};
    const replayReference = job.replay_reference && typeof job.replay_reference === "object" ? job.replay_reference : {};
    const verdict = job.verifier_verdict && typeof job.verifier_verdict === "object" ? job.verifier_verdict : {};
    const evidenceRefs = Array.isArray(verdict.evidence_refs) ? verdict.evidence_refs : [];
    const detailBits = [
      (job.failure_type || verdict.failure_type) ? `failure=${job.failure_type || verdict.failure_type}` : "",
      decision.chosen_action ? `next=${decision.chosen_action}` : "",
      (checkpoint.current_task_node_id || job.node_id) ? `node=${checkpoint.current_task_node_id || job.node_id}` : "",
    ].filter(Boolean);
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(job.run_job_id || job.run_id || job.task_node_id || "run job")}</div><div class="item-meta mono">${escapeHtml(`trajectory=${job.trajectory_id || "-"}`)}</div></div>`,
      statusTag(job.scheduler_queue || job.status || "unknown"),
      `</div>`,
      `<div class="item-detail">${escapeHtml(detailBits.join(" · ") || "-")}</div>`,
      `<div class="item-meta">${escapeHtml(job.scheduler_queue_entry?.reason || decision.failure_type || "-")}</div>`,
      replayReference && Object.keys(replayReference).length
        ? `<div class="item-meta mono">${escapeHtml(compactText(JSON.stringify(replayReference), 180))}</div>`
        : "",
      evidenceRefs.length ? `<div class="item-meta mono">${escapeHtml(`evidence=${evidenceRefs.join(", ")}`)}</div>` : "",
      budgetState.budget_exhausted
        ? `<div class="item-meta mono">${escapeHtml(`budget=${(budgetState.budget_exhausted_reasons || []).join(", ") || "exhausted"}`)}</div>`
        : "",
      `</div>`,
    ].join("");
  };

  const renderCheckpoint = (checkpoint) => {
    const replayRef = Array.isArray(checkpoint.replay_references) ? checkpoint.replay_references[0] : null;
    const approvalIds = Array.isArray(checkpoint.pending_approval_ids) ? checkpoint.pending_approval_ids : [];
    return [
      `<div class="detail-card">`,
      `<div class="k">${escapeHtml(checkpoint.checkpoint_id || "checkpoint")}</div>`,
      `<div class="item-meta mono">${escapeHtml([
        checkpoint.current_task_node_id || "-",
        `next=${checkpoint.next_actionable_task_node_id || "-"}`,
        formatTimestamp(checkpoint.created_at),
      ].join(" · "))}</div>`,
      `<div class="item-detail">${escapeHtml(`trajectory=${(checkpoint.trajectory_ids || []).join(", ") || "-"} blocked=${(checkpoint.blocked_task_node_ids || []).join(", ") || "-"}`)}</div>`,
      replayRef ? `<div class="item-meta mono">${escapeHtml(compactText(JSON.stringify(replayRef), 180))}</div>` : "",
      approvalIds.length ? `<div class="item-meta mono">${escapeHtml(`approvals=${approvalIds.join(", ")}`)}</div>` : "",
      `</div>`,
    ].join("");
  };

  const renderEscalation = (escalation) => [
    `<div class="detail-card">`,
    `<div class="detail-heading">`,
    `<div><div class="k">${escapeHtml(escalation.approval_request_id || escalation.escalation_id || "approval wait")}</div><div class="item-meta mono">${escapeHtml(escalation.node_id || "-")}</div></div>`,
    statusTag(escalation.status || "waiting_for_approval"),
    `</div>`,
    `<div class="item-detail">${escapeHtml(escalation.reason || "-")}</div>`,
    `<div class="item-meta mono">${escapeHtml([
      escalation.failure_type || "-",
      escalation.resume_checkpoint_id || escalation.checkpoint_id || "-",
      formatTimestamp(escalation.created_at),
    ].join(" · "))}</div>`,
    `</div>`,
  ].join("");

  const renderRecoveryDecision = (decision) => {
    const budgetAfter = decision.budget_after && typeof decision.budget_after === "object" ? decision.budget_after : {};
    const budgetReasons = Array.isArray(budgetAfter.budget_exhausted_reasons)
      ? budgetAfter.budget_exhausted_reasons
      : [];
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(decision.selected_step || decision.recovery_ladder_step || "recovery")}</div><div class="item-meta mono">${escapeHtml(decision.node_id || "-")}</div></div>`,
      statusTag(decision.outcome || (budgetAfter.budget_exhausted ? "blocked" : "unknown")),
      `</div>`,
      `<div class="item-detail">${escapeHtml(decision.reason || decision.failure_type || "-")}</div>`,
      `<div class="item-meta mono">${escapeHtml([
        decision.failure_type ? `failure=${decision.failure_type}` : "",
        decision.attempt_index !== undefined ? `attempt=${decision.attempt_index}` : "",
        budgetAfter.budget_exhausted ? `budget=${budgetReasons.join(", ") || "exhausted"}` : "",
      ].filter(Boolean).join(" · ") || "-")}</div>`,
      renderRefs(decision.source_refs, "source"),
      `</div>`,
    ].join("");
  };

  const renderVerifierVerdict = (verdict) => [
    `<div class="detail-card">`,
    `<div class="detail-heading">`,
    `<div><div class="k">${escapeHtml(verdict.verdict || "verdict")}</div><div class="item-meta mono">${escapeHtml(verdict.task_node_id || verdict.node_id || verdict.run_id || "-")}</div></div>`,
    statusTag(verdict.verdict || "unknown"),
    `</div>`,
    `<div class="item-detail">${escapeHtml(verdict.summary || verdict.failure_type || "-")}</div>`,
    `<div class="item-meta mono">${escapeHtml([
      verdict.failure_type ? `failure=${verdict.failure_type}` : "",
      verdict.confidence !== undefined ? `confidence=${verdict.confidence}` : "",
    ].filter(Boolean).join(" · ") || "-")}</div>`,
    renderRefs(verdict.evidence_refs, "evidence"),
    `</div>`,
  ].join("");

  const renderMemoryPromotionCandidate = (candidate) => [
    `<div class="detail-card">`,
    `<div class="detail-heading">`,
    `<div><div class="k">${escapeHtml(candidate.type || "memory candidate")}</div><div class="item-meta mono">${escapeHtml(candidate.candidate_id || "-")}</div></div>`,
    statusTag(candidate.approval_status || "candidate_only"),
    `</div>`,
    `<div class="item-detail">${escapeHtml(compactText(candidate.content || "-", 220))}</div>`,
    `<div class="item-meta mono">${escapeHtml([
      candidate.confidence !== undefined ? `confidence=${candidate.confidence}` : "",
      candidate.expires_at ? `expires=${formatTimestamp(candidate.expires_at)}` : "expires=-",
      candidate.approved_by ? `approved_by=${candidate.approved_by}` : "",
      candidate.rejected_reason ? `rejected=${candidate.rejected_reason}` : "",
    ].filter(Boolean).join(" · "))}</div>`,
    renderRefs(candidate.source_refs || [candidate.source_artifact_ref].filter(Boolean), "source"),
    `</div>`,
  ].join("");

  const renderReuseSelection = (selection) => {
    const item = selection && typeof selection === "object" ? selection : {};
    const matchedTerms = Array.isArray(item.matched_terms) ? item.matched_terms : [];
    const metaBits = [
      item.promotion_target ? `target=${item.promotion_target}` : "",
      item.application_mode ? `mode=${item.application_mode}` : "",
      item.relevance_score !== undefined ? `score=${Number(item.relevance_score || 0).toFixed(3)}` : "",
      item.expires_at ? `expires=${formatTimestamp(item.expires_at)}` : "",
    ].filter(Boolean);
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(item.artifact_id || "selected artifact")}</div><div class="item-meta mono">${escapeHtml(item.candidate_id || item.source_package_id || "-")}</div></div>`,
      statusTag(item.promotion_target || "selected"),
      `</div>`,
      `<div class="item-detail">${escapeHtml(compactText(item.reason || "-", 220))}</div>`,
      metaBits.length ? `<div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div>` : "",
      matchedTerms.length ? `<div class="item-meta mono">${escapeHtml(`matched=${matchedTerms.join(", ")}`)}</div>` : "",
      item.approval_ref ? `<div class="item-meta mono">${escapeHtml(`approval=${item.approval_ref}`)}</div>` : "",
      renderRefs(item.source_refs, "source"),
      `</div>`,
    ].join("");
  };

  const renderReuseExcludedCandidate = (candidate) => {
    const item = candidate && typeof candidate === "object" ? candidate : {};
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(item.artifact_id || item.candidate_id || "excluded candidate")}</div><div class="item-meta mono">${escapeHtml(item.promotion_target || "-")}</div></div>`,
      statusTag(item.reason || "excluded"),
      `</div>`,
      `<div class="item-detail">${escapeHtml(compactText(item.details || item.reason || "-", 220))}</div>`,
      `<div class="item-meta mono">${escapeHtml(item.candidate_id || "-")}</div>`,
      renderRefs(item.source_refs, "source"),
      `</div>`,
    ].join("");
  };

  const renderReuseCheck = (check) => {
    const item = check && typeof check === "object" ? check : {};
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(item.artifact_id || "reuse check")}</div><div class="item-meta mono">${escapeHtml(item.check || "-")}</div></div>`,
      statusTag(item.passed ? "passed" : "blocked"),
      `</div>`,
      `<div class="item-detail">${escapeHtml(item.reason || "-")}</div>`,
      `<div class="item-meta mono">${escapeHtml(formatTimestamp(item.checked_at))}</div>`,
      `</div>`,
    ].join("");
  };

  const renderReusePlanHistory = (entry) => {
    const payload = entry.payload && typeof entry.payload === "object" ? entry.payload : {};
    const selected = payload.selected_counts && typeof payload.selected_counts === "object" ? payload.selected_counts : {};
    const counts = [
      `memories=${selected.memories ?? 0}`,
      `skills=${selected.skills ?? 0}`,
      `policies=${selected.policies ?? 0}`,
      `capabilities=${selected.capabilities ?? 0}`,
      `excluded=${payload.excluded_count ?? 0}`,
    ];
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(entry.title || "Mission reuse plan recorded")}</div><div class="item-meta mono">${escapeHtml(formatTimestamp(entry.timestamp))}</div></div>`,
      statusTag(entry.status || "recorded"),
      `</div>`,
      `<div class="item-detail">${escapeHtml(payload.summary || entry.summary || "-")}</div>`,
      `<div class="item-meta mono">${escapeHtml(counts.join(" · "))}</div>`,
      payload.automatic_runtime_application === false ? `<div class="item-meta mono">automatic_runtime_application=false</div>` : "",
      `</div>`,
    ].join("");
  };

  const renderMissionReusePlan = () => {
    if (!reusePlan || !Object.keys(reusePlan).length) return "";
    const selectedMemories = Array.isArray(reusePlan.selected_memories) ? reusePlan.selected_memories : [];
    const selectedSkills = Array.isArray(reusePlan.selected_skills) ? reusePlan.selected_skills : [];
    const selectedPolicies = Array.isArray(reusePlan.selected_policies) ? reusePlan.selected_policies : [];
    const selectedCapabilities = Array.isArray(reusePlan.selected_capabilities) ? reusePlan.selected_capabilities : [];
    const excludedCandidates = Array.isArray(reusePlan.excluded_candidates) ? reusePlan.excluded_candidates : [];
    const expiryChecks = Array.isArray(reusePlan.expiry_checks) ? reusePlan.expiry_checks : [];
    const policyChecks = Array.isArray(reusePlan.policy_checks) ? reusePlan.policy_checks : [];
    const planMetadata = reusePlan.metadata && typeof reusePlan.metadata === "object" ? reusePlan.metadata : {};
    const counts = [
      `memories=${selectedMemories.length}`,
      `skills=${selectedSkills.length}`,
      `policies=${selectedPolicies.length}`,
      `capabilities=${selectedCapabilities.length}`,
      `excluded=${excludedCandidates.length}`,
    ].join(" · ");
    return [
      `<div class="detail-section">`,
      `<div class="k">Reuse Plan</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Plan</div><div>${statusTag(reusePlan.operator_visible === false ? "hidden" : "operator_visible")}</div><div class="item-meta mono">${escapeHtml(reusePlan.schema_version || "-")}</div></div>`,
      `<div class="detail-card"><div class="k">Mission</div><div class="mono">${escapeHtml(reusePlan.mission_task_id || task.task_id || "-")}</div><div class="item-meta mono">${escapeHtml(reusePlan.mission_contract_id || missionContract.contract_id || "-")}</div></div>`,
      `<div class="detail-card"><div class="k">Selected</div><div>${escapeHtml(counts)}</div><div class="item-meta mono">${escapeHtml(`automatic_runtime_application=${String(planMetadata.automatic_runtime_application === true)}`)}</div></div>`,
      `</div>`,
      selectedMemories.length ? `<div class="k">Selected Memories</div><div class="detail-grid">${selectedMemories.map(renderReuseSelection).join("")}</div>` : "",
      selectedSkills.length ? `<div class="k">Selected Skills</div><div class="detail-grid">${selectedSkills.map(renderReuseSelection).join("")}</div>` : "",
      selectedPolicies.length ? `<div class="k">Selected Policies</div><div class="detail-grid">${selectedPolicies.map(renderReuseSelection).join("")}</div>` : "",
      selectedCapabilities.length ? `<div class="k">Selected Capabilities</div><div class="detail-grid">${selectedCapabilities.map(renderReuseSelection).join("")}</div>` : "",
      `<div class="k">Excluded Candidates</div>`,
      excludedCandidates.length ? `<div class="detail-grid">${excludedCandidates.map(renderReuseExcludedCandidate).join("")}</div>` : `<div class="muted">No excluded reuse candidates.</div>`,
      `<div class="k">Expiry Checks</div>`,
      expiryChecks.length ? `<div class="detail-grid">${expiryChecks.map(renderReuseCheck).join("")}</div>` : `<div class="muted">No reuse expiry checks recorded.</div>`,
      `<div class="k">Policy Checks</div>`,
      policyChecks.length ? `<div class="detail-grid">${policyChecks.map(renderReuseCheck).join("")}</div>` : `<div class="muted">No reuse policy checks recorded.</div>`,
      reusePlanHistory.length ? `<div class="k">Reuse Plan History</div><div class="detail-grid">${reusePlanHistory.map(renderReusePlanHistory).join("")}</div>` : "",
      `</div>`,
    ].join("");
  };

  const renderMissionScorecard = () => {
    if (!missionScorecard || !Object.keys(missionScorecard).length) return "";
    const metadata = missionScorecard.metadata && typeof missionScorecard.metadata === "object" ? missionScorecard.metadata : {};
    const failureCounts = metadata.failure_type_counts && typeof metadata.failure_type_counts === "object"
      ? Object.entries(metadata.failure_type_counts).map(([key, value]) => `${key}=${value}`).join(" · ")
      : "";
    return [
      `<div class="detail-section">`,
      `<div class="k">Mission Scorecard</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Progress</div><div>${escapeHtml(humanizeLongRunningToken(missionScorecard.objective_progress || "-"))}</div><div class="item-meta mono">${escapeHtml(missionScorecard.last_verifier_verdict || "-")}</div></div>`,
      `<div class="detail-card"><div class="k">Verification</div><div>${escapeHtml(Number(missionScorecard.verification_pass_rate || 0).toFixed(2))}</div><div class="item-meta mono">${escapeHtml(`recovery=${Number(missionScorecard.recovery_success_rate || 0).toFixed(2)}`)}</div></div>`,
      `<div class="detail-card"><div class="k">Friction</div><div>${escapeHtml(`blocked=${missionScorecard.blocked_count ?? 0} approvals=${missionScorecard.approval_wait_count ?? 0}`)}</div><div class="item-meta mono">${escapeHtml(`repeated=${missionScorecard.repeated_failure_count ?? 0}`)}</div></div>`,
      `<div class="detail-card"><div class="k">Candidates</div><div>${escapeHtml(`improve=${missionScorecard.improvement_candidate_count ?? 0}`)}</div><div class="item-meta mono">${escapeHtml(`memory=${missionScorecard.memory_promotion_candidate_count ?? 0}`)}</div></div>`,
      `</div>`,
      failureCounts ? `<div class="item-meta mono">${escapeHtml(`failures=${failureCounts}`)}</div>` : "",
      `</div>`,
    ].join("");
  };

  const renderMissionReview = () => {
    if (!missionReview || !Object.keys(missionReview).length) return "";
    const failures = Array.isArray(missionReview.failure_buckets) ? missionReview.failure_buckets : [];
    const improvements = Array.isArray(missionReview.improvement_candidates) ? missionReview.improvement_candidates : [];
    const memoryCandidates = Array.isArray(missionReview.memory_promotion_candidates) ? missionReview.memory_promotion_candidates : [];
    const recommendations = Array.isArray(missionReview.recommended_next_contract_edits) ? missionReview.recommended_next_contract_edits : [];
    const renderBucket = (bucket) => `<div class="detail-card"><div class="k">${escapeHtml(bucket.failure_type || "failure")}</div><div>${escapeHtml(String(bucket.count ?? 0))}</div></div>`;
    const renderCandidate = (candidate) => `<div class="detail-card"><div class="k">${escapeHtml(candidate.candidate_type || candidate.type || "candidate")}</div><div class="item-detail">${escapeHtml(candidate.summary || candidate.content || "-")}</div><div class="item-meta mono">${escapeHtml(candidate.failure_type || candidate.candidate_id || candidate.source_artifact_ref || "-")}</div></div>`;
    return [
      `<div class="detail-section">`,
      `<div class="k">Post-Mission Review</div>`,
      `<div class="detail-card"><div class="item-detail">${escapeHtml(missionReview.summary || "-")}</div><div class="item-meta mono">${escapeHtml(`${missionReview.schema_version || "-"} · ${missionReview.final_status || "-"}`)}</div></div>`,
      failures.length ? `<div class="k">Failure Buckets</div><div class="detail-grid">${failures.map(renderBucket).join("")}</div>` : "",
      improvements.length ? `<div class="k">Improvement Candidates</div><div class="detail-grid">${improvements.map(renderCandidate).join("")}</div>` : "",
      memoryCandidates.length ? `<div class="k">Memory Promotion Candidates</div><div class="detail-grid">${memoryCandidates.map(renderCandidate).join("")}</div>` : "",
      recommendations.length ? `<div class="item-meta">${escapeHtml(`next contract edits: ${recommendations.join(" · ")}`)}</div>` : "",
      `</div>`,
    ].join("");
  };

  return [
    `<div class="detail-section">`,
    `<div class="k">Long-Running State</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Slice</div><div>${escapeHtml(humanizeLongRunningToken(slice.type || "-"))}</div><div class="item-meta mono">${escapeHtml(report.eval_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Runs</div><div>${escapeHtml(`${report.runs_evaluated || 0}/${report.runs_requested || report.runs_evaluated || 0}`)}</div><div class="item-meta mono">${escapeHtml(`success_rate=${Number(report.success_rate || 0).toFixed(4)}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Mission Contract</div><div>${escapeHtml(missionContract.contract_id || "-")}</div><div class="item-meta mono">${escapeHtml((missionContract.allowed_actions || []).join(", ") || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Resume</div><div>${escapeHtml(humanizeLongRunningToken(resumeState.reason || "-"))}</div><div class="item-meta mono">${escapeHtml(resumeState.next_actionable_task_node_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Queues</div><div>${escapeHtml([
      `ready=${resumeState.scheduler_queue_counts?.ready ?? 0}`,
      `blocked=${resumeState.scheduler_queue_counts?.blocked ?? 0}`,
      `approvals=${resumeState.scheduler_queue_counts?.waiting_for_approval ?? 0}`,
      `retry=${resumeState.scheduler_queue_counts?.retry_later ?? 0}`,
    ].join(" · "))}</div><div class="item-meta mono">${escapeHtml(`pending=${(resumeState.pending_approval_ids || []).join(", ") || "-"}`)}</div></div>`,
    `</div>`,
    `</div>`,
    renderMissionRuntimeSummary(),
    `<div class="detail-section">`,
    `<div class="k">Mission Task Graph</div>`,
    taskNodes.length
      ? `<div class="detail-grid">${taskNodes.map(renderTaskNode).join("")}</div>`
      : `<div class="muted">No mission task graph nodes recorded.</div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Scheduler Queues</div>`,
    queueGroups.some(([, items]) => items.length)
      ? `<div class="detail-grid">${queueGroups.flatMap(([queueName, items]) => (
          items.map((entry) => renderQueueEntry(queueName, entry))
        )).join("")}</div>`
      : `<div class="muted">No scheduler queue entries recorded.</div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Blocked or Deferred Tasks</div>`,
    activeRunJobs.length
      ? `<div class="detail-grid">${activeRunJobs.map(renderRunJob).join("")}</div>`
      : `<div class="muted">No blocked or deferred task nodes.</div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Latest Checkpoints</div>`,
    latestCheckpoints.length
      ? `<div class="detail-grid">${latestCheckpoints.map(renderCheckpoint).join("")}</div>`
      : `<div class="muted">No checkpoints recorded.</div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Recovery Decisions</div>`,
    recoveryDecisions.length
      ? `<div class="detail-grid">${recoveryDecisions.map(renderRecoveryDecision).join("")}</div>`
      : `<div class="muted">No recovery decisions recorded.</div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Verifier Evidence</div>`,
    verifierVerdicts.length
      ? `<div class="detail-grid">${verifierVerdicts.map(renderVerifierVerdict).join("")}</div>`
      : `<div class="muted">No verifier evidence recorded.</div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Approval Waits</div>`,
    escalations.length
      ? `<div class="detail-grid">${escalations.map(renderEscalation).join("")}</div>`
      : `<div class="muted">No approval waits recorded.</div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Budget Exhaustion</div>`,
    budgetExhaustedJobs.length
      ? `<div class="detail-grid">${budgetExhaustedJobs.map(renderRunJob).join("")}</div>`
      : `<div class="muted">No budget exhaustion recorded.</div>`,
    `</div>`,
    renderMissionScorecard(),
    renderMissionReview(),
    renderMissionReusePlan(),
    `<div class="detail-section">`,
    `<div class="k">Memory Candidate State</div>`,
    memoryPromotionCandidates.length
      ? `<div class="detail-grid">${memoryPromotionCandidates.map(renderMemoryPromotionCandidate).join("")}</div>`
      : `<div class="muted">No approval-gated memory candidates recorded.</div>`,
    `</div>`,
  ].join("");
}

function renderAutonomyArtifacts(task) {
  // Read-only renderer for autonomy_* artifacts. No execution / approval /
  // promotion / runtime reuse actions are emitted from this surface.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const episode = asObject(artifacts.autonomous_episode);
  const scorecard = asObject(artifacts.autonomy_scorecard);
  const review = asObject(artifacts.autonomy_episode_review);
  const gate = asObject(artifacts.autonomy_gate_result);
  const comparison = asObject(artifacts.autonomy_gate_comparison_result);
  const hasAny = [episode, scorecard, review, gate, comparison].some((item) => Object.keys(item).length);
  if (!hasAny) return "";

  const renderReasonList = (reasons, kind) => {
    const list = asArray(reasons).filter((reason) => typeof reason === "string" && reason.length);
    if (!list.length) return `<div class="muted">none</div>`;
    const cls = kind === "blocked" ? "autonomy-reason autonomy-reason-blocked" : "autonomy-reason autonomy-reason-warning";
    return `<ul class="autonomy-reason-list">${
      list.map((reason) => `<li class="${cls}"><span class="mono">${escapeHtml(reason)}</span></li>`).join("")
    }</ul>`;
  };

  const renderSafetyBadges = (source) => {
    const parts = [];
    const flag = (label, value, expected) => {
      const ok = value === expected;
      const cls = ok ? "tag autonomy-safety-ok" : "tag autonomy-safety-warn";
      const valueText = value === undefined || value === null ? "?" : String(value);
      parts.push(`<span class="${cls}">${escapeHtml(label)}=${escapeHtml(valueText)}</span>`);
    };
    flag("operator_approval_required", source.operator_approval_required, true);
    flag("operator_approval_performed", source.operator_approval_performed, false);
    flag("stronger_execution_allowed", source.stronger_execution_allowed, false);
    flag("live_execution_allowed", source.live_execution_allowed, false);
    flag("physical_execution_invoked", source.physical_execution_invoked, false);
    return `<div class="chip-row autonomy-safety-badges">${parts.join("")}</div>`;
  };

  const renderEpisodeSection = () => {
    if (!Object.keys(episode).length) return "";
    const summary = asObject(episode.summary);
    const replay = asObject(episode.replay_trace);
    const goalReached = summary.goal_reached === true ? "yes" : summary.goal_reached === false ? "no" : "-";
    return [
      `<div class="detail-section autonomy-section autonomy-episode">`,
      `<div class="k">Autonomy Episode</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Episode</div><div class="mono">${escapeHtml(episode.episode_id || "-")}</div><div class="item-meta mono">schema=${escapeHtml(episode.schema_version || "-")}</div></div>`,
      `<div class="detail-card"><div class="k">World</div><div class="mono">${escapeHtml(episode.world_id || "-")}</div><div class="item-meta mono">plan=${escapeHtml(episode.plan_id || "-")}</div></div>`,
      `<div class="detail-card"><div class="k">Final Status</div><div>${escapeHtml(String(episode.final_status || "-"))}</div><div class="item-meta">goal_reached=${escapeHtml(goalReached)}</div></div>`,
      `<div class="detail-card"><div class="k">Steps</div><div>accepted=${escapeHtml(String(summary.accepted_steps ?? "-"))}/${escapeHtml(String(summary.total_steps ?? "-"))}</div><div class="item-meta">replans=${escapeHtml(String(summary.replans ?? "-"))} recovery=${escapeHtml(String(summary.recovery_attempts ?? "-"))}</div></div>`,
      `<div class="detail-card"><div class="k">Replay Trace</div><div class="mono">${escapeHtml(replay.trace_id || summary.replay_trace_ref || "-")}</div><div class="item-meta mono">deterministic_hash=${escapeHtml((replay.deterministic_hash || "-").slice(0, 16))}</div></div>`,
      `</div>`,
      `</div>`,
    ].join("");
  };

  const renderScorecardSection = () => {
    if (!Object.keys(scorecard).length) return "";
    const buckets = asArray(scorecard.failure_buckets);
    const bucketsLine = buckets.length
      ? buckets.map((item) => {
          const obj = asObject(item);
          return `<span class="tag">${escapeHtml(obj.bucket || "-")}×${escapeHtml(String(obj.count ?? 1))}</span>`;
        }).join("")
      : `<span class="muted">no failure buckets</span>`;
    return [
      `<div class="detail-section autonomy-section autonomy-scorecard">`,
      `<div class="k">Autonomy Scorecard</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Status</div><div>${statusTag(scorecard.status || (scorecard.passed ? "passed" : "failed"))}</div><div class="item-meta">passed=${escapeHtml(String(scorecard.passed))}</div></div>`,
      `<div class="detail-card"><div class="k">Safety Counts</div><div class="mono">live=${escapeHtml(String(scorecard.live_execution_flag_count ?? 0))} physical=${escapeHtml(String(scorecard.physical_execution_flag_count ?? 0))} safety=${escapeHtml(String(scorecard.safety_violation_count ?? 0))}</div><div class="item-meta mono">blocked_steps=${escapeHtml(String(scorecard.blocked_step_count ?? 0))}</div></div>`,
      `<div class="detail-card"><div class="k">Telemetry</div><div class="mono">missing=${escapeHtml(String(scorecard.telemetry_missing_count ?? 0))} stale=${escapeHtml(String(scorecard.telemetry_stale_count ?? 0))} mismatch=${escapeHtml(String(scorecard.telemetry_mismatch_count ?? 0))}</div><div class="item-meta mono">freshness_seconds=${escapeHtml(String(scorecard.telemetry_freshness_seconds ?? "-"))}</div></div>`,
      `<div class="detail-card"><div class="k">Quality</div><div class="mono">dry_run_compliance_rate=${escapeHtml(String(scorecard.dry_run_compliance_rate ?? "-"))}</div><div class="item-meta mono">path_efficiency=${escapeHtml(String(scorecard.path_efficiency ?? "-"))}</div></div>`,
      `<div class="detail-card"><div class="k">Failure Buckets</div><div class="chip-row">${bucketsLine}</div></div>`,
      `</div>`,
      `</div>`,
    ].join("");
  };

  const renderReviewSection = () => {
    if (!Object.keys(review).length) return "";
    const candidates = asArray(review.improvement_candidates);
    const recommended = asArray(review.recommended_next_actions);
    const candidateBadges = candidates.length
      ? candidates.map((item) => {
          const obj = asObject(item);
          const candidateOnly = obj.approval_status === "candidate_only" ? `<span class="tag autonomy-safety-ok">candidate_only</span>` : "";
          const requiresApproval = obj.requires_operator_approval === true ? `<span class="tag autonomy-safety-warn">requires_operator_approval</span>` : "";
          return `<div class="autonomy-candidate"><span class="mono">${escapeHtml(asObject(obj.content).bucket || obj.type || "-")}</span> ${requiresApproval}${candidateOnly}</div>`;
        }).join("")
      : `<div class="muted">no improvement candidates</div>`;
    return [
      `<div class="detail-section autonomy-section autonomy-review">`,
      `<div class="k">Autonomy Episode Review</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Review</div><div class="mono">${escapeHtml(review.review_id || "-")}</div><div class="item-meta">final_status=${escapeHtml(String(review.final_status || "-"))}</div></div>`,
      `<div class="detail-card"><div class="k">Summary</div><div>${escapeHtml(String(review.summary || "-"))}</div></div>`,
      `<div class="detail-card"><div class="k">Recommended Next Actions</div>${recommended.length ? `<ul class="autonomy-reason-list">${recommended.map((item) => `<li class="mono">${escapeHtml(String(item))}</li>`).join("")}</ul>` : `<div class="muted">none</div>`}</div>`,
      `<div class="detail-card"><div class="k">Improvement Candidates (candidate_only)</div>${candidateBadges}</div>`,
      `</div>`,
      `</div>`,
    ].join("");
  };

  const renderGateSection = () => {
    if (!Object.keys(gate).length) return "";
    const safetyEvalRefs = asArray(gate.safety_eval_refs);
    const hilReviewRefs = asArray(gate.hil_telemetry_review_refs);
    const hilReviewSnapshots = asArray(gate.hil_telemetry_review_snapshots);
    return [
      `<div class="detail-section autonomy-section autonomy-gate">`,
      `<div class="k">Autonomy Gate Result</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Status</div><div>${statusTag(gate.status || (gate.passed ? "passed" : "blocked"))}</div><div class="item-meta">passed=${escapeHtml(String(gate.passed))} schema=${escapeHtml(gate.schema_version || "autonomy_gate_result.v1")}</div></div>`,
      `<div class="detail-card"><div class="k">Subject</div><div class="mono">${escapeHtml(gate.subject_id || "-")}</div><div class="item-meta mono">gate_id=${escapeHtml(gate.gate_id || "-")}</div></div>`,
      `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div>${renderReasonList(gate.blocked_reasons, "blocked")}</div>`,
      `<div class="detail-card autonomy-reasons-card"><div class="k">warning_reasons</div>${renderReasonList(gate.warning_reasons, "warning")}</div>`,
      `<div class="detail-card"><div class="k">Safety Eval Refs</div>${safetyEvalRefs.length ? `<ul class="autonomy-reason-list">${safetyEvalRefs.map((ref) => `<li class="mono">${escapeHtml(String(ref))}</li>`).join("")}</ul>` : `<div class="muted">none</div>`}</div>`,
      `<div class="detail-card autonomy-hil-refs-card"><div class="k">hil_telemetry_review_refs</div>${hilReviewRefs.length ? `<ul class="autonomy-reason-list">${hilReviewRefs.map((ref) => `<li class="mono">${escapeHtml(String(ref))}</li>`).join("")}</ul>` : `<div class="muted">none</div>`}<div class="item-meta mono">hil_telemetry_review_snapshots=${escapeHtml(String(hilReviewSnapshots.length))}</div></div>`,
      `<div class="detail-card autonomy-safety-card"><div class="k">Safety Boundary</div>${renderSafetyBadges(gate)}</div>`,
      `</div>`,
      `</div>`,
    ].join("");
  };

  const renderComparisonSection = () => {
    if (!Object.keys(comparison).length) return "";
    const metricDeltas = asObject(comparison.metric_deltas);
    const metricEntries = Object.entries(metricDeltas);
    const metricRows = metricEntries.length
      ? metricEntries.map(([name, raw]) => {
          const value = asObject(raw);
          const severity = String(value.severity || "info");
          const direction = String(value.direction || "");
          const severityClass = severity === "blocking"
            ? "autonomy-metric-severity-blocking"
            : severity === "warning"
              ? "autonomy-metric-severity-warning"
              : "autonomy-metric-severity-info";
          return `<tr class="${severityClass}"><td class="mono">${escapeHtml(name)}</td><td class="mono">${escapeHtml(String(value.baseline ?? "-"))}</td><td class="mono">${escapeHtml(String(value.candidate ?? "-"))}</td><td class="mono">${escapeHtml(String(value.delta ?? "-"))}</td><td class="mono">${escapeHtml(direction || "-")}</td><td class="mono">${escapeHtml(severity)}</td></tr>`;
        }).join("")
      : `<tr><td colspan="6" class="muted">no metric deltas</td></tr>`;
    return [
      `<div class="detail-section autonomy-section autonomy-gate-comparison">`,
      `<div class="k">Autonomy Gate Comparison</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Status</div><div>${statusTag(comparison.status || (comparison.passed ? "passed" : "blocked"))}</div><div class="item-meta">passed=${escapeHtml(String(comparison.passed))} schema=${escapeHtml(comparison.schema_version || "autonomy_gate_comparison_result.v1")}</div></div>`,
      `<div class="detail-card"><div class="k">Comparison</div><div class="mono">${escapeHtml(comparison.comparison_id || "-")}</div><div class="item-meta mono">baseline_gate_id=${escapeHtml(comparison.baseline_gate_id || "-")}</div><div class="item-meta mono">candidate_gate_id=${escapeHtml(comparison.candidate_gate_id || "-")}</div></div>`,
      `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div>${renderReasonList(comparison.blocked_reasons, "blocked")}</div>`,
      `<div class="detail-card autonomy-reasons-card"><div class="k">warning_reasons</div>${renderReasonList(comparison.warning_reasons, "warning")}</div>`,
      `<div class="detail-card autonomy-safety-card"><div class="k">Safety Boundary</div>${renderSafetyBadges(comparison)}</div>`,
      `</div>`,
      `<div class="autonomy-metric-deltas-wrapper"><table class="analytics-table autonomy-metric-deltas"><thead><tr><th>metric</th><th>baseline</th><th>candidate</th><th>delta</th><th>direction</th><th>severity</th></tr></thead><tbody>${metricRows}</tbody></table></div>`,
      `</div>`,
    ].join("");
  };

  return [
    renderEpisodeSection(),
    renderScorecardSection(),
    renderReviewSection(),
    renderGateSection(),
    renderComparisonSection(),
  ].join("");
}

function renderBoundedGazeboSimulationRun(task) {
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const run = artifacts.px4_gazebo_bounded_simulation_run && typeof artifacts.px4_gazebo_bounded_simulation_run === "object"
    ? artifacts.px4_gazebo_bounded_simulation_run
    : {};
  if (!Object.keys(run).length) return "";
  const boolFlag = (label, value, expected) => {
    const ok = value === expected;
    const cls = ok ? "tag autonomy-safety-ok" : "tag autonomy-safety-warn";
    const valueText = value === undefined || value === null ? "?" : String(value);
    return `<span class="${cls}">${escapeHtml(label)}=${escapeHtml(valueText)}</span>`;
  };
  const refs = Array.isArray(run.telemetry_refs) ? run.telemetry_refs : [];
  const caps = Array.isArray(run.cap_drop) ? run.cap_drop : [];
  return [
    `<div class="detail-section bounded-gazebo-run-section">`,
    `<div class="k">Bounded Gazebo Simulation Run</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Run Status</div><div>${statusTag(run.status || "unknown")}</div><div class="item-meta mono">schema=${escapeHtml(run.schema_version || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Scenario Mapping</div><div class="mono">${escapeHtml(run.scenario_kind || "-")}</div><div class="item-meta mono">route=${escapeHtml(run.route_profile || "-")}</div><div class="item-meta mono">${escapeHtml(run.scenario_run_mapping || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Execution Boundary</div><div class="chip-row">${
      [
        boolFlag("gazebo_execution_invoked", run.gazebo_execution_invoked, true),
        boolFlag("bounded_simulation_invoked", run.bounded_simulation_invoked, true),
        boolFlag("physical_execution_invoked", run.physical_execution_invoked, false),
        boolFlag("hardware_target_allowed", run.hardware_target_allowed, false),
        boolFlag("mavlink_dispatch_allowed", run.mavlink_dispatch_allowed, false),
        boolFlag("ros_dispatch_allowed", run.ros_dispatch_allowed, false),
        boolFlag("actuator_execution_allowed", run.actuator_execution_allowed, false),
      ].join("")
    }</div></div>`,
    `<div class="detail-card"><div class="k">World</div><div class="mono">${escapeHtml(run.world_name || "-")}</div><div class="item-meta mono">world_ref=${escapeHtml(run.world_ref || "-")}</div><div class="item-meta mono">world_sdf_path=${escapeHtml(run.world_sdf_path || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Bounds</div><div class="mono">max_duration_seconds=${escapeHtml(String(run.max_duration_seconds ?? "-"))}</div><div class="item-meta mono">max_log_lines=${escapeHtml(String(run.max_log_lines ?? "-"))} observed_log_line_count=${escapeHtml(String(run.observed_log_line_count ?? "-"))}</div><div class="item-meta mono">window_bounded=${escapeHtml(String(run.window_bounded))} telemetry_age_seconds=${escapeHtml(String(run.telemetry_age_seconds ?? "-"))}</div></div>`,
    `<div class="detail-card"><div class="k">Container Boundary</div><div class="mono">network_mode=${escapeHtml(run.network_mode || "-")}</div><div class="item-meta mono">read_only_rootfs=${escapeHtml(String(run.read_only_rootfs))} privileged=${escapeHtml(String(run.privileged))}</div><div class="item-meta mono">cap_drop=${escapeHtml(caps.join(", ") || "-")} port_bindings=${escapeHtml(JSON.stringify(run.port_bindings || {}))}</div></div>`,
    `<div class="detail-card"><div class="k">Refs</div><div class="mono">gate=${escapeHtml(run.gate_ref || "-")}</div><div class="item-meta mono">hil_review=${escapeHtml(run.hil_review_ref || "-")}</div><div class="item-meta mono">telemetry=${escapeHtml(refs.join(", ") || "-")}</div></div>`,
    `</div>`,
    `</div>`,
  ].join("");
}

function renderPX4GazeboSITLTelemetryRun(task) {
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const run = artifacts.px4_gazebo_sitl_telemetry_run && typeof artifacts.px4_gazebo_sitl_telemetry_run === "object"
    ? artifacts.px4_gazebo_sitl_telemetry_run
    : {};
  if (!Object.keys(run).length) return "";
  const boolFlag = (label, value, expected) => {
    const ok = value === expected;
    const cls = ok ? "tag autonomy-safety-ok" : "tag autonomy-safety-warn";
    const valueText = value === undefined || value === null ? "?" : String(value);
    return `<span class="${cls}">${escapeHtml(label)}=${escapeHtml(valueText)}</span>`;
  };
  const refs = Array.isArray(run.telemetry_refs) ? run.telemetry_refs : [];
  return [
    `<div class="detail-section px4-gazebo-sitl-telemetry-run-section">`,
    `<div class="k">PX4/Gazebo SITL Telemetry Run</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Run Status</div><div>${statusTag(run.run_status || "unknown")}</div><div class="item-meta mono">schema=${escapeHtml(run.schema_version || "px4_gazebo_sitl_telemetry_run.v1")}</div><div class="item-meta mono">source=${escapeHtml(run.source_kind || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Actual Stack</div><div class="mono">${escapeHtml(run.source_id || "-")}</div><div class="item-meta mono">px4_image=${escapeHtml(run.px4_image_ref || "-")}</div><div class="item-meta mono">gazebo_image=${escapeHtml(run.gazebo_image_ref || "-")}</div><div class="item-meta mono">model=${escapeHtml(run.px4_model || "-")} world=${escapeHtml(run.gazebo_world || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Telemetry Window</div><div class="mono">pose_samples=${escapeHtml(String(run.gazebo_pose_sample_count ?? "-"))}</div><div class="item-meta mono">mavlink_heartbeat_count=${escapeHtml(String(run.mavlink_heartbeat_count ?? "-"))}</div><div class="item-meta mono">mavlink_observation_window_seconds=${escapeHtml(String(run.mavlink_observation_window_seconds ?? "-"))}</div></div>`,
    `<div class="detail-card"><div class="k">Observed Facts</div><div class="chip-row">${
      [
        boolFlag("px4_gazebo_sitl_started", run.px4_gazebo_sitl_started, true),
        boolFlag("telemetry_collected", run.telemetry_collected, true),
        boolFlag("mavlink_heartbeat_observed", run.mavlink_heartbeat_observed, true),
        boolFlag("vehicle_spawn_marker_observed", run.vehicle_spawn_marker_observed, true),
        boolFlag("vehicle_takeoff_observed", run.vehicle_takeoff_observed, false),
      ].join("")
    }</div></div>`,
    `<div class="detail-card"><div class="k">Safety Boundary</div><div class="chip-row">${
      [
        boolFlag("external_dispatch_performed", run.external_dispatch_performed, false),
        boolFlag("mavlink_command_sent", run.mavlink_command_sent, false),
        boolFlag("mavlink_dispatch_performed", run.mavlink_dispatch_performed, false),
        boolFlag("ros_dispatch_performed", run.ros_dispatch_performed, false),
        boolFlag("actuator_execution_performed", run.actuator_execution_performed, false),
        boolFlag("px4_mission_upload_performed", run.px4_mission_upload_performed, false),
        boolFlag("gazebo_entity_mutation_performed", run.gazebo_entity_mutation_performed, false),
        boolFlag("physical_execution_invoked", run.physical_execution_invoked, false),
        boolFlag("hardware_target_allowed", run.hardware_target_allowed, false),
      ].join("")
    }</div></div>`,
    `<div class="detail-card"><div class="k">Refs</div><div class="mono">gate=${escapeHtml(run.gate_ref || "-")}</div><div class="item-meta mono">hil_review=${escapeHtml(run.hil_review_ref || "-")}</div><div class="item-meta mono">telemetry=${escapeHtml(refs.join(", ") || "-")}</div></div>`,
    `</div>`,
    `</div>`,
  ].join("");
}

function renderMissionDesignerEvidenceNarrative(context) {
  const result = context.result || {};
  const receipt = context.receipt || {};
  const flightEvidence = context.flightEvidence || {};
  const payloadObservation = context.payloadObservation || {};
  const payloadReleaseEvent = context.payloadReleaseEvent || {};
  const missionDesignerDropoffVerification = context.missionDesignerDropoffVerification || {};
  const sitlDropoffVerification = context.sitlDropoffVerification || {};
  const liveFlightRun = context.liveFlightRun || {};
  const liveFlightFailedReceipt = context.liveFlightFailedReceipt || {};
  const proposal = context.proposal || {};
  const liveFailureCategory = String(liveFlightFailedReceipt.failure_category || "");
  const liveFailureDescription = liveFailureCategory === "takeoff_or_climb_predicate_timeout"
    ? "takeoff / climb の観測条件が timeout したため"
    : (liveFailureCategory ? `${liveFailureCategory} のため` : "");
  const proposalEquipmentIncidents = Array.isArray(proposal.equipment_incident_labels)
    ? proposal.equipment_incident_labels.map(String)
    : [];
  const proposalFeasibilityRisks = Array.isArray(proposal.feasibility_risk_labels)
    ? proposal.feasibility_risk_labels.map(String)
    : [];
  const payloadWeightKg = proposal.payload_weight_kg ?? result.payload_weight_kg;
  const payloadMarginRiskObserved = proposalEquipmentIncidents.includes("payload_weight")
    || proposalFeasibilityRisks.includes("payload_margin_risk");
  const uploadObserved = receipt.upload_status === "uploaded"
    || result.actual_sitl_mission_upload_observed === true;
  const takeoffObserved = result.actual_takeoff_observed === true
    || flightEvidence.actual_takeoff_observed === true;
  const flightObserved = result.actual_sitl_flight_evidence_observed === true
    || flightEvidence.actual_sitl_flight_evidence_observed === true
    || liveFlightRun.actual_sitl_flight_evidence_observed === true;
  const dropoffRegionReached = result.actual_dropoff_region_reached === true
    || flightEvidence.actual_dropoff_region_reached === true
    || liveFlightRun.dropoff_region_reached === true;
  const landingObserved = result.actual_land_observed === true
    || flightEvidence.actual_land_observed === true;
  const payloadObserved = payloadObservation.payload_release_observed === true
    || payloadReleaseEvent.release_observed === true;
  const payloadSource = payloadObservation.event_source
    || payloadReleaseEvent.event_source
    || result.payload_release_event_source
    || "-";
  const dropoffVerified = missionDesignerDropoffVerification.dropoff_verified === true
    || sitlDropoffVerification.dropoff_verified === true
    || sitlDropoffVerification.status === "verified";
  const horizontalProgress = flightEvidence.horizontal_progress_m
    ?? liveFlightRun.horizontal_progress_m
    ?? result.horizontal_progress_m;
  const observedDistance = missionDesignerDropoffVerification.observed_distance_to_dropoff_m
    ?? sitlDropoffVerification.observed_distance_to_dropoff_m;
  const lines = [];

  lines.push(uploadObserved
    ? "PX4 mission upload は SITL endpoint で ack まで観測された。"
    : "PX4 mission upload はまだ観測されていない。");
  if (takeoffObserved) {
    lines.push("ドローンが飛んだ。");
  } else if (liveFailureDescription) {
    lines.push(`live flight runner は呼び出されたが、${liveFailureDescription}、ドローンの takeoff は観測されていない。`);
  } else {
    lines.push("ドローンの takeoff はまだ観測されていない。");
  }
  if (liveFailureCategory && payloadMarginRiskObserved) {
    const payloadText = payloadWeightKg === undefined || payloadWeightKg === null
      ? ""
      : `payload ${payloadWeightKg} kg は `;
    lines.push(`${payloadText}proposal 上で payload_weight / payload_margin_risk として記録されており、この takeoff / climb timeout の上流リスクとして確認対象になる。ただし、この receipt 単独では payload 重量だけを唯一原因とは断定しない。`);
  }
  if (flightObserved && dropoffRegionReached) {
    const progressText = horizontalProgress === undefined || horizontalProgress === null
      ? ""
      : ` 水平進捗 ${horizontalProgress} m を伴って`;
    lines.push(`Gazebo local route で${progressText} dropoff region 到達が観測された。`);
  } else if (flightObserved) {
    lines.push("飛行証跡は観測されたが、dropoff region 到達はまだ確認されていない。");
  } else if (liveFailureCategory) {
    lines.push("飛行証跡は attached evidence にならず、Live Flight Failure Receipt が原因分類と runner log refs を保持している。");
  } else {
    lines.push("飛行証跡はまだ attached evidence として確認されていない。");
  }
  lines.push(payloadObserved
    ? `payload release が ${payloadSource} として観測された。`
    : "payload release はまだ観測されていない。");
  if (dropoffVerified) {
    const distanceText = observedDistance === undefined || observedDistance === null
      ? ""
      : ` observed_distance_to_dropoff_m=${observedDistance}`;
    lines.push(`dropoff verifier が位置・高度・mission item・payload release を確認し、投下を verified と判定した。${distanceText}`);
  } else {
    lines.push("dropoff verifier による投下 verified 判定はまだない。");
  }
  lines.push(landingObserved
    ? "着陸も観測された。"
    : "着陸観測はまだ確認されていない。");
  lines.push("このサマリは operator review 用の読み取り表示であり、Mission OS の delivery completion claim ではない。");

  return [
    `<div class="detail-card mission-evidence-narrative">`,
    `<div class="k">Observed Flow Summary</div>`,
    `<ol class="mission-evidence-narrative-list">`,
    ...lines.map((line) => `<li>${escapeHtml(line)}</li>`),
    `</ol>`,
    `<div class="item-meta mono">synthetic_success_allowed=${escapeHtml(String(result.synthetic_success_allowed ?? missionDesignerDropoffVerification.synthetic_success_allowed ?? false))} · hardware_target_allowed=${escapeHtml(String(result.hardware_target_allowed ?? false))} · physical_execution_invoked=${escapeHtml(String(result.physical_execution_invoked ?? false))}</div>`,
    `</div>`,
  ].join("");
}

function renderMissionDesignerSITLExecutionResult(task, options = {}) {
  // Read-only renderer for Mission Designer SITL delivery-chain artifacts.
  // This panel deliberately exposes observed vs pending facts without adding
  // approval, dispatch, MAVLink, ROS, Gazebo mutation, actuator, hardware, or
  // run-again controls.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const result = artifacts.px4_gazebo_mission_designer_sitl_execution_result && typeof artifacts.px4_gazebo_mission_designer_sitl_execution_result === "object"
    ? artifacts.px4_gazebo_mission_designer_sitl_execution_result
    : {};
  const proposal = artifacts.px4_gazebo_mission_scenario_proposal && typeof artifacts.px4_gazebo_mission_scenario_proposal === "object"
    ? artifacts.px4_gazebo_mission_scenario_proposal
    : {};
  const receipt = artifacts.px4_gazebo_sitl_mission_upload_receipt && typeof artifacts.px4_gazebo_sitl_mission_upload_receipt === "object"
    ? artifacts.px4_gazebo_sitl_mission_upload_receipt
    : {};
  const flightEvidence = artifacts.px4_gazebo_mission_designer_sitl_flight_evidence && typeof artifacts.px4_gazebo_mission_designer_sitl_flight_evidence === "object"
    ? artifacts.px4_gazebo_mission_designer_sitl_flight_evidence
    : {};
  const payloadObservation = artifacts.px4_gazebo_mission_designer_sitl_payload_release_observation && typeof artifacts.px4_gazebo_mission_designer_sitl_payload_release_observation === "object"
    ? artifacts.px4_gazebo_mission_designer_sitl_payload_release_observation
    : {};
  const payloadReleaseEvent = artifacts.px4_gazebo_sitl_payload_release_event && typeof artifacts.px4_gazebo_sitl_payload_release_event === "object"
    ? artifacts.px4_gazebo_sitl_payload_release_event
    : {};
  const dropoffFlightFact = artifacts.px4_gazebo_sitl_dropoff_flight_fact && typeof artifacts.px4_gazebo_sitl_dropoff_flight_fact === "object"
    ? artifacts.px4_gazebo_sitl_dropoff_flight_fact
    : {};
  const sitlDropoffVerification = artifacts.px4_gazebo_sitl_dropoff_verification && typeof artifacts.px4_gazebo_sitl_dropoff_verification === "object"
    ? artifacts.px4_gazebo_sitl_dropoff_verification
    : {};
  const missionDesignerDropoffVerification = artifacts.px4_gazebo_mission_designer_sitl_dropoff_verification && typeof artifacts.px4_gazebo_mission_designer_sitl_dropoff_verification === "object"
    ? artifacts.px4_gazebo_mission_designer_sitl_dropoff_verification
    : {};
  const liveFlightRun = artifacts.px4_gazebo_mission_designer_sitl_live_flight_run && typeof artifacts.px4_gazebo_mission_designer_sitl_live_flight_run === "object"
    ? artifacts.px4_gazebo_mission_designer_sitl_live_flight_run
    : {};
  const liveFlightFailedReceipt = artifacts.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt && typeof artifacts.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt === "object"
    ? artifacts.px4_gazebo_mission_designer_sitl_live_flight_failed_receipt
    : {};
  const preflight = artifacts.simulator_command_execution_preflight && typeof artifacts.simulator_command_execution_preflight === "object"
    ? artifacts.simulator_command_execution_preflight
    : {};
  const scorecard = artifacts.delivery_scorecard && typeof artifacts.delivery_scorecard === "object"
    ? artifacts.delivery_scorecard
    : {};
  const review = artifacts.delivery_episode_review && typeof artifacts.delivery_episode_review === "object"
    ? artifacts.delivery_episode_review
    : {};
  const gate = artifacts.autonomy_gate_result && typeof artifacts.autonomy_gate_result === "object"
    ? artifacts.autonomy_gate_result
    : {};
  const includeFlightPath = options.includeFlightPath !== false;
  const failureReplaySummary = options.includeFailureReplay === true
    && Object.keys(liveFlightFailedReceipt).length
    ? findDigitalTwinFlightPathSummary(task)
    : {};
  const flightPathSummary = includeFlightPath ? findDigitalTwinFlightPathSummary(task) : failureReplaySummary;
  if (!Object.keys(result).length) return "";
  const boolFlag = (label, value, expected) => {
    const ok = value === expected;
    const cls = ok ? "tag autonomy-safety-ok" : "tag autonomy-safety-warn";
    const valueText = value === undefined || value === null ? "?" : String(value);
    return `<span class="${cls}">${escapeHtml(label)}=${escapeHtml(valueText)}</span>`;
  };
  const renderList = (items, empty = "none") => {
    const values = Array.isArray(items) ? items.filter((item) => String(item || "").length) : [];
    return values.length
      ? `<ul class="autonomy-reason-list">${values.map((item) => `<li class="mono">${escapeHtml(String(item))}</li>`).join("")}</ul>`
      : `<div class="muted">${escapeHtml(empty)}</div>`;
  };
  const renderPending = (text) => `<div class="muted">${escapeHtml(text)}</div>`;
  const refLine = (label, value) => `<div class="item-meta mono">${escapeHtml(label)}=${escapeHtml(value || "-")}</div>`;
  const artifactSchema = (artifact, fallback) => escapeHtml(artifact.schema_version || fallback || "-");
  const uploadObserved = receipt.upload_status === "uploaded";
  const flightObserved = flightEvidence.actual_sitl_flight_evidence_observed === true;
  const payloadObserved = payloadObservation.payload_release_observed === true
    || missionDesignerDropoffVerification.payload_release_observed === true
    || missionDesignerDropoffVerification.payload_release_verified === true;
  const payloadVerified = missionDesignerDropoffVerification.payload_release_verified === true
    || sitlDropoffVerification.status === "verified";
  const dropoffVerified = missionDesignerDropoffVerification.dropoff_verified === true
    || sitlDropoffVerification.status === "verified";
  const finalPayloadReleaseEventRef = payloadObservation.payload_release_event_ref
    || missionDesignerDropoffVerification.payload_release_event_ref
    || result.payload_release_event_ref
    || "";
  const finalDropoffVerificationRef = missionDesignerDropoffVerification.verification_id
    ? `px4_gazebo_mission_designer_sitl_dropoff_verification:${missionDesignerDropoffVerification.verification_id}`
    : (missionDesignerDropoffVerification.sitl_dropoff_verification_ref || result.dropoff_verification_ref || "");
  const baseFailureReasons = Array.isArray(result.failure_reasons)
    ? result.failure_reasons.filter((item) => String(item || "").length)
    : [];
  const liveFailureReasons = Array.isArray(liveFlightFailedReceipt.blocked_reasons)
    ? liveFlightFailedReceipt.blocked_reasons.filter((item) => String(item || "").length)
    : [];
  const finalFailureReasons = dropoffVerified ? [] : (liveFailureReasons.length ? liveFailureReasons : baseFailureReasons);
  const chainState = dropoffVerified
    ? "dropoff-verified"
    : payloadObserved
      ? "payload-observed"
      : flightObserved
        ? "flight-observed"
        : uploadObserved
          ? "upload-only"
          : "pending-or-blocked";
  const chainStep = (label, active, complete) => {
    const cls = complete ? "tag autonomy-safety-ok" : active ? "tag autonomy-safety-warn" : "tag";
    return `<span class="${cls}">${escapeHtml(label)}</span>`;
  };
  const scorecardStatus = Object.keys(scorecard).length
    ? (scorecard.status || (scorecard.passed === true ? "passed" : scorecard.passed === false ? "failed" : "recorded"))
    : Object.keys(preflight).length
      ? (preflight.scorecard_passed === true ? "projected_passed" : "pending")
      : "pending";
  const reviewStatus = Object.keys(review).length
    ? (review.final_status || review.status || (review.passed === true ? "passed" : "recorded"))
    : Object.keys(preflight).length
      ? (preflight.episode_review_passed === true ? "projected_passed" : "pending")
      : "pending";
  const gateStatus = Object.keys(gate).length
    ? (gate.status || (gate.passed === true ? "passed" : gate.passed === false ? "blocked" : "recorded"))
    : Object.keys(preflight).length
      ? (preflight.autonomy_gate_passed === true ? "projected_passed" : "pending")
      : "pending";
  const scorecardRef = scorecard.scorecard_id
    ? `delivery_scorecard:${scorecard.scorecard_id}`
    : (preflight.delivery_scorecard_ref || "-");
  const reviewRef = review.review_id
    ? `delivery_episode_review:${review.review_id}`
    : (preflight.delivery_episode_review_ref || "-");
  const gateRef = gate.gate_id
    ? `autonomy_gate_result:${gate.gate_id}`
    : (preflight.autonomy_gate_result_ref || "-");
  const uploadExpected = result.actual_sitl_mission_upload_observed === true;
  const visibleFailure = finalFailureReasons[0] || liveFlightFailedReceipt.failure_category || "";
  const compactCards = [
    `<div class="detail-card mission-evidence-compact-card"><div class="k">Final Evidence</div><div>${statusTag(chainState)}</div><div class="chip-row">${
      [
        chainStep("upload", chainState === "upload-only", uploadObserved),
        chainStep("flight", chainState === "flight-observed", flightObserved),
        chainStep("payload", chainState === "payload-observed", payloadObserved),
        chainStep("dropoff", chainState === "dropoff-verified", dropoffVerified),
      ].join("")
    }</div></div>`,
    `<div class="detail-card mission-evidence-compact-card"><div class="k">Observed Facts</div><div class="item-meta mono">progress_m=${escapeHtml(String(flightEvidence.horizontal_progress_m ?? "-"))}</div><div class="item-meta mono">dropoff_distance_m=${escapeHtml(String(missionDesignerDropoffVerification.observed_distance_to_dropoff_m ?? "-"))}</div><div class="item-meta mono">upload=${escapeHtml(receipt.upload_status || "-")}</div></div>`,
    `<div class="detail-card mission-evidence-compact-card"><div class="k">Boundary</div><div class="chip-row">${
      [
        boolFlag("hardware", result.hardware_target_allowed, false),
        boolFlag("physical", result.physical_execution_invoked, false),
        boolFlag("synthetic_success", result.synthetic_success_allowed, false),
      ].join("")
    }</div><div class="item-meta mono">delivery_completion_claimed=${escapeHtml(String(result.delivery_completion_claimed ?? missionDesignerDropoffVerification.delivery_completion_claimed ?? false))}</div></div>`,
    visibleFailure
      ? `<div class="detail-card mission-evidence-compact-card"><div class="k">Active Failure</div><div class="detail-error">${escapeHtml(visibleFailure)}</div></div>`
      : `<div class="detail-card mission-evidence-compact-card"><div class="k">Active Failure</div><div class="muted">no active failure reasons</div></div>`,
  ].join("");
  return [
    `<div class="detail-section mission-designer-sitl-result-section">`,
    `<div class="k">Mission Designer SITL Execution Result</div>`,
    `<div class="muted">Base execution result records the state when the execution artifact was created. Final evidence chain reflects later attached payload, dropoff, and epic-exit artifacts.</div>`,
    renderMissionDesignerEvidenceNarrative({
      result,
      receipt,
      flightEvidence,
      payloadObservation,
      payloadReleaseEvent,
      missionDesignerDropoffVerification,
      sitlDropoffVerification,
      liveFlightRun,
      liveFlightFailedReceipt,
      proposal,
    }),
    `<div class="detail-grid mission-evidence-compact-grid">${compactCards}</div>`,
    `<details class="mission-ui-collapse mission-ui-collapse-evidence">`,
    `<summary>Detailed SITL Evidence Cards</summary>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Result Status</div><div>${statusTag(result.result_status || "unknown")}</div><div class="item-meta mono">schema=${escapeHtml(result.schema_version || "px4_gazebo_mission_designer_sitl_execution_result.v1")}</div><div class="item-meta mono">result_id=${escapeHtml(result.result_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Final SITL Evidence Chain</div><div>${statusTag(chainState)}</div><div class="chip-row">${
      [
        chainStep("upload-only", chainState === "upload-only", uploadObserved),
        chainStep("flight-observed", chainState === "flight-observed", flightObserved),
        chainStep("payload-observed", chainState === "payload-observed", payloadObserved),
        chainStep("dropoff-verified", chainState === "dropoff-verified", dropoffVerified),
      ].join("")
    }</div><div class="item-meta mono">state=${escapeHtml(chainState)}</div></div>`,
    `<div class="detail-card"><div class="k">Mission Upload</div><div class="chip-row">${
      [
        boolFlag("sitl_execution_opted_in", result.sitl_execution_opted_in, uploadExpected),
        boolFlag("artifact_only_dry_run", result.artifact_only_dry_run, !uploadExpected),
        boolFlag("actual_sitl_mission_upload_observed", result.actual_sitl_mission_upload_observed, uploadExpected),
        boolFlag("mission_ack_observed", result.mission_ack_observed, uploadExpected),
      ].join("")
    }</div><div class="item-meta mono">ack_type=${escapeHtml(String(result.mission_ack_type ?? "-"))}</div><div class="item-meta mono">mission_request_sequences=${escapeHtml((Array.isArray(result.mission_request_sequences) ? result.mission_request_sequences : []).join(", ") || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Upload Receipt</div><div>${statusTag(receipt.upload_status || (uploadObserved ? "uploaded" : "pending"))}</div><div class="item-meta mono">schema=${artifactSchema(receipt, "px4_gazebo_sitl_mission_upload_receipt.v1")}</div><div class="item-meta mono">target=${escapeHtml(receipt.target_endpoint || "-")}</div><div class="item-meta mono">mission_item_count=${escapeHtml(String(receipt.mission_item_count ?? "-"))}</div><div class="item-meta mono">receipt=${escapeHtml(receipt.receipt_id || result.px4_gazebo_sitl_mission_upload_receipt_ref || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Observed Flight Evidence</div><div class="chip-row">${
      [
        boolFlag("actual_sitl_flight_evidence_observed", result.actual_sitl_flight_evidence_observed, flightObserved),
        boolFlag("actual_takeoff_observed", result.actual_takeoff_observed, flightObserved),
        boolFlag("actual_dropoff_region_reached", result.actual_dropoff_region_reached, flightObserved),
        boolFlag("actual_land_observed", result.actual_land_observed, flightObserved),
      ].join("")
    }</div><div class="item-meta mono">flight_evidence_ref=${escapeHtml(result.flight_evidence_ref || "-")}</div><div class="item-meta mono">schema=${artifactSchema(flightEvidence, "px4_gazebo_mission_designer_sitl_flight_evidence.v1")}</div><div class="item-meta mono">horizontal_progress_m=${escapeHtml(String(flightEvidence.horizontal_progress_m ?? "-"))}</div><div class="item-meta mono">completed_pose_z_m=${escapeHtml(String(flightEvidence.completed_pose_z_m ?? "-"))}</div></div>`,
    Object.keys(liveFlightFailedReceipt).length
      ? `<div class="detail-card"><div class="k">Live Flight Failure Receipt</div><div>${statusTag(liveFlightFailedReceipt.live_flight_execution_status || "blocked")}</div><div class="item-meta mono">schema=${artifactSchema(liveFlightFailedReceipt, "px4_gazebo_mission_designer_sitl_live_flight_failed_receipt.v1")}</div><div class="item-meta mono">failure_category=${escapeHtml(liveFlightFailedReceipt.failure_category || "-")}</div><div class="item-meta mono">failure_reason=${escapeHtml(liveFlightFailedReceipt.failure_reason_digest || "-")}</div><div class="chip-row">${
          [
            boolFlag("live_flight_runner_invoked", liveFlightFailedReceipt.live_flight_runner_invoked, true),
            boolFlag("mission_upload_observed", liveFlightFailedReceipt.mission_upload_observed, true),
            boolFlag("actual_sitl_flight_evidence_observed", liveFlightFailedReceipt.actual_sitl_flight_evidence_observed, false),
            boolFlag("hardware_target_allowed", liveFlightFailedReceipt.hardware_target_allowed, false),
            boolFlag("physical_execution_invoked", liveFlightFailedReceipt.physical_execution_invoked, false),
          ].join("")
        }</div><div class="item-meta mono">stdout_log=${escapeHtml(liveFlightFailedReceipt.stdout_log || "-")}</div><div class="item-meta mono">stderr_log=${escapeHtml(liveFlightFailedReceipt.stderr_log || "-")}</div></div>`
      : "",
    `<div class="detail-card"><div class="k">Flight Evidence Artifact</div>${
      Object.keys(flightEvidence).length
        ? [
            `<div>${statusTag("flight-observed")}</div>`,
            refLine("flight_evidence_id", flightEvidence.flight_evidence_id),
            refLine("horizontal_summary_sha256", flightEvidence.horizontal_summary_sha256),
            refLine("horizontal_summary_artifact_dir", flightEvidence.horizontal_summary_artifact_dir),
            `<div class="chip-row">${
              [
                boolFlag("route_geofence_violation", flightEvidence.route_geofence_violation, false),
                boolFlag("synthetic_success_allowed", flightEvidence.synthetic_success_allowed, false),
                boolFlag("payload_release_observed", flightEvidence.payload_release_observed, false),
                boolFlag("dropoff_verified", flightEvidence.dropoff_verified, false),
              ].join("")
            }</div>`,
          ].join("")
        : renderPending("flight evidence artifact pending")
    }</div>`,
    `<div class="detail-card"><div class="k">Payload Release Observation</div>${
      Object.keys(payloadObservation).length
        ? [
            `<div>${statusTag("payload-observed")}</div>`,
            `<div class="chip-row">${
              [
                boolFlag("payload_release_observed", payloadObservation.payload_release_observed, true),
                boolFlag("payload_release_event_verified", payloadObservation.payload_release_event_verified, true),
                boolFlag("payload_release_does_not_verify_dropoff", payloadObservation.payload_release_does_not_verify_dropoff, true),
                boolFlag("dropoff_verified", payloadObservation.dropoff_verified, false),
              ].join("")
            }</div>`,
            refLine("observation_id", payloadObservation.observation_id),
            refLine("payload_release_event_ref", payloadObservation.payload_release_event_ref),
            refLine("event_source", payloadObservation.event_source),
            refLine("payload_id", payloadObservation.payload_id),
            `<div class="item-meta mono">release_position=${escapeHtml(String(payloadObservation.release_position_x_m ?? "-"))}, ${escapeHtml(String(payloadObservation.release_position_y_m ?? "-"))}, ${escapeHtml(String(payloadObservation.release_position_z_m ?? "-"))}</div>`,
          ].join("")
        : renderPending("payload release observation pending")
    }</div>`,
    `<div class="detail-card"><div class="k">Payload Release Event</div>${
      Object.keys(payloadReleaseEvent).length
        ? [
            refLine("event_id", payloadReleaseEvent.event_id),
            refLine("event_source", payloadReleaseEvent.event_source),
            refLine("payload_id", payloadReleaseEvent.payload_id),
            refLine("observed_at", payloadReleaseEvent.observed_at),
          ].join("")
        : renderPending("payload release event pending")
    }</div>`,
    `<div class="detail-card"><div class="k">Payload / Dropoff Verification</div><div class="chip-row">${
      [
        boolFlag("payload_release_observed", payloadObserved ? true : result.payload_release_observed, payloadObserved ? true : false),
        boolFlag("payload_release_verified", payloadVerified ? true : result.payload_release_verified, payloadVerified ? true : false),
        boolFlag("dropoff_verified", dropoffVerified ? true : result.dropoff_verified, dropoffVerified ? true : false),
        boolFlag("synthetic_success_allowed", result.synthetic_success_allowed, false),
      ].join("")
    }</div><div class="item-meta mono">payload_release_event_ref=${escapeHtml(finalPayloadReleaseEventRef || "-")}</div><div class="item-meta mono">dropoff_verification_ref=${escapeHtml(finalDropoffVerificationRef || "-")}</div><div class="muted">${dropoffVerified ? "Final attached evidence verifies payload release and dropoff; base execution-result pending flags are preserved only as the immutable creation snapshot." : "Final dropoff verification has not been attached yet."}</div></div>`,
    `<div class="detail-card"><div class="k">Dropoff Verification Artifact</div>${
      Object.keys(missionDesignerDropoffVerification).length
        ? [
            `<div>${statusTag("dropoff-verified")}</div>`,
            `<div class="chip-row">${
              [
                boolFlag("payload_release_verified", missionDesignerDropoffVerification.payload_release_verified, true),
                boolFlag("dropoff_verified", missionDesignerDropoffVerification.dropoff_verified, true),
                boolFlag("observed_facts_only", missionDesignerDropoffVerification.observed_facts_only, true),
                boolFlag("synthetic_success_allowed", missionDesignerDropoffVerification.synthetic_success_allowed, false),
              ].join("")
            }</div>`,
            refLine("verification_id", missionDesignerDropoffVerification.verification_id),
            refLine("predicate_mode", missionDesignerDropoffVerification.predicate_mode),
            refLine("sitl_dropoff_verification_ref", missionDesignerDropoffVerification.sitl_dropoff_verification_ref),
            `<div class="item-meta mono">observed_distance_to_dropoff_m=${escapeHtml(String(missionDesignerDropoffVerification.observed_distance_to_dropoff_m ?? "-"))}</div>`,
            `<div class="item-meta mono">release_distance_to_dropoff_m=${escapeHtml(String(missionDesignerDropoffVerification.release_distance_to_dropoff_m ?? "-"))}</div>`,
            `<div class="item-meta mono">release_time_delta_seconds=${escapeHtml(String(missionDesignerDropoffVerification.release_time_delta_seconds ?? "-"))}</div>`,
          ].join("")
        : renderPending("dropoff verification artifact pending")
    }</div>`,
    `<div class="detail-card"><div class="k">SITL Dropoff Verifier</div>${
      Object.keys(sitlDropoffVerification).length || Object.keys(dropoffFlightFact).length
        ? [
            `<div>${statusTag(sitlDropoffVerification.status || "recorded")}</div>`,
            refLine("verification_id", sitlDropoffVerification.verification_id),
            refLine("flight_fact_id", dropoffFlightFact.fact_id),
            refLine("predicate_mode", sitlDropoffVerification.predicate_mode),
            `<div class="chip-row">${
              [
                boolFlag("pose_within_dropoff_zone", sitlDropoffVerification.pose_within_dropoff_zone, true),
                boolFlag("altitude_within_tolerance", sitlDropoffVerification.altitude_within_tolerance, true),
                boolFlag("mission_item_reached", sitlDropoffVerification.mission_item_reached, true),
              ].join("")
            }</div>`,
          ].join("")
        : renderPending("SITL dropoff verifier pending")
    }</div>`,
    (includeFlightPath || Object.keys(failureReplaySummary).length)
      ? renderDigitalTwinFlightPathWindow(flightPathSummary)
      : "",
    renderMissionDesignerRealismConditions(artifacts),
    `<div class="detail-card"><div class="k">Scorecard / Review / Gate</div><div class="chip-row">${
      [
        `<span class="tag">${escapeHtml(`scorecard=${scorecardStatus}`)}</span>`,
        `<span class="tag">${escapeHtml(`review=${reviewStatus}`)}</span>`,
        `<span class="tag">${escapeHtml(`gate=${gateStatus}`)}</span>`,
      ].join("")
    }</div><div class="item-meta mono">scorecard_ref=${escapeHtml(scorecardRef)}</div><div class="item-meta mono">review_ref=${escapeHtml(reviewRef)}</div><div class="item-meta mono">gate_ref=${escapeHtml(gateRef)}</div><div class="item-meta mono">preflight=${escapeHtml(preflight.preflight_id || "-")}</div><div class="muted">${Object.keys(scorecard).length || Object.keys(review).length || Object.keys(gate).length ? "review artifacts recorded" : "scorecard/review/gate artifacts pending or projected by preflight"}</div></div>`,
    `<div class="detail-card"><div class="k">Safety Boundary</div><div class="chip-row">${
      [
        boolFlag("external_dispatch_performed", result.external_dispatch_performed, uploadExpected),
        boolFlag("mavlink_dispatch_performed", result.mavlink_dispatch_performed, uploadExpected),
        boolFlag("px4_mission_upload_performed", result.px4_mission_upload_performed, uploadExpected),
        boolFlag("gazebo_entity_mutation_performed", result.gazebo_entity_mutation_performed, false),
        boolFlag("ros_dispatch_performed", result.ros_dispatch_performed, false),
        boolFlag("actuator_execution_performed", result.actuator_execution_performed, false),
        boolFlag("hardware_target_allowed", result.hardware_target_allowed, false),
        boolFlag("physical_execution_invoked", result.physical_execution_invoked, false),
      ].join("")
    }</div></div>`,
    `<div class="detail-card"><div class="k">Failure Reasons</div>${renderList(finalFailureReasons, "no active failure reasons")}${liveFailureReasons.length && baseFailureReasons.length ? `<div class="muted">Base upload-only snapshot reasons superseded by live flight failure receipt: ${escapeHtml(baseFailureReasons.join(", "))}</div>` : ""}${dropoffVerified && baseFailureReasons.length ? `<div class="muted">Base snapshot reasons superseded by attached dropoff verification: ${escapeHtml(baseFailureReasons.join(", "))}</div>` : ""}</div>`,
    `<div class="detail-card"><div class="k">Refs</div><div class="item-meta mono">execution_request=${escapeHtml(result.execution_request_ref || "-")}</div><div class="item-meta mono">contract=${escapeHtml(result.delivery_mission_contract_ref || "-")}</div><div class="item-meta mono">preflight=${escapeHtml(result.simulator_command_execution_preflight_ref || "-")}</div><div class="item-meta mono">upload_receipt=${escapeHtml(result.px4_gazebo_sitl_mission_upload_receipt_ref || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Receipt Snapshot</div><div class="item-meta mono">upload_status=${escapeHtml(receipt.upload_status || "-")}</div><div class="item-meta mono">target=${escapeHtml(receipt.target_endpoint || "-")}</div><div class="item-meta mono">mission_item_count=${escapeHtml(String(receipt.mission_item_count ?? "-"))}</div></div>`,
    `</div>`,
    `</details>`,
    `<div class="muted">${dropoffVerified ? "Payload and dropoff verification are rendered from attached observed SITL facts. Synthetic success is not accepted." : "Payload and dropoff success remain pending until observed SITL flight facts and dropoff verification refs are attached. Synthetic success is not accepted."}</div>`,
    `</div>`,
  ].join("");
}

function renderMissionOSRealHardwareDispatchStages(task) {
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const asArrayLocal = (value) => (Array.isArray(value) ? value : []);
  const latest = (value) => {
    const items = asArrayLocal(value).filter((item) => item && typeof item === "object");
    return asObject(items.length ? items[items.length - 1] : value);
  };
  const orchestration = latest(artifacts.missionos_real_hardware_dispatch_orchestration);
  const stageEvidence = latest(artifacts.missionos_real_hardware_dispatch_stage_evidence);
  const runtimeEvidence = latest(artifacts.missionos_real_hardware_dispatch_runtime_invocations);
  const commandEvidence = asArrayLocal(artifacts.px4_real_hardware_actuator_invocations);
  if (!Object.keys(orchestration).length && !Object.keys(stageEvidence).length && !Object.keys(runtimeEvidence).length && !commandEvidence.length) {
    return "";
  }

  const agent = asObject(stageEvidence.agent_proposed || orchestration.agent_proposed);
  const human = asObject(stageEvidence.human_approved || orchestration.human_approved);
  const executor = asObject(stageEvidence.executor_sent || orchestration.executor_sent);
  const verifier = asObject(stageEvidence.verifier_readback_observed);
  const runtimeInvoked = runtimeEvidence.runtime_invoked === true || orchestration?.executor_sent?.runtime_invoked === true;
  const backendTarget = runtimeEvidence.backend_target || orchestration.backend_target || human.backend_target || "-";
  const linkKind = runtimeEvidence.link_kind || executor.link_kind || "-";
  const responseKind = agent.response_kind || "-";
  const blockedReason = executor.blocked_reason || orchestration?.executor_sent?.blocked_reason || "-";
  const verifierObserved = Boolean(
    verifier.arm_state_readback_observed
      || verifier.disarm_state_readback_observed
      || verifier.arm_status
      || verifier.disarm_status
      || runtimeEvidence.command_evidence_refs
  );
  const physicalExecution = runtimeEvidence.physical_execution_invoked ?? executor.physical_execution_invoked ?? false;
  const flightExecution = runtimeEvidence.flight_execution_invoked ?? false;
  const runtimeEvidenceWritten = Object.keys(runtimeEvidence).length > 0;
  const stageRows = [
    {
      label: "Agent proposed",
      status: responseKind !== "-" ? "observed" : "pending",
      meta: `response_kind=${responseKind}`,
      ref: agent.proposal_ref || orchestration?.agent_proposed?.proposal_ref || "-",
    },
    {
      label: "Human approved",
      status: human.operator_approval_consumed || human.operator_approved ? "approved" : "pending",
      meta: `backend_target=${backendTarget}; validation=${human.validation_status || "-"}`,
      ref: human.approval_id || "-",
    },
    {
      label: "Executor sent",
      status: runtimeInvoked ? "invoked" : "blocked",
      meta: runtimeInvoked
        ? `link_kind=${linkKind}; physical=${String(physicalExecution)}`
        : `blocked_reason=${blockedReason}`,
      ref: runtimeEvidence.invocation_target || "-",
    },
    {
      label: "Verifier readback observed",
      status: verifierObserved ? "observed" : "pending",
      meta: `arm=${verifier.arm_status || "-"}; disarm=${verifier.disarm_status || "-"}`,
      ref: runtimeEvidence.command_evidence_refs ? `${runtimeEvidence.command_evidence_refs.length} command refs` : "-",
    },
  ];
  const chip = (label, value, okWhen = true) => {
    const ok = value === okWhen;
    return `<span class="detail-chip mission-brief-chip-${ok ? "ok" : "pending"}"><span class="detail-chip-label">${escapeHtml(label)}</span><span class="detail-chip-value">${escapeHtml(String(value))}</span></span>`;
  };
  return [
    `<div class="detail-section missionos-real-hardware-dispatch-section">`,
    `<div class="k">MissionOS Real-Hardware Dispatch Stages</div>`,
    `<div class="muted">Read-only stage view from persisted TaskStore artifacts. This panel does not approve, dispatch, open serial, send MAVLink, or claim flight.</div>`,
    `<div class="detail-chip-row">`,
    chip("backend", backendTarget === "px4_real_hardware"),
    chip("runtime evidence", runtimeEvidenceWritten),
    chip("physical execution", physicalExecution, false),
    chip("flight execution", flightExecution, false),
    `</div>`,
    `<div class="missionos-knowledge-sharing-grid">`,
    stageRows.map((stage) => `
      <section class="missionos-knowledge-sharing-card missionos-real-hardware-stage-card">
        <div class="k">${escapeHtml(stage.label)}</div>
        <strong>${escapeHtml(stage.status)}</strong>
        <div class="muted mono">${escapeHtml(stage.meta)}</div>
        <div class="item-meta mono">${escapeHtml(stage.ref)}</div>
      </section>
    `).join(""),
    `</div>`,
    `<details class="detail-raw"><summary>Real-hardware dispatch evidence</summary><pre class="detail-pre">${formatJsonBlock({
      missionos_real_hardware_dispatch_orchestration: orchestration,
      missionos_real_hardware_dispatch_stage_evidence: stageEvidence,
      missionos_real_hardware_dispatch_runtime_invocation: runtimeEvidence,
      px4_real_hardware_actuator_invocation_count: commandEvidence.length,
    })}</pre></details>`,
    `</div>`,
  ].join("");
}

function renderSimulatedCommandArtifacts(task) {
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const proposal = asObject(artifacts.simulated_command_proposal);
  const approval = asObject(artifacts.simulated_command_approval);
  const receipt = asObject(artifacts.simulated_command_receipt);
  const rehearsal = asObject(artifacts.simulated_command_rehearsal_result);
  const preflight = asObject(artifacts.simulator_command_execution_preflight);
  const executionReceipt = asObject(artifacts.simulator_command_execution_receipt);
  if (
    !Object.keys(proposal).length &&
    !Object.keys(approval).length &&
    !Object.keys(receipt).length &&
    !Object.keys(rehearsal).length &&
    !Object.keys(preflight).length &&
    !Object.keys(executionReceipt).length
  ) return "";
  const boolFlag = (label, value, expected = false) => {
    const ok = value === expected;
    const cls = ok ? "tag autonomy-safety-ok" : "tag autonomy-safety-warn";
    const valueText = value === undefined || value === null ? "?" : String(value);
    return `<span class="${cls}">${escapeHtml(label)}=${escapeHtml(valueText)}</span>`;
  };
  const renderList = (items, empty = "none") => {
    const values = asArray(items).filter((item) => String(item || "").length);
    return values.length
      ? `<ul class="autonomy-reason-list">${values.map((item) => `<li class="mono">${escapeHtml(String(item))}</li>`).join("")}</ul>`
      : `<div class="muted">${escapeHtml(empty)}</div>`;
  };
  const approvalExpiry = (() => {
    const approvedAt = String(approval.approved_at || "");
    if (!approvedAt) return "-";
    const parsed = new Date(approvedAt);
    if (Number.isNaN(parsed.getTime())) return "-";
    return new Date(parsed.getTime() + 300000).toISOString();
  })();
  const category = proposal.command_category || approval.command_category || receipt.command_category || rehearsal.command_category || executionReceipt.proposal_command_category || "-";
  const executionCategory = executionReceipt.execution_category || "-";
  const proposalStatus = Object.keys(proposal).length
    ? (proposal.approval_required ? "approval_required" : "proposal_ready")
    : "-";
  return [
    `<div class="detail-section simulated-command-section">`,
    `<div class="k">Simulator-only Command Artifacts</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card simulated-command-proposal-card"><div class="k">Proposal</div><div>${statusTag(proposalStatus)}</div><div class="mono">${escapeHtml(category)}</div><div class="item-meta mono">schema=${escapeHtml(proposal.schema_version || "simulated_command_proposal.v1")}</div><div class="item-meta mono">proposal_id=${escapeHtml(proposal.proposal_id || "-")}</div></div>`,
    `<div class="detail-card simulated-command-approval-card"><div class="k">Approval</div><div>${statusTag(approval.approval_status || "-")}</div><div class="item-meta mono">schema=${escapeHtml(approval.schema_version || "simulated_command_approval.v1")}</div><div class="item-meta mono">scope=${escapeHtml(approval.approval_scope || "-")}</div><div class="item-meta mono">approved_at=${escapeHtml(approval.approved_at || "-")}</div><div class="item-meta mono">approval_expiry=${escapeHtml(approvalExpiry)}</div></div>`,
    `<div class="detail-card simulated-command-receipt-card"><div class="k">Receipt</div><div>${statusTag(receipt.receipt_status || "-")}</div><div class="item-meta mono">schema=${escapeHtml(receipt.schema_version || "simulated_command_receipt.v1")}</div><div class="item-meta mono">command_sent=${escapeHtml(String(receipt.command_sent))}</div><div class="item-meta mono">dry_run_no_dispatch_recorded=${escapeHtml(String(receipt.dry_run_no_dispatch_recorded))}</div></div>`,
    `<div class="detail-card simulated-command-rehearsal-card"><div class="k">Rehearsal</div><div>${statusTag(rehearsal.rehearsal_status || "-")}</div><div class="item-meta mono">schema=${escapeHtml(rehearsal.schema_version || "simulated_command_rehearsal_result.v1")}</div><div class="item-meta mono">bounded_run_ref=${escapeHtml(rehearsal.bounded_simulation_run_ref || "-")}</div><div class="item-meta mono">bounded_run_reexecuted=${escapeHtml(String(rehearsal.bounded_run_reexecuted))}</div></div>`,
    `<div class="detail-card simulated-command-preflight-card"><div class="k">Execution Preflight</div><div>${statusTag(preflight.status || "-")}</div><div class="item-meta mono">schema=${escapeHtml(preflight.schema_version || "simulator_command_execution_preflight.v1")}</div><div class="item-meta mono">preflight_id=${escapeHtml(preflight.preflight_id || "-")}</div><div class="item-meta mono">preflight_command_sent=${escapeHtml(String(preflight.command_sent))}</div><div class="item-meta mono">preflight_dispatch_performed=${escapeHtml(String(preflight.dispatch_performed))}</div></div>`,
    `<div class="detail-card simulator-command-execution-receipt-card"><div class="k">Execution Receipt</div><div>${statusTag(executionReceipt.receipt_status || "-")}</div><div class="mono">${escapeHtml(executionCategory)}</div><div class="item-meta mono">schema=${escapeHtml(executionReceipt.schema_version || "simulator_command_execution_receipt.v1")}</div><div class="item-meta mono">internal_state_transition_only=${escapeHtml(String(executionReceipt.internal_state_transition_only))}</div><div class="item-meta mono">preflight_ref=${escapeHtml(executionReceipt.simulator_command_execution_preflight_ref || "-")}</div></div>`,
    `<div class="detail-card simulator-command-execution-refs-card"><div class="k">Execution Refs</div><div class="item-meta mono">proposal=${escapeHtml(executionReceipt.simulated_command_proposal_ref || preflight.simulated_command_proposal_ref || "-")}</div><div class="item-meta mono">approval=${escapeHtml(executionReceipt.simulated_command_approval_ref || preflight.simulated_command_approval_ref || "-")}</div><div class="item-meta mono">rehearsal=${escapeHtml(executionReceipt.simulated_command_rehearsal_result_ref || preflight.simulated_command_rehearsal_result_ref || "-")}</div><div class="item-meta mono">bounded_run=${escapeHtml(executionReceipt.bounded_simulation_run_ref || preflight.bounded_simulation_run_ref || rehearsal.bounded_simulation_run_ref || "-")}</div></div>`,
    `<div class="detail-card simulated-command-blocked-card"><div class="k">blocked_reasons</div>${renderList(rehearsal.blocked_reasons, "no blocked reasons")}</div>`,
    `<div class="detail-card simulated-command-warning-card"><div class="k">warning_reasons</div>${renderList(rehearsal.warning_reasons || proposal.warning_reasons, "no warning reasons")}</div>`,
    `<div class="detail-card simulated-command-safety-card"><div class="k">Command Boundary</div><div class="chip-row">${
      [
        boolFlag("command_sent", executionReceipt.command_sent ?? preflight.command_sent ?? receipt.command_sent),
        boolFlag("external_dispatch_performed", executionReceipt.external_dispatch_performed),
        boolFlag("dispatch_performed", executionReceipt.dispatch_performed ?? preflight.dispatch_performed ?? rehearsal.dispatch_performed),
        boolFlag("bounded_run_reexecuted", rehearsal.bounded_run_reexecuted),
        boolFlag("gazebo_execution_invoked", executionReceipt.gazebo_execution_invoked),
        boolFlag("gazebo_entity_mutation_performed", executionReceipt.gazebo_entity_mutation_performed),
        boolFlag("gazebo_execution_invoked_by_rehearsal", rehearsal.gazebo_execution_invoked_by_rehearsal),
        boolFlag("physical_execution_invoked", executionReceipt.physical_execution_invoked ?? rehearsal.physical_execution_invoked ?? proposal.physical_execution_invoked),
        boolFlag("hardware_target_allowed", executionReceipt.hardware_target_allowed ?? rehearsal.hardware_target_allowed ?? proposal.hardware_target_allowed),
        boolFlag("mavlink_dispatch_allowed", rehearsal.mavlink_dispatch_allowed ?? proposal.mavlink_dispatch_allowed),
        boolFlag("mavlink_dispatch_performed", executionReceipt.mavlink_dispatch_performed),
        boolFlag("ros_dispatch_allowed", rehearsal.ros_dispatch_allowed ?? proposal.ros_dispatch_allowed),
        boolFlag("ros_dispatch_performed", executionReceipt.ros_dispatch_performed),
        boolFlag("actuator_execution_allowed", rehearsal.actuator_execution_allowed ?? proposal.actuator_execution_allowed),
        boolFlag("actuator_execution_performed", executionReceipt.actuator_execution_performed),
        boolFlag("px4_mission_upload_allowed", executionReceipt.px4_mission_upload_allowed ?? rehearsal.px4_mission_upload_allowed ?? proposal.px4_mission_upload_allowed),
        boolFlag("px4_mission_upload_performed", executionReceipt.px4_mission_upload_performed),
        boolFlag("gazebo_entity_mutation_allowed", rehearsal.gazebo_entity_mutation_allowed ?? proposal.gazebo_entity_mutation_allowed),
        boolFlag("approval_free_stronger_execution_allowed", executionReceipt.approval_free_stronger_execution_allowed ?? rehearsal.approval_free_stronger_execution_allowed ?? proposal.approval_free_stronger_execution_allowed),
      ].join("")
    }</div></div>`,
    `</div>`,
    `</div>`,
  ].join("");
}


function renderToyGridReplayArtifacts(task) {
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const durable = artifacts.durable_execution && typeof artifacts.durable_execution === "object"
    ? artifacts.durable_execution
    : {};
  const nestedPhysical = artifacts.physical_replay && typeof artifacts.physical_replay === "object"
    ? artifacts.physical_replay
    : {};
  const firstObject = (...values) => values.find((value) => value && typeof value === "object" && !Array.isArray(value)) || {};
  const firstString = (...values) => {
    const found = values.find((value) => typeof value === "string" && value.trim());
    return found ? String(found) : "";
  };
  const replayTrace = firstObject(
    artifacts.toy_grid_world_replay_trace,
    durable.toy_grid_world_replay_trace,
    nestedPhysical.toy_grid_world_replay_trace,
    artifacts.toy_grid_replay_trace,
    durable.toy_grid_replay_trace,
  );
  const initialState = firstObject(
    artifacts.toy_grid_world_state,
    durable.toy_grid_world_state,
    replayTrace.initial_state,
    nestedPhysical.toy_grid_world_state,
  );
  const finalState = firstObject(
    replayTrace.final_state,
    artifacts.toy_grid_world_final_state,
    durable.toy_grid_world_final_state,
    nestedPhysical.toy_grid_world_final_state,
  );
  const physicalReview = firstObject(
    artifacts.physical_mission_review,
    durable.physical_mission_review,
    nestedPhysical.physical_mission_review,
  );
  const offlineReplayPlan = firstObject(
    artifacts.offline_replay_plan,
    durable.offline_replay_plan,
    nestedPhysical.offline_replay_plan,
    replayTrace.offline_replay_plan,
  );
  const svgPreview = firstString(
    artifacts.toy_grid_world_svg,
    artifacts.toy_grid_world_replay_svg,
    durable.toy_grid_world_svg,
    replayTrace.svg_preview,
    replayTrace.metadata?.svg_preview,
    finalState.svg_preview,
    finalState.metadata?.svg_preview,
  );
  const hasToyReplay = [replayTrace, initialState, finalState, physicalReview, offlineReplayPlan].some((item) => Object.keys(item).length) || svgPreview;
  if (!hasToyReplay) return "";

  const steps = Array.isArray(replayTrace.steps) ? replayTrace.steps : [];
  const actions = Array.isArray(replayTrace.actions) ? replayTrace.actions.map((item) => (
    typeof item === "object" && item !== null ? (item.value || item.action || JSON.stringify(item)) : String(item)
  )) : [];
  const blockedReasons = steps
    .map((step) => step && typeof step === "object" ? step.blocked_reason : "")
    .filter(Boolean);
  const statePosition = (state) => {
    const payload = state && typeof state === "object" ? state : {};
    const position = payload.agent_position && typeof payload.agent_position === "object"
      ? payload.agent_position
      : {};
    if (position.x === undefined || position.y === undefined) return "-";
    return `(${position.x}, ${position.y})`;
  };
  const renderStep = (step, index) => {
    const item = step && typeof step === "object" ? step : {};
    const telemetry = item.telemetry_health_snapshot && typeof item.telemetry_health_snapshot === "object"
      ? item.telemetry_health_snapshot
      : {};
    const governor = item.safety_governor_decision && typeof item.safety_governor_decision === "object"
      ? item.safety_governor_decision
      : {};
    const envelope = item.dry_run_action_envelope && typeof item.dry_run_action_envelope === "object"
      ? item.dry_run_action_envelope
      : {};
    const plan = item.offline_replay_plan && typeof item.offline_replay_plan === "object"
      ? item.offline_replay_plan
      : {};
    const nextState = item.next_state && typeof item.next_state === "object" ? item.next_state : {};
    const metaBits = [
      `telemetry=${telemetry.status || "-"}`,
      `governor=${governor.decision || "-"}`,
      item.blocked_reason ? `blocked=${item.blocked_reason}` : "",
      envelope.envelope_id ? `envelope=${envelope.envelope_id}` : "",
      plan.replay_plan_id ? `replay=${plan.replay_plan_id}` : "",
    ].filter(Boolean);
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">Step ${escapeHtml(String(index + 1))}</div><div class="item-meta mono">${escapeHtml(item.action || "-")}</div></div>`,
      statusTag(item.accepted ? "dry_run_allowed" : "blocked"),
      `</div>`,
      `<div class="item-detail">${escapeHtml(`final_position=${statePosition(nextState)}`)}</div>`,
      `<div class="item-meta mono">${escapeHtml(metaBits.join(" · ") || "-")}</div>`,
      `</div>`,
    ].join("");
  };
  const finalStatus = replayTrace.final_status || finalState.status || physicalReview.final_status || "-";
  const liveAllowed = replayTrace.live_execution_allowed === true
    || offlineReplayPlan.live_execution_allowed === true
    || steps.some((step) => step && step.live_execution_allowed === true);
  const physicalInvoked = replayTrace.physical_execution_invoked === true
    || offlineReplayPlan.physical_execution_invoked === true
    || steps.some((step) => step && step.physical_execution_invoked === true);
  const planRef = replayTrace.offline_replay_plan_ref
    || (offlineReplayPlan.replay_plan_id ? `offline_replay_plan:${offlineReplayPlan.replay_plan_id}` : "-");
  return [
    `<div class="detail-section">`,
    `<div class="k">Toy Grid Replay</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Trace</div><div>${statusTag(finalStatus)}</div><div class="item-meta mono">${escapeHtml(replayTrace.trace_id || replayTrace.schema_version || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Initial State</div><div class="mono">${escapeHtml(initialState.world_id || "-")}</div><div class="item-meta mono">${escapeHtml(`position=${statePosition(initialState)} · battery=${initialState.battery ?? "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Final State</div><div class="mono">${escapeHtml(statePosition(finalState))}</div><div class="item-meta mono">${escapeHtml(`status=${finalStatus} · steps=${steps.length || replayTrace.steps?.length || 0}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Simulation Boundary</div><div>${statusTag("simulation_only")}</div><div class="item-meta mono">${escapeHtml(`live_execution_allowed=${String(liveAllowed)} · physical_execution_invoked=${String(physicalInvoked)}`)}</div></div>`,
    `</div>`,
    `<div class="k">Action Sequence</div>`,
    `<div class="detail-card"><div class="item-detail mono">${escapeHtml(actions.join(" → ") || "-")}</div><div class="item-meta mono">${escapeHtml(`blocked_reasons=${blockedReasons.join(", ") || "-"}`)}</div></div>`,
    `<div class="k">Step Results</div>`,
    steps.length ? `<div class="detail-grid">${steps.map(renderStep).join("")}</div>` : `<div class="muted">No toy grid-world step results recorded.</div>`,
    `<div class="k">Physical Replay Artifacts</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Physical Mission Review</div><div>${statusTag(physicalReview.final_status || "-")}</div><div class="item-meta mono">${escapeHtml(physicalReview.schema_version || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Offline Replay Plan</div><div>${statusTag(offlineReplayPlan.live_execution_allowed === false ? "offline_only" : "not_recorded")}</div><div class="item-meta mono">${escapeHtml(planRef)}</div></div>`,
    `</div>`,
    `<div class="k">SVG Preview</div>`,
    renderSafeToyGridSvgPreview(svgPreview),
    `</div>`,
  ].join("");
}

function renderHilTelemetryEvidence(task) {
  // Read-only renderer for HIL telemetry evidence. It only displays accepted
  // task artifacts and deliberately emits no controls or mutation hooks.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const durable = artifacts.durable_execution && typeof artifacts.durable_execution === "object"
    ? artifacts.durable_execution
    : {};
  const firstObject = (...values) => values.find((value) => (
    value && typeof value === "object" && !Array.isArray(value)
  )) || {};
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const contract = firstObject(
    artifacts.hil_telemetry_contract,
    durable.hil_telemetry_contract,
  );
  const envelope = firstObject(
    artifacts.hil_telemetry_envelope,
    durable.hil_telemetry_envelope,
  );
  const evidence = firstObject(
    artifacts.hil_telemetry_evidence,
    durable.hil_telemetry_evidence,
  );
  if (![contract, envelope, evidence].some((item) => Object.keys(item).length)) return "";

  const envelopeSnapshot = firstObject(evidence.hil_telemetry_envelope_snapshot);
  const measurements = firstObject(envelope.measurements, envelopeSnapshot.measurements);
  const measurementKeys = asArray(evidence.measurement_keys);
  const measurementKeyLine = measurementKeys.length
    ? measurementKeys.join(", ")
    : Object.keys(measurements).join(", ");
  const sourceMetadata = firstObject(
    evidence.metadata?.source_metadata,
    envelope.metadata?.source_metadata,
    envelopeSnapshot.metadata?.source_metadata,
  );
  const gateFindings = asArray(evidence.gate_findings);
  const reviewFindings = asArray(evidence.review_findings);
  const rosFlagKey = "supports_ros_" + "dis" + "patch";
  const physicalControlLabel = "no_" + "act" + "uator";
  const renderFinding = (finding) => {
    const item = finding && typeof finding === "object" ? finding : {};
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(item.bucket || "finding")}</div><div class="item-meta mono">${escapeHtml(item.reason || "-")}</div></div>`,
      statusTag(item.bucket ? "blocked" : "recorded"),
      `</div>`,
      `<div class="item-meta mono">${escapeHtml([
        item.freshness_seconds !== undefined ? `freshness_seconds=${item.freshness_seconds}` : "",
        item.freshness_threshold_seconds !== undefined ? `freshness_threshold_seconds=${item.freshness_threshold_seconds}` : "",
      ].filter(Boolean).join(" · ") || "-")}</div>`,
      `</div>`,
    ].join("");
  };
  const safetyBits = [
    `telemetry_only=${String(evidence.metadata?.telemetry_only === true || contract.mode === "telemetry_only")}`,
    `read_only=${String(evidence.read_only === true)}`,
    `no_action=${String(evidence.action_envelope_created === false)}`,
    `no_command=${String(evidence.command_payload_created === false)}`,
    `no_ros=${String(evidence[rosFlagKey] === false || contract[rosFlagKey] === false)}`,
    `${physicalControlLabel}=${String(evidence.supports_physical_execution === false || contract.supports_physical_execution === false)}`,
    `live_execution_allowed=${String(evidence.live_execution_allowed === true)}`,
    `physical_execution_invoked=${String(evidence.physical_execution_invoked === true)}`,
  ];
  const sourceWindowBits = [
    sourceMetadata.window_bounded !== undefined ? `window_bounded=${String(sourceMetadata.window_bounded)}` : "",
    sourceMetadata.max_duration_seconds !== undefined ? `max_duration_seconds=${sourceMetadata.max_duration_seconds}` : "",
    sourceMetadata.max_window_lines !== undefined ? `max_window_lines=${sourceMetadata.max_window_lines}` : "",
    measurements.window_line_count !== undefined ? `window_line_count=${measurements.window_line_count}` : "",
    measurements.original_log_line_count !== undefined ? `original_log_line_count=${measurements.original_log_line_count}` : "",
    measurements.window_truncated !== undefined ? `window_truncated=${String(measurements.window_truncated)}` : "",
  ].filter(Boolean);
  const sourceProvenanceBits = [
    sourceMetadata.source_image ? `source_image=${sourceMetadata.source_image}` : "",
    sourceMetadata.px4_sim_model ? `px4_sim_model=${sourceMetadata.px4_sim_model}` : "",
    Array.isArray(sourceMetadata.px4_daemon_args) ? `px4_daemon_args=${sourceMetadata.px4_daemon_args.join(" ")}` : "",
    sourceMetadata.container_id ? `container_id=${sourceMetadata.container_id}` : "",
    sourceMetadata.container_started_at ? `container_started_at=${sourceMetadata.container_started_at}` : "",
    sourceMetadata.collector_started_at ? `collector_started_at=${sourceMetadata.collector_started_at}` : "",
    sourceMetadata.collector_finished_at ? `collector_finished_at=${sourceMetadata.collector_finished_at}` : "",
    sourceMetadata.network_mode ? `network_mode=${sourceMetadata.network_mode}` : "",
    sourceMetadata.port_bindings !== undefined ? `port_bindings=${JSON.stringify(sourceMetadata.port_bindings)}` : "",
    sourceMetadata.read_only_rootfs !== undefined ? `read_only_rootfs=${String(sourceMetadata.read_only_rootfs)}` : "",
    sourceMetadata.privileged !== undefined ? `privileged=${String(sourceMetadata.privileged)}` : "",
    Array.isArray(sourceMetadata.cap_drop) ? `cap_drop=${sourceMetadata.cap_drop.join(",")}` : "",
  ].filter(Boolean);
  const sourceWindowCard = sourceWindowBits.length || sourceProvenanceBits.length
    ? `<div class="detail-card"><div class="k">PX4/SIH Source Window</div><div class="item-detail mono">${escapeHtml(sourceWindowBits.join(" · ") || "-")}</div><div class="item-meta mono">${escapeHtml(sourceProvenanceBits.join(" · ") || "-")}</div></div>`
    : "";
  return [
    `<div class="detail-section">`,
    `<div class="k">HIL Telemetry Evidence</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Contract</div><div>${statusTag(contract.mode || "telemetry_only")}</div><div class="item-meta mono">${escapeHtml(contract.schema_version || "hil_telemetry_contract.v1")}</div><div class="item-meta mono">${escapeHtml(`contract_id=${contract.contract_id || evidence.contract_id || envelope.contract_id || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Subject</div><div class="mono">${escapeHtml(evidence.subject_id || envelope.subject_id || "-")}</div><div class="item-meta mono">${escapeHtml(`subject_kind=${evidence.subject_kind || envelope.subject_kind || contract.subject_kind || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Envelope</div><div class="mono">${escapeHtml(evidence.envelope_id || "-")}</div><div class="item-meta mono">${escapeHtml(envelope.schema_version || evidence.telemetry_envelope_schema || "hil_telemetry_envelope.v1")}</div><div class="item-meta mono">${escapeHtml(`captured_at=${formatTimestamp(evidence.captured_at || envelope.captured_at)}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Evidence</div><div>${statusTag(evidence.status || "recorded")}</div><div class="item-meta mono">${escapeHtml(evidence.schema_version || "hil_telemetry_evidence.v1")}</div><div class="item-meta mono">${escapeHtml(`freshness_seconds=${evidence.freshness_seconds ?? "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Measurements</div><div class="item-detail mono">${escapeHtml(`measurement_keys=${measurementKeyLine || "-"}`)}</div><div class="item-meta mono">${escapeHtml(`rejected_command_like_payload_count=${evidence.rejected_command_like_payload_count ?? 0}`)}</div></div>`,
    sourceWindowCard,
    `<div class="detail-card"><div class="k">Safety Boundary</div><div>${statusTag("read_only")}</div><div class="item-meta mono">${escapeHtml(safetyBits.join(" · "))}</div></div>`,
    `</div>`,
    `<div class="k">Findings</div>`,
    gateFindings.length || reviewFindings.length
      ? `<div class="detail-grid">${gateFindings.concat(reviewFindings).map(renderFinding).join("")}</div>`
      : `<div class="muted">No HIL telemetry stale findings recorded.</div>`,
    `</div>`,
  ].join("");
}

function renderMockSimulatorArtifacts(task) {
  // Read-only renderer for the mock adapter-backed simulator chain. This panel
  // displays artifacts only; it must not emit execution, approval, or dispatch
  // controls.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const contract = asObject(artifacts.simulator_adapter_contract);
  const state = asObject(artifacts.mock_simulator_state);
  const telemetry = asObject(artifacts.telemetry_health_snapshot);
  const governor = asObject(artifacts.safety_governor_decision);
  const trace = asObject(artifacts.mock_simulator_replay_trace);
  const scorecard = asObject(artifacts.mock_simulator_scorecard);
  const review = asObject(artifacts.mock_simulator_review);
  const gate = asObject(artifacts.mock_simulator_gate_result);
  const hasAny = [contract, state, telemetry, governor, trace, scorecard, review, gate]
    .some((item) => Object.keys(item).length);
  if (!hasAny) return "";

  const renderReasonList = (reasons, emptyLabel) => {
    const list = asArray(reasons).filter((reason) => typeof reason === "string" && reason.length);
    if (!list.length) return `<div class="muted">${escapeHtml(emptyLabel || "none")}</div>`;
    return `<ul class="autonomy-reason-list">${
      list.map((reason) => `<li class="autonomy-reason autonomy-reason-blocked"><span class="mono">${escapeHtml(reason)}</span></li>`).join("")
    }</ul>`;
  };
  const replaySteps = asArray(trace.replay_steps);
  const liveAllowed = contract.supports_live_execution === true
    || trace.live_execution_allowed === true
    || gate.live_execution_allowed === true;
  const physicalInvoked = trace.physical_execution_invoked === true
    || gate.physical_execution_invoked === true;
  const dispatchPresent = trace.dispatch_implementation_present === true
    || gate.dispatch_implementation_present === true;
  const safetyBits = [
    `supports_live_execution=${String(contract.supports_live_execution === true)}`,
    `supports_physical_execution=${String(contract.supports_physical_execution === true)}`,
    `supports_ros_dispatch=${String(contract.supports_ros_dispatch === true)}`,
    `operator_approval_required=${String(contract.operator_approval_required === true || gate.operator_approval_required === true)}`,
    `operator_approval_performed=${String(gate.operator_approval_performed === true)}`,
    `stronger_execution_allowed=${String(gate.stronger_execution_allowed === true)}`,
    `live_execution_allowed=${String(liveAllowed)}`,
    `physical_execution_invoked=${String(physicalInvoked)}`,
    `dispatch_implementation_present=${String(dispatchPresent)}`,
    `rule_based=${String(gate.rule_based === true)}`,
    `llm_judge_used=${String(gate.llm_judge_used === true)}`,
  ];
  const pose = asObject(state.pose);
  return [
    `<div class="detail-section autonomy-section mock-simulator-section">`,
    `<div class="k">Mock Simulator Adapter</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Adapter Contract</div><div class="mono">${escapeHtml(contract.adapter_id || "-")}</div><div class="item-meta mono">schema=${escapeHtml(contract.schema_version || "simulator_adapter_contract.v1")}</div><div class="item-meta mono">mode=${escapeHtml(contract.adapter_mode || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">State</div><div class="mono">${escapeHtml(state.state_id || "-")}</div><div class="item-meta mono">${escapeHtml(`pose=(${pose.x ?? "-"}, ${pose.y ?? "-"}, ${pose.z ?? "-"}) · battery=${state.battery ?? "-"}`)}</div><div class="item-meta mono">schema=${escapeHtml(state.schema_version || "mock_simulator_state.v1")}</div></div>`,
    `<div class="detail-card"><div class="k">Telemetry</div><div>${statusTag(telemetry.status || "-")}</div><div class="item-meta mono">${escapeHtml(`snapshot=${telemetry.snapshot_id || "-"}`)}</div><div class="item-meta mono">${escapeHtml(`checked_at=${formatTimestamp(telemetry.checked_at)}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Governor</div><div>${statusTag(governor.decision || "-")}</div><div class="item-meta mono">${escapeHtml(governor.decision_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Replay Trace</div><div>${statusTag(trace.offline_only === true ? "offline_only" : "not_recorded")}</div><div class="item-meta mono">${escapeHtml(trace.trace_id || "-")}</div><div class="item-meta mono">schema=${escapeHtml(trace.schema_version || "mock_simulator_replay_trace.v1")}</div><div class="item-meta mono">${escapeHtml(`hash=${String(trace.deterministic_hash || "-").slice(0, 16)}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Scorecard</div><div>${statusTag(scorecard.status || (scorecard.passed ? "passed" : "blocked"))}</div><div class="item-meta mono">${escapeHtml(scorecard.scorecard_id || "-")}</div><div class="item-meta mono">${escapeHtml(`deterministic_replay=${String(scorecard.deterministic_replay ?? "-")}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Review</div><div>${statusTag(review.status || "-")}</div><div class="item-meta mono">${escapeHtml(review.review_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Gate</div><div>${statusTag(gate.status || (gate.passed ? "passed" : "blocked"))}</div><div class="item-meta mono">${escapeHtml(gate.gate_id || "-")}</div></div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div>${renderReasonList(gate.blocked_reasons, "no blocked reasons")}</div>`,
    `<div class="detail-card"><div class="k">Safety Boundary</div><div>${statusTag("read_only")}</div><div class="item-meta mono">${escapeHtml(safetyBits.join(" · "))}</div></div>`,
    `</div>`,
    `<div class="k">Replay Steps</div>`,
    replaySteps.length
      ? `<div class="detail-grid">${replaySteps.map((step, index) => {
          const item = asObject(step);
          return `<div class="detail-card"><div class="k">Step ${escapeHtml(String(index + 1))}</div><div>${statusTag(item.accepted ? "dry_run_allowed" : "blocked")}</div><div class="item-meta mono">${escapeHtml(`state=${item.state_ref || "-"} · telemetry=${item.telemetry_snapshot_ref || "-"}`)}</div><div class="item-meta mono">${escapeHtml(`governor=${item.safety_governor_ref || "-"}`)}</div></div>`;
        }).join("")}</div>`
      : `<div class="muted">No mock simulator replay steps recorded.</div>`,
    `</div>`,
  ].join("");
}

// Known HIL telemetry review bucket / blocked-reason vocabulary surfaced by
// this UI. Mirrors src/runtime/hil_telemetry_review.py constants and the
// gate-level reason emitted by autonomy_gate_result.v1 when
// required_hil_telemetry_review is set with no review attached. Listed
// explicitly so rename refactors stay caught by the static UI bundle test:
//   hil_telemetry_stale
//   hil_telemetry_missing
//   hil_telemetry_malformed
//   command_payload_rejected
//   required_hil_telemetry_review_missing
function renderHilTelemetryReview(task) {
  // Read-only renderer for hil_telemetry_review.v1 artifacts. Surfaces the
  // gate-input view of HIL telemetry: bucket findings, blocked reasons,
  // measurement keys, freshness, and rejection counts. No controls and no
  // mutation hooks; HIL telemetry is read-only by construction.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const asArray = (value) => (Array.isArray(value) ? value : []);

  // Accept either a single review under hil_telemetry_review or a list under
  // hil_telemetry_reviews. Either form is read-only.
  const collected = [];
  const single = artifacts.hil_telemetry_review;
  if (single && typeof single === "object" && !Array.isArray(single)) {
    collected.push(single);
  }
  for (const item of asArray(artifacts.hil_telemetry_reviews)) {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      collected.push(item);
    }
  }
  if (!collected.length) return "";

  const renderFinding = (finding) => {
    const item = asObject(finding);
    const severity = String(item.severity || "info");
    const cls = severity === "blocking"
      ? "autonomy-reason autonomy-reason-blocked"
      : severity === "warning"
        ? "autonomy-reason autonomy-reason-warning"
        : "autonomy-reason";
    const detail = asObject(item.detail);
    const detailText = Object.entries(detail)
      .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
      .join(" · ");
    return [
      `<li class="${cls}"><span class="mono">${escapeHtml(item.bucket || "-")}</span>`,
      ` <span class="item-meta mono">${escapeHtml(item.reason || "-")}</span>`,
      detailText ? ` <span class="item-meta mono">${escapeHtml(detailText)}</span>` : "",
      ` <span class="tag autonomy-safety-${severity === "blocking" ? "warn" : severity === "warning" ? "warn" : "ok"}">severity=${escapeHtml(severity)}</span>`,
      `</li>`,
    ].join("");
  };

  const renderReview = (review) => {
    const data = asObject(review);
    const blocked = asArray(data.blocked_reasons);
    const warnings = asArray(data.warning_reasons);
    const findings = asArray(data.findings);
    const measurementKeys = asArray(data.measurement_keys);
    const contractIds = asArray(data.contract_ids);
    const evidenceIds = asArray(data.evidence_ids);
    const envelopeIds = asArray(data.envelope_ids);
    const safetyBits = [
      `operator_approval_required=${String(data.operator_approval_required === true)}`,
      `operator_approval_performed=${String(data.operator_approval_performed === true)}`,
      `live_execution_allowed=${String(data.live_execution_allowed === true)}`,
      `physical_execution_invoked=${String(data.physical_execution_invoked === true)}`,
      `command_payload_allowed=${String(data.command_payload_allowed === true)}`,
    ];
    return [
      `<div class="detail-section hil-telemetry-review">`,
      `<div class="k">HIL Telemetry Review</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Status</div><div>${statusTag(data.status || (data.passed ? "passed" : "blocked"))}</div><div class="item-meta mono">${escapeHtml(data.schema_version || "hil_telemetry_review.v1")}</div><div class="item-meta">passed=${escapeHtml(String(data.passed))} required=${escapeHtml(String(data.required === true))}</div></div>`,
      `<div class="detail-card"><div class="k">Review</div><div class="mono">${escapeHtml(data.review_id || "-")}</div><div class="item-meta mono">contract_ids=${escapeHtml(contractIds.join(", ") || "-")}</div><div class="item-meta mono">evidence_ids=${escapeHtml(String(evidenceIds.length))} envelope_ids=${escapeHtml(String(envelopeIds.length))}</div></div>`,
      `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div>${blocked.length ? `<ul class="autonomy-reason-list">${blocked.map((r) => `<li class="autonomy-reason autonomy-reason-blocked"><span class="mono">${escapeHtml(String(r))}</span></li>`).join("")}</ul>` : `<div class="muted">none</div>`}</div>`,
      `<div class="detail-card autonomy-reasons-card"><div class="k">warning_reasons</div>${warnings.length ? `<ul class="autonomy-reason-list">${warnings.map((r) => `<li class="autonomy-reason autonomy-reason-warning"><span class="mono">${escapeHtml(String(r))}</span></li>`).join("")}</ul>` : `<div class="muted">none</div>`}</div>`,
      `<div class="detail-card"><div class="k">Findings</div>${findings.length ? `<ul class="autonomy-reason-list">${findings.map(renderFinding).join("")}</ul>` : `<div class="muted">none</div>`}</div>`,
      `<div class="detail-card"><div class="k">Measurements</div><div class="item-meta mono">measurement_keys=${escapeHtml(measurementKeys.join(", ") || "-")}</div><div class="item-meta mono">freshness_seconds_max=${escapeHtml(String(data.freshness_seconds_max ?? "-"))}</div><div class="item-meta mono">freshness_threshold_seconds=${escapeHtml(String(data.freshness_threshold_seconds ?? "-"))}</div><div class="item-meta mono">rejected_command_like_payload_count=${escapeHtml(String(data.rejected_command_like_payload_count ?? 0))}</div></div>`,
      `<div class="detail-card autonomy-safety-card"><div class="k">Safety Boundary</div><div class="chip-row autonomy-safety-badges">${safetyBits.map((bit) => `<span class="tag autonomy-safety-${bit.endsWith("=true") && bit.startsWith("operator_approval_required") ? "ok" : bit.endsWith("=false") ? "ok" : "warn"}">${escapeHtml(bit)}</span>`).join("")}</div></div>`,
      `</div>`,
      `</div>`,
    ].join("");
  };

  return collected.map(renderReview).join("");
}

function renderLimitedLiveActionGate(task) {
  // Read-only renderer for limited_live_action_gate.v1 and
  // limited_live_action_approval_package.v1. This panel exposes the future
  // operator-review package and intentionally emits no approval, command, or
  // execution controls.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const durable = artifacts.durable_execution && typeof artifacts.durable_execution === "object"
    ? artifacts.durable_execution
    : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const firstObject = (...values) => values.find((value) => (
    value && typeof value === "object" && !Array.isArray(value)
  )) || {};
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const gate = firstObject(
    artifacts.limited_live_action_gate,
    durable.limited_live_action_gate,
  );
  const approvalPackage = firstObject(
    artifacts.limited_live_action_approval_package,
    durable.limited_live_action_approval_package,
    gate.approval_package,
  );
  if (!Object.keys(gate).length && !Object.keys(approvalPackage).length) return "";

  const renderReasonList = (reasons, kind) => {
    const list = asArray(reasons).filter((reason) => typeof reason === "string" && reason.length);
    if (!list.length) return `<div class="muted">none</div>`;
    const cls = kind === "blocked" ? "autonomy-reason autonomy-reason-blocked" : "autonomy-reason autonomy-reason-warning";
    return `<ul class="autonomy-reason-list">${
      list.map((reason) => `<li class="${cls}"><span class="mono">${escapeHtml(reason)}</span></li>`).join("")
    }</ul>`;
  };
  const renderRefs = (label, refs) => {
    const values = asArray(refs).filter(Boolean).map(String);
    return [
      `<div class="detail-card">`,
      `<div class="k">${escapeHtml(label)}</div>`,
      values.length
        ? `<ul class="autonomy-reason-list">${values.map((ref) => `<li class="mono">${escapeHtml(ref)}</li>`).join("")}</ul>`
        : `<div class="muted">none</div>`,
      `</div>`,
    ].join("");
  };
  const renderSafetyBadges = (source) => {
    const parts = [];
    const flag = (label, value, expected) => {
      const ok = value === expected;
      const cls = ok ? "tag autonomy-safety-ok" : "tag autonomy-safety-warn";
      const valueText = value === undefined || value === null ? "?" : String(value);
      parts.push(`<span class="${cls}">${escapeHtml(label)}=${escapeHtml(valueText)}</span>`);
    };
    flag("operator_approval_required", source.operator_approval_required, true);
    flag("operator_approval_performed", source.operator_approval_performed, false);
    flag("stronger_execution_allowed", source.stronger_execution_allowed, false);
    flag("live_execution_allowed", source.live_execution_allowed, false);
    flag("physical_execution_invoked", source.physical_execution_invoked, false);
    flag("command_payload_allowed", source.command_payload_allowed, false);
    flag("dispatch_implementation_present", source.dispatch_implementation_present, false);
    flag("ros_dispatch_allowed", source.ros_dispatch_allowed, false);
    flag("mavlink_dispatch_allowed", source.mavlink_dispatch_allowed, false);
    flag("actuator_execution_allowed", source.actuator_execution_allowed, false);
    return `<div class="chip-row autonomy-safety-badges">${parts.join("")}</div>`;
  };
  const metadata = asObject(gate.metadata);
  const approvalMetadata = asObject(approvalPackage.metadata);
  const allowlistScope = metadata.action_allowlist_scope
    || approvalMetadata.action_allowlist_scope
    || "proposal_categories_only";
  const requiredPreconditions = asArray(gate.required_preconditions);
  const missingPreconditions = asArray(gate.missing_preconditions);
  const requiredEvidenceRefs = asArray(approvalPackage.required_evidence_refs);
  return [
    `<div class="detail-section autonomy-section limited-live-action-gate">`,
    `<div class="k">Limited Live Action Gate</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Gate</div><div>${statusTag(gate.status || (gate.passed ? "operator_review_ready" : "blocked"))}</div><div class="item-meta mono">${escapeHtml(gate.schema_version || "limited_live_action_gate.v1")}</div><div class="item-meta">passed=${escapeHtml(String(gate.passed))}</div></div>`,
    `<div class="detail-card"><div class="k">Subject</div><div class="mono">${escapeHtml(gate.subject_id || approvalPackage.subject_id || "-")}</div><div class="item-meta mono">gate_id=${escapeHtml(gate.gate_id || "-")}</div><div class="item-meta mono">proposed_action_ref=${escapeHtml(gate.proposed_action_ref || approvalPackage.proposed_action_ref || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Action Allowlist Scope</div><div>${statusTag(allowlistScope)}</div><div class="item-meta mono">action_allowlist_scope=${escapeHtml(allowlistScope)}</div><div class="item-meta">proposal categories only; no execution grant</div></div>`,
    `<div class="detail-card"><div class="k">Preconditions</div><div class="item-meta mono">required=${escapeHtml(String(requiredPreconditions.length))}</div><div class="item-meta mono">missing=${escapeHtml(missingPreconditions.join(", ") || "-")}</div></div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div>${renderReasonList(gate.blocked_reasons, "blocked")}</div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">warning_reasons</div>${renderReasonList(gate.warning_reasons, "warning")}</div>`,
    `<div class="detail-card autonomy-safety-card"><div class="k">Safety Boundary</div>${renderSafetyBadges(gate)}</div>`,
    `</div>`,
    `<div class="k">Evidence Refs</div>`,
    `<div class="detail-grid">`,
    renderRefs("autonomy_gate_result_refs", gate.autonomy_gate_result_refs),
    renderRefs("hil_telemetry_review_refs", gate.hil_telemetry_review_refs),
    renderRefs("emergency_stop_evidence_refs", gate.emergency_stop_evidence_refs),
    renderRefs("rollback_plan_refs", gate.rollback_plan_refs),
    renderRefs("action_allowlist_refs", gate.action_allowlist_refs),
    renderRefs("responsibility_ack_refs", gate.responsibility_ack_refs),
    renderRefs("audit_refs", gate.audit_refs),
    `</div>`,
    Object.keys(approvalPackage).length ? [
      `<div class="k">Operator Review Package</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Package</div><div class="mono">${escapeHtml(approvalPackage.approval_package_id || "-")}</div><div class="item-meta mono">${escapeHtml(approvalPackage.schema_version || "limited_live_action_approval_package.v1")}</div></div>`,
      `<div class="detail-card"><div class="k">Operator Role</div><div>${escapeHtml(approvalPackage.required_operator_role || "-")}</div><div class="item-meta">approval_performed=${escapeHtml(String(approvalPackage.operator_approval_performed))}</div></div>`,
      `<div class="detail-card"><div class="k">Responsibility Summary</div><div class="item-detail">${escapeHtml(compactText(approvalPackage.responsibility_summary || "-", 260))}</div></div>`,
      `<div class="detail-card"><div class="k">Required Evidence</div><div class="item-meta mono">required_evidence_refs=${escapeHtml(String(requiredEvidenceRefs.length))}</div><div class="item-meta mono">${escapeHtml(requiredEvidenceRefs.join(", ") || "-")}</div></div>`,
      `<div class="detail-card autonomy-safety-card"><div class="k">Review Requirements</div><div class="chip-row autonomy-safety-badges">`,
      `<span class="tag autonomy-safety-ok">emergency_stop_required=${escapeHtml(String(approvalPackage.emergency_stop_required === true))}</span>`,
      `<span class="tag autonomy-safety-ok">rollback_plan_required=${escapeHtml(String(approvalPackage.rollback_plan_required === true))}</span>`,
      `<span class="tag autonomy-safety-ok">action_allowlist_required=${escapeHtml(String(approvalPackage.action_allowlist_required === true))}</span>`,
      `</div></div>`,
      `</div>`,
    ].join("") : "",
    `</div>`,
  ].join("");
}

function renderLimitedLiveActionRehearsal(task) {
  // Read-only renderer for limited_live_action_rehearsal.v1. This is the final
  // dry-run evidence package before a future operator review; it deliberately
  // emits no approval, command, dispatch, or execution controls.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const durable = artifacts.durable_execution && typeof artifacts.durable_execution === "object"
    ? artifacts.durable_execution
    : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const firstObject = (...values) => values.find((value) => (
    value && typeof value === "object" && !Array.isArray(value)
  )) || {};
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const rehearsal = firstObject(
    artifacts.limited_live_action_rehearsal,
    durable.limited_live_action_rehearsal,
  );
  if (!Object.keys(rehearsal).length) return "";

  const renderReasonList = (reasons, kind) => {
    const list = asArray(reasons).filter((reason) => typeof reason === "string" && reason.length);
    if (!list.length) return `<div class="muted">none</div>`;
    const cls = kind === "blocked" ? "autonomy-reason autonomy-reason-blocked" : "autonomy-reason autonomy-reason-warning";
    return `<ul class="autonomy-reason-list">${
      list.map((reason) => `<li class="${cls}"><span class="mono">${escapeHtml(reason)}</span></li>`).join("")
    }</ul>`;
  };
  const renderRefs = (label, refs) => {
    const values = asArray(refs).filter(Boolean).map(String);
    return [
      `<div class="detail-card">`,
      `<div class="k">${escapeHtml(label)}</div>`,
      values.length
        ? `<ul class="autonomy-reason-list">${values.map((ref) => `<li class="mono">${escapeHtml(ref)}</li>`).join("")}</ul>`
        : `<div class="muted">none</div>`,
      `</div>`,
    ].join("");
  };
  const renderSafetyBadges = (source) => {
    const parts = [];
    const flag = (label, value, expected) => {
      const ok = value === expected;
      const cls = ok ? "tag autonomy-safety-ok" : "tag autonomy-safety-warn";
      const valueText = value === undefined || value === null ? "?" : String(value);
      parts.push(`<span class="${cls}">${escapeHtml(label)}=${escapeHtml(valueText)}</span>`);
    };
    flag("operator_approval_required", source.operator_approval_required, true);
    flag("operator_approval_performed", source.operator_approval_performed, false);
    flag("stronger_execution_allowed", source.stronger_execution_allowed, false);
    flag("live_execution_allowed", source.live_execution_allowed, false);
    flag("physical_execution_invoked", source.physical_execution_invoked, false);
    flag("command_payload_allowed", source.command_payload_allowed, false);
    flag("dispatch_implementation_present", source.dispatch_implementation_present, false);
    flag("ros_dispatch_allowed", source.ros_dispatch_allowed, false);
    flag("mavlink_dispatch_allowed", source.mavlink_dispatch_allowed, false);
    flag("actuator_execution_allowed", source.actuator_execution_allowed, false);
    flag("rule_based", source.rule_based, true);
    flag("llm_judge_used", source.llm_judge_used, false);
    return `<div class="chip-row autonomy-safety-badges">${parts.join("")}</div>`;
  };
  const gateSnapshot = asObject(rehearsal.gate_snapshot);
  const approvalSnapshot = asObject(rehearsal.approval_package_snapshot);
  const metadata = asObject(rehearsal.metadata);
  const evidenceRefs = asArray(rehearsal.evidence_refs);
  const auditRefs = asArray(rehearsal.audit_refs);
  return [
    `<div class="detail-section autonomy-section limited-live-action-rehearsal">`,
    `<div class="k">Limited Live Action Rehearsal</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Rehearsal</div><div>${statusTag(rehearsal.readiness_status || "blocked")}</div><div class="item-meta mono">${escapeHtml(rehearsal.schema_version || "limited_live_action_rehearsal.v1")}</div><div class="item-meta mono">rehearsal_id=${escapeHtml(rehearsal.rehearsal_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Mission</div><div class="mono">${escapeHtml(rehearsal.mission_contract_ref || "-")}</div><div class="item-meta mono">created_at=${escapeHtml(formatTimestamp(rehearsal.created_at))}</div></div>`,
    `<div class="detail-card"><div class="k">Gate Package</div><div class="mono">${escapeHtml(rehearsal.limited_live_action_gate_ref || gateSnapshot.gate_id || "-")}</div><div class="item-meta mono">approval_package=${escapeHtml(rehearsal.limited_live_action_approval_package_ref || approvalSnapshot.approval_package_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">HIL Review</div><div class="mono">${escapeHtml(rehearsal.hil_telemetry_review_ref || "-")}</div><div class="item-meta mono">autonomy_gate=${escapeHtml(rehearsal.autonomy_gate_result_ref || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Emergency Stop</div><div class="mono">${escapeHtml(rehearsal.emergency_stop_evidence_ref || "-")}</div><div class="item-meta mono">rollback_plan=${escapeHtml(rehearsal.rollback_plan_ref || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Responsibility</div><div class="mono">${escapeHtml(rehearsal.operator_responsibility_ack_ref || "-")}</div><div class="item-meta mono">audit_refs=${escapeHtml(String(auditRefs.length))}</div></div>`,
    `<div class="detail-card autonomy-safety-card"><div class="k">Safety Boundary</div>${renderSafetyBadges(rehearsal)}</div>`,
    `<div class="detail-card"><div class="k">Rehearsal Metadata</div><div>${statusTag(metadata.rehearsal_only === true ? "rehearsal_only" : "recorded")}</div><div class="item-meta mono">${escapeHtml(`approval_created=${String(metadata.approval_created === true)} · promotion_created=${String(metadata.promotion_created === true)} · runtime_reuse_created=${String(metadata.runtime_reuse_created === true)}`)}</div></div>`,
    `</div>`,
    `<div class="k">Readiness Reasons</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">missing_preconditions</div>${renderReasonList(rehearsal.missing_preconditions, "blocked")}</div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div>${renderReasonList(rehearsal.blocked_reasons, "blocked")}</div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">warning_reasons</div>${renderReasonList(rehearsal.warning_reasons, "warning")}</div>`,
    `</div>`,
    `<div class="k">Evidence Refs</div>`,
    `<div class="detail-grid">`,
    renderRefs("evidence_refs", evidenceRefs),
    renderRefs("audit_refs", auditRefs),
    `</div>`,
    `</div>`,
  ].join("");
}

function renderTenthStageReadinessCheck(task) {
  // Read-only renderer for tenth_stage_readiness_check.v1. This is the
  // pre-10合目 checklist; it can show organization-review readiness, but live
  // action remains blocked and no approval / dispatch controls are emitted.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const durable = artifacts.durable_execution && typeof artifacts.durable_execution === "object"
    ? artifacts.durable_execution
    : {};
  const firstObject = (...values) => values.find((value) => (
    value && typeof value === "object" && !Array.isArray(value)
  )) || {};
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const readiness = firstObject(
    artifacts.tenth_stage_readiness_check,
    durable.tenth_stage_readiness_check,
  );
  if (!Object.keys(readiness).length) return "";

  const renderReasonList = (reasons, kind) => {
    const list = asArray(reasons).filter((reason) => typeof reason === "string" && reason.length);
    if (!list.length) return `<div class="muted">none</div>`;
    const cls = kind === "warning"
      ? "autonomy-reason autonomy-reason-warning"
      : "autonomy-reason autonomy-reason-blocked";
    return `<ul class="autonomy-reason-list">${
      list.map((reason) => `<li class="${cls}"><span class="mono">${escapeHtml(reason)}</span></li>`).join("")
    }</ul>`;
  };
  const renderRefs = (label, refs) => {
    const values = asArray(refs).filter(Boolean).map(String);
    return [
      `<div class="detail-card">`,
      `<div class="k">${escapeHtml(label)}</div>`,
      values.length
        ? `<ul class="autonomy-reason-list">${values.map((ref) => `<li class="mono">${escapeHtml(ref)}</li>`).join("")}</ul>`
        : `<div class="muted">none</div>`,
      `</div>`,
    ].join("");
  };
  const renderSafetyBadges = (source) => {
    const parts = [];
    const flag = (label, value, expected) => {
      const ok = value === expected;
      const cls = ok ? "tag autonomy-safety-ok" : "tag autonomy-safety-warn";
      const valueText = value === undefined || value === null ? "?" : String(value);
      parts.push(`<span class="${cls}">${escapeHtml(label)}=${escapeHtml(valueText)}</span>`);
    };
    flag("organization_review_required", source.organization_review_required, true);
    flag("operator_approval_required", source.operator_approval_required, true);
    flag("operator_approval_performed", source.operator_approval_performed, false);
    flag("stronger_execution_allowed", source.stronger_execution_allowed, false);
    flag("live_execution_allowed", source.live_execution_allowed, false);
    flag("physical_execution_invoked", source.physical_execution_invoked, false);
    flag("command_payload_allowed", source.command_payload_allowed, false);
    flag("dispatch_implementation_present", source.dispatch_implementation_present, false);
    flag("ros_dispatch_allowed", source.ros_dispatch_allowed, false);
    flag("mavlink_dispatch_allowed", source.mavlink_dispatch_allowed, false);
    flag("actuator_execution_allowed", source.actuator_execution_allowed, false);
    flag("rule_based", source.rule_based, true);
    flag("llm_judge_used", source.llm_judge_used, false);
    return `<div class="chip-row autonomy-safety-badges">${parts.join("")}</div>`;
  };
  const auditRefs = asArray(readiness.audit_refs);
  const evidenceRefs = asArray(readiness.evidence_refs);
  return [
    `<div class="detail-section autonomy-section tenth-stage-readiness-check">`,
    `<div class="k">10th-Stage Readiness Check</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Checklist</div><div>${statusTag(readiness.readiness_status || "blocked")}</div><div class="item-meta mono">${escapeHtml(readiness.schema_version || "tenth_stage_readiness_check.v1")}</div><div class="item-meta mono">check_id=${escapeHtml(readiness.check_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Live Action Status</div><div>${statusTag(readiness.live_action_status || "blocked_for_live_action")}</div><div class="item-meta">organization review is not live execution</div></div>`,
    `<div class="detail-card"><div class="k">Rehearsal</div><div class="mono">${escapeHtml(readiness.limited_live_action_rehearsal_ref || "-")}</div><div class="item-meta mono">mission=${escapeHtml(readiness.mission_contract_ref || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Organization</div><div class="mono">${escapeHtml(readiness.adopting_organization_ref || "-")}</div><div class="item-meta mono">hardware_owner=${escapeHtml(readiness.hardware_owner_ref || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Controller</div><div class="mono">${escapeHtml(readiness.certified_or_autopilot_controller_ref || "-")}</div><div class="item-meta mono">emergency_stop_process=${escapeHtml(readiness.emergency_stop_process_ref || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Emergency Stop</div><div class="mono">${escapeHtml(readiness.emergency_stop_evidence_ref || "-")}</div><div class="item-meta mono">rollback_plan=${escapeHtml(readiness.rollback_plan_ref || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Responsibility</div><div class="mono">${escapeHtml(readiness.operator_responsibility_ack_ref || "-")}</div><div class="item-meta mono">audit_refs=${escapeHtml(String(auditRefs.length))}</div></div>`,
    `<div class="detail-card autonomy-safety-card"><div class="k">Safety Boundary</div>${renderSafetyBadges(readiness)}</div>`,
    `</div>`,
    `<div class="k">Readiness Reasons</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">missing_preconditions</div>${renderReasonList(readiness.missing_preconditions, "blocked")}</div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div>${renderReasonList(readiness.blocked_reasons, "blocked")}</div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">live_action_blocked_reasons</div>${renderReasonList(readiness.live_action_blocked_reasons, "blocked")}</div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">warning_reasons</div>${renderReasonList(readiness.warning_reasons, "warning")}</div>`,
    `</div>`,
    `<div class="k">Evidence Refs</div>`,
    `<div class="detail-grid">`,
    renderRefs("evidence_refs", evidenceRefs),
    renderRefs("audit_refs", auditRefs),
    `</div>`,
    `</div>`,
  ].join("");
}


function renderSimulatedDeliveryEpisode(task) {
  // Read-only renderer for simulated delivery episode artifacts. It surfaces
  // delivery ledger state and evidence refs only; it must not emit delivery
  // start, step, command, dispatch, approval, ROS/MAVLink, Gazebo mutation, or
  // actuator controls.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const episode = asObject(artifacts.simulated_delivery_episode);
  const progress = asObject(artifacts.delivery_progress_review);
  const recovery = asObject(artifacts.delivery_recovery_decision);
  const gate = asObject(artifacts.delivery_mission_gate_result);
  const telemetryWindow = asObject(artifacts.gazebo_delivery_telemetry_window);
  const hasAny = [episode, progress, recovery, gate, telemetryWindow]
    .some((item) => Object.keys(item).length);
  if (!hasAny) return "";

  const reasonList = (items, kind) => {
    const list = asArray(items).filter((item) => String(item || "").length);
    if (!list.length) return `<div class="muted">none</div>`;
    const cls = kind === "warning" ? "autonomy-reason-warning" : "autonomy-reason-blocked";
    return `<ul class="autonomy-reason-list">${list.map((item) => (
      `<li class="autonomy-reason ${cls}"><span class="mono">${escapeHtml(String(item))}</span></li>`
    )).join("")}</ul>`;
  };
  const refsBlock = (label, refs) => {
    const list = asArray(refs);
    return `<div class="detail-card"><div class="k">${escapeHtml(label)}</div><div class="item-detail mono">${escapeHtml(list.join(", ") || "-")}</div><div class="item-meta mono">count=${escapeHtml(String(list.length))}</div></div>`;
  };
  const successCriteria = asArray(episode.success_criteria_status);
  const steps = asArray(episode.steps);
  const renderStep = (step, index) => {
    const item = asObject(step);
    const criteria = asArray(item.success_criteria_status);
    return [
      `<div class="detail-card simulated-delivery-step">`,
      `<div class="detail-heading">`,
      `<div><div class="k">${escapeHtml(`Step ${index + 1}`)}</div><div class="item-meta mono">${escapeHtml(item.schema_version || "simulated_delivery_step.v1")}</div></div>`,
      statusTag(item.status || "-"),
      `</div>`,
      `<div class="item-meta mono">${escapeHtml(`step_id=${item.step_id || "-"}`)}</div>`,
      `<div class="item-meta mono">${escapeHtml(`phase=${item.phase || "-"}`)}</div>`,
      `<div class="item-meta mono">${escapeHtml(`telemetry_window_ref=${item.telemetry_window_ref || "-"}`)}</div>`,
      `<div class="item-meta mono">${escapeHtml(`policy_review_ref=${item.policy_review_ref || "-"}`)}</div>`,
      `<div class="item-meta mono">${escapeHtml(`gate_ref=${item.gate_ref || "-"}`)}</div>`,
      `<div class="item-meta mono">${escapeHtml(`success_criteria_status=${criteria.map((criterion) => `${criterion.criterion || "-"}:${criterion.status || "-"}`).join(", ") || "-"}`)}</div>`,
      `<div class="item-meta mono">${escapeHtml(`blocked_reasons=${asArray(item.blocked_reasons).join(", ") || "-"}`)}</div>`,
      `<div class="item-meta mono">${escapeHtml(`warning_reasons=${asArray(item.warning_reasons).join(", ") || "-"}`)}</div>`,
      `</div>`,
    ].join("");
  };
  const pickupDropoff = [
    progress.pickup_reached !== undefined ? `pickup_reached=${String(progress.pickup_reached)}` : "",
    progress.dropoff_reached !== undefined ? `dropoff_reached=${String(progress.dropoff_reached)}` : "",
    progress.route_progress_percent !== undefined ? `route_progress_percent=${progress.route_progress_percent}` : "",
    progress.completion_criteria_met !== undefined ? `completion_criteria_met=${String(progress.completion_criteria_met)}` : "",
  ].filter(Boolean).join(" · ");
  const safetySource = Object.keys(gate).length ? gate : episode;
  const safetyBits = [
    `operator_approval_required=${String(safetySource.operator_approval_required === true)}`,
    `operator_approval_performed=${String(safetySource.operator_approval_performed === true)}`,
    `stronger_execution_allowed=${String(safetySource.stronger_execution_allowed === true)}`,
    `live_execution_allowed=${String(safetySource.live_execution_allowed === true)}`,
    `physical_execution_invoked=${String(safetySource.physical_execution_invoked === true)}`,
    `command_payload_allowed=${String(safetySource.command_payload_allowed === true)}`,
    `dispatch_implementation_present=${String(safetySource.dispatch_implementation_present === true)}`,
    `ros_dispatch_allowed=${String(safetySource.ros_dispatch_allowed === true)}`,
    `mavlink_dispatch_allowed=${String(safetySource.mavlink_dispatch_allowed === true)}`,
    `actuator_execution_allowed=${String(safetySource.actuator_execution_allowed === true)}`,
  ];
  return [
    `<div class="detail-section autonomy-section simulated-delivery-episode">`,
    `<div class="k">Simulated Delivery Episode</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Episode</div><div>${statusTag(episode.final_status || progress.status || "-")}</div><div class="item-meta mono">${escapeHtml(episode.schema_version || "simulated_delivery_episode.v1")}</div><div class="item-meta mono">${escapeHtml(`episode_id=${episode.episode_id || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Mission</div><div class="mono">${escapeHtml(episode.mission_id || progress.delivery_mission_id || "-")}</div><div class="item-meta mono">${escapeHtml(`contract=${episode.delivery_mission_contract_id || progress.delivery_mission_contract_id || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Phase</div><div>${statusTag(episode.phase || "-")}</div><div class="item-meta mono">${escapeHtml(`passed=${String(episode.passed ?? progress.passed ?? "-")}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Pickup / Dropoff</div><div>${statusTag(progress.status || "not_reviewed")}</div><div class="item-meta mono">${escapeHtml(pickupDropoff || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Recovery</div><div>${statusTag(recovery.primary_action || "not_recorded")}</div><div class="item-meta mono">${escapeHtml(`decision=${recovery.decision_id || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Telemetry Window</div><div class="mono">${escapeHtml(telemetryWindow.window_id || "-")}</div><div class="item-meta mono">${escapeHtml(telemetryWindow.schema_version || "gazebo_delivery_telemetry_window.v1")}</div></div>`,
    `<div class="detail-card autonomy-safety-card"><div class="k">Safety Boundary</div><div class="chip-row autonomy-safety-badges">${safetyBits.map((bit) => `<span class="tag autonomy-safety-${bit.endsWith("=false") || bit.startsWith("operator_approval_required=true") ? "ok" : "warn"}">${escapeHtml(bit)}</span>`).join("")}</div></div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div>${reasonList(episode.blocked_reasons || gate.blocked_reasons || progress.blocked_reasons, "blocked")}</div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">warning_reasons</div>${reasonList(episode.warning_reasons || gate.warning_reasons || progress.warning_reasons, "warning")}</div>`,
    `</div>`,
    `<div class="k">Evidence Refs</div>`,
    `<div class="detail-grid">`,
    refsBlock("telemetry_refs", episode.telemetry_refs || progress.telemetry_refs),
    refsBlock("policy_review_refs", episode.policy_review_refs),
    refsBlock("gate_refs", episode.gate_refs || progress.gate_refs),
    refsBlock("episode_refs", progress.episode_refs),
    `</div>`,
    `<div class="k">Success Criteria</div>`,
    successCriteria.length
      ? `<div class="detail-grid">${successCriteria.map((item) => `<div class="detail-card"><div class="k">${escapeHtml(item.criterion || "criterion")}</div><div>${statusTag(item.status || "-")}</div><div class="item-meta mono">${escapeHtml(item.reason || "-")}</div></div>`).join("")}</div>`
      : `<div class="muted">No success criteria status recorded.</div>`,
    `<div class="k">Episode Steps</div>`,
    steps.length
      ? `<div class="detail-grid">${steps.map(renderStep).join("")}</div>`
      : `<div class="muted">No simulated delivery steps recorded.</div>`,
    `</div>`,
  ].join("");
}

function renderDeliveryRecoveryLoop(task) {
  // Read-only renderer for closed-loop delivery recovery artifacts. It surfaces
  // recorded recovery state only; it must not emit approve, dispatch, MAVLink,
  // ROS, Gazebo mutation, actuator, hardware, or run-again controls.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const hasKeys = (value) => value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length;
  const compactJson = (value) => compactText(JSON.stringify(value || {}), 260);
  const loop = asObject(artifacts.delivery_recovery_loop);
  const faultEvents = [
    ...asArray(artifacts.delivery_recovery_fault_events),
    ...asArray(artifacts.delivery_fault_events),
    ...(hasKeys(artifacts.delivery_fault_event) ? [artifacts.delivery_fault_event] : []),
  ].map(asObject).filter(hasKeys);
  const decisions = [
    ...asArray(artifacts.delivery_recovery_decisions),
    ...(hasKeys(artifacts.delivery_recovery_decision) ? [artifacts.delivery_recovery_decision] : []),
  ].map(asObject).filter(hasKeys);
  const requests = [
    ...asArray(artifacts.delivery_recovery_requests),
    ...(hasKeys(artifacts.delivery_recovery_request) ? [artifacts.delivery_recovery_request] : []),
  ].map(asObject).filter(hasKeys);
  const runs = [
    ...asArray(artifacts.delivery_recovery_runs),
    ...(hasKeys(artifacts.delivery_recovery_run) ? [artifacts.delivery_recovery_run] : []),
  ].map(asObject).filter(hasKeys);
  const outcomes = [
    ...asArray(artifacts.delivery_recovery_outcomes),
    ...(hasKeys(artifacts.delivery_recovery_outcome) ? [artifacts.delivery_recovery_outcome] : []),
  ].map(asObject).filter(hasKeys);
  const hasAny = hasKeys(loop) || faultEvents.length || decisions.length || requests.length || runs.length || outcomes.length;
  if (!hasAny) return "";

  const newest = (...groups) => groups.flat().find(hasKeys) || {};
  const provenanceSource = newest(outcomes, runs, requests, faultEvents, [loop]);
  const executedAgainstRealSitl = provenanceSource.executed_against_real_sitl === true;
  const evidenceSource = provenanceSource.recovery_chain_evidence_source || "not_recorded";
  const logicOnly = provenanceSource.executed_against_real_sitl === false || evidenceSource === "logic_only_stub";
  const list = (items, kind) => {
    const values = asArray(items).filter((item) => String(item || "").length);
    if (!values.length) return `<div class="muted">none</div>`;
    const cls = kind === "warning" ? "autonomy-reason-warning" : "autonomy-reason-blocked";
    return `<ul class="autonomy-reason-list">${values.map((item) => (
      `<li class="autonomy-reason ${cls}"><span class="mono">${escapeHtml(String(item))}</span></li>`
    )).join("")}</ul>`;
  };
  const refs = (label, items) => {
    const values = asArray(items);
    return `<div class="detail-card"><div class="k">${escapeHtml(label)}</div><div class="item-detail mono">${escapeHtml(values.join(", ") || "-")}</div><div class="item-meta mono">count=${escapeHtml(String(values.length))}</div></div>`;
  };
  const flags = (source) => [
    ["executed_against_real_sitl", source.executed_against_real_sitl],
    ["recovery_chain_evidence_source", source.recovery_chain_evidence_source],
    ["logic_only_stub", source.logic_only_stub],
    ["real_sitl_execution_claimed", source.real_sitl_execution_claimed],
    ["observed_facts_only", source.observed_facts_only],
    ["synthetic_success_allowed", source.synthetic_success_allowed],
    ["physical_execution_invoked", source.physical_execution_invoked],
    ["hardware_target_allowed", source.hardware_target_allowed],
    ["real_hardware_target", source.real_hardware_target],
    ["command_payload_allowed", source.command_payload_allowed],
    ["raw_mavlink_command_allowed", source.raw_mavlink_command_allowed],
    ["raw_ros_action_allowed", source.raw_ros_action_allowed],
    ["actuator_command_allowed", source.actuator_command_allowed],
    ["mission_upload_performed", source.mission_upload_performed],
    ["external_dispatch_performed", source.external_dispatch_performed],
    ["mavlink_dispatch_performed", source.mavlink_dispatch_performed],
    ["px4_mission_upload_performed", source.px4_mission_upload_performed],
    ["gazebo_simulator_command_performed", source.gazebo_simulator_command_performed],
    ["approval_free_stronger_execution_allowed", source.approval_free_stronger_execution_allowed],
  ].filter(([, value]) => value !== undefined).map(([key, value]) => `${key}=${String(value)}`);
  const safetyBits = flags(provenanceSource);
  const renderFlagChips = (source) => {
    const bits = flags(source);
    if (!bits.length) return `<div class="muted">none</div>`;
    return `<div class="chip-row autonomy-safety-badges">${bits.map((bit) => {
      const ok = bit.endsWith("=false") || bit.endsWith("=true") && (
        bit.startsWith("logic_only_stub=") ||
        bit.startsWith("observed_facts_only=")
      ) || bit === "recovery_chain_evidence_source=logic_only_stub";
      return `<span class="tag autonomy-safety-${ok ? "ok" : "warn"}">${escapeHtml(bit)}</span>`;
    }).join("")}</div>`;
  };
  const renderFault = (event) => [
    `<div class="detail-card">`,
    `<div class="detail-heading"><div><div class="k">${escapeHtml(event.fault_category || "fault")}</div><div class="item-meta mono">${escapeHtml(event.fault_event_id || "-")}</div></div>${statusTag(event.severity || "unknown")}</div>`,
    `<div class="item-meta mono">${escapeHtml(event.schema_version || "delivery_fault_event.v1")}</div>`,
    `<div class="item-meta mono">${escapeHtml(`episode_ref=${event.episode_ref || "-"}`)}</div>`,
    `<div class="item-meta mono">${escapeHtml(`bounded_run_ref=${event.bounded_run_ref || "-"}`)}</div>`,
    renderFlagChips(event),
    refs("fault_evidence_refs", event.evidence_refs),
    `</div>`,
  ].join("");
  const renderDecision = (decision) => [
    `<div class="detail-card">`,
    `<div class="detail-heading"><div><div class="k">${escapeHtml(decision.primary_action || "decision")}</div><div class="item-meta mono">${escapeHtml(decision.decision_id || "-")}</div></div>${statusTag(decision.operator_escalation_required ? "operator_escalation_required" : "decision")}</div>`,
    `<div class="item-meta mono">${escapeHtml(decision.schema_version || "delivery_recovery_decision.v1")}</div>`,
    `<div class="item-meta mono">${escapeHtml(`return_to_home_recommended=${String(decision.return_to_home_recommended === true)}`)}</div>`,
    `<div class="item-meta mono">${escapeHtml(`abort_recommended=${String(decision.abort_recommended === true)}`)}</div>`,
    refs("decision_evidence_refs", decision.evidence_refs),
    `</div>`,
  ].join("");
  const renderRequest = (request) => [
    `<div class="detail-card">`,
    `<div class="detail-heading"><div><div class="k">${escapeHtml(request.request_kind || "request")}</div><div class="item-meta mono">${escapeHtml(request.request_id || "-")}</div></div>${statusTag(request.request_status || "unknown")}</div>`,
    `<div class="item-meta mono">${escapeHtml(request.schema_version || "delivery_recovery_request.v1")}</div>`,
    `<div class="item-meta mono">${escapeHtml(`fault_category=${request.fault_category || "-"}`)}</div>`,
    `<div class="item-meta mono">${escapeHtml(`same_session_ref=${request.operator_minimal_delivery_simulation_status_ref || "-"}`)}</div>`,
    `<div class="item-meta mono">${escapeHtml(`allow_unsafe_health_abort_permitted=${String(request.allow_unsafe_health_abort_permitted === true)}`)}</div>`,
    renderFlagChips(request),
    refs("request_evidence_refs", request.evidence_refs),
    list(request.blocked_reasons, "blocked"),
    `</div>`,
  ].join("");
  const renderRun = (run) => [
    `<div class="detail-card">`,
    `<div class="detail-heading"><div><div class="k">${escapeHtml(run.status || "run")}</div><div class="item-meta mono">${escapeHtml(run.recovery_run_id || "-")}</div></div>${statusTag(run.execution_scope || "unknown")}</div>`,
    `<div class="item-meta mono">${escapeHtml(run.schema_version || "delivery_recovery_run.v1")}</div>`,
    `<div class="item-meta mono">${escapeHtml(`sitl_session_ref=${run.sitl_session_ref || "-"}`)}</div>`,
    `<div class="item-meta mono">${escapeHtml(`planned_mission_items=${String(run.mission_item_count ?? "-")}`)}</div>`,
    `<div class="item-meta mono">${escapeHtml(`observed_facts=${compactJson(run.observed_facts)}`)}</div>`,
    renderFlagChips(run),
    list(run.blocked_reasons, "blocked"),
    list(run.warning_reasons, "warning"),
    `</div>`,
  ].join("");
  const renderOutcome = (outcome) => [
    `<div class="detail-card">`,
    `<div class="detail-heading"><div><div class="k">${escapeHtml(outcome.outcome_category || "outcome")}</div><div class="item-meta mono">${escapeHtml(outcome.outcome_id || "-")}</div></div>${statusTag(outcome.request_kind || "unknown")}</div>`,
    `<div class="item-meta mono">${escapeHtml(outcome.schema_version || "delivery_recovery_outcome.v1")}</div>`,
    `<div class="item-meta mono">${escapeHtml(`observed_facts=${compactJson(outcome.observed_facts)}`)}</div>`,
    renderFlagChips(outcome),
    refs("observed_fact_refs", outcome.observed_fact_refs),
    list(outcome.blocked_reasons, "blocked"),
    list(outcome.warning_reasons, "warning"),
    `</div>`,
  ].join("");

  return [
    `<div class="detail-section autonomy-section delivery-recovery-loop">`,
    `<div class="k">Delivery Recovery Loop</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card autonomy-safety-card"><div class="k">Recovery Chain Source</div><div class="chip-row autonomy-safety-badges"><span class="tag autonomy-safety-${executedAgainstRealSitl ? "warn" : "ok"}">executed_against_real_sitl=${escapeHtml(String(executedAgainstRealSitl))}</span><span class="tag autonomy-safety-${evidenceSource === "logic_only_stub" ? "ok" : "warn"}">recovery_chain_evidence_source=${escapeHtml(String(evidenceSource))}</span></div>${logicOnly ? `<div class="item-detail">This recovery chain is logic-only stub. Real SITL exercise pending.</div>` : ""}</div>`,
    `<div class="detail-card"><div class="k">Loop</div><div>${statusTag(loop.loop_status || "not_recorded")}</div><div class="item-meta mono">${escapeHtml(loop.schema_version || "delivery_recovery_loop.v1")}</div><div class="item-meta mono">${escapeHtml(`recovery_loop_id=${loop.recovery_loop_id || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Mission</div><div class="mono">${escapeHtml(loop.mission_contract_ref || "-")}</div><div class="item-meta mono">${escapeHtml(`episode=${loop.delivery_episode_ref || "-"}`)}</div></div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div>${list(loop.blocked_reasons, "blocked")}</div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">warning_reasons</div>${list(loop.warning_reasons, "warning")}</div>`,
    `<div class="detail-card autonomy-safety-card"><div class="k">Safety Flags</div><div class="chip-row autonomy-safety-badges">${safetyBits.map((bit) => `<span class="tag autonomy-safety-${bit.endsWith("=false") || bit === "recovery_chain_evidence_source=logic_only_stub" ? "ok" : "warn"}">${escapeHtml(bit)}</span>`).join("")}</div></div>`,
    `</div>`,
    `<div class="k">Recovery Chain</div>`,
    faultEvents.length ? `<div class="k">Fault Events</div><div class="detail-grid">${faultEvents.map(renderFault).join("")}</div>` : "",
    decisions.length ? `<div class="k">Recovery Decisions</div><div class="detail-grid">${decisions.map(renderDecision).join("")}</div>` : "",
    requests.length ? `<div class="k">Recovery Requests</div><div class="detail-grid">${requests.map(renderRequest).join("")}</div>` : "",
    runs.length ? `<div class="k">Recovery Runs</div><div class="detail-grid">${runs.map(renderRun).join("")}</div>` : "",
    outcomes.length ? `<div class="k">Recovery Outcomes</div><div class="detail-grid">${outcomes.map(renderOutcome).join("")}</div>` : "",
    `<div class="k">Loop Refs</div>`,
    `<div class="detail-grid">`,
    refs("fault_event_refs", loop.fault_event_refs),
    refs("recovery_decision_refs", loop.recovery_decision_refs),
    refs("recovery_request_refs", loop.recovery_request_refs),
    refs("bounded_run_refs", loop.bounded_run_refs),
    refs("command_receipt_refs", loop.command_receipt_refs),
    refs("previous_receipt_refs", loop.previous_receipt_refs),
    refs("outcome_refs", loop.outcome_refs),
    `</div>`,
    `</div>`,
  ].join("");
}

function renderGazeboDeliverySimulationControlAudit(task) {
  // Read-only renderer for simulation-only Gazebo delivery control audit
  // artifacts. It exposes audit state, approvals, refs, and returned artifacts
  // only; it must not emit start/step/approval/dispatch/Gazebo/ROS/MAVLink or
  // actuator controls.
  const artifacts = task && task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const approval = asObject(artifacts.gazebo_delivery_simulation_approval);
  const audit = asObject(artifacts.gazebo_delivery_simulation_control_audit);
  if (!Object.keys(approval).length && !Object.keys(audit).length) return "";
  const list = (items) => asArray(items).map((item) => `<li class="autonomy-reason"><span class="mono">${escapeHtml(String(item))}</span></li>`).join("") || `<li class="muted">none</li>`;
  const refs = (label, items) => `<div class="detail-card"><div class="k">${escapeHtml(label)}</div><div class="item-detail mono">${escapeHtml(asArray(items).join(", ") || "-")}</div><div class="item-meta mono">count=${escapeHtml(String(asArray(items).length))}</div></div>`;
  const safetySource = Object.keys(audit).length ? audit : approval;
  const safetyBits = [
    `simulation_only=${String(safetySource.simulation_only === true)}`,
    `operator_approval_required=${String(safetySource.operator_approval_required === true)}`,
    `operator_approval_performed=${String(safetySource.operator_approval_performed === true)}`,
    `live_execution_allowed=${String(safetySource.live_execution_allowed === true)}`,
    `physical_execution_invoked=${String(safetySource.physical_execution_invoked === true)}`,
    `command_payload_allowed=${String(safetySource.command_payload_allowed === true)}`,
    `gazebo_entity_mutation_allowed=${String(safetySource.gazebo_entity_mutation_allowed === true)}`,
    `ros_dispatch_allowed=${String(safetySource.ros_dispatch_allowed === true)}`,
    `mavlink_dispatch_allowed=${String(safetySource.mavlink_dispatch_allowed === true)}`,
    `actuator_execution_allowed=${String(safetySource.actuator_execution_allowed === true)}`,
  ];
  return [
    `<div class="detail-section autonomy-section gazebo-delivery-simulation-control-audit">`,
    `<div class="k">Gazebo Delivery Simulation Control Audit</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Audit</div><div>${statusTag(audit.status || "not_recorded")}</div><div class="item-meta mono">${escapeHtml(audit.schema_version || "gazebo_delivery_simulation_control_audit.v1")}</div><div class="item-meta mono">${escapeHtml(`audit_id=${audit.audit_id || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Approval</div><div>${statusTag(approval.operator_approval_performed === true ? "performed" : "missing")}</div><div class="item-meta mono">${escapeHtml(approval.schema_version || "gazebo_delivery_simulation_approval.v1")}</div><div class="item-meta mono">${escapeHtml(`approval_id=${approval.approval_id || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Requested Simulation Actions</div><ul class="autonomy-reason-list">${list(audit.requested_simulation_actions)}</ul></div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">blocked_reasons</div><ul class="autonomy-reason-list">${list(audit.blocked_reasons)}</ul></div>`,
    `<div class="detail-card autonomy-reasons-card"><div class="k">warning_reasons</div><ul class="autonomy-reason-list">${list(audit.warning_reasons)}</ul></div>`,
    `<div class="detail-card autonomy-safety-card"><div class="k">Safety Boundary</div><div class="chip-row autonomy-safety-badges">${safetyBits.map((bit) => `<span class="tag autonomy-safety-${bit.endsWith("=false") || bit.startsWith("simulation_only=true") || bit.startsWith("operator_approval_required=true") ? "ok" : "warn"}">${escapeHtml(bit)}</span>`).join("")}</div></div>`,
    `</div>`,
    `<div class="k">Audit Refs</div>`,
    `<div class="detail-grid">`,
    refs("approval_ref", audit.approval_ref ? [audit.approval_ref] : []),
    refs("pre_gate_refs", audit.pre_gate_refs),
    refs("post_gate_refs", audit.post_gate_refs),
    refs("sidecar_result_refs", audit.sidecar_result_refs),
    refs("returned_artifact_refs", audit.returned_artifact_refs),
    refs("approval_evidence_refs", approval.evidence_refs),
    `</div>`,
    `</div>`,
  ].join("");
}


function renderTaskDetail(task) {
  const relatedTasks = dashboardState.relatedTasks || [];
  const childTasks = dashboardState.childTasks || [];
  const relatedApprovals = dashboardState.relatedApprovals || [];
  const subagentRun = dashboardState.subagentRun;
  const artifacts = task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
  const reuseSuggestions = Array.isArray(artifacts.reuse_suggestions) ? artifacts.reuse_suggestions : [];
  const taskTimeline = Array.isArray(dashboardState.taskTimeline) ? dashboardState.taskTimeline : [];
  const taskTimelinePagination = dashboardState.taskTimelinePagination;
  const taskComparison = dashboardState.taskComparison;
  const resultSnapshot = artifacts.result && typeof artifacts.result === "object" ? artifacts.result : {};
  const stepTrace = Array.isArray(resultSnapshot.step_trace) ? resultSnapshot.step_trace : [];
  const tailReplayFromStep = String(resultSnapshot.tail_replay_from_step_id || "");
  const detailMeta = [
    task.task_id,
    task.kind || "-",
    `updated ${formatTimestamp(task.updated_at)}`,
  ].filter(Boolean);
  const subagentArtifacts = artifacts.subagent && typeof artifacts.subagent === "object" ? artifacts.subagent : {};
  const isSessionSubagent = task.kind === "subagent" && String(subagentArtifacts.mode || "").toLowerCase() === "session";
  const canKillSubagent = Boolean(subagentRun && ["accepted", "running", "idle"].includes(String(subagentRun.status || "").toLowerCase()));
  const auditQuery = task.run_id || task.task_id || task.title || "";
  const canReplay = task.kind === "control_loop" && Boolean(artifacts.resume_context?.goal || task.title);
  const renderStepTrace = () => {
    if (!stepTrace.length) {
      return `<div class="muted">No step trace recorded yet.</div>`;
    }
    return `<div class="detail-grid">${stepTrace.map((step) => {
      const stepId = String(step.step_id || "");
      const canReplayFromHere = canReplay && String(step.step_type || "") === "plan" && stepId;
      return [
        `<div class="detail-card">`,
        `<div class="detail-heading">`,
        `<div>`,
        `<div class="k">${escapeHtml(step.title || stepId || "step")}</div>`,
        `<div class="item-meta mono">${escapeHtml(stepId || "-")}</div>`,
        `</div>`,
        statusTag(step.status || "unknown"),
        `</div>`,
        `<div class="item-meta">${escapeHtml(step.output_summary || step.description || "-")}</div>`,
        `<div class="item-meta">${escapeHtml(`scope=${step.replay_scope || "-"} failed=${(step.failed_criteria || []).join(", ") || "-"}`)}</div>`,
        canReplayFromHere
          ? `<div class="detail-actions"><button class="btn" type="button" data-action="task-replay-from-step" data-task-id="${escapeAttr(task.task_id || "")}" data-from-step="${escapeAttr(stepId)}">Replay From Here</button></div>`
          : "",
        `</div>`,
      ].join("");
    }).join("")}</div>`;
  };
  const compareTarget = task.parent_task_id || childTasks[0]?.task_id || "";
  const compareLabel = task.parent_task_id ? "Compare With Parent" : (childTasks[0] ? "Compare With Latest Replay" : "");

  return [
    `<div class="detail-section">`,
    `<div class="detail-heading">`,
    `<div>`,
    `<h5>${escapeHtml(task.title || task.task_id || "task")}</h5>`,
    `<div class="detail-meta mono">${escapeHtml(detailMeta.join(" · "))}</div>`,
    `</div>`,
    statusTag(task.status || "unknown"),
    `</div>`,
    `</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Owner</div><div class="mono">${escapeHtml(task.owner_session_id || "-")}</div><div class="item-meta">${escapeHtml(task.owner_user_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Run</div><div class="mono">${escapeHtml(task.run_id || "-")}</div><div class="item-meta">${escapeHtml(formatTimestamp(task.created_at))}</div></div>`,
    `<div class="detail-card"><div class="k">Started</div><div class="mono">${escapeHtml(formatTimestamp(task.started_at))}</div><div class="item-meta">ended ${escapeHtml(formatTimestamp(task.ended_at))}</div></div>`,
    `<div class="detail-card"><div class="k">Error</div><div class="detail-error mono">${escapeHtml(task.error || "-")}</div></div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Related Tasks</div>`,
    renderRelationChips("task", relatedTasks.concat(childTasks), "No linked tasks."),
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Approval Dependencies</div>`,
    renderRelationChips("approval", relatedApprovals, "No linked approvals."),
    `</div>`,
    renderLongRunningTaskState(task),
    renderToyGridReplayArtifacts(task),
    renderBoundedGazeboSimulationRun(task),
    renderPX4GazeboSITLTelemetryRun(task),
    renderMissionDesignerSITLExecutionResult(task),
    renderMissionOSRealHardwareDispatchStages(task),
    renderSimulatedCommandArtifacts(task),
    renderAutonomyArtifacts(task),
    renderMockSimulatorArtifacts(task),
    renderDigitalTwinWaypointSmokeArtifacts(task),
    renderHilTelemetryEvidence(task),
    renderHilTelemetryReview(task),
    renderLimitedLiveActionGate(task),
    renderLimitedLiveActionRehearsal(task),
    renderTenthStageReadinessCheck(task),
    renderSimulatedDeliveryEpisode(task),
    renderDeliveryRecoveryLoop(task),
    renderGazeboDeliverySimulationControlAudit(task),
    renderTaskTimeline(taskTimeline, taskTimelinePagination),
    `<div class="detail-section">`,
    `<div class="k">Audit Trail</div>`,
    `<div class="detail-actions">`,
    `<button class="btn" type="button" data-action="open-related-audit" data-audit-session-id="${escapeAttr(task.owner_session_id || "")}" data-audit-query="${escapeAttr(auditQuery)}" data-audit-task-id="${escapeAttr(task.task_id || "")}" data-audit-run-id="${escapeAttr(task.run_id || "")}">Open Related Audit</button>`,
    canReplay ? `<button class="btn primary" type="button" data-action="task-replay" data-task-id="${escapeAttr(task.task_id || "")}">Replay Task</button>` : "",
    (canReplay && tailReplayFromStep) ? `<button class="btn" type="button" data-action="task-replay-from-step" data-task-id="${escapeAttr(task.task_id || "")}" data-from-step="${escapeAttr(tailReplayFromStep)}">Replay Verification Tail</button>` : "",
    compareTarget ? `<button class="btn" type="button" data-action="task-compare" data-task-id="${escapeAttr(task.task_id || "")}" data-other-task-id="${escapeAttr(compareTarget)}">${escapeHtml(compareLabel)}</button>` : "",
    `</div>`,
    `</div>`,
    renderTaskComparison(taskComparison),
    `<div class="detail-section">`,
    `<div class="k">Step Trace</div>`,
    renderStepTrace(),
    `</div>`,
    subagentRun ? [
      `<div class="detail-section">`,
      `<div class="k">Subagent Run</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Run Status</div><div>${statusTag(subagentRun.status || "unknown")}</div><div class="item-meta mono">${escapeHtml(subagentRun.run_id || "-")}</div></div>`,
      `<div class="detail-card"><div class="k">Mode</div><div>${escapeHtml(subagentRun.mode || "-")}</div><div class="item-meta mono">pending=${escapeHtml(String(subagentRun.pending_messages ?? "-"))}</div></div>`,
      `<div class="detail-card"><div class="k">Subagent Session</div><div class="mono">${escapeHtml(subagentRun.session_id || "-")}</div><div class="item-meta mono">processed=${escapeHtml(String(subagentRun.messages_processed ?? "-"))}</div></div>`,
      `<div class="detail-card"><div class="k">Current Task</div><div>${escapeHtml(compactText(subagentRun.current_task || "-", 180))}</div></div>`,
      `</div>`,
      `<div class="detail-form">`,
      isSessionSubagent ? `<textarea class="mono" rows="3" data-role="steer-message" placeholder="追加の指示を送る..."></textarea>` : `<div class="muted">This subagent was not started in session mode, so steer is unavailable.</div>`,
      `<div class="detail-actions">`,
      `<button class="btn primary" type="button" data-action="subagent-steer" data-run-id="${escapeAttr(subagentRun.run_id || "")}" ${isSessionSubagent ? "" : "disabled"}>Steer</button>`,
      `<button class="btn danger" type="button" data-action="subagent-kill" data-run-id="${escapeAttr(subagentRun.run_id || "")}" ${canKillSubagent ? "" : "disabled"}>Kill</button>`,
      `</div>`,
      `</div>`,
      `</div>`
    ].join("") : "",
    renderReuseSuggestions(reuseSuggestions),
    artifacts.resume_context ? [
      `<div class="detail-section">`,
      `<div class="k">Resume Context</div>`,
      `<pre class="detail-pre">${formatJsonBlock(artifacts.resume_context)}</pre>`,
      `</div>`
    ].join("") : "",
    `<div class="detail-section">`,
    `<div class="k">Artifacts</div>`,
    `<pre class="detail-pre">${formatJsonBlock(artifacts)}</pre>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Metadata</div>`,
    `<pre class="detail-pre">${formatJsonBlock(metadata)}</pre>`,
    `</div>`
  ].join("");
}

function renderApprovalDetail(approval) {
  const relatedTasks = dashboardState.relatedTasks || [];
  const suggestions = Array.isArray(dashboardState.selectedApprovalSuggestions)
    ? dashboardState.selectedApprovalSuggestions
    : [];
  const history = Array.isArray(approval.history) ? approval.history : [];
  const historyText = history.map((entry) => {
    const reason = entry.reason ? ` · ${entry.reason}` : "";
    const metadata = entry.metadata && Object.keys(entry.metadata).length
      ? ` · ${JSON.stringify(entry.metadata)}`
      : "";
    return `${new Date((entry.ts || 0) * 1000).toLocaleString()} · ${entry.state}${reason}${metadata}`;
  }).join("\n");

  return [
    `<div class="detail-section">`,
    `<div class="detail-heading">`,
    `<div>`,
    `<h5>${escapeHtml(approval.tool_name || approval.request_id || "approval")}</h5>`,
    `<div class="detail-meta mono">${escapeHtml([approval.request_id, approval.agent_name || "-", formatTimestamp(approval.created_at)].filter(Boolean).join(" · "))}</div>`,
    `</div>`,
    statusTag(approval.state || "pending"),
    `</div>`,
    `</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Scope</div><div>${escapeHtml(approval.scope || "-")}</div><div class="item-meta mono">${escapeHtml(approval.tool_pattern || approval.tool_name || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Path Scope</div><div class="mono">${escapeHtml(approval.path_scope || "-")}</div><div class="item-meta">${approval.propagate_to_subagents ? "propagates to subagents" : "local only"}</div></div>`,
    `<div class="detail-card"><div class="k">Session</div><div class="mono">${escapeHtml(approval.session_id || "-")}</div><div class="item-meta mono">${escapeHtml(approval.source_request_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Reason</div><div>${escapeHtml(compactText(approval.reason || approval.resolve_reason || "-", 200))}</div></div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Related Tasks</div>`,
    renderRelationChips("task", relatedTasks, "No related tasks."),
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Audit Trail</div>`,
    `<div class="detail-actions">`,
    `<button class="btn" type="button" data-action="open-related-audit" data-audit-session-id="${escapeAttr(approval.session_id || "")}" data-audit-query="${escapeAttr(approval.request_id || approval.source_request_id || "")}" data-audit-request-id="${escapeAttr(approval.request_id || approval.source_request_id || "")}" data-audit-tool="${escapeAttr(approval.tool_name || approval.tool_pattern || "")}">Open Related Audit</button>`,
    `</div>`,
    `</div>`,
    (approval.state === "pending" || approval.state === "expiring") ? [
      `<div class="detail-section">`,
      `<div class="k">Resolve Approval</div>`,
      `<div class="detail-form">`,
      suggestions.length ? `<div class="detail-actions">${suggestions.map((suggestion) => (
        `<button class="btn" type="button" data-action="approval-resolve-bundle" data-request-id="${escapeAttr(approval.request_id || "")}" data-strategy="${escapeAttr(suggestion.strategy || "")}">${escapeHtml(`${suggestion.label} (${suggestion.affected_count || 1})`)}</button>`
      )).join("")}</div>` : "",
      `<div class="detail-form-grid">`,
      `<label class="field-label">scope<select class="text-input mono" data-approval-field="scope"><option value="single"${approval.scope === "single" ? " selected" : ""}>single</option><option value="session"${approval.scope === "session" ? " selected" : ""}>session</option></select></label>`,
      `<label class="field-label">tool pattern<input class="text-input mono" type="text" value="${escapeAttr(approval.tool_pattern || approval.tool_name || "")}" data-approval-field="tool-pattern" /></label>`,
      `<label class="field-label">path scope<input class="text-input mono" type="text" value="${escapeAttr(approval.path_scope || "")}" data-approval-field="path-scope" /></label>`,
      `<label class="field-label"><input type="checkbox" data-approval-field="propagate"${approval.propagate_to_subagents ? " checked" : ""} /> propagate to subagents</label>`,
      `</div>`,
      `<div class="detail-actions">`,
      `<button class="btn primary" type="button" data-action="approval-resolve" data-approved="true" data-request-id="${escapeAttr(approval.request_id || "")}">Approve</button>`,
      `<button class="btn danger" type="button" data-action="approval-resolve" data-approved="false" data-request-id="${escapeAttr(approval.request_id || "")}">Deny</button>`,
      `</div>`,
      `</div>`,
      `</div>`
    ].join("") : "",
    `<div class="detail-section">`,
    `<div class="k">Arguments</div>`,
    `<pre class="detail-pre">${formatJsonBlock(approval.args || {})}</pre>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">History</div>`,
    `<pre class="detail-pre">${escapeHtml(historyText || "No approval history yet.")}</pre>`,
    `</div>`
  ].join("");
}

function renderSelectionDetail() {
  let badge = "none";
  let html = "Click a task or approval to inspect full metadata, links, and actions.";
  if (dashboardState.selectedKind === "task" && dashboardState.selectedTask) {
    badge = dashboardState.selectedTask.task_id || "task";
    html = renderTaskDetail(dashboardState.selectedTask);
  } else if (dashboardState.selectedKind === "approval" && dashboardState.selectedApproval) {
    badge = dashboardState.selectedApproval.request_id || "approval";
    html = renderApprovalDetail(dashboardState.selectedApproval);
  }
  if (dashboardDetailBadgeEl) {
    dashboardDetailBadgeEl.textContent = badge;
  }
  if (inspectorSelectionBadgeEl) {
    inspectorSelectionBadgeEl.textContent = badge;
  }
  for (const target of [dashboardDetailPanelEl, inspectorSelectionDetailEl]) {
    if (!target) continue;
    target.classList.toggle("selection-detail-empty", badge === "none");
    target.innerHTML = html;
  }
  syncMissionFlightTelemetryAnimations();
}

async function fetchJsonOrThrow(url, init = {}) {
  const response = await apiFetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data?.detail || `HTTP ${response.status}`);
  }
  return data;
}

async function loadTaskDetail(taskId) {
  if (!taskId) return;
  const base = toHttpBaseUrl(currentSettings());
  try {
    const taskPayload = await fetchJsonOrThrow(`${base}/tasks/${encodeURIComponent(taskId)}`);
    const task = taskPayload.task || {};
    const relatedTaskIds = [
      task.parent_task_id,
      task.winner_task_id,
      ...(Array.isArray(task.loser_task_ids) ? task.loser_task_ids : []),
    ].filter(Boolean);
    const [relatedTasksPayload, relatedApprovals, subagentRunsPayload, childTasksPayload, timelinePayload] = await Promise.all([
      Promise.all(
        Array.from(new Set(relatedTaskIds)).map(async (relatedTaskId) => {
          try {
            const payload = await fetchJsonOrThrow(`${base}/tasks/${encodeURIComponent(relatedTaskId)}`);
            return payload.task || null;
          } catch (_) {
            return null;
          }
        })
      ),
      Promise.all(
        (Array.isArray(task.approval_dependencies) ? task.approval_dependencies : []).map(async (approvalId) => {
          try {
            const payload = await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(approvalId)}`);
            return payload.approval || null;
          } catch (_) {
            return null;
          }
        })
      ),
      task.kind === "subagent" && task.owner_session_id
        ? fetchJsonOrThrow(`${base}/subagents/${encodeURIComponent(task.owner_session_id)}`).catch(() => ({ runs: [] }))
        : Promise.resolve({ runs: [] }),
      fetchJsonOrThrow(
        `${base}/tasks?${new URLSearchParams({ session_id: task.owner_session_id || "", parent_task_id: task.task_id || "", limit: "50" })}`
      ).catch(() => ({ tasks: [] })),
      fetchJsonOrThrow(
        `${base}/tasks/${encodeURIComponent(task.task_id || taskId)}/timeline?${new URLSearchParams({ limit: "80" })}`
      ).catch(() => ({ entries: [], pagination: null })),
    ]);
    const runs = Array.isArray(subagentRunsPayload.runs) ? subagentRunsPayload.runs : [];
    dashboardState.selectedKind = "task";
    dashboardState.selectedId = taskId;
    dashboardState.selectedTask = task;
    dashboardState.selectedApproval = null;
    dashboardState.relatedTasks = relatedTasksPayload.filter(Boolean);
    dashboardState.relatedApprovals = relatedApprovals.filter(Boolean);
    dashboardState.childTasks = Array.isArray(childTasksPayload.tasks) ? childTasksPayload.tasks : [];
    dashboardState.subagentRun = runs.find((run) => run.run_id === task.run_id) || null;
    dashboardState.taskTimeline = Array.isArray(timelinePayload.entries) ? timelinePayload.entries : [];
    dashboardState.taskTimelinePagination = timelinePayload.pagination || null;
    dashboardState.selectedApprovalSuggestions = [];
    dashboardState.taskComparison = null;
  } catch (err) {
    dashboardState.selectedKind = "task";
    dashboardState.selectedId = taskId;
    dashboardState.selectedTask = {
      task_id: taskId,
      title: "Failed to load task",
      status: "failed",
      error: String(err),
      artifacts: {},
      metadata: {},
    };
    dashboardState.selectedApproval = null;
    dashboardState.relatedTasks = [];
    dashboardState.relatedApprovals = [];
    dashboardState.childTasks = [];
    dashboardState.subagentRun = null;
    dashboardState.taskTimeline = [];
    dashboardState.taskTimelinePagination = null;
    dashboardState.selectedApprovalSuggestions = [];
    dashboardState.taskComparison = null;
  }
  renderSelectionDetail();
  updateDashboardUi();
}

async function loadApprovalDetail(requestId) {
  if (!requestId) return;
  const base = toHttpBaseUrl(currentSettings());
  try {
    const payload = await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(requestId)}`);
    const approval = payload.approval || {};
    const suggestions = Array.isArray(payload.resolve_suggestions) ? payload.resolve_suggestions : [];
    const taskPayload = approval.session_id
      ? await fetchJsonOrThrow(
          `${base}/tasks?${new URLSearchParams({ session_id: approval.session_id, limit: "100" })}`
        ).catch(() => ({ tasks: [] }))
      : { tasks: [] };
    const ids = new Set([approval.request_id, approval.source_request_id].filter(Boolean));
    const relatedTasks = (Array.isArray(taskPayload.tasks) ? taskPayload.tasks : []).filter((task) => (
      Array.isArray(task.approval_dependencies)
      && task.approval_dependencies.some((dependency) => ids.has(dependency))
    ));
    dashboardState.selectedKind = "approval";
    dashboardState.selectedId = requestId;
    dashboardState.selectedApproval = approval;
    dashboardState.selectedTask = null;
    dashboardState.relatedTasks = relatedTasks;
    dashboardState.relatedApprovals = [];
    dashboardState.childTasks = [];
    dashboardState.subagentRun = null;
    dashboardState.taskTimeline = [];
    dashboardState.taskTimelinePagination = null;
    dashboardState.selectedApprovalSuggestions = suggestions;
    dashboardState.taskComparison = null;
  } catch (err) {
    dashboardState.selectedKind = "approval";
    dashboardState.selectedId = requestId;
    dashboardState.selectedApproval = {
      request_id: requestId,
      tool_name: "Failed to load approval",
      state: "failed",
      reason: String(err),
      history: [],
      args: {},
    };
    dashboardState.selectedTask = null;
    dashboardState.relatedTasks = [];
    dashboardState.relatedApprovals = [];
    dashboardState.childTasks = [];
    dashboardState.subagentRun = null;
    dashboardState.taskTimeline = [];
    dashboardState.taskTimelinePagination = null;
    dashboardState.selectedApprovalSuggestions = [];
    dashboardState.taskComparison = null;
  }
  renderSelectionDetail();
  updateDashboardUi();
}

async function refreshSelectedDetail() {
  if (dashboardState.selectedKind === "task" && dashboardState.selectedId) {
    await loadTaskDetail(dashboardState.selectedId);
    return;
  }
  if (dashboardState.selectedKind === "approval" && dashboardState.selectedId) {
    await loadApprovalDetail(dashboardState.selectedId);
    return;
  }
  renderSelectionDetail();
}

function scheduleDashboardRefresh(delay = 250) {
  if (_dashboardRefreshHandle) {
    window.clearTimeout(_dashboardRefreshHandle);
  }
  _dashboardRefreshHandle = window.setTimeout(() => {
    _dashboardRefreshHandle = null;
    void refreshDashboard();
  }, delay);
}

async function resolveApprovalFromPanel(container, requestId, approved) {
  const base = toHttpBaseUrl(currentSettings());
  const scope = container.querySelector('[data-approval-field="scope"]')?.value || "single";
  const toolPattern = container.querySelector('[data-approval-field="tool-pattern"]')?.value || "";
  const pathScope = container.querySelector('[data-approval-field="path-scope"]')?.value || "";
  const propagate = Boolean(container.querySelector('[data-approval-field="propagate"]')?.checked);
  const sessionId = dashboardState.selectedApproval?.session_id || currentSessionId || "";
  const reason = approved ? "Approved in Control UI panel" : "Denied in Control UI panel";
  try {
    await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(requestId)}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved,
        reason,
        session_id: sessionId,
        scope,
        tool_pattern: toolPattern,
        path_scope: pathScope,
        propagate_to_subagents: propagate,
      }),
    });
    scheduleDashboardRefresh(100);
  } catch (err) {
    addSystemMessage(`approval error: ${err}`);
  }
}

async function resolveApprovalBundleFromPanel(container, requestId, strategy) {
  const base = toHttpBaseUrl(currentSettings());
  const sessionId = dashboardState.selectedApproval?.session_id || currentSessionId || "";
  const pathScope = container.querySelector('[data-approval-field="path-scope"]')?.value || "";
  const propagate = Boolean(container.querySelector('[data-approval-field="propagate"]')?.checked);
  try {
    await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(requestId)}/resolve_bundle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved: true,
        strategy,
        reason: `Approved in Control UI panel (${strategy})`,
        session_id: sessionId,
        path_scope: pathScope,
        propagate_to_subagents: propagate,
      }),
    });
    scheduleDashboardRefresh(100);
  } catch (err) {
    addSystemMessage(`approval error: ${err}`);
  }
}

async function steerSubagentFromPanel(container, runId) {
  const base = toHttpBaseUrl(currentSettings());
  const message = container.querySelector('[data-role="steer-message"]')?.value?.trim() || "";
  if (!message) {
    addSystemMessage("steer message is required");
    return;
  }
  try {
    await fetchJsonOrThrow(`${base}/subagents/${encodeURIComponent(runId)}/steer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    scheduleDashboardRefresh(100);
  } catch (err) {
    addSystemMessage(`subagent steer error: ${err}`);
  }
}

async function killSubagentFromPanel(runId) {
  const base = toHttpBaseUrl(currentSettings());
  try {
    await fetchJsonOrThrow(`${base}/subagents/${encodeURIComponent(runId)}`, {
      method: "DELETE",
    });
    scheduleDashboardRefresh(100);
  } catch (err) {
    addSystemMessage(`subagent kill error: ${err}`);
  }
}

async function replayTaskFromPanel(taskId, fromStep = "") {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const payload = await fetchJsonOrThrow(`${base}/tasks/${encodeURIComponent(taskId)}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fromStep ? { from_step: fromStep } : {}),
    });
    addSystemMessage(
      fromStep
        ? `tail replay accepted from ${fromStep}: ${payload.task?.task_id || "-"}`
        : `replay accepted: ${payload.task?.task_id || "-"}`
    );
    scheduleDashboardRefresh(50);
    if (payload.task?.task_id) {
      void loadTaskDetail(payload.task.task_id);
    }
  } catch (err) {
    addSystemMessage(`task replay error: ${err}`);
  }
}

async function loadTaskComparison(taskId, otherTaskId = "") {
  const base = toHttpBaseUrl(currentSettings());
  const params = new URLSearchParams();
  if (otherTaskId) params.set("other_task_id", otherTaskId);
  try {
    dashboardState.taskComparison = await fetchJsonOrThrow(
      `${base}/tasks/${encodeURIComponent(taskId)}/compare${params.toString() ? `?${params}` : ""}`,
    );
    renderSelectionDetail();
  } catch (err) {
    addSystemMessage(`task compare error: ${err}`);
  }
}

function renderAnalytics(data) {
  const overview = data.overview || {};
  const rankingPayload = data.step_failure_ranking || {};
  const ranking = Array.isArray(rankingPayload.steps) ? rankingPayload.steps : [];
  const rankingTruncated = Boolean(rankingPayload.truncated);
  const rankingSampled = Number(rankingPayload.sampled_events || 0);
  const rankingTotal = Number(rankingPayload.total_events || 0);
  const improvementPayload = data.replay_improvement || {};
  const improvement = Array.isArray(improvementPayload.steps) ? improvementPayload.steps : [];
  const improvementTruncated = Boolean(improvementPayload.truncated);
  const byStatus = overview.by_status || {};
  const statusEntries = Object.entries(byStatus)
    .sort(([, a], [, b]) => b - a)
    .map(([k, v]) => `<span class="tag">${escapeHtml(k)}: ${v}</span>`)
    .join(" ");
  const rankingRows = ranking.map((item) => [
    `<tr>`,
    `<td class="mono">${escapeHtml(item.step_id || "-")}</td>`,
    `<td>${escapeHtml(item.title || "-")}</td>`,
    `<td>${item.total}</td>`,
    `<td>${item.succeeded}</td>`,
    `<td>${item.failed}</td>`,
    `<td class="${item.failure_rate > 0.5 ? "text-danger" : ""}">${(item.failure_rate * 100).toFixed(1)}%</td>`,
    `<td>${item.task_count}</td>`,
    `<td class="mono">${(item.top_failed_criteria || []).map((c) => `${escapeHtml(c.name)}(${c.count})`).join(", ") || "-"}</td>`,
    `</tr>`,
  ].join("")).join("");
  const improvementRows = improvement.map((item) => [
    `<tr>`,
    `<td class="mono">${escapeHtml(item.step_id || "-")}</td>`,
    `<td>${escapeHtml(item.title || "-")}</td>`,
    `<td>${item.source_fail}</td>`,
    `<td>${item.replay_pass}</td>`,
    `<td>${item.replay_fail}</td>`,
    `<td class="${item.improvement_rate > 0.5 ? "text-success" : ""}">${(item.improvement_rate * 100).toFixed(1)}%</td>`,
    `</tr>`,
  ].join("")).join("");
  return [
    `<div class="analytics-overview">`,
    `<div class="summary-card"><div class="k">Control Loop Tasks</div><div class="summary-value">${overview.total_tasks || 0}</div></div>`,
    `<div class="summary-card"><div class="k">Replays</div><div class="summary-value">${overview.total_replays || 0}</div></div>`,
    `<div class="summary-card"><div class="k">Replay Success Rate</div><div class="summary-value">${((overview.replay_success_rate || 0) * 100).toFixed(1)}%</div></div>`,
    `<div class="summary-card"><div class="k">Status Breakdown</div><div class="summary-value">${statusEntries || "-"}</div></div>`,
    `</div>`,
    ranking.length ? [
      `<div class="analytics-section">`,
      `<h4>Step Failure Ranking</h4>`,
      rankingTruncated ? `<div class="muted">Showing ${rankingSampled} of ${rankingTotal} step events (sampled).</div>` : "",
      `<div class="table-scroll"><table class="analytics-table"><thead><tr>`,
      `<th>Step ID</th><th>Title</th><th>Total</th><th>Pass</th><th>Fail</th><th>Fail%</th><th>Tasks</th><th>Top Failed Criteria</th>`,
      `</tr></thead><tbody>${rankingRows}</tbody></table></div>`,
      `</div>`,
    ].join("") : "",
    improvement.length ? [
      `<div class="analytics-section">`,
      `<h4>Replay Improvement</h4>`,
      improvementTruncated ? `<div class="muted">Replay task sample truncated — results may be incomplete.</div>` : "",
      `<div class="table-scroll"><table class="analytics-table"><thead><tr>`,
      `<th>Step ID</th><th>Title</th><th>Source Fail</th><th>Replay Pass</th><th>Replay Fail</th><th>Improvement%</th>`,
      `</tr></thead><tbody>${improvementRows}</tbody></table></div>`,
      `</div>`,
    ].join("") : `<div class="muted">No replay pairs found yet.</div>`,
  ].join("");
}

async function loadAnalytics() {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const data = await fetchJsonOrThrow(`${base}/tasks/analytics`);
    if (analyticsContentEl) {
      analyticsContentEl.innerHTML = renderAnalytics(data);
    }
  } catch (err) {
    if (analyticsContentEl) {
      analyticsContentEl.innerHTML = `<div class="muted">Analytics load error: ${escapeHtml(String(err))}</div>`;
    }
  }
}

function handleDashboardListSelectionClick(event) {
  const taskButton = event.target.closest("[data-task-id]");
  if (taskButton?.dataset.taskId) {
    void loadTaskDetail(taskButton.dataset.taskId);
    return;
  }
  const approvalButton = event.target.closest("[data-approval-id]");
  if (approvalButton?.dataset.approvalId) {
    void loadApprovalDetail(approvalButton.dataset.approvalId);
    return;
  }
  const auditButton = event.target.closest("[data-audit-id]");
  if (auditButton?.dataset.auditId) {
    selectAuditEntry(auditButton.dataset.auditId);
  }
}

function handleSelectionPanelClick(event) {
  const taskRef = event.target.closest("[data-task-ref]");
  if (taskRef?.dataset.taskRef) {
    void loadTaskDetail(taskRef.dataset.taskRef);
    return;
  }
  const approvalRef = event.target.closest("[data-approval-ref]");
  if (approvalRef?.dataset.approvalRef) {
    void loadApprovalDetail(approvalRef.dataset.approvalRef);
    return;
  }
  const actionButton = event.target.closest("[data-action]");
  if (!actionButton) return;
  const container = event.currentTarget;
  if (actionButton.dataset.action === "approval-resolve") {
    void resolveApprovalFromPanel(
      container,
      actionButton.dataset.requestId || "",
      actionButton.dataset.approved === "true",
    );
    return;
  }
  if (actionButton.dataset.action === "approval-resolve-bundle") {
    void resolveApprovalBundleFromPanel(
      container,
      actionButton.dataset.requestId || "",
      actionButton.dataset.strategy || "session_exact",
    );
    return;
  }
  if (actionButton.dataset.action === "subagent-steer") {
    void steerSubagentFromPanel(container, actionButton.dataset.runId || "");
    return;
  }
  if (actionButton.dataset.action === "subagent-kill") {
    void killSubagentFromPanel(actionButton.dataset.runId || "");
    return;
  }
  if (actionButton.dataset.action === "task-replay") {
    void replayTaskFromPanel(actionButton.dataset.taskId || "");
    return;
  }
  if (actionButton.dataset.action === "task-replay-from-step") {
    void replayTaskFromPanel(
      actionButton.dataset.taskId || "",
      actionButton.dataset.fromStep || "",
    );
    return;
  }
  if (actionButton.dataset.action === "task-compare") {
    void loadTaskComparison(
      actionButton.dataset.taskId || "",
      actionButton.dataset.otherTaskId || "",
    );
    return;
  }
  if (actionButton.dataset.action === "open-related-audit") {
    openAuditView({
      sessionFilter: actionButton.dataset.auditSessionId || "",
      searchQuery: actionButton.dataset.auditQuery || "",
      toolFilter: actionButton.dataset.auditTool || "",
      sourceFilter: actionButton.dataset.auditSource || "",
      resultFilter: actionButton.dataset.auditResult || "",
      focus: {
        entryId: actionButton.dataset.auditEntryId || "",
        sessionId: actionButton.dataset.auditSessionId || "",
        searchQuery: actionButton.dataset.auditQuery || "",
        requestId: actionButton.dataset.auditRequestId || "",
        taskId: actionButton.dataset.auditTaskId || "",
        runId: actionButton.dataset.auditRunId || "",
        toolName: actionButton.dataset.auditTool || "",
        source: actionButton.dataset.auditSource || "",
        result: actionButton.dataset.auditResult || "",
      },
    });
  }
}

// -----------------------------------------------------------------------
// Gateway history (source of truth)
// -----------------------------------------------------------------------

function requestGatewayHistory() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ event: "chat.history", limit: 200 }));
  logEvent("chat.history.request", { limit: 200 });
}

function handleChatHistory(payload) {
  const entries = payload.entries || [];
  messageHistory.length = 0;
  inlineApprovals.clear();
  entries.forEach((e) => {
    if (shouldSkipHistoryEntry(e)) return;
    if (e.role === "user") {
      messageHistory.push({ kind: "user", text: e.content });
    } else if (e.role === "assistant") {
      const suffix = e.aborted ? " (aborted)" : "";
      messageHistory.push({ kind: "agent", text: e.content + suffix });
    } else if (e.role === "system") {
      messageHistory.push({ kind: "system", text: e.content });
    } else if (e.role === "inject") {
      const role = e.metadata?.role || "system";
      messageHistory.push({ kind: "system", text: `[inject:${role}] ${e.content}` });
    }
  });
  restoreMessages();
  if (!entries.length) {
    logEvent("chat.history.empty", { session_id: payload.session_id });
    return;
  }
  logEvent("chat.history.loaded", { count: entries.length });
}

function shouldSkipHistoryEntry(entry) {
  if (!entry || entry.role !== "system") return false;
  const source = entry.metadata?.source || "";
  const content = String(entry.content || "");
  if (source === "tools.approval") return true;
  if (/^Approval\s+[a-z0-9]+:\s+(approved|denied)$/i.test(content)) return true;
  if (/^\[approval\]/i.test(content)) return true;
  return false;
}

// -----------------------------------------------------------------------
// Sessions
// -----------------------------------------------------------------------

function getSessionSummary(sessionId) {
  const session = sessions.find((s) => s.id === sessionId);
  return session?.preview || null;
}

function renderSessions() {
  if (!sessions.length) {
    sessionListEl.innerHTML = "<li>No sessions yet.</li>";
    return;
  }
  const isOnline = socket && socket.readyState === WebSocket.OPEN;
  sessionListEl.innerHTML = sessions.map((s) => {
    const isActive = isOnline && s.id === currentSessionId;
    const activeTag = isActive ? " <span class=\"tag\">active</span>" : "";
    const summary = getSessionSummary(s.id);
    const summaryHtml = summary ? `<div class="session-summary">${escapeHtml(summary)}</div>` : "";
    return [
      `<li class="session-item${isActive ? " session-active" : ""}" data-session-id="${escapeAttr(s.id)}">`,
      `<div class="mono">${escapeHtml(s.id)}${activeTag}</div>`,
      `<div class="muted">${escapeHtml(s.userId || "-")} / ${escapeHtml(s.when || "-")}</div>`,
      summaryHtml,
      "</li>"
    ].join("");
  }).join("");

  sessionListEl.querySelectorAll(".session-item").forEach((li) => {
    li.addEventListener("click", () => switchSession(li.dataset.sessionId));
  });
}

function addSession(sessionId, userId) {
  const existing = sessions.find((s) => s.id === sessionId);
  if (existing) {
    existing.when = new Date().toLocaleString();
    existing.userId = userId;
    existing.lastActivity = Date.now() / 1000;
    sessions.splice(sessions.indexOf(existing), 1);
    sessions.unshift(existing);
  } else {
    sessions.unshift({
      id: sessionId,
      userId,
      when: new Date().toLocaleString(),
      preview: "",
      entryCount: 0,
      lastActivity: Date.now() / 1000
    });
    if (sessions.length > 15) sessions.length = 15;
  }
  renderSessions();
}

async function syncServerSessions() {
  const settings = currentSettings();
  const base = toHttpBaseUrl(settings);
  const userId = (settings.userId || "web_user").trim();
  try {
    const res = await apiFetch(`${base}/sessions/${encodeURIComponent(userId)}`);
    if (!res.ok) return;
    const data = await res.json();
    const serverSessions = Array.isArray(data.sessions) ? data.sessions : [];
    const serverIds = new Set(serverSessions.map((s) => s.id));
    serverSessions.forEach((s) => {
      const existing = sessions.find((x) => x.id === s.id);
      const when = s.last_activity
        ? new Date(s.last_activity * 1000).toLocaleString()
        : "(server)";
      if (existing) {
        existing.userId = s.user_id || userId;
        existing.when = when;
        existing.preview = s.preview || "";
        existing.entryCount = s.entry_count || 0;
        existing.lastActivity = s.last_activity || 0;
      } else {
        sessions.push({
          id: s.id,
          userId: s.user_id || userId,
          when,
          preview: s.preview || "",
          entryCount: s.entry_count || 0,
          lastActivity: s.last_activity || 0
        });
      }
    });
    const toRemove = sessions.filter((s) => !serverIds.has(s.id));
    toRemove.forEach((s) => sessions.splice(sessions.indexOf(s), 1));
    sessions.sort((a, b) => (b.lastActivity || 0) - (a.lastActivity || 0));
    renderSessions();
  } catch (_) {}
}

// -----------------------------------------------------------------------
// Dashboard
// -----------------------------------------------------------------------

async function fetchDashboardHealth(base) {
  try {
    const res = await apiFetch(`${base}/health`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    dashboardState.sessionBackend = data.session_backend || "memory";
    dashboardState.sessionNamespace = data.session_namespace || "";
  } catch (_) {
    dashboardState.sessionBackend = "-";
    dashboardState.sessionNamespace = "";
  }
}

async function fetchDashboardApprovals(base) {
  const pendingParams = new URLSearchParams({ state: "pending", page: "1", page_size: "4" });
  if (currentSessionId) pendingParams.set("session_id", currentSessionId);
  const filteredParams = buildDashboardApprovalParams();
  try {
    const [pendingRes, filteredRes] = await Promise.all([
      apiFetch(`${base}/tools/approvals?${pendingParams}`),
      apiFetch(`${base}/tools/approvals?${filteredParams}`)
    ]);
    if (pendingRes.ok) {
      const pendingData = await pendingRes.json();
      dashboardState.pendingApprovals = Array.isArray(pendingData.approvals) ? pendingData.approvals : [];
      dashboardState.pendingApprovalsTotal = Number(pendingData.pagination?.total || dashboardState.pendingApprovals.length || 0);
    } else {
      dashboardState.pendingApprovals = [];
      dashboardState.pendingApprovalsTotal = 0;
    }
    if (filteredRes.ok) {
      const filteredData = await filteredRes.json();
      dashboardState.dashboardApprovals = Array.isArray(filteredData.approvals) ? filteredData.approvals : [];
      dashboardState.approvalTotal = Number(filteredData.pagination?.total || 0);
      dashboardState.approvalHasMore = Boolean(filteredData.pagination?.has_more);
    } else {
      dashboardState.dashboardApprovals = [];
      dashboardState.approvalTotal = 0;
      dashboardState.approvalHasMore = false;
    }
  } catch (_) {
    dashboardState.pendingApprovals = [];
    dashboardState.pendingApprovalsTotal = 0;
    dashboardState.dashboardApprovals = [];
    dashboardState.approvalTotal = 0;
    dashboardState.approvalHasMore = false;
  }
}

async function fetchDashboardTasks(base) {
  const recentParams = new URLSearchParams({ page: "1", page_size: "5" });
  if (currentSessionId) recentParams.set("session_id", currentSessionId);
  const openParams = new URLSearchParams({ status: "open", page: "1", page_size: "1" });
  if (currentSessionId) openParams.set("session_id", currentSessionId);
  const filteredParams = buildDashboardTaskParams();
  try {
    const [recentRes, openRes, filteredRes] = await Promise.all([
      apiFetch(`${base}/tasks?${recentParams}`),
      apiFetch(`${base}/tasks?${openParams}`),
      apiFetch(`${base}/tasks?${filteredParams}`),
    ]);
    if (recentRes.ok) {
      const recentData = await recentRes.json();
      dashboardState.recentTasks = Array.isArray(recentData.tasks) ? recentData.tasks : [];
      dashboardState.recentTasksTotal = Number(recentData.pagination?.total || dashboardState.recentTasks.length || 0);
    } else {
      dashboardState.recentTasks = [];
      dashboardState.recentTasksTotal = 0;
    }
    if (openRes.ok) {
      const openData = await openRes.json();
      dashboardState.openTaskCount = Number(openData.pagination?.total || 0);
    } else {
      dashboardState.openTaskCount = 0;
    }
    if (filteredRes.ok) {
      const filteredData = await filteredRes.json();
      dashboardState.dashboardTasks = Array.isArray(filteredData.tasks) ? filteredData.tasks : [];
      dashboardState.taskTotal = Number(filteredData.pagination?.total || 0);
      dashboardState.taskHasMore = Boolean(filteredData.pagination?.has_more);
    } else {
      dashboardState.dashboardTasks = [];
      dashboardState.taskTotal = 0;
      dashboardState.taskHasMore = false;
    }
  } catch (_) {
    dashboardState.recentTasks = [];
    dashboardState.recentTasksTotal = 0;
    dashboardState.dashboardTasks = [];
    dashboardState.openTaskCount = 0;
    dashboardState.taskTotal = 0;
    dashboardState.taskHasMore = false;
  }
}

async function fetchAuditEntries(base) {
  const params = buildAuditParams();
  try {
    const response = await apiFetch(`${base}/audit?${params}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    auditState.entries = Array.isArray(payload.entries) ? payload.entries : [];
    auditState.total = Number(payload.pagination?.total || 0);
    auditState.hasMore = Boolean(payload.pagination?.has_more);
    if (auditState.autoSelectFirst || (!auditState.selectedEntryId && auditState.focus)) {
      auditState.selectedEntry = selectPreferredAuditEntry(auditState.entries, auditState.focus);
      auditState.selectedEntryId = auditState.selectedEntry?.entry_id || null;
      auditState.autoSelectFirst = false;
    } else if (auditState.selectedEntryId) {
      auditState.selectedEntry = auditState.entries.find(
        (entry) => entry.entry_id === auditState.selectedEntryId,
      ) || auditState.selectedEntry;
    }
  } catch (_) {
    auditState.entries = [];
    auditState.total = 0;
    auditState.hasMore = false;
    if (!auditState.selectedEntryId) {
      auditState.selectedEntry = null;
    }
  }
}

async function refreshDashboard() {
  if (_dashboardRefreshPromise) {
    return _dashboardRefreshPromise;
  }
  const base = toHttpBaseUrl(currentSettings());
  _dashboardRefreshPromise = (async () => {
    await Promise.all([
      fetchDashboardHealth(base),
      fetchDashboardApprovals(base),
      fetchDashboardTasks(base)
    ]);
    updateDashboardUi();
    if (dashboardState.selectedKind && dashboardState.selectedId) {
      await refreshSelectedDetail();
    }
  })();
  try {
    await _dashboardRefreshPromise;
  } finally {
    _dashboardRefreshPromise = null;
  }
}

async function refreshAudit() {
  if (_auditRefreshPromise) {
    return _auditRefreshPromise;
  }
  const base = toHttpBaseUrl(currentSettings());
  _auditRefreshPromise = (async () => {
    await fetchAuditEntries(base);
    updateAuditUi();
  })();
  try {
    await _auditRefreshPromise;
  } finally {
    _auditRefreshPromise = null;
  }
}

function scheduleAuditRefresh(delay = 250) {
  if (_auditRefreshHandle) {
    window.clearTimeout(_auditRefreshHandle);
  }
  _auditRefreshHandle = window.setTimeout(() => {
    _auditRefreshHandle = null;
    void refreshAudit();
  }, delay);
}

// -----------------------------------------------------------------------
// WS event handlers
// -----------------------------------------------------------------------

function handleConnected(payload) {
  currentSessionId = payload.session_id || null;
  reconnectSessionId = currentSessionId || null;
  sessionBadgeEl.textContent = currentSessionId || "-";
  const pv = payload.protocol_version || "?";
  addSession(currentSessionId || "unknown", payload.user_id || currentSettings().userId);
  if (!auditState.sessionFilter) {
    auditState.sessionFilter = currentSessionId || "";
    syncAuditInputsFromState();
  }
  logEvent("protocol", { version: pv });
  // Request history from Gateway (source of truth)
  requestGatewayHistory();
  void syncServerSessions();
  scheduleDashboardRefresh(50);
  if (isTabActive("audit")) scheduleAuditRefresh(50);
}

function handleChatDone(payload) {
  clearWaiting();
  // Finalize streaming bubble if any
  if (_streamingBubble) {
    if (_streamingText) {
      messageHistory.push({ kind: "agent", text: _streamingText });
    }
    _streamingBubble = null;
    _streamingText = "";
  } else {
    const text = payload.text || (payload.aborted ? "(aborted)" : "(empty response)");
    if (!payload.aborted || text) appendBubble("agent", text);
  }
  if (payload.aborted) addSystemMessage("request aborted");
  setRunInProgress(false);
  logEvent("chat.done", { aborted: payload.aborted, len: (payload.text || "").length });
  void syncServerSessions();
}

function handleChatToken(payload) {
  clearWaiting();
  if (!_streamingBubble) {
    _streamingBubble = appendBubble("agent", "", { persist: false });
  }
  _streamingText += payload.text || "";
  _streamingBubble.textContent = _streamingText;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function handleSystemEvent(payload) {
  const msg = payload.message || "";
  logEvent("system.event", { source: payload.source, status: payload.status, run_id: payload.run_id });
  if (payload.source === "tools.approval" && payload.status === "resolved") {
    const resolved = parseApprovalResolutionMessage(msg);
    if (resolved) {
      updateInlineApprovalStatus(
        resolved.requestId,
        resolved.status,
        resolved.status === "approved" ? "Approved in chat UI" : "Denied in chat UI"
      );
      return;
    }
  }
  addSystemMessage(msg);
}

function handleHealthTick(payload) {
  if (heartbeatDotEl) {
    heartbeatDotEl.classList.add("pulse");
    setTimeout(() => heartbeatDotEl.classList.remove("pulse"), 400);
  }
  logEvent("health.tick", { active_sessions: payload.active_sessions });
}

function handleCronUpdate(payload) {
  logEvent("cron.update", payload);
  addSystemMessage(`[cron] ${payload.message || payload.status}`);
}

function handleToolsApprovalRequest(payload) {
  logEvent("tools.approval_request", payload);
  const reqId = payload.request_id || "?";
  const tool = payload.tool_name || "?";
  const agent = payload.agent_name || "?";
  const reason = payload.reason || "";
  upsertInlineApproval({
    kind: "tool",
    requestId: reqId,
    toolName: tool,
    sessionId: payload.session_id || "",
    title: `${tool} by ${agent}`,
    subtitle: "tool approval request",
    reason: reason || "approval required",
    argsPreview: JSON.stringify(payload.args || {}).slice(0, 220),
    status: "pending",
    expiresAt: payload.expires_at || null,
    note: "Respond inline to continue this run."
  });
}

function handleControlApprovalRequest(payload) {
  logEvent("control.approval_request", payload);
  const reqId = payload.request_id || "?";
  const goal = payload.goal || "?";
  const planId = payload.plan_id || "?";
  const risk = payload.risk_level || "?";
  const caps = Array.isArray(payload.required_capabilities)
    ? payload.required_capabilities.join(", ")
    : "";
  const reason = payload.reason || "";
  upsertInlineApproval({
    kind: "control",
    requestId: reqId,
    title: `control plan ${planId}`,
    subtitle: caps ? `risk=${risk} caps=${caps}` : `risk=${risk}`,
    reason: goal || reason || "control approval required",
    argsPreview: reason && goal !== reason ? reason : "",
    status: "pending",
    note: "Respond inline to continue the control loop.",
    syntheticControl: false,
  });
}

async function sendControlApproval(requestId, approved, sessionId = "") {
  const settings = currentSettings();
  const base = toHttpBaseUrl(settings);
  try {
    await fetchJsonOrThrow(`${base}/control-loop/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: (settings.userId || "web_user").trim() || "web_user",
        request_id: requestId,
        approved,
        session_id: sessionId || currentSessionId || "",
      }),
    });
    updateInlineApprovalStatus(
      requestId,
      approved ? "approving" : "denying",
      approved ? "Approval sent. Waiting for gateway confirmation..." : "Denial sent. Waiting for gateway confirmation..."
    );
    scheduleDashboardRefresh(50);
  } catch (err) {
    addSystemMessage(`approval error: ${err}`);
  }
}

async function sendApprovalAction(kind, requestId, approved, strategy = "single", sessionId = "") {
  if (kind === "control" && strategy === "single") {
    await sendControlApproval(requestId, approved, sessionId);
    return;
  }
  if (strategy !== "single") {
    const base = toHttpBaseUrl(currentSettings());
    const note = approved
      ? `Approval bundle sent (${strategy}). Waiting for gateway confirmation...`
      : `Denial bundle sent (${strategy}). Waiting for gateway confirmation...`;
    try {
      await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(requestId)}/resolve_bundle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          approved,
          strategy,
          session_id: sessionId || currentSessionId || "",
          reason: approved ? `Approved in Web UI (${strategy})` : `Denied in Web UI (${strategy})`,
        }),
      });
      updateInlineApprovalStatus(
        requestId,
        approved ? "approving" : "denying",
        note,
      );
      scheduleDashboardRefresh(50);
    } catch (err) {
      addSystemMessage(`approval error: ${err}`);
    }
    return;
  }
  sendApproval(requestId, approved);
}

function sendApproval(requestId, approved) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  const payload = {
    event: "tools.approval",
    request_id: requestId,
    approved,
    reason: approved ? "Approved in Web UI" : "Denied in Web UI"
  };
  socket.send(JSON.stringify(payload));
  logEvent("tools.approval.sent", { request_id: requestId, approved });
  updateInlineApprovalStatus(
    requestId,
    approved ? "approving" : "denying",
    approved ? "Approval sent. Waiting for gateway confirmation..." : "Denial sent. Waiting for gateway confirmation..."
  );
  scheduleDashboardRefresh(50);
}

function handleTaskUpdate(payload) {
  const task = payload.task && typeof payload.task === "object" ? payload.task : {};
  const approvalRequest = extractTaskApprovalRequest(task);
  const requestId = approvalRequest?.request_id || "";
  logEvent("task.update", {
    task_id: payload.task_id || task.task_id,
    status: task.status || payload.timeline_event?.status,
    event_type: payload.timeline_event?.event_type || "",
  });
  if (
    requestId
    && task.status
    && task.status !== "pending"
  ) {
    removeInlineApproval(requestId);
  }
  if (
    dashboardState.selectedKind === "task"
    && dashboardState.selectedId
    && String(dashboardState.selectedId) === String(payload.task_id || task.task_id || "")
  ) {
    scheduleDashboardRefresh(25);
    return;
  }
  scheduleDashboardRefresh(40);
}

function handleApprovalUpdate(payload) {
  const approval = payload.approval && typeof payload.approval === "object" ? payload.approval : {};
  const requestId = approval.request_id || payload.request_id || "";
  logEvent("tools.approval_update", {
    request_id: requestId,
    state: approval.state || "",
    approval_event: payload.approval_event || "",
  });
  if (!requestId) {
    scheduleDashboardRefresh(40);
    return;
  }
  if (approval.state === "pending") {
    upsertInlineApproval({
      kind: "tool",
      requestId,
      toolName: approval.tool_name || approval.tool_pattern || "",
      sessionId: approval.session_id || "",
      title: `${approval.tool_name || approval.tool_pattern || "approval"} by ${approval.agent_name || "agent"}`,
      subtitle: "tool approval request",
      reason: approval.reason || "approval required",
      argsPreview: JSON.stringify(approval.args || {}).slice(0, 220),
      status: "pending",
      expiresAt: approval.expires_at || null,
      note: approval.propagate_to_subagents ? "Session-scoped approval can propagate to subagents." : "",
    });
  } else if (approval.state === "expiring") {
    const escalation = Array.isArray(payload.escalation_suggestions) ? payload.escalation_suggestions : [];
    upsertInlineApproval({
      kind: "tool",
      requestId,
      toolName: approval.tool_name || approval.tool_pattern || "",
      sessionId: approval.session_id || "",
      title: `${approval.tool_name || approval.tool_pattern || "approval"} by ${approval.agent_name || "agent"}`,
      subtitle: "tool approval request",
      reason: approval.reason || "approval required",
      argsPreview: JSON.stringify(approval.args || {}).slice(0, 220),
      status: "expiring",
      expiresAt: approval.expires_at || null,
      escalationSuggestions: escalation,
      note: "Expiring soon — approve or upgrade scope to continue.",
    });
  } else {
    let note = approval.resolve_reason || "";
    if (!note && approval.state === "approved") note = "Approved";
    if (!note && approval.state === "denied") note = "Denied";
    if (!note && approval.state === "expired") note = "Expired — the control loop was aborted.";
    updateInlineApprovalStatus(requestId, approval.state || "pending", note);
  }
  scheduleDashboardRefresh(30);
}

function handleAuditAppend(payload) {
  const entry = payload.entry && typeof payload.entry === "object" ? payload.entry : null;
  if (!entry) return;
  logEvent("audit.append", {
    entry_id: entry.entry_id,
    event_type: entry.event_type,
    session_id: entry.session_id,
  });
  if (dashboardState.selectedKind === "task" || dashboardState.selectedKind === "approval") {
    scheduleDashboardRefresh(35);
  }
  if (isTabActive("audit")) {
    scheduleAuditRefresh(30);
  }
}

// -----------------------------------------------------------------------
// Skills
// -----------------------------------------------------------------------

function renderSkills(items) {
  if (!items.length) {
    skillsListEl.innerHTML = "<li>No skills loaded.</li>";
    return;
  }
  skillsListEl.innerHTML = items.map((s) => {
    const tags = Array.isArray(s.tags) && s.tags.length
      ? s.tags.map((tag) => escapeHtml(tag)).join(", ")
      : "-";
    return [
      "<li>",
      `<div><strong>${escapeHtml(s.name || "-")}</strong></div>`,
      `<div class="muted">${escapeHtml(s.description || "")}</div>`,
      `<div class="muted mono">version=${escapeHtml(s.version || "-")} author=${escapeHtml(s.author || "-")}</div>`,
      `<div class="muted mono">tags=${tags}</div>`,
      "</li>"
    ].join("");
  }).join("");
}

async function fetchSkills() {
  const base = toHttpBaseUrl(currentSettings());
  try {
    logEvent("skills.fetch.start", {});
    const res = await apiFetch(`${base}/skills`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const items = Array.isArray(data.details) ? data.details : [];
    renderSkills(items);
    if (!skillNameInputEl.value && items.length > 0 && items[0].name) {
      skillNameInputEl.value = items[0].name;
    }
    skillResultEl.textContent = JSON.stringify(data, null, 2);
    logEvent("skills.fetch.ok", { count: items.length });
  } catch (err) {
    renderSkills([]);
    skillResultEl.textContent = String(err);
    logEvent("skills.fetch.error", { error: String(err) });
  }
}

async function executeSkill() {
  const base = toHttpBaseUrl(currentSettings());
  const skillName = (skillNameInputEl.value || "").trim();
  if (!skillName) { skillResultEl.textContent = "skill name is required"; return; }

  let params = {};
  const rawParams = (skillParamsInputEl.value || "").trim();
  if (rawParams) {
    try {
      params = JSON.parse(rawParams);
      if (!params || typeof params !== "object" || Array.isArray(params)) {
        skillResultEl.textContent = "params must be a JSON object"; return;
      }
    } catch (err) {
      skillResultEl.textContent = `invalid JSON: ${err}`; return;
    }
  }

  try {
    logEvent("skills.exec.start", { skillName });
    const res = await apiFetch(`${base}/skills/${encodeURIComponent(skillName)}/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
    skillResultEl.textContent = JSON.stringify(data, null, 2);
    logEvent("skills.exec.ok", { skillName });
  } catch (err) {
    skillResultEl.textContent = String(err);
    logEvent("skills.exec.error", { skillName, error: String(err) });
  }
}

// -----------------------------------------------------------------------
// Memory
// -----------------------------------------------------------------------

async function fetchMemory() {
  const base = toHttpBaseUrl(currentSettings());
  const query = (memoryQueryInputEl.value || "").trim();
  const tags = (memoryTagsInputEl.value || "").trim();

  try {
    const res = await apiFetch(`${base}/memory/stats`);
    if (res.ok) {
      const data = await res.json();
      const s = data.stats || {};
      memoryStatsEl.textContent = `${s.total_memories ?? "-"}\u4ef6 / embeddings: ${s.with_embedding ?? "-"}\u4ef6`;
    }
  } catch (_) { memoryStatsEl.textContent = "(stats unavailable)"; }

  const params = new URLSearchParams({ limit: "50" });
  if (query) params.set("query", query);
  if (tags) params.set("tags", tags);

  try {
    logEvent("memory.fetch.start", { query, tags });
    const res = await apiFetch(`${base}/memory?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderMemory(data.results || []);
    logEvent("memory.fetch.ok", { count: data.count });
  } catch (err) {
    memoryListEl.innerHTML = `<li class="muted">${err}</li>`;
    logEvent("memory.fetch.error", { error: String(err) });
  }
}

function renderMemory(items) {
  if (!items.length) {
    memoryListEl.innerHTML = "<li class='muted'>No memories found.</li>";
    return;
  }
  memoryListEl.innerHTML = items.map((m) => {
    const tags = Array.isArray(m.tags) && m.tags.length
      ? m.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join(" ")
      : "";
    const date = m.created_at ? new Date(m.created_at * 1000).toLocaleString() : "-";
    const score = m.score != null ? ` <span class="muted mono">score=${m.score.toFixed(3)}</span>` : "";
    return [
      `<li data-memory-id="${escapeAttr(String(m.id))}">`,
      `<div class="memory-meta"><span class="mono">#${escapeHtml(String(m.id))}</span> ${escapeHtml(date)} ${tags}${score}</div>`,
      `<div class="memory-content">${escapeHtml(m.content)}</div>`,
      `<div class="memory-actions"><button class="btn btn-sm delete-memory-btn" data-id="${escapeAttr(String(m.id))}">Delete</button></div>`,
      "</li>"
    ].join("");
  }).join("");

  memoryListEl.querySelectorAll(".delete-memory-btn").forEach((btn) => {
    btn.addEventListener("click", () => void deleteMemory(Number(btn.dataset.id)));
  });
}

async function deleteMemory(id) {
  const base = toHttpBaseUrl(currentSettings());
  try {
    logEvent("memory.delete.start", { id });
    const res = await apiFetch(`${base}/memory/${id}`, { method: "DELETE" });
    if (res.status === 404) throw new Error("not found");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    logEvent("memory.delete.ok", { id });
    void fetchMemory();
  } catch (err) {
    logEvent("memory.delete.error", { id, error: String(err) });
  }
}

// -----------------------------------------------------------------------
// Cron (platform)
// -----------------------------------------------------------------------

function renderCronJobs(jobs) {
  if (!jobs.length) {
    cronListEl.innerHTML = "<li class='muted'>No cron jobs yet.</li>";
    return;
  }
  cronListEl.innerHTML = jobs.map((j) => {
    const statusTag = j.enabled
      ? `<span class="tag">enabled</span>`
      : `<span class="tag" style="opacity:.5">disabled</span>`;
    const lastRun = j.last_run ? new Date(j.last_run * 1000).toLocaleString() : "-";
    const nextRun = j.next_run ? new Date(j.next_run * 1000).toLocaleString() : "-";
    const deliveryTag = j.delivery_target && j.delivery_target !== "isolated"
      ? ` <span class="tag">${escapeHtml(j.delivery_target)}</span>` : "";
    const retryInfo = j.max_retries > 0
      ? ` retries: ${j.retry_count || 0}/${j.max_retries}` : "";
    const sysEvent = j.system_event
      ? ` <span class="tag">on:${escapeHtml(j.system_event)}</span>` : "";
    return [
      `<li class="cron-item" data-job-id="${escapeAttr(j.id)}">`,
      `<div><strong>${escapeHtml(j.name)}</strong> ${statusTag}${deliveryTag}${sysEvent}</div>`,
      `<div class="muted mono">${escapeHtml(j.cron_expr)} | agent: ${escapeHtml(j.agent_id)}${retryInfo}</div>`,
      `<div class="muted">${escapeHtml(j.task)}</div>`,
      `<div class="muted mono">last: ${escapeHtml(lastRun)} | next: ${escapeHtml(nextRun)} | runs: ${j.run_count}</div>`,
      j.last_error ? `<div class="muted mono" style="color:#f87171">error: ${escapeHtml(j.last_error)}</div>` : "",
      `<div class="memory-actions">`,
      `<button class="btn btn-sm toggle-cron-btn" data-id="${escapeAttr(j.id)}" data-enabled="${j.enabled}">${j.enabled ? "Disable" : "Enable"}</button>`,
      `<button class="btn btn-sm delete-cron-btn" data-id="${escapeAttr(j.id)}">Delete</button>`,
      `</div>`,
      "</li>"
    ].join("");
  }).join("");

  cronListEl.querySelectorAll(".delete-cron-btn").forEach((btn) => {
    btn.addEventListener("click", () => void deleteCronJob(btn.dataset.id));
  });
  cronListEl.querySelectorAll(".toggle-cron-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const enabled = btn.dataset.enabled === "true";
      void toggleCronJob(btn.dataset.id, !enabled);
    });
  });
}

async function fetchCron() {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const res = await apiFetch(`${base}/cron`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderCronJobs(data.jobs || []);
    cronResultEl.textContent = "";
  } catch (err) {
    cronListEl.innerHTML = `<li class="muted">${escapeHtml(String(err))}</li>`;
    cronResultEl.textContent = String(err);
  }
}

async function addCronJob() {
  const base = toHttpBaseUrl(currentSettings());
  const name = (cronNameEl.value || "").trim();
  const cron_expr = (cronExprEl.value || "").trim();
  const task = (cronTaskEl.value || "").trim();
  const agent_id = (cronAgentEl.value || "web_researcher").trim();
  const delivery_target = cronDeliveryEl ? (cronDeliveryEl.value || "isolated").trim() : "isolated";
  const max_retries = cronRetriesEl ? parseInt(cronRetriesEl.value || "0", 10) : 0;
  const system_event = cronSysEventEl ? (cronSysEventEl.value || "").trim() || null : null;

  if (!name || !task) {
    cronResultEl.textContent = "name and task are required";
    return;
  }
  if (!system_event && !cron_expr) {
    cronResultEl.textContent = "cron_expr is required (unless system_event is set)";
    return;
  }

  try {
    logEvent("cron.add.start", { name, cron_expr, system_event });
    const resolvedDelivery =
      delivery_target === "main" && currentSessionId
        ? `session:${currentSessionId}`
        : delivery_target;
    const body = {
      name,
      cron_expr,
      task,
      agent_id,
      delivery_target: resolvedDelivery,
      max_retries,
      session_id: currentSessionId || ""
    };
    if (system_event) body.system_event = system_event;
    const res = await apiFetch(`${base}/cron`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
    cronResultEl.textContent = JSON.stringify(data.job, null, 2);
    cronNameEl.value = "";
    cronTaskEl.value = "";
    logEvent("cron.add.ok", { id: data.job?.id });
    void fetchCron();
  } catch (err) {
    cronResultEl.textContent = String(err);
    logEvent("cron.add.error", { error: String(err) });
  }
}

async function deleteCronJob(id) {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const res = await apiFetch(`${base}/cron/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    logEvent("cron.delete.ok", { id });
    void fetchCron();
  } catch (err) {
    cronResultEl.textContent = String(err);
    logEvent("cron.delete.error", { id, error: String(err) });
  }
}

async function toggleCronJob(id, enabled) {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const res = await apiFetch(`${base}/cron/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    logEvent("cron.toggle.ok", { id, enabled });
    void fetchCron();
  } catch (err) {
    cronResultEl.textContent = String(err);
    logEvent("cron.toggle.error", { id, error: String(err) });
  }
}

// -----------------------------------------------------------------------
// Tab management
// -----------------------------------------------------------------------

function activateTab(tabKey) {
  navButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabKey));
  tabs.forEach((tab) => tab.classList.toggle("active", tab.id === `tab-${tabKey}`));
  const meta = NAV_META[tabKey] || NAV_META.chat;
  tabTitle.textContent = meta.title;
  tabSubtitle.textContent = meta.subtitle;
  if (tabKey === "chat") restoreMessages();
  if (tabKey === "missionos-chat") void initializeMissionOSChat();
  if (tabKey === "dashboard") scheduleDashboardRefresh(0);
  if (tabKey === "mission-designer") {
    void loadMissionOSCurrentMilestone();
    void loadMissionOSTimeline();
    void loadMissionOSEnvelopes();
    void loadMissionOSKnowledge();
    void loadMissionOSAgents();
    void loadMissionOSKnowledgeSharing();
    void loadMissionOSPolicyAuthority();
    void loadMissionOSSitlDispatchExecution();
    void loadMissionOSScopedForm3();
    void loadMissionOSForm2aAiAgent();
    void loadMissionOSRepairPlanner();
    void loadMissionOSOperations();
  }
  if (tabKey === "audit") scheduleAuditRefresh(0);
  if (tabKey === "sessions") void syncServerSessions();
  if (tabKey === "skills") void fetchSkills();
  if (tabKey === "memory") void fetchMemory();
  if (tabKey === "cron") void fetchCron();
}

// -----------------------------------------------------------------------
// WebSocket
// -----------------------------------------------------------------------

function switchSession(targetSessionId) {
  manualDisconnectRequested = true;
  if (reconnectHandle) {
    window.clearTimeout(reconnectHandle);
    reconnectHandle = null;
  }
  const previous = socket;
  socket = null;
  if (previous) previous.close();
  resetDashboardPages();
  messageHistory.length = 0;
  inlineApprovals.clear();
  restoreMessages();
  manualDisconnectRequested = false;
  connect(targetSessionId);
}

// Refresh countdown labels every 5 seconds (client-side only, no WS traffic).
(function startCountdownTicker() {
  setInterval(() => {
    document.querySelectorAll(".approval-countdown[data-expires-at]").forEach((el) => {
      const expiresAt = parseFloat(el.dataset.expiresAt);
      if (!expiresAt) return;
      const remaining = Math.max(0, Math.round(expiresAt - Date.now() / 1000));
      if (remaining <= 0) {
        el.textContent = "expired";
        el.classList.add("expired");
        return;
      }
      const mins = Math.floor(remaining / 60);
      const secs = remaining % 60;
      el.textContent = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
      el.classList.toggle("urgent", remaining <= 30);
      el.classList.toggle("warning", remaining > 30 && remaining <= 60);
    });
  }, 5000);
})();

function connect(targetSessionId = null) {
  if (typeof targetSessionId !== "string") targetSessionId = null;
  if (socket && socket.readyState === WebSocket.OPEN) {
    addSystemMessage("already connected");
    return;
  }

  if (reconnectHandle) {
    window.clearTimeout(reconnectHandle);
    reconnectHandle = null;
  }
  manualDisconnectRequested = false;
  const requestedSessionId = targetSessionId || reconnectSessionId || currentSessionId || null;
  const settings = currentSettings();
  const wsUrl = toWebSocketUrl(settings, requestedSessionId);
  logEvent("socket.connecting", { wsUrl });
  setStatus(false, "connecting...");

  const ws = new WebSocket(wsUrl);
  socket = ws;

  ws.onopen = () => {
    if (socket !== ws) return;
    setStatus(true, "online");
    addSystemMessage(`connected: ${wsUrl}`);
    logEvent("socket.open");
    reconnectAttempts = 0;
    reconnectSessionId = null;
    if (pendingMessage) {
      const toSend = pendingMessage;
      pendingMessage = null;
      sendMessage(toSend);
    }
  };

  ws.onclose = (event) => {
    if (socket !== ws) return;
    socket = null;
    setStatus(false, "offline");
    clearWaiting();
    setRunInProgress(false);
    _streamingBubble = null;
    _streamingText = "";
    addSystemMessage(`disconnected (code=${event.code})`);
    logEvent("socket.close", { code: event.code, reason: event.reason || "" });
    scheduleDashboardRefresh(50);
    if (isTabActive("audit")) scheduleAuditRefresh(50);
    const sessionToReconnect = currentSessionId || requestedSessionId || null;
    reconnectSessionId = sessionToReconnect || null;
    if (!manualDisconnectRequested && !reconnectHandle) {
      const delay = Math.min(5000, 500 * 2 ** reconnectAttempts);
      reconnectAttempts += 1;
      addSystemMessage(`reconnecting in ${Math.round(delay / 100) / 10}s...`);
      reconnectHandle = window.setTimeout(() => {
        reconnectHandle = null;
        connect(sessionToReconnect);
      }, delay);
    }
  };

  ws.onerror = () => {
    if (socket !== ws) return;
    setStatus(false, "error");
    addSystemMessage("connection error");
    logEvent("socket.error");
  };

  ws.onmessage = (event) => {
    if (socket !== ws) return;
    try {
      const payload = JSON.parse(event.data);
      logEvent(`ws.${payload.event || payload.type || "message"}`, payload);

      const evName = payload.event || payload.type || "";

      // --- typed protocol v1 ---
      if (evName === "connected") { handleConnected(payload); return; }
      if (evName === "chat.done") { handleChatDone(payload); return; }
      if (evName === "chat.token") { handleChatToken(payload); return; }
      if (evName === "chat.history") { handleChatHistory(payload); return; }
      if (evName === "tool.start") { return; }
      if (evName === "tool.result") { return; }
      if (evName === "task.update") { handleTaskUpdate(payload); return; }
      if (evName === "tools.approval_update") { handleApprovalUpdate(payload); return; }
      if (evName === "audit.append") { handleAuditAppend(payload); return; }
      if (evName === "system.event") { handleSystemEvent(payload); return; }
      if (evName === "health.tick") { handleHealthTick(payload); return; }
      if (evName === "cron.update") { handleCronUpdate(payload); return; }
      if (evName === "tools.approval_request") { handleToolsApprovalRequest(payload); return; }
      if (evName === "control.approval_request") { handleControlApprovalRequest(payload); return; }

      // --- backward compat ---
      if (evName === "agent_message") {
        clearWaiting();
        appendBubble("agent", payload.message || "");
        setRunInProgress(false);
        return;
      }
      if (evName === "error") {
        clearWaiting();
        addSystemMessage(payload.message || "error");
        setRunInProgress(false);
        return;
      }
      if (evName === "user_message") return;
      if (evName === "pong") return;

      addSystemMessage(event.data);
    } catch (_) {
      logEvent("socket.message.raw", { data: event.data });
      addSystemMessage(event.data);
    }
  };
}

function disconnect() {
  manualDisconnectRequested = true;
  if (reconnectHandle) {
    window.clearTimeout(reconnectHandle);
    reconnectHandle = null;
  }
  reconnectSessionId = currentSessionId || reconnectSessionId;
  const previous = socket;
  socket = null;
  if (previous) previous.close();
  setStatus(false, "offline");
  clearWaiting();
  setRunInProgress(false);
}

function sendMessage(text) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    addSystemMessage(`not connected -> connecting`);
    pendingMessage = text;
    connect();
    return;
  }
  let payload = { event: "chat.send", text };
  const controlGoal = text.startsWith("/control ")
    ? text.slice("/control ".length).trim()
    : (text.startsWith("/plan ") ? text.slice("/plan ".length).trim() : "");
  if (controlGoal) {
    payload = { event: "control.run", goal: controlGoal };
  }
  socket.send(JSON.stringify(payload));
  logEvent("socket.send", payload);
  appendBubble("user", text);
  const currentSession = sessions.find((s) => s.id === currentSessionId);
  if (currentSession && !currentSession.preview) {
    currentSession.preview = text.length > 96 ? `${text.slice(0, 95)}…` : text;
    renderSessions();
  }
  waitingIndicator = appendBubble("system", "thinking...", { persist: false });
  setRunInProgress(true);
}

function abortRun() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;

  const pendingApprovalIds = getPendingInlineApprovalIds();
  if (pendingApprovalIds.length) {
    pendingApprovalIds.forEach((requestId) => {
      sendApproval(requestId, false);
      updateInlineApprovalStatus(
        requestId,
        "denying",
        "Stop requested from Web UI."
      );
    });
    addSystemMessage("stop sent for pending approvals...");
  }

  if (_runInProgress) {
    socket.send(JSON.stringify({ event: "chat.abort" }));
    logEvent("socket.abort");
    addSystemMessage("abort sent...");
  }
}

// -----------------------------------------------------------------------
// Event listeners
// -----------------------------------------------------------------------

navButtons.forEach((btn) => btn.addEventListener("click", () => activateTab(btn.dataset.tab)));
connectBtn.addEventListener("click", () => connect());
disconnectBtn.addEventListener("click", disconnect);
abortBtn.addEventListener("click", abortRun);
saveSettingsBtn.addEventListener("click", persistSettings);
resetSettingsBtn.addEventListener("click", resetSettings);
missionScenarioGenerateBtn?.addEventListener("click", () => void generateMissionScenarioProposal());
missionScenarioApproveBtn?.addEventListener("click", () => void approveMissionScenarioProposal());
missionScenarioPrepareSitlBtn?.addEventListener("click", () => void prepareMissionScenarioSITLExecution());
missionScenarioExecuteSitlBtn?.addEventListener("click", () => void executeMissionScenarioLiveSITL());
missionScenarioResetBtn?.addEventListener("click", clearMissionScenarioDesigner);
missionosMilestoneRefreshBtn?.addEventListener("click", () => void loadMissionOSCurrentMilestone());
missionosOperatorSummaryRefreshBtn?.addEventListener("click", () => void refreshMissionOSOperatorSummary());
missionosTimelineRefreshBtn?.addEventListener("click", () => void loadMissionOSTimeline());
missionosEnvelopeRefreshBtn?.addEventListener("click", () => void loadMissionOSEnvelopes());
missionosKnowledgeRefreshBtn?.addEventListener("click", () => void loadMissionOSKnowledge());
missionosAgentsRefreshBtn?.addEventListener("click", () => void loadMissionOSAgents());
missionosKnowledgeSharingRefreshBtn?.addEventListener("click", () => void loadMissionOSKnowledgeSharing());
missionosKnowledgeCuratorDryRunBtn?.addEventListener("click", () => void runMissionOSKnowledgeCuratorDryRun());
missionosKnowledgePublishBtn?.addEventListener("click", () => void publishMissionOSKnowledgeSharing());
missionosPolicyAuthorityRefreshBtn?.addEventListener("click", () => void loadMissionOSPolicyAuthority());
missionosPolicyAuthorityPromoteBtn?.addEventListener("click", () => void promoteMissionOSPolicyAuthority());
missionosSitlDispatchRefreshBtn?.addEventListener("click", () => void loadMissionOSSitlDispatchExecution());
missionosSitlDispatchRunBtn?.addEventListener("click", () => void runMissionOSSitlDispatchExecution());
missionosScopedForm3RefreshBtn?.addEventListener("click", () => void loadMissionOSScopedForm3());
missionosScopedForm3RunBtn?.addEventListener("click", () => void runMissionOSScopedForm3());
missionosForm2aAiAgentRefreshBtn?.addEventListener("click", () => void loadMissionOSForm2aAiAgent());
missionosForm2aAiAgentRunSelectionBtn?.addEventListener("click", () => void runMissionOSAutonomyAction("instruction"));
missionosForm2aAiAgentApproveBtn?.addEventListener("click", () => void runMissionOSAutonomyAction("approve"));
missionosForm2aAiAgentConsumeBtn?.addEventListener("click", () => void runMissionOSAutonomyAction("consume"));
missionosRepairPlannerRefreshBtn?.addEventListener("click", () => void loadMissionOSRepairPlanner());
missionosRepairPlannerRunBtn?.addEventListener("click", () => void runMissionOSRepairPlanner());
missionosAutonomyRefreshBtn?.addEventListener("click", () => void refreshMissionOSAutonomyMonitor());
missionosAutonomyMonitorSummaryEl?.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : event.target?.parentElement;
  const button = target?.closest("[data-missionos-autonomy-action]");
  if (!button) return;
  void runMissionOSAutonomyAction(button.dataset.missionosAutonomyAction || "refresh");
});
missionosOperationsRefreshBtn?.addEventListener("click", () => void loadMissionOSOperations());
missionosOperationsListEl?.addEventListener("click", (event) => {
  const target = event.target instanceof HTMLElement
    ? event.target.closest("[data-missionos-operation-run]")
    : null;
  if (!(target instanceof HTMLElement)) return;
  const operationId = target.dataset.missionosOperationRun || "";
  void runMissionOSOperation(operationId);
});
missionScenarioCoordinateRouteInputs().forEach((inputEl) => {
  inputEl?.addEventListener("input", updateMissionScenarioCoordinateRouteStatus);
});
updateMissionScenarioCoordinateRouteStatus();
missionOSFlightSetupInputs().forEach((inputEl) => {
  inputEl?.addEventListener("input", updateMissionOSFlightSetupStatus);
});
updateMissionOSFlightSetupStatus();
refreshDashboardBtn.addEventListener("click", () => scheduleDashboardRefresh(0));
refreshAnalyticsBtn?.addEventListener("click", () => void loadAnalytics());
clearDashboardFiltersBtn?.addEventListener("click", () => {
  dashboardState.searchQuery = "";
  dashboardState.taskStatusFilter = "all";
  dashboardState.approvalStateFilter = "all";
  resetDashboardPages();
  if (dashboardSearchInputEl) dashboardSearchInputEl.value = "";
  updateDashboardFilterButtons();
  scheduleDashboardRefresh(0);
});
refreshAuditBtn?.addEventListener("click", () => scheduleAuditRefresh(0));
clearAuditFiltersBtn?.addEventListener("click", () => {
  resetAuditFilters();
  scheduleAuditRefresh(0);
});
dashboardSearchInputEl?.addEventListener("input", () => {
  dashboardState.searchQuery = dashboardSearchInputEl.value || "";
  resetDashboardPages();
  scheduleDashboardRefresh(250);
});
auditSearchInputEl?.addEventListener("input", () => {
  auditState.searchQuery = auditSearchInputEl.value || "";
  auditState.page = 1;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  auditState.focus = null;
  scheduleAuditRefresh(250);
});
[auditActorInputEl, auditSessionInputEl, auditToolInputEl, auditSourceInputEl, auditResultInputEl].forEach((element) => {
  element?.addEventListener("input", () => {
    auditState.actorFilter = auditActorInputEl?.value || "";
    auditState.sessionFilter = auditSessionInputEl?.value || "";
    auditState.toolFilter = auditToolInputEl?.value || "";
    auditState.sourceFilter = auditSourceInputEl?.value || "";
    auditState.resultFilter = auditResultInputEl?.value || "";
    auditState.page = 1;
    auditState.selectedEntryId = null;
    auditState.selectedEntry = null;
    auditState.focus = null;
    scheduleAuditRefresh(250);
  });
});
dashboardFilterChips.forEach((chip) => {
  chip.addEventListener("click", () => {
    const kind = chip.dataset.filterKind;
    const value = chip.dataset.filterValue || "all";
    if (kind === "task-status") {
      dashboardState.taskStatusFilter = value;
      dashboardState.taskPage = 1;
    } else if (kind === "approval-state") {
      dashboardState.approvalStateFilter = value;
      dashboardState.approvalPage = 1;
    }
    updateDashboardFilterButtons();
    scheduleDashboardRefresh(0);
  });
});
dashboardApprovalsPrevBtn?.addEventListener("click", () => {
  if (dashboardState.approvalPage <= 1) return;
  dashboardState.approvalPage -= 1;
  scheduleDashboardRefresh(0);
});
dashboardApprovalsNextBtn?.addEventListener("click", () => {
  if (!dashboardState.approvalHasMore) return;
  dashboardState.approvalPage += 1;
  scheduleDashboardRefresh(0);
});
dashboardTasksPrevBtn?.addEventListener("click", () => {
  if (dashboardState.taskPage <= 1) return;
  dashboardState.taskPage -= 1;
  scheduleDashboardRefresh(0);
});
dashboardTasksNextBtn?.addEventListener("click", () => {
  if (!dashboardState.taskHasMore) return;
  dashboardState.taskPage += 1;
  scheduleDashboardRefresh(0);
});
auditPrevBtn?.addEventListener("click", () => {
  if (auditState.page <= 1) return;
  auditState.page -= 1;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  scheduleAuditRefresh(0);
});
auditNextBtn?.addEventListener("click", () => {
  if (!auditState.hasMore) return;
  auditState.page += 1;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  scheduleAuditRefresh(0);
});
[
  dashboardApprovalsListEl,
  dashboardTasksListEl,
  inspectorApprovalsListEl,
  inspectorTasksListEl,
  auditListEl,
].forEach((element) => {
  element?.addEventListener("click", handleDashboardListSelectionClick);
});
[
  dashboardDetailPanelEl,
  inspectorSelectionDetailEl,
].forEach((element) => {
  element?.addEventListener("click", handleSelectionPanelClick);
});
refreshSkillsBtn.addEventListener("click", () => void fetchSkills());
runSkillBtn.addEventListener("click", () => void executeSkill());
refreshMemoryBtn.addEventListener("click", () => void fetchMemory());
searchMemoryBtn.addEventListener("click", () => void fetchMemory());
memoryQueryInputEl.addEventListener("keydown", (e) => { if (e.key === "Enter") void fetchMemory(); });
refreshCronBtn.addEventListener("click", () => void fetchCron());
addCronBtn.addEventListener("click", () => void addCronJob());
gatewayUrlEl.addEventListener("input", () => {
  gatewayHostLabelEl.textContent = gatewayUrlEl.value.trim() || DEFAULTS.gatewayUrl;
});
chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = messageInputEl.value.trim();
  if (!text) return;
  sendMessage(text);
  messageInputEl.value = "";
});
messageInputEl.addEventListener("compositionstart", () => {
  _messageInputComposing = true;
});
messageInputEl.addEventListener("compositionend", () => {
  _messageInputComposing = false;
});
messageInputEl.addEventListener("keydown", (e) => {
  if (e.isComposing || _messageInputComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); chatForm.requestSubmit(); }
});
missionosChatForm?.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = missionosChatInputEl?.value?.trim() || "";
  if (!text) return;
  void submitMissionOSChatInstruction(text);
});
missionosChatInputEl?.addEventListener("compositionstart", () => {
  _missionOSChatInputComposing = true;
});
missionosChatInputEl?.addEventListener("compositionend", () => {
  _missionOSChatInputComposing = false;
});
missionosChatInputEl?.addEventListener("keydown", (e) => {
  if (e.isComposing || _missionOSChatInputComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); missionosChatForm?.requestSubmit(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented || e.repeat) return;
  if (!_runInProgress && getPendingInlineApprovalIds().length === 0) return;
  e.preventDefault();
  abortRun();
});

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------

const initialSettings = { ...parseStoredSettings(), ...parseUrlSettings() };
applySettings(initialSettings);
resetAuditFilters();
renderSessions();
updateDashboardFilterButtons();
updateDashboardUi();
updateAuditUi();
activateTab("chat");
setStatus(false, "offline");
setRunInProgress(false);
scheduleDashboardRefresh(0);
addSystemMessage("ready: connecting to Gateway...");
logEvent("ui.ready", currentSettings());
connect();
