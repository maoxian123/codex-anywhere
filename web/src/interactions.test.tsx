import "./test-dom";
import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import Interactions from "./Interactions";
import type { Command, Interaction, JsonObject } from "./types";

const originalFetch = globalThis.fetch;
afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
  sessionStorage.clear();
  localStorage.clear();
});

const command: Command = {
  id: "answer-command",
  kind: "interaction.answer",
  status: "queued",
};
const question = (params: JsonObject = {}, id = "question-1"): Interaction => ({
  id,
  thread_id: "thread-1",
  method: "item/tool/requestUserInput",
  status: "pending",
  params: {
    questions: [
      {
        id: "approach",
        header: "实现方式",
        question: "选择使用哪种实现方式？",
        options: [
          { label: "完整实现 (Recommended)", description: "包含测试和发布。" },
          { label: "只做原型", description: "先验证交互。" },
        ],
      },
    ],
    ...params,
  },
});
const response = (body: unknown, status = 202) =>
  new Response(JSON.stringify(body), { status });
function capturePosts(result: Command = command) {
  const calls: { path: string; body: unknown }[] = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ path: String(input), body: JSON.parse(String(init?.body)) });
    return response({ command: result });
  };
  return calls;
}
function viewQuestion(interaction = question()) {
  return render(
    <Interactions interactions={[interaction]} onCommand={() => {}} />,
  );
}

test("native question options show descriptions without choosing or submitting a recommended answer", async () => {
  const calls = capturePosts();
  viewQuestion();
  assert.ok(
    screen
      .getByRole("region", { name: "Codex 提问" })
      .closest(".interactions--questions"),
  );
  assert.ok(screen.getByText("选择使用哪种实现方式？"));
  assert.ok(screen.getByText("包含测试和发布。"));
  assert.ok(
    screen
      .getAllByRole("radio")
      .every((input) => !(input as HTMLInputElement).checked),
  );
  fireEvent.click(screen.getByRole("radio", { name: /完整实现/ }));
  assert.equal(calls.length, 0);
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.deepEqual(calls, [
    {
      path: "/api/interactions/question-1/answer",
      body: {
        response: {
          answers: { approach: { answers: ["完整实现 (Recommended)"] } },
        },
      },
    },
  ]);
  assert.equal(screen.queryByRole("button", { name: /取消|跳过/ }), null);
});

