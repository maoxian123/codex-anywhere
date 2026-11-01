import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Folder,
  FolderPlus,
  Home,
  Monitor,
  RefreshCw,
} from "lucide-react";
import { errorText } from "./api";
import { remoteCommand } from "./commands";
import { ErrorMessage, Modal, Spinner } from "./components";
import type {
  DesktopRegistration,
  DirectoryListing,
  Host,
  Project,
} from "./types";

export function desktopRegistrationMessage(registration?: DesktopRegistration) {
  if (!registration) return "更新节点后支持同步到该设备的 Codex 项目列表。";
  if (
    registration.status === "registered" ||
    registration.status === "already_registered"
  )
    return `已登记到该设备的 Codex 项目列表。${registration.refresh_required ? "桌面版尚未显示时，请重新打开桌面版。" : ""}`;
  if (registration.status === "pending") {
    if (registration.reason === "state_changed")
      return "已排队，节点会在项目列表稳定后自动完成登记。";
    return registration.reason === "desktop_state_unverified"
      ? "已排队，节点确认桌面版退出后会自动完成登记，重新打开即可查看。"
      : "桌面版正在运行，已排队；退出桌面版后节点会自动完成登记，重新打开即可查看。";
  }
  if (registration.reason === "outcome_unknown")
    return "登记结果尚未确认，请重新检查后手动同步。";
  if (registration.reason === "state_changed")
    return "桌面项目列表已变化，本次同步未完成。请重新检查后手动同步。";
  if (registration.reason === "state_invalid")
    return "此设备的桌面项目列表暂不可用，请检查 Codex 桌面版后手动重试。";
  if (registration.status === "unavailable") {
    if (registration.reason === "desktop_not_found")
      return "该设备未检测到桌面项目列表，项目仍可在 Anywhere 使用。";
    if (registration.reason === "unsupported_state")
      return "该设备的桌面版本暂不兼容，请更新 Codex 桌面版后重试。";
    if (registration.reason === "native_unsupported")
      return "此设备的 Codex 版本暂不支持项目同步，请更新 Codex 后重试。";
    if (registration.reason === "runtime_unavailable")
      return "此设备的 Codex 暂不可用，请稍后手动重试。";
    return "此设备暂时无法同步 Codex 项目列表，可稍后手动重试。";
  }
  return "同步到桌面版失败，可在项目默认设置中手动重试。";
}

export function joinDirectory(path: string, name: string, platform: string) {
  if (!name.trim() || /[\\/]/.test(name) || [".", ".."].includes(name.trim()))
    throw new Error("请输入单个文件夹名称，不要包含路径分隔符。");
  const separator =
    platform.toLowerCase().includes("win") &&
    !platform.toLowerCase().includes("darwin")
      ? "\\"
      : "/";
  return path.replace(/[\\/]+$/, "") + separator + name.trim();
}

