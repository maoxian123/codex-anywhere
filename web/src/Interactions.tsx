import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import {
  Check,
  ChevronDown,
  ExternalLink,
  HelpCircle,
  ShieldCheck,
} from "lucide-react";
import { ApiError, errorText, post } from "./api";
import { ErrorMessage, Spinner } from "./components";
import type { Command, Interaction, JsonObject } from "./types";
import { json, list, object, safeLink, string } from "./utils";

const decisionNames: Record<string, string> = {
  accept: "允许这一次",
  acceptForSession: "本次会话允许",
  decline: "拒绝",
  cancel: "取消执行",
  approved: "允许",
  approved_for_session: "本次会话允许",
  denied: "拒绝",
  abort: "取消执行",
};

function Elicitation({
  interaction,
  onAnswer,
  sending,
  disabled = sending,
}: {
  interaction: Interaction;
  onAnswer: (response: JsonObject) => void;
  sending: boolean;
  disabled?: boolean;
}) {
  const params = interaction.params;
  const request = object(params.request);
  const schema = object(params.requestedSchema || request.requestedSchema);
  const properties = Object.entries(object(schema.properties)).map(
    ([name, value]) => [name, object(value)] as const,
  );
  const required = list(schema.required);
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const url = safeLink(string(params.url || request.url));
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (disabled) return;
    setError("");
    try {
      const content: JsonObject = {};
      properties.forEach(([name, property]) => {
        const value =
          values[name] ??
          (property.default != null ? String(property.default) : "");
        if (!value && !required.includes(name)) return;
        if (property.type === "boolean") content[name] = value === "true";
        else if (property.type === "number" || property.type === "integer") {
          const number = Number(value);
          if (
            !Number.isFinite(number) ||
            (property.type === "integer" && !Number.isInteger(number))
          )
            throw new Error(
              `${name} 必须是有效${property.type === "integer" ? "整数" : "数字"}。`,
            );
          content[name] = number;
        } else if (property.type === "object" || property.type === "array")
          content[name] = JSON.parse(value);
        else content[name] = value;
      });
      onAnswer({ action: "accept", ...(properties.length ? { content } : {}) });
    } catch (reason) {
      setError(errorText(reason));
    }
  };
  return (
    <form className="form-stack elicitation-form" onSubmit={submit}>
      {url && (
        <a
          className="button button-small"
          href={url}
          target="_blank"
          rel="noopener noreferrer"
        >
          <ExternalLink size={14} />
          打开请求的网页
        </a>
      )}
      {properties.map(([name, property]) => {
        const fieldRequired = required.includes(name);
        const common = {
          value:
            values[name] ??
            (property.default != null ? String(property.default) : ""),
          onChange: (
            event: React.ChangeEvent<
              HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
            >,
          ) =>
            setValues((current) => ({
              ...current,
              [name]: event.target.value,
            })),
          required: fieldRequired,
          disabled,
        };
        return (
          <label key={name}>
            {string(property.title, name)}
            {fieldRequired && <span className="required">*</span>}
            {Array.isArray(property.enum) ? (
              <select {...common}>
                <option value="">请选择</option>
                {property.enum.map((option, index) => (
                  <option key={index} value={String(option)}>
                    {String(option)}
                  </option>
                ))}
              </select>
            ) : property.type === "boolean" ? (
              <select {...common}>
                <option value="">请选择</option>
                <option value="true">是</option>
                <option value="false">否</option>
              </select>
            ) : property.type === "object" || property.type === "array" ? (
              <textarea {...common} rows={3} placeholder="JSON" />
            ) : (
              <input
                {...common}
                type={
                  property.type === "number" || property.type === "integer"
                    ? "number"
                    : "text"
                }
                step={property.type === "integer" ? 1 : "any"}
              />
            )}
            {property.description != null && (
              <small className="field-hint">
                {string(property.description)}
              </small>
            )}
          </label>
        );
      })}
      {!properties.length && !url && (
        <p className="field-hint">确认后将向工具返回接受请求。</p>
      )}
      {error && <ErrorMessage>{error}</ErrorMessage>}
      <div className="approval-actions">
        <button
          className="button button-primary button-small"
          type="submit"
          disabled={disabled}
        >
          {sending ? <Spinner label="提交中" /> : "确认"}
        </button>
        <button
          className="button button-quiet button-small"
          type="button"
          disabled={disabled}
          onClick={() => onAnswer({ action: "decline" })}
        >
          拒绝
        </button>
        <button
          className="button button-quiet button-small"
          type="button"
          disabled={disabled}
          onClick={() => onAnswer({ action: "cancel" })}
        >
          取消
        </button>
      </div>
    </form>
  );
}

