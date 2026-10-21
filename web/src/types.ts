export type JsonObject = Record<string, unknown>;
export type Session = {
  authenticated: boolean;
  username?: string;
  csrf_token: string;
};
export type Host = {
  id: string;
  name: string;
  platform: string;
  online: boolean;
  version?: string;
  last_seen_at?: string;
  last_sync_at?: string | null;
  sync_error?: string | null;
  capabilities?: string[];
  runtime_id?: string | null;
};
export type ExecutionSettings = {
  model?: string;
  effort?: string;
  approval_policy?: string;
  sandbox_mode?: string;
  approvals_reviewer?: string;
  network_access?: boolean;
};
export type NativeExecutionSettings = Omit<
  {
    [Key in keyof ExecutionSettings]?: ExecutionSettings[Key] | null;
  },
  "approval_policy"
> & { approval_policy?: string | JsonObject | null };
export type RuntimeModel = {
  id: string;
  model: string;
  displayName: string;
  description?: string;
  hidden?: boolean;
  isDefault?: boolean;
  defaultReasoningEffort?: string;
  supportedReasoningEfforts?: {
    reasoningEffort: string;
    description?: string;
  }[];
  inputModalities?: string[];
};
export type RuntimeCapabilities = {
  models: RuntimeModel[];
  defaults: NativeExecutionSettings;
  constraints?: {
    approval_policies?: string[];
    sandbox_modes?: string[];
    approvals_reviewers?: string[];
  };
  supported?: Record<string, unknown>;
  cwd?: string;
  runtime_id?: string;
  fetched_at?: string;
};
export type Thread = {
  id: string;
  host_id: string;
  project_id?: string | null;
  imported?: boolean;
  history_loaded?: boolean;
  history_stale?: boolean;
  native_updated_at?: number | null;
  history_native_updated_at?: number | null;
  history_synced_at?: string | null;
  history_cursor?: string | null;
  history_complete?: boolean;
  history_generation?: string | null;
  native_id?: string;
  title: string;
  cwd: string;
  status: string;
  active_turn_id?: string;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
  last_activity_at?: string;
  execution_settings?: ExecutionSettings;
  effective_settings?: NativeExecutionSettings;
  settings_revision?: number;
  effective_settings_revision?: number;
};
export type Asset = {
  id: string;
  name: string;
  mime: string;
  size: number;
  url: string;
  source_path?: string;
  sourcePath?: string;
};
export type Reference = {
  kind: "task" | "web";
  title?: string;
  thread_id?: string;
  item_id?: string;
  url?: string;
  excerpt?: string;
  snapshot_id?: string;
};
export type Item = {
  id: string;
  native_id: string;
  thread_id: string;
  turn_id?: string;
  type: string;
  data: JsonObject & { _assets?: Asset[]; _references?: Reference[] };
  created_at?: string;
  position?: number;
};
export type Project = {
  id: string;
  host_id: string;
  name: string;
  cwd: string;
  thread_count: number;
  created_at: string;
  updated_at: string;
  last_activity_at?: string;
  execution_settings?: ExecutionSettings;
};
export type ThreadPage = {
  threads: Thread[];
  has_more?: boolean;
  next_offset?: number | null;
};
export type DirectoryListing = {
  path: string;
  parent: string | null;
  home: string;
  locations: { name: string; path: string }[];
  directories: { name: string; path: string }[];
  truncated: boolean;
};
export type Interaction = {
  id: string;
  thread_id: string;
  method: string;
  params: JsonObject;
  status: string;
};
export type ThreadDetail = {
  thread: Thread;
  items: Item[];
  interactions: Interaction[];
  has_more_items?: boolean;
  next_before_position?: number | null;
};
export type Command = {
  id: string;
  kind: string;
  status: string;
  result?: unknown;
  error?: string;
};
export type HubEvent = {
  type: "event";
  id: number;
  thread_id: string | null;
  method: string;
  params: JsonObject;
};
export type StreamItem = {
  id: string;
  text: string;
  kind: "message" | "command" | "reasoning";
};
export type MessagePayload = {
  text: string;
  asset_ids: string[];
  references: Reference[];
  client_request_id: string;
};