export function ProjectDialog({
  hosts,
  onClose,
  onCreated,
}: {
  hosts: Host[];
  onClose: () => void;
  onCreated: (project: Project, registration?: DesktopRegistration) => void;
}) {
  const [hostId, setHostId] = useState(
    hosts.find((host) => host.online)?.id || "",
  );
  const host = hosts.find((value) => value.id === hostId);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [folderName, setFolderName] = useState("");
  const [newFolder, setNewFolder] = useState(false);
  const [createDirectory, setCreateDirectory] = useState(false);
  const request = useRef<AbortController | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      request.current?.abort();
    };
  }, []);
  const browse = async (next?: string) => {
    if (!hostId || saving) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    try {
      const result = await remoteCommand<DirectoryListing>(
        `/hosts/${hostId}/directories`,
        next ? { path: next } : {},
        controller.signal,
      );
      if (controller.signal.aborted || !alive.current) return;
      setListing(result);
      setPath(result.path);
      setCreateDirectory(false);
      setNewFolder(false);
    } catch (reason) {
      if (!controller.signal.aborted && alive.current)
        setError(errorText(reason));
    } finally {
      if (!controller.signal.aborted && alive.current) setLoading(false);
    }
  };
  useEffect(() => {
    setListing(null);
    setPath("");
    setError("");
    if (host?.online) void browse();
    return () => request.current?.abort();
  }, [hostId]); // Each node owns a different filesystem.
  const create = async (cwd = path, mkdir = createDirectory) => {
    if (!host?.online || !cwd.trim() || saving) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setSaving(true);
    setError("");
    setLoading(false);
    try {
      const result = await remoteCommand<{
        project: Project;
        desktop_registration?: DesktopRegistration;
      }>(
        "/projects",
        {
          host_id: hostId,
          cwd: cwd.trim(),
          ...(name.trim() ? { name: name.trim() } : {}),
          create_directory: mkdir,
        },
        controller.signal,
      );
      if (!controller.signal.aborted && alive.current)
        onCreated(result.project, result.desktop_registration);
    } catch (reason) {
      if (!controller.signal.aborted && alive.current)
        setError(errorText(reason));
    } finally {
      if (!controller.signal.aborted && alive.current) setSaving(false);
    }
  };
  return (
    <Modal
      title="新建项目"
      subtitle="浏览所选设备的目录。项目会话按设备与工作目录归类。"
      onClose={onClose}
    >
      <div className="form-stack">
        <label>
          <span className="label-with-icon">
            <Monitor size={14} />
            工作设备
          </span>
          <select
            value={hostId}
            onChange={(event) => setHostId(event.target.value)}
            disabled={saving}
          >
            <option value="" disabled>
              选择在线设备
            </option>
            {hosts.map((value) => (
              <option key={value.id} value={value.id} disabled={!value.online}>
                {value.name}
                {value.online ? "" : " · 离线"}
              </option>
            ))}
          </select>
        </label>
        <label>
          项目名称<span className="field-optional">可选</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="默认使用文件夹名称"
            disabled={saving}
            maxLength={200}
          />
        </label>
        <label>
          设备上的绝对目录
          <div className="directory-path">
            <input
              value={path}
              onChange={(event) => setPath(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void browse(path);
                }
              }}
              placeholder="输入目录路径，或在下方浏览"
              disabled={saving}
            />
            <button
              className="button button-small"
              onClick={() => void browse(path)}
              disabled={!host?.online || saving || !path.trim()}
            >
              <ArrowRight size={14} />
              前往
            </button>
          </div>
        </label>
        <div className="directory-browser">
          <div className="directory-toolbar">
            <button
              className="text-button"
              disabled={loading || saving || !listing?.parent}
              onClick={() => void browse(listing?.parent || undefined)}
            >
              <ArrowLeft size={14} />
              上级目录
            </button>
            <button
              className="icon-button"
              disabled={loading || saving || !host?.online}
              onClick={() => void browse(listing?.home)}
              aria-label="设备主目录"
            >
              <Home size={15} />
            </button>
            <button
              className="icon-button"
              disabled={loading || saving || !host?.online}
              onClick={() => void browse(path)}
              aria-label="刷新目录"
            >
              <RefreshCw size={14} />
            </button>
            <button
              className="text-button"
              disabled={
                loading || saving || !listing || path.trim() !== listing.path
              }
              onClick={() => setNewFolder((value) => !value)}
            >
              <FolderPlus size={14} />
              新建文件夹
            </button>
          </div>
          {listing && (
            <div className="directory-locations">
              {listing.locations.map((location) => (
                <button
                  key={location.path}
                  onClick={() => void browse(location.path)}
                  disabled={saving || loading}
                >
                  {location.name}
                </button>
              ))}
            </div>
          )}
          <div className="directory-contents" aria-label="远程目录列表">
            {loading ? (
              <div className="empty-small">
                <Spinner label="读取设备目录" />
              </div>
            ) : listing ? (
              listing.directories.length ? (
                listing.directories.map((directory) => (
                  <button
                    key={directory.path}
                    className="directory-row"
                    disabled={saving}
                    onClick={() => void browse(directory.path)}
                  >
                    <Folder size={16} />
                    <span>{directory.name}</span>
                    <ArrowRight size={13} />
                  </button>
                ))
              ) : (
                <p className="empty-small">
                  当前目录没有子文件夹，可以直接使用此目录。
                </p>
              )
            ) : (
              <p className="empty-small">选择在线设备后即可浏览其文件夹。</p>
            )}
          </div>
          {listing?.truncated && (
            <p className="directory-hint">
              目录较多，当前展示部分结果；可手动输入具体路径。
            </p>
          )}
        </div>
        {newFolder && (
          <div className="new-folder-row">
            <input
              aria-label="新文件夹名称"
              value={folderName}
              onChange={(event) => setFolderName(event.target.value)}
              placeholder="新文件夹名称"
              disabled={saving}
            />
            <button
              className="button button-small"
              disabled={saving || !folderName.trim() || !listing}
              onClick={() => {
                try {
                  void create(
                    joinDirectory(
                      listing!.path,
                      folderName,
                      host?.platform || "",
                    ),
                    true,
                  );
                } catch (reason) {
                  setError(errorText(reason));
                }
              }}
            >
              创建并用作项目
            </button>
          </div>
        )}
        {listing && (
          <p className="directory-hint" title={listing.path}>
            当前浏览：{listing.path}
          </p>
        )}
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={createDirectory}
            onChange={(event) => setCreateDirectory(event.target.checked)}
            disabled={saving}
          />
          手动输入的目录不存在时，创建该目录
        </label>
        <p className="field-hint">
          {host?.capabilities?.includes("desktopProjects")
            ? "同时登记到所选设备当前 Codex 数据目录的项目列表。"
            : "更新节点后支持同步到所选设备的 Codex 项目列表。"}
        </p>
        {!host?.online && <ErrorMessage>请选择一台在线设备。</ErrorMessage>}
        {error && <ErrorMessage>{error}</ErrorMessage>}
        <div className="modal-actions">
          <button className="button button-quiet" onClick={onClose}>
            取消
          </button>
          <button
            className="button button-primary"
            disabled={saving || loading || !host?.online || !path.trim()}
            onClick={() => void create()}
          >
            {saving ? (
              <Spinner label="创建项目中" />
            ) : (
              <>
                <FolderPlus size={15} />
                创建项目
              </>
            )}
          </button>
        </div>
      </div>
    </Modal>
  );
}