function Questions({
  interaction,
  onAnswer,
  sending,
  canSubmit,
  locked = false,
}: {
  interaction: Interaction;
  onAnswer: (response: JsonObject) => void;
  sending: boolean;
  canSubmit: boolean;
  locked?: boolean;
}) {
  const questions = list(interaction.params.questions).map(object);
  const groupId = useId();
  const form = useRef<HTMLFormElement>(null);
  const [choices, setChoices] = useState<Record<string, number | "other">>({});
  const [text, setText] = useState<Record<string, string>>({});
  const [missing, setMissing] = useState<number[]>([]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit || sending) return;
    const result: Record<string, { answers: string[] }> = Object.create(null);
    const unanswered: number[] = [];
    questions.forEach((question, index) => {
      const id = string(question.id, String(index));
      const options = list(question.options).map(object);
      const choice = choices[id];
      const value =
        !options.length || choice === "other"
          ? text[id] || ""
          : typeof choice === "number"
            ? string(options[choice]?.label)
            : "";
      if (!value.trim()) unanswered.push(index);
      result[id] = { answers: [value] };
    });
    setMissing(unanswered);
    if (unanswered.length) {
      form.current
        ?.querySelector<HTMLElement>(
          `[data-question-index="${unanswered[0]}"] input`,
        )
        ?.focus();
      return;
    }
    onAnswer({ answers: result });
  };
  return (
    <form ref={form} onSubmit={submit} className="questions-form" noValidate>
      <div className="questions-scroll">
        {questions.map((question, index) => {
          const id = string(question.id, String(index));
          const options = list(question.options).map(object);
          const inputName = `${groupId}-question-${index}`;
          const errorId = `${inputName}-error`;
          const title = string(
            question.header || question.question,
            `问题 ${index + 1}`,
          );
          const hasError = missing.includes(index);
          const customInput = !options.length || choices[id] === "other";
          return (
            <fieldset
              key={id}
              disabled={sending || locked}
              data-question-index={index}
              aria-describedby={hasError ? errorId : undefined}
              className={hasError ? "question-invalid" : undefined}
            >
              <legend>
                <span className="question-number">{index + 1}</span>
                {title}
              </legend>
              {Boolean(question.header && question.question) && (
                <p>{string(question.question)}</p>
              )}
              {options.length ? (
                <div className="question-options">
                  {options.map((option, optionIndex) => (
                    <label
                      className={`question-option ${choices[id] === optionIndex ? "selected" : ""}`}
                      key={optionIndex}
                    >
                      <input
                        type="radio"
                        name={inputName}
                        value={string(option.label)}
                        checked={choices[id] === optionIndex}
                        onChange={() =>
                          setChoices((current) => ({
                            ...current,
                            [id]: optionIndex,
                          }))
                        }
                        aria-invalid={hasError || undefined}
                      />
                      <span>
                        <strong>{string(option.label)}</strong>
                        {option.description != null && (
                          <small>{string(option.description)}</small>
                        )}
                      </span>
                      {choices[id] === optionIndex && (
                        <Check size={15} aria-hidden="true" />
                      )}
                    </label>
                  ))}
                  {question.isOther === true && (
                    <label
                      className={`question-option ${choices[id] === "other" ? "selected" : ""}`}
                    >
                      <input
                        type="radio"
                        name={inputName}
                        checked={choices[id] === "other"}
                        onChange={() =>
                          setChoices((current) => ({
                            ...current,
                            [id]: "other",
                          }))
                        }
                        aria-invalid={hasError || undefined}
                      />
                      <span>自定义回答</span>
                    </label>
                  )}
                </div>
              ) : null}
              {customInput && (
                <label className="question-answer">
                  <span>
                    {options.length
                      ? `${title}：自定义回答`
                      : string(question.question, title)}
                  </span>
                  <input
                    type={question.isSecret === true ? "password" : "text"}
                    placeholder="输入你的回答"
                    autoComplete="off"
                    autoCapitalize="off"
                    spellCheck={question.isSecret === true ? false : undefined}
                    value={text[id] || ""}
                    onChange={(event) =>
                      setText((current) => ({
                        ...current,
                        [id]: event.target.value,
                      }))
                    }
                    aria-invalid={hasError || undefined}
                    aria-describedby={hasError ? errorId : undefined}
                  />
                </label>
              )}
              {hasError && (
                <p className="question-error" id={errorId}>
                  请选择一个选项或填写回答。
                </p>
              )}
            </fieldset>
          );
        })}
      </div>
      <div className="questions-footer">
        {missing.length > 0 && (
          <ErrorMessage>
            请回答全部 {questions.length} 个问题后再提交。
          </ErrorMessage>
        )}
        <button
          type="submit"
          className="button button-primary button-small"
          disabled={sending || !canSubmit}
        >
          {sending ? <Spinner label="提交中" /> : "提交回答"}
        </button>
        <span className="questions-submit-hint">
          选择后点击提交，Codex 才会收到回答。
        </span>
      </div>
    </form>
  );
}

