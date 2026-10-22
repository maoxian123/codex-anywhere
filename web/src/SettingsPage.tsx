import { useEffect, useRef, useState, type FormEvent } from "react";
import { Check, HardDrive, RefreshCw, UserRound } from "lucide-react";
import { api, errorText, setSession } from "./api";
import { ErrorMessage, Spinner } from "./components";
import HostSettings from "./HostSettings";
import type { Session } from "./types";

type Settings = { administrator: { username: string } };
type AdministratorUpdate = { settings: Settings; session: Session };

export default function SettingsPage({
  onSession,
  onHostDeleted,
}: {
  onSession: (session: Session) => void;
  onHostDeleted?: (hostId: string) => void;
}) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [loadError, setLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [username, setUsername] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState(false);
  const [pending, setPending] = useState(false);
  const saving = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoadError("");
    void api<Settings>("/settings", { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        setSettings(value);
        setUsername(value.administrator.username);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setLoadError(errorText(error));
      });
    return () => controller.abort();
  }, [loadAttempt]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving.current) return;
    setSaveError("");
    setSaved(false);
    const normalizedUsername = username.trim();
    if (
      !normalizedUsername ||
      normalizedUsername.length > 120 ||
      /[\u0000-\u001f\u007f-\u009f]/.test(normalizedUsername)
    ) {
      setSaveError("请输入 1–120 个字符的用户名，不能包含控制字符。");
      return;
    }
    if (!currentPassword) {
      setSaveError("请输入当前密码以确认修改。");
      return;
    }
    if (newPassword !== confirmation) {
      setSaveError("两次输入的新密码不一致。");
      return;
    }
    if (newPassword && newPassword.length < 8) {
      setSaveError("新密码至少需要 8 个字符。");
      return;
    }
    saving.current = true;
    setPending(true);
    try {
      const result = await api<AdministratorUpdate>("/settings/administrator", {
        method: "PATCH",
        body: JSON.stringify({
          username: normalizedUsername,
          current_password: currentPassword,
          ...(newPassword ? { new_password: newPassword } : {}),
        }),
      });
      setSession(result.session);
      onSession(result.session);
      setSettings(result.settings);
      setUsername(result.settings.administrator.username);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmation("");
      setSaved(true);
    } catch (error) {
      setSaveError(errorText(error));
    } finally {
      saving.current = false;
      setPending(false);
    }
  }

  return (
    <div className="settings-page">
      <header className="settings-header">
        <h2>应用配置</h2>
        <p>管理 Codex Anywhere 的应用配置。</p>
      </header>
      <div className="settings-layout">
        <nav className="settings-navigation" aria-label="设置分类">
          <a href="#administrator-settings" aria-current="page">
            <UserRound size={16} aria-hidden="true" />
            账户
          </a>
          <a href="#host-settings">
            <HardDrive size={16} aria-hidden="true" />
            机器与数据
          </a>
        </nav>
        <div className="settings-sections">
          <section
            className="settings-section"
            id="administrator-settings"
            aria-labelledby="administrator-settings-title"
          >
            <header className="settings-section-header">
              <h2 id="administrator-settings-title">管理员账号</h2>
              <p>更新用于登录管理中心的用户名和密码。</p>
            </header>
            {!settings ? (
              <div className="settings-load-state">
                {loadError ? (
                  <>
                    <ErrorMessage>{loadError}</ErrorMessage>
                    <button
                      className="button button-small"
                      onClick={() => setLoadAttempt((attempt) => attempt + 1)}
                    >
                      <RefreshCw size={14} aria-hidden="true" />
                      重新加载
                    </button>
                  </>
                ) : (
                  <Spinner label="正在加载配置" />
                )}
              </div>
            ) : (
              <form className="settings-account-form" onSubmit={save}>
                <fieldset disabled={pending}>
                  <label htmlFor="settings-username">管理员用户名</label>
                  <input
                    id="settings-username"
                    name="username"
                    autoComplete="username"
                    maxLength={120}
                    required
                    value={username}
                    onChange={(event) => {
                      setUsername(event.target.value);
                      setSaved(false);
                    }}
                  />
                  <label htmlFor="settings-current-password">当前密码</label>
                  <input
                    id="settings-current-password"
                    name="current_password"
                    type="password"
                    autoComplete="current-password"
                    maxLength={1024}
                    required
                    aria-describedby="settings-current-password-hint"
                    value={currentPassword}
                    onChange={(event) => {
                      setCurrentPassword(event.target.value);
                      setSaved(false);
                    }}
                  />
                  <p
                    className="settings-field-hint"
                    id="settings-current-password-hint"
                  >
                    修改账号配置前，需要验证当前密码。
                  </p>
                  <div className="settings-password-fields">
                    <div>
                      <label htmlFor="settings-new-password">新密码</label>
                      <input
                        id="settings-new-password"
                        name="new_password"
                        type="password"
                        autoComplete="new-password"
                        minLength={8}
                        maxLength={1024}
                        placeholder="留空保留当前密码"
                        aria-describedby="settings-new-password-hint"
                        value={newPassword}
                        onChange={(event) => {
                          setNewPassword(event.target.value);
                          setSaved(false);
                        }}
                      />
                    </div>
                    <div>
                      <label htmlFor="settings-confirm-password">
                        确认新密码
                      </label>
                      <input
                        id="settings-confirm-password"
                        name="confirm_new_password"
                        type="password"
                        autoComplete="new-password"
                        maxLength={1024}
                        required={Boolean(newPassword)}
                        placeholder="再次输入新密码"
                        value={confirmation}
                        onChange={(event) => {
                          setConfirmation(event.target.value);
                          setSaved(false);
                        }}
                      />
                    </div>
                  </div>
                  <p
                    className="settings-field-hint"
                    id="settings-new-password-hint"
                  >
                    新密码至少 8 个字符，留空则仅更新用户名。
                  </p>
                  {saveError && <ErrorMessage>{saveError}</ErrorMessage>}
                  {saved && (
                    <div className="settings-save-status" role="status">
                      <Check size={16} aria-hidden="true" />
                      <span>管理员账号已更新</span>
                    </div>
                  )}
                  <footer className="settings-form-footer">
                    <p>其他登录会话将失效，当前浏览器保持登录。</p>
                    <button className="button button-primary" type="submit">
                      {pending ? "正在保存…" : "保存更改"}
                    </button>
                  </footer>
                </fieldset>
              </form>
            )}
          </section>
          <HostSettings onHostDeleted={onHostDeleted} />
        </div>
      </div>
    </div>
  );
}
