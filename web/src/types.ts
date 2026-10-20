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
  roots: string[];
  version?: string;
  last_seen_at?: string;
};
export type Thread = {
  id: string;
  host_id: string;
  native_id?: string;
  title: string;
  cwd: string;
  status: string;
  active_turn_id?: string;
  created_at: string;
  updated_at: string;
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