function InteractionCard({
  interaction,
  onCommand,
  hostOnline,
  onResolved,
}: {
  interaction: Interaction;
  onCommand: (command: Command) => void;
  hostOnline: boolean;
  onResolved?: () => void;
}) {
  const inFlight = useRef(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [rawResponse, setRawResponse] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [stale, setStale] = useState(false);
  useEffect(() => {
    // A 409 can also mean the device went offline. Only an authoritative
    // refreshed pending request makes an explicit retry available again.
    if (["pending", "open", "waiting"].includes(interaction.status)) {
      setStale(false);
      setError("");
    }
  }, [interaction]);
  const questions = list(interaction.params.questions).length > 0;
  const nonBlocking = questions && interaction.params.isBlocking === false;
  const waitingForDevice = interaction.status === "answering" || submitted;
  const disabled = sending || waitingForDevice || stale || !hostOnline;
  const approval = interaction.method.toLowerCase().includes("approval");
  const permissions = interaction.method === "item/permissions/requestApproval";
  const elicitation = interaction.method.toLowerCase().includes("elicitation");
  const nativeDecisionApproval = [
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
  ].includes(interaction.method);
  const decisions = list(
    interaction.params.availableDecisions ??
      (nativeDecisionApproval
        ? ["accept", "acceptForSession", "decline", "cancel"]
        : []),
  );
  const answer = async (response: JsonObject) => {
    if (inFlight.current || disabled) return;
    inFlight.current = true;
    setSending(true);
    setError("");
    let command: Command;
    try {
      const result = await post<{ command: Command }>(
        `/interactions/${interaction.id}/answer`,
        { response },
      );
      command = result.command;
      setUncertain(command.status === "unknown");
      setSubmitted(true);
    } catch (reason) {
      setError(errorText(reason));
      if (reason instanceof ApiError && reason.status === 409) {
        setStale(true);
        onResolved?.();
      }
      inFlight.current = false;
      return;
    } finally {
      setSending(false);
    }
    onCommand(command);
  };
  if (waitingForDevice)
    return (
      <div className="interaction-submitted" role="status">
        {uncertain ? <HelpCircle size={15} /> : <Check size={15} />}
        <span>
          {uncertain
            ? "回答的提交结果尚未确认，请等待设备同步，避免重复回答。"
            : "已提交，等待远程设备确认"}
        </span>
      </div>
    );
  return (
    <section
      className={`interaction-card ${questions ? "interaction-card--questions" : ""}`}
      aria-label={questions ? "Codex 提问" : "交互请求"}
    >
      <div className="interaction-heading">
        {approval ? <ShieldCheck size={19} /> : <HelpCircle size={19} />}
        <div>
          <strong>
            {approval
              ? "需要你的审批"
              : questions
                ? nonBlocking
                  ? "Codex 提问"
                  : "Codex 等待你的回答"
                : "需要处理交互请求"}
          </strong>
          <p>
            {string(
              interaction.params.reason || interaction.params.message,
              approval
                ? "请检查操作内容，再选择允许范围。"
                : questions
                  ? nonBlocking
                    ? "任务仍在继续，你可以在这里回答。"
                    : "请回答下面的问题，提交后继续任务。"
                  : interaction.method,
            )}
          </p>
        </div>
      </div>
      {!hostOnline && (
        <p className="interaction-notice" role="status">
          设备离线，回答暂时无法提交。设备上线后可继续。
        </p>
      )}
      {stale && (
        <p className="interaction-notice" role="status">
          请求已处理或失效，正在更新状态。
        </p>
      )}
      {interaction.params.command != null && (
        <pre className="approval-command">
          {typeof interaction.params.command === "string"
            ? interaction.params.command
            : json(interaction.params.command)}
        </pre>
      )}
      {interaction.params.cwd != null && (
        <p className="field-hint">工作目录：{string(interaction.params.cwd)}</p>
      )}
      {questions ? (
        <Questions
          interaction={interaction}
          sending={sending}
          locked={stale}
          canSubmit={!disabled}
          onAnswer={(response) => void answer(response)}
        />
      ) : permissions ? (
        <>
          <pre className="approval-command">
            {json(interaction.params.permissions)}
          </pre>
          <div className="approval-actions">
            <button
              className="button button-primary button-small"
              disabled={disabled}
              onClick={() =>
                void answer({
                  permissions: object(interaction.params.permissions),
                  scope: "turn",
                })
              }
            >
              仅当前执行允许
            </button>
            <button
              className="button button-quiet button-small"
              disabled={disabled}
              onClick={() => void answer({ permissions: {}, scope: "turn" })}
            >
              拒绝
            </button>
            {sending && <Spinner label="提交中" />}
          </div>
        </>
      ) : elicitation ? (
        <Elicitation
          interaction={interaction}
          sending={sending}
          disabled={disabled}
          onAnswer={(response) => void answer(response)}
        />
      ) : approval && decisions.length ? (
        <div className="approval-actions">
          {decisions.map((decision, index) => {
            const name =
              typeof decision === "string"
                ? decision
                : Object.keys(object(decision))[0] || "自定义决定";
            return (
              <button
                key={index}
                className={`button button-small ${["accept", "approved"].includes(name) ? "button-primary" : "button-quiet"}`}
                disabled={disabled}
                onClick={() => void answer({ decision })}
              >
                {decisionNames[name] || name}
              </button>
            );
          })}
          {sending && <Spinner label="提交中" />}
        </div>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            try {
              const parsed: unknown = JSON.parse(rawResponse);
              if (
                !parsed ||
                typeof parsed !== "object" ||
                Array.isArray(parsed)
              )
                throw new Error("回复必须是 JSON 对象。");
              void answer(parsed as JsonObject);
            } catch (reason) {
              setError(errorText(reason));
            }
          }}
          className="form-stack"
        >
          <label>
            原生 JSON 回复
            <textarea
              value={rawResponse}
              onChange={(event) => setRawResponse(event.target.value)}
              rows={3}
              placeholder="输入此请求所需的原生回复对象"
              required
              disabled={disabled}
              spellCheck={false}
            />
          </label>
          <button
            type="submit"
            className="button button-small"
            disabled={disabled}
          >
            {sending ? <Spinner label="提交中" /> : "提交回复"}
          </button>
        </form>
      )}
      {error && <ErrorMessage>{error}</ErrorMessage>}
      <details className="raw-details">
        <summary>
          <ChevronDown size={13} />
          原生请求详情
        </summary>
        <pre>{json(interaction.params)}</pre>
      </details>
    </section>
  );
}

export default function Interactions({
  interactions,
  onCommand,
  hostOnline = true,
  onResolved,
}: {
  interactions: Interaction[];
  onCommand: (command: Command) => void;
  hostOnline?: boolean;
  onResolved?: () => void;
}) {
  const pending = interactions.filter((interaction) =>
    ["pending", "open", "waiting", "answering"].includes(interaction.status),
  );
  if (!pending.length) return null;
  const hasQuestions = pending.some(
    (interaction) => list(interaction.params.questions).length > 0,
  );
  return (
    <div
      className={`interactions${hasQuestions ? " interactions--questions" : ""}`}
      aria-live="polite"
    >
      {pending.map((interaction) => (
        <InteractionCard
          key={interaction.id}
          interaction={interaction}
          onCommand={onCommand}
          hostOnline={hostOnline}
          onResolved={onResolved}
        />
      ))}
    </div>
  );
}