test("native question IDs are preserved even when they match JavaScript prototype names", async () => {
  const calls = capturePosts();
  viewQuestion(
    question({ questions: [{ id: "__proto__", question: "填写回答" }] }),
  );
  fireEvent.change(screen.getByRole("textbox", { name: "填写回答" }), {
    target: { value: "原样回答" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.deepEqual(
    calls[0].body,
    JSON.parse(
      '{"response":{"answers":{"__proto__":{"answers":["原样回答"]}}}}',
    ),
  );
});

test("every question must be answered and whitespace-only free text is rejected without trimming valid answers", async () => {
  const calls = capturePosts();
  viewQuestion(
    question({
      questions: [
        {
          id: "choice",
          question: "选择一项",
          options: [{ label: "__other__" }, { label: "另一项" }],
        },
        { id: "details", header: "补充内容", question: "请填写细节" },
      ],
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  assert.ok(screen.getByRole("alert"));
  assert.equal(calls.length, 0);
  assert.equal(
    document.activeElement,
    screen.getByRole("radio", { name: "__other__" }),
  );
  fireEvent.click(screen.getByRole("radio", { name: "__other__" }));
  fireEvent.change(screen.getByRole("textbox", { name: "请填写细节" }), {
    target: { value: "   " },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  assert.equal(calls.length, 0);
  fireEvent.change(screen.getByRole("textbox", { name: "请填写细节" }), {
    target: { value: "  保留两侧空格  " },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.deepEqual(calls[0].body, {
    response: {
      answers: {
        choice: { answers: ["__other__"] },
        details: { answers: ["  保留两侧空格  "] },
      },
    },
  });
});

test("custom secret answers use password controls and remain only in transient form state", async () => {
  const calls = capturePosts();
  viewQuestion(
    question({
      questions: [
        {
          id: "credential",
          header: "凭据",
          question: "提供凭据",
          isOther: true,
          isSecret: true,
          options: [{ label: "使用已有凭据" }],
        },
      ],
    }),
  );
  fireEvent.click(screen.getByRole("radio", { name: "自定义回答" }));
  const input = screen.getByLabelText("凭据：自定义回答") as HTMLInputElement;
  assert.equal(input.type, "password");
  assert.equal(input.autocomplete, "off");
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  assert.equal(calls.length, 0);
  fireEvent.change(input, { target: { value: "  private-value  " } });
  assert.equal(localStorage.length, 0);
  assert.equal(sessionStorage.length, 0);
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.deepEqual(calls[0].body, {
    response: {
      answers: {
        credential: { answers: ["  private-value  "] },
      },
    },
  });
  assert.equal(screen.queryByDisplayValue("  private-value  "), null);
});

test("secret questions without options render a password field", () => {
  viewQuestion(
    question({
      questions: [{ id: "secret", question: "输入口令", isSecret: true }],
    }),
  );
  assert.equal(
    (screen.getByLabelText("输入口令") as HTMLInputElement).type,
    "password",
  );
});

test("simultaneous requests with identical native question IDs keep independent accessible radio groups", () => {
  render(
    <Interactions
      interactions={[question({}, "first"), question({}, "second")]}
      onCommand={() => {}}
    />,
  );
  const cards = screen.getAllByRole("region", { name: "Codex 提问" });
  const first = within(cards[0]).getAllByRole("radio") as HTMLInputElement[];
  const second = within(cards[1]).getAllByRole("radio") as HTMLInputElement[];
  assert.equal(first[0].name, first[1].name);
  assert.equal(second[0].name, second[1].name);
  assert.notEqual(first[0].name, second[0].name);
  fireEvent.click(first[0]);
  fireEvent.click(second[1]);
  assert.equal(first[0].checked, true);
  assert.equal(second[1].checked, true);
});

test("offline requests preserve editable answers while submission waits for the device", async () => {
  const calls = capturePosts();
  const interaction = question({
    questions: [{ id: "text", question: "填写回答" }],
  });
  const props = { interactions: [interaction], onCommand: () => {} };
  const view = render(<Interactions {...props} hostOnline={false} />);
  const input = screen.getByRole("textbox", {
    name: "填写回答",
  }) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "离线时填写的回答" } });
  assert.equal(input.disabled, false);
  assert.equal(
    (screen.getByRole("button", { name: "提交回答" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  assert.ok(screen.getByText(/设备离线/));
  fireEvent.submit(input.closest("form")!);
  assert.equal(calls.length, 0);
  view.rerender(<Interactions {...props} hostOnline />);
  assert.equal(input.value, "离线时填写的回答");
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.equal(calls.length, 1);
});

test("submitting synchronously locks repeated clicks until acknowledgement", async () => {
  let resolve!: (result: Response) => void;
  let calls = 0;
  let delivered = 0;
  globalThis.fetch = async () => {
    calls++;
    return new Promise<Response>((accept) => {
      resolve = accept;
    });
  };
  render(
    <Interactions
      interactions={[question()]}
      onCommand={() => {
        delivered++;
      }}
    />,
  );
  fireEvent.click(screen.getByRole("radio", { name: /只做原型/ }));
  const submit = screen.getByRole("button", { name: "提交回答" });
  const form = submit.closest("form")!;
  fireEvent.submit(form);
  fireEvent.submit(form);
  assert.equal(calls, 1);
  assert.equal(
    (screen.getByRole("button", { name: "提交中" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  resolve(response({ command }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.equal(delivered, 1);
  assert.equal(calls, 1);
});

test("HTTP errors retain option selection and custom drafts for an explicit retry", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return calls === 1
      ? response({ error: "服务暂时不可用" }, 503)
      : response({ command });
  };
  viewQuestion(question({ questions: [{ id: "text", question: "填写回答" }] }));
  const input = screen.getByRole("textbox", {
    name: "填写回答",
  }) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "保留这份回答" } });
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("服务暂时不可用");
  assert.equal(input.value, "保留这份回答");
  assert.equal(calls, 1);
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.equal(calls, 2);
});

test("a stale answer conflict triggers refresh and cannot resubmit an expired request", async () => {
  let calls = 0;
  let refreshes = 0;
  globalThis.fetch = async () => {
    calls++;
    return response({ error: "请求已经失效" }, 409);
  };
  const interaction = question({
    questions: [{ id: "text", question: "填写回答" }],
  });
  const view = render(
    <Interactions
      interactions={[interaction]}
      onCommand={() => {}}
      onResolved={() => {
        refreshes++;
      }}
    />,
  );
  const input = screen.getByRole("textbox", {
    name: "填写回答",
  }) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "未提交的回答" } });
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("请求已经失效");
  assert.equal(input.value, "未提交的回答");
  assert.equal(
    (screen.getByRole("button", { name: "提交回答" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  fireEvent.submit(input.closest("form")!);
  assert.equal(calls, 1);
  assert.equal(refreshes, 1);
  view.rerender(
    <Interactions
      interactions={[{ ...interaction, status: "expired" }]}
      onCommand={() => {}}
    />,
  );
  assert.equal(screen.queryByRole("region", { name: "Codex 提问" }), null);
});

test("answering requests and unknown command outcomes cannot be answered again", async () => {
  const calls = capturePosts({ ...command, status: "unknown" });
  const interaction = question();
  const view = viewQuestion({ ...interaction, status: "answering" });
  assert.ok(screen.getByText("已提交，等待远程设备确认"));
  assert.equal(screen.queryByRole("button", { name: "提交回答" }), null);
  view.unmount();
  viewQuestion(interaction);
  fireEvent.click(screen.getByRole("radio", { name: /只做原型/ }));
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText(/提交结果尚未确认/);
  assert.equal(screen.queryByRole("button", { name: "提交回答" }), null);
  assert.equal(calls.length, 1);
});

test("an offline conflict allows explicit retry only after a refreshed pending request reconnects", async () => {
  let calls = 0;
  globalThis.fetch = async (_input, init) => {
    calls++;
    assert.deepEqual(JSON.parse(String(init?.body)), {
      response: { answers: { text: { answers: ["断线时保留的回答"] } } },
    });
    return calls === 1
      ? response({ error: "设备已离线" }, 409)
      : response({ command });
  };
  const interaction = question({
    questions: [{ id: "text", question: "填写回答" }],
  });
  const props = { interactions: [interaction], onCommand: () => {} };
  const view = render(<Interactions {...props} hostOnline />);
  const input = screen.getByRole("textbox", {
    name: "填写回答",
  }) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "断线时保留的回答" } });
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("设备已离线");
  assert.equal(calls, 1);
  assert.equal(
    (screen.getByRole("button", { name: "提交回答" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  const refreshed = { ...interaction, params: { ...interaction.params } };
  view.rerender(
    <Interactions
      interactions={[refreshed]}
      onCommand={() => {}}
      hostOnline={false}
    />,
  );
  assert.equal(input.value, "断线时保留的回答");
  assert.equal(input.disabled, false);
  assert.equal(
    (screen.getByRole("button", { name: "提交回答" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  assert.equal(calls, 1);
  view.rerender(
    <Interactions interactions={[refreshed]} onCommand={() => {}} hostOnline />,
  );
  assert.equal(input.value, "断线时保留的回答");
  assert.equal(
    (screen.getByRole("button", { name: "提交回答" }) as HTMLButtonElement)
      .disabled,
    false,
  );
  assert.equal(calls, 1);
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.equal(calls, 2);
});

test("nonblocking model questions explain that the task continues", () => {
  viewQuestion(question({ isBlocking: false }));
  assert.ok(screen.getByText("Codex 提问"));
  assert.ok(screen.getByText("任务仍在继续，你可以在这里回答。"));
  assert.equal(screen.queryByText("Codex 等待你的回答"), null);
});

test("arbitrary native approval decisions preserve the complete object payload", async () => {
  const calls = capturePosts();
  const decision = {
    acceptWithExecpolicyAmendment: {
      execpolicy_amendment: ["python", "script.py"],
    },
  };
  viewQuestion({
    ...question(),
    method: "item/commandExecution/requestApproval",
    params: { command: "python script.py", availableDecisions: [decision] },
  });
  assert.equal(
    screen
      .getByRole("region", { name: "交互请求" })
      .closest(".interactions--questions"),
    null,
  );
  fireEvent.click(
    screen.getByRole("button", { name: "acceptWithExecpolicyAmendment" }),
  );
  await screen.findByText("已提交，等待远程设备确认");
  assert.deepEqual(calls[0].body, { response: { decision } });
});

test("permission approvals and schema elicitations retain native response shapes", async () => {
  const calls = capturePosts();
  const permission = { network: { enabled: true } };
  const first = viewQuestion({
    ...question(),
    method: "item/permissions/requestApproval",
    params: { permissions: permission },
  });
  fireEvent.click(screen.getByRole("button", { name: "仅当前执行允许" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.deepEqual(calls[0].body, {
    response: { permissions: permission, scope: "turn" },
  });
  first.unmount();
  viewQuestion({
    ...question(),
    method: "mcpServer/elicitation/request",
    params: {
      requestedSchema: {
        type: "object",
        properties: { port: { title: "端口", type: "integer" } },
        required: ["port"],
      },
    },
  });
  fireEvent.change(screen.getByRole("spinbutton", { name: /端口/ }), {
    target: { value: "8080" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.deepEqual(calls[1].body, {
    response: { action: "accept", content: { port: 8080 } },
  });
});

test("unknown interactions continue to accept explicitly entered native JSON", async () => {
  const calls = capturePosts();
  viewQuestion({
    ...question(),
    method: "future/native/request",
    params: { futureField: true },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "原生 JSON 回复" }), {
    target: { value: '{"custom":{"accepted":true}}' },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交回复" }));
  await screen.findByText("已提交，等待远程设备确认");
  assert.deepEqual(calls[0].body, { response: { custom: { accepted: true } } });
});
