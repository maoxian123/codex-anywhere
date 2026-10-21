import { useState } from "react";
import { CircleHelp, Download, LogOut, Monitor, Plus } from "lucide-react";
import type { Host } from "./types";

export default function SidebarMenu({
  hosts,
  onPair,
  onHistory,
  onAbout,
  onLogout,
}: {
  hosts: Host[];
  onPair: () => void;
  onHistory: () => void;
  onAbout: () => void;
  onLogout: () => void;
}) {
  const [open, setOpen] = useState(false);
  const choose = (action: () => void) => {
    setOpen(false);
    action();
  };
  return (
    <details className="sidebar-menu" open={open}>
      <summary
        aria-label="设备与设置"
        aria-expanded={open}
        onClick={(event) => {
          event.preventDefault();
          setOpen((value) => !value);
        }}
      >
        <Monitor size={15} />
        <span>{hosts.filter((host) => host.online).length}</span>
        <i
          className={
            hosts.some((host) => host.sync_error) ? "sync-warning" : ""
          }
        />
      </summary>
      {open && (
        <div className="sidebar-menu-popover">
          <p className="menu-title">工作设备 · 自动同步最近会话</p>
          {hosts.map((host) => (
            <div className="compact-host" key={host.id}>
              <div>
                <span
                  className={`mini-status ${host.online ? "online" : ""}`}
                />
                <strong>{host.name}</strong>
                <small>{host.online ? "在线" : "离线"}</small>
              </div>
              {host.sync_error ? (
                <p className="text-error">同步失败：{host.sync_error}</p>
              ) : (
                <p>
                  {host.last_sync_at
                    ? `同步于 ${new Date(host.last_sync_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`
                    : host.online
                      ? "等待首次会话同步"
                      : "连接后自动同步"}
                </p>
              )}
            </div>
          ))}
          {!hosts.length && (
            <p className="menu-empty">连接工作电脑后，本机会话会自动出现。</p>
          )}
          <button onClick={() => choose(onPair)}>
            <Plus size={14} />
            连接设备
          </button>
          <button onClick={() => choose(onHistory)}>
            <Download size={14} />
            更多本机历史
          </button>
          <button onClick={() => choose(onAbout)}>
            <CircleHelp size={14} />
            使用说明
          </button>
          <button onClick={() => choose(onLogout)}>
            <LogOut size={14} />
            退出登录
          </button>
        </div>
      )}
    </details>
  );
}
