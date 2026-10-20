import { useState, type FormEvent } from "react";
import {
  Check,
  ChevronDown,
  ExternalLink,
  HelpCircle,
  ShieldCheck,
} from "lucide-react";
import { errorText, post } from "./api";
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
}: {
  interaction: Interaction;
  onAnswer: (response: JsonObject) => void;
  sending: boolean;
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
          disabled: sending,
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
          disabled={sending}
        >
          {sending ? <Spinner label="提交中" /> : "确认"}
        </button>
        <button
          className="button button-quiet button-small"
          type="button"
          disabled={sending}
          onClick={() => onAnswer({ action: "decline" })}
        >
          拒绝
        </button>
        <button
          className="button button-quiet button-small"
          type="button"
          disabled={sending}
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
}: {
  interaction: Interaction;
  onAnswer: (response: JsonObject) => void;
  sending: boolean;
}) {
  const questions = list(interaction.params.questions).map(object);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [other, setOther] = useState<Record<string, string>>({});
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const result: Record<string, { answers: string[] }> = {};
    questions.forEach((question, index) => {
      const id = string(question.id, String(index));
      result[id] = {
        answers: [
          answers[id] === "__other__" ? other[id] || "" : answers[id] || "",
        ],
      };
    });
    onAnswer({ answers: result });
  };
  return (
    <form onSubmit={submit} className="questions-form">
      {questions.map((question, index) => {
        const id = string(question.id, String(index));
        const options = list(question.options).map(object);
        return (
          <fieldset key={id} disabled={sending}>
            <legend>
              {string(
                question.header || question.question,
                `问题 ${index + 1}`,
              )}
            </legend>
            {Boolean(question.header && question.question) && (
              <p>{string(question.question)}</p>
            )}
            {options.length ? (
              <div className="question-options">
                {options.map((option, optionIndex) => (
                  <label
                    className={`question-option ${answers[id] === string(option.label) ? "selected" : ""}`}
                    key={optionIndex}
                  >
                    <input
                      type="radio"
                      name={id}
                      value={string(option.label)}
                      checked={answers[id] === string(option.label)}
                      onChange={() =>
                        setAnswers((current) => ({
                          ...current,
                          [id]: string(option.label),
                        }))
                      }
                      required
                    />
                    <span>
                      <strong>{string(option.label)}</strong>
                      {option.description != null && (
                        <small>{string(option.description)}</small>
                      )}
                    </span>
                    {answers[id] === string(option.label) && (
                      <Check size={15} />
                    )}
                  </label>
                ))}
                {question.isOther === true && (
                  <label className="question-option">
                    <input
                      type="radio"
                      name={id}
                      checked={answers[id] === "__other__"}
                      onChange={() =>
                        setAnswers((current) => ({
                          ...current,
                          [id]: "__other__",
                        }))
                      }
                    />
                    <span>自定义回答</span>
                  </label>
                )}
                {answers[id] === "__other__" && (
                  <input
                    aria-label="自定义回答"
                    type={question.isSecret ? "password" : "text"}
                    value={other[id] || ""}
                    onChange={(event) =>
                      setOther((current) => ({
                        ...current,
                        [id]: event.target.value,
                      }))
                    }
                    required
                  />
                )}
              </div>
            ) : (
              <input
                type={question.isSecret ? "password" : "text"}
                aria-label={string(question.question, `问题 ${index + 1}`)}
                placeholder="输入你的回答"
                value={answers[id] || ""}
                onChange={(event) =>
                  setAnswers((current) => ({
                    ...current,
                    [id]: event.target.value,
                  }))
                }
                required
              />
            )}
          </fieldset>
        );
      })}
      <button
        type="submit"
        className="button button-primary button-small"
        disabled={sending}
      >
        {sending ? <Spinner label="提交中" /> : "提交回答"}
      </button>
    </form>
  );
}

function InteractionCard({
  interaction,
  onCommand,
}: {
  interaction: Interaction;
  onCommand: (command: Command) => void;
}) {
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [rawResponse, setRawResponse] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const questions = list(interaction.params.questions).length > 0;
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
    setSending(true);
    setError("");
    try {
      const result = await post<{ command: Command }>(
        `/interactions/${interaction.id}/answer`,
        { response },
      );
      setSubmitted(true);
      onCommand(result.command);
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setSending(false);
    }
  };
  if (submitted)
    return (
      <div className="interaction-submitted">
        <Check size={15} />
        已提交，等待远程设备确认
      </div>
    );
  return (
    <section className="interaction-card">
      <div className="interaction-heading">
        {approval ? <ShieldCheck size={19} /> : <HelpCircle size={19} />}
        <div>
          <strong>
            {approval
              ? "需要你的审批"
              : questions
                ? "Codex 需要补充信息"
                : "需要处理交互请求"}
          </strong>
          <p>
            {string(
              interaction.params.reason || interaction.params.message,
              approval
                ? "请检查操作内容，再选择允许范围。"
                : interaction.method,
            )}
          </p>
        </div>
      </div>
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
              disabled={sending}
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
              disabled={sending}
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
                disabled={sending}
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
              disabled={sending}
              spellCheck={false}
            />
          </label>
          <button
            type="submit"
            className="button button-small"
            disabled={sending}
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
}: {
  interactions: Interaction[];
  onCommand: (command: Command) => void;
}) {
  const pending = interactions.filter((interaction) =>
    ["pending", "open", "waiting"].includes(interaction.status),
  );
  if (!pending.length) return null;
  return (
    <div className="interactions" aria-live="polite">
      {pending.map((interaction) => (
        <InteractionCard
          key={interaction.id}
          interaction={interaction}
          onCommand={onCommand}
        />
      ))}
    </div>
  );
}
