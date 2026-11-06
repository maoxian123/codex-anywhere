import "./test-dom";
import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Composer from "./Composer";
import Interactions from "./Interactions";
import Timeline from "./Timeline";
import { CodeBlock, CopyButton, Markdown, References } from "./components";
import type { Asset, MessagePayload, Thread } from "./types";

const baseThread: Thread = {
  id: "t1",
  host_id: "h1",
  title: "test",
  cwd: "C:\\work",
  status: "idle",
  created_at: "2026-10-20T00:00:00Z",
  updated_at: "2026-10-20T00:00:00Z",
};
afterEach(cleanup);

test("highlighted Markdown code copies plain text and wraps independently from inline code", async () => {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const copied: string[] = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: async (text: string) => copied.push(text) },
  });
  try {
    const source = 'function greet() {\n  console.log("<hello>");\n}\n';
    const { container } = render(
      <Markdown
        text={`先读 \`inline code\`。\n\n\`\`\`javascript\n${source}\`\`\``}
        onImage={() => {}}
        onNavigate={() => {}}
      />,
    );
    const block = container.querySelector(".message-code-block")!;
    assert.equal(container.querySelectorAll(".message-code-block").length, 1);
    assert.equal(container.querySelector("p code")?.textContent, "inline code");
    assert.equal(block.getAttribute("data-wrap"), "on");
    assert.ok(block.querySelector(".hljs-keyword"));
    assert.equal(block.querySelector("pre")?.textContent, source);
    assert.ok(screen.getByText("javascript"));
    const wrap = screen.getByRole("button", { name: "自动换行" });
    assert.equal(wrap.getAttribute("aria-pressed"), "true");
    fireEvent.click(wrap);
    assert.equal(block.getAttribute("data-wrap"), "off");
    assert.equal(wrap.getAttribute("aria-pressed"), "false");
    fireEvent.click(screen.getByRole("button", { name: "复制内容" }));
    await waitFor(() => assert.deepEqual(copied, [source]));
    await screen.findByRole("button", { name: "已复制" });
    fireEvent.click(wrap);
    assert.equal(block.getAttribute("data-wrap"), "on");
    assert.equal(block.querySelector("pre")?.textContent, source);
  } finally {
    if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

test("indented text blocks have their own toolbar and preserve meaningful indentation", async () => {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  let copied = "";
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      writeText: async (text: string) => {
        copied = text;
      },
    },
  });
  try {
    render(
      <Markdown
        text={"    first line\n      second line\n\n最后一段。"}
        onImage={() => {}}
        onNavigate={() => {}}
      />,
    );
    assert.ok(screen.getByText("文本"));
    fireEvent.click(screen.getByRole("button", { name: "复制内容" }));
    await waitFor(() => assert.equal(copied, "first line\n  second line\n"));
  } finally {
    if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

test("code blocks copy the current streaming value after an update", async () => {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const copied: string[] = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: async (text: string) => copied.push(text) },
  });
  try {
    const view = render(<CodeBlock text="partial" language="终端输出" />);
    view.rerender(
      <CodeBlock text={"partial\n  completed\n"} language="终端输出" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "复制内容" }));
    await waitFor(() => assert.deepEqual(copied, ["partial\n  completed\n"]));
  } finally {
    if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

test("copy falls back on HTTP, restores focus, and reports failure if copying is blocked", async () => {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const commandDescriptor = Object.getOwnPropertyDescriptor(
    document,
    "execCommand",
  );
  const copied: string[] = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: undefined,
  });
  Object.defineProperty(document, "execCommand", {
    configurable: true,
    value: (command: string) => {
      assert.equal(command, "copy");
      copied.push((document.activeElement as HTMLTextAreaElement).value);
      return true;
    },
  });
  try {
    const view = render(
      <CopyButton text={"  original\n"} label="复制测试" showLabel />,
    );
    const button = screen.getByRole("button", { name: "复制测试" });
    button.focus();
    fireEvent.click(button);
    await waitFor(() => assert.deepEqual(copied, ["  original\n"]));
    assert.equal(document.activeElement, button);
    assert.equal(document.querySelectorAll("textarea").length, 0);
    view.unmount();
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: () => false,
    });
    render(<CopyButton text="blocked" label="复制测试" showLabel />);
    fireEvent.click(screen.getByRole("button", { name: "复制测试" }));
    await screen.findByRole("button", { name: "复制失败，请手动复制" });
    assert.equal(document.querySelectorAll("textarea").length, 0);
  } finally {
    if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor);
    else Reflect.deleteProperty(navigator, "clipboard");
    if (commandDescriptor)
      Object.defineProperty(document, "execCommand", commandDescriptor);
    else Reflect.deleteProperty(document, "execCommand");
  }
});

test("messages and native terminal output expose separate copies of the original content", async () => {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const copied: string[] = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: async (text: string) => copied.push(text) },
  });
  try {
    const userText = "  保留用户输入\n以及换行";
    const reply = "**回复**\n\n原始 Markdown";
    const output = "  success\n\tfinal\n";
    const { container } = render(
      <Timeline
        items={[
          {
            id: "u",
            native_id: "u",
            thread_id: "t1",
            type: "userMessage",
            data: { text: userText },
          },
          {
            id: "a",
            native_id: "a",
            thread_id: "t1",
            type: "agentMessage",
            data: { text: reply },
          },
          {
            id: "c",
            native_id: "c",
            thread_id: "t1",
            type: "commandExecution",
            data: { command: "npm run build", aggregatedOutput: output },
          },
        ]}
        targetItem="c"
        streams={{}}
        busy={false}
        loading={false}
        onImage={() => {}}
        onNavigate={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "复制消息" }));
    fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    fireEvent.click(screen.getByRole("button", { name: "复制内容" }));
    await waitFor(() =>
      assert.deepEqual(copied, [userText, reply, `npm run build\n\n${output}`]),
    );
    assert.equal(
      container.querySelector(".terminal-output")?.textContent,
      `$ npm run build\n\n${output}`,
    );
  } finally {
    if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

test("Markdown reading controls preserve HTML and unsafe-link protections", () => {
  const { container } = render(
    <Markdown
      text={
        "<script>alert(1)</script>\n\n[危险](javascript:alert%281%29)\n\n```html\n<img src=x onerror=alert(1)>\n```\n\n| 名称 | 内容 |\n| --- | --- |\n| a | b |"
      }
      onImage={() => {}}
      onNavigate={() => {}}
    />,
  );
  assert.equal(
    container.querySelectorAll("script, img, a[href^='javascript:']").length,
    0,
  );
  assert.equal(
    container.querySelector("pre")?.textContent,
    "<img src=x onerror=alert(1)>\n",
  );
  assert.ok(screen.getByRole("region", { name: "表格（可横向滚动）" }));
  assert.equal(
    container.querySelector(".table-scroll")?.getAttribute("tabindex"),
    "0",
  );
});

test("compact composer keeps runtime settings available behind its toolbar disclosure", () => {
  render(
    <Composer
      thread={baseThread}
      hostOnline={true}
      onSend={async () => {}}
      onInterrupt={async () => {}}
      onNavigate={() => {}}
    />,
  );
  const toggle = screen.getByLabelText("运行设置");
  const disclosure = toggle.closest("details")!;
  assert.ok(!disclosure.open);
  fireEvent.click(toggle);
  assert.ok(disclosure.open);
  assert.ok(screen.getByRole("combobox", { name: "模型" }));
  assert.ok(screen.getByRole("combobox", { name: "推理强度" }));
  assert.ok(screen.getByRole("combobox", { name: "权限" }));
  fireEvent.click(toggle);
  assert.ok(!disclosure.open);
  assert.ok(screen.getByRole("textbox", { name: "发送消息给 Codex" }));
});

test("compact native items fetch full content only on an explicit click and keep bridged images", async () => {
  const original = globalThis.fetch;
  let reads = 0;
  globalThis.fetch = async (input) => {
    reads += 1;
    assert.equal(String(input), "/api/threads/t1/items/compact-item");
    return new Response(
      JSON.stringify({
        item: {
          id: "compact-item",
          native_id: "native-compact",
          thread_id: "t1",
          type: "agentMessage",
          data: { text: "完整的模型消息，已从原始条目读取。" },
        },
      }),
    );
  };
  try {
    render(
      <Timeline
        items={[
          {
            id: "compact-item",
            native_id: "native-compact",
            thread_id: "t1",
            type: "agentMessage",
            data: {
              text: "简短预览",
              _truncated: true,
              _assets: [
                {
                  id: "picture",
                  name: "output.png",
                  mime: "image/png",
                  size: 1,
                  url: "/api/assets/picture",
                },
              ],
            },
          },
        ]}
        streams={{}}
        busy={false}
        loading={false}
        onImage={() => {}}
        onNavigate={() => {}}
      />,
    );
    assert.equal(reads, 0);
    fireEvent.click(screen.getByRole("button", { name: "查看完整内容" }));
    await screen.findByText("完整的模型消息，已从原始条目读取。");
    assert.equal(reads, 1);
    assert.ok(screen.getByAltText("output.png"));
    fireEvent.click(screen.getByRole("button", { name: "收起完整内容" }));
    assert.ok(screen.getByText("简短预览"));
    fireEvent.click(screen.getByRole("button", { name: "查看完整内容" }));
    await screen.findByText("完整的模型消息，已从原始条目读取。");
    assert.equal(reads, 1);
  } finally {
    globalThis.fetch = original;
  }
});

test("idle sends start a turn, active sends queue by default, and explicit steering is separate", async () => {
  const calls: { payload: MessagePayload; steer: boolean }[] = [];
  let interruptions = 0;
  const props = {
    hostOnline: true,
    onSend: async (payload: MessagePayload, steer: boolean) => {
      calls.push({ payload, steer });
    },
    onInterrupt: async () => {
      interruptions += 1;
    },
    onNavigate: () => {},
  };
  const view = render(<Composer thread={baseThread} {...props} />);
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息给 Codex" }), {
    target: { value: "first message" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => assert.equal(calls.length, 1));
  assert.equal(calls[0].steer, false);
  view.rerender(
    <Composer
      thread={{ ...baseThread, status: "active", active_turn_id: "turn-1" }}
      {...props}
    />,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "排队消息" }), {
    target: { value: "add a test" },
  });
  fireEvent.click(screen.getByRole("button", { name: "加入队列" }));
  await waitFor(() => assert.equal(calls.length, 2));
  assert.equal(calls[1].steer, false);
  await waitFor(() =>
    assert.equal(
      (screen.getByRole("textbox", { name: "排队消息" }) as HTMLTextAreaElement)
        .value,
      "",
    ),
  );
  fireEvent.change(screen.getByRole("textbox", { name: "排队消息" }), {
    target: { value: "adjust the current task now" },
  });
  fireEvent.click(screen.getByRole("button", { name: "立即补充" }));
  await waitFor(() => assert.equal(calls.length, 3));
  assert.equal(calls[2].steer, true);
  fireEvent.click(screen.getByRole("button", { name: "中断当前执行" }));
  await waitFor(() => assert.equal(interruptions, 1));
});

test("offline hosts preserve drafts and disable remote send and interruption", () => {
  render(
    <Composer
      thread={{ ...baseThread, status: "active" }}
      hostOnline={false}
      onSend={async () => {
        throw new Error("must not send");
      }}
      onInterrupt={async () => {
        throw new Error("must not interrupt");
      }}
      onNavigate={() => {}}
    />,
  );
  const input = screen.getByRole("textbox", {
    name: "排队消息",
  }) as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: "draft while offline" } });
  assert.equal(input.value, "draft while offline");
  assert.equal(
    (screen.getByRole("button", { name: "加入队列" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  assert.equal(
    (screen.getByRole("button", { name: "立即补充" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  assert.equal(
    (screen.getByRole("button", { name: "中断当前执行" }) as HTMLButtonElement)
      .disabled,
    true,
  );
});

test("a failed send preserves draft and reuses idempotency ID until the content changes", async () => {
  const requestIds: string[] = [];
  render(
    <Composer
      thread={baseThread}
      hostOnline
      onSend={async (payload) => {
        requestIds.push(payload.client_request_id);
        throw new Error("connection lost");
      }}
      onInterrupt={async () => {}}
      onNavigate={() => {}}
    />,
  );
  const input = screen.getByRole("textbox", {
    name: "发送消息给 Codex",
  }) as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: "keep this draft" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText("connection lost");
  assert.equal(input.value, "keep this draft");
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => assert.equal(requestIds.length, 2));
  assert.equal(requestIds[0], requestIds[1]);
  await waitFor(() =>
    assert.equal(
      (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
        .disabled,
      false,
    ),
  );
  fireEvent.change(input, { target: { value: "edited draft" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => assert.equal(requestIds.length, 3));
  assert.notEqual(requestIds[1], requestIds[2]);
});

test("native known approval default choices send a decision response", async () => {
  const original = globalThis.fetch;
  let sent: unknown;
  globalThis.fetch = async (_input, init) => {
    sent = JSON.parse(String(init?.body));
    return new Response(
      JSON.stringify({
        command: { id: "c1", kind: "interaction.answer", status: "queued" },
      }),
      { status: 202 },
    );
  };
  try {
    render(
      <Interactions
        interactions={[
          {
            id: "i1",
            thread_id: "t1",
            method: "item/commandExecution/requestApproval",
            params: { command: "echo hello" },
            status: "pending",
          },
        ]}
        onCommand={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "允许这一次" }));
    await screen.findByText("已提交，等待远程设备确认");
    assert.deepEqual(sent, { response: { decision: "accept" } });
  } finally {
    globalThis.fetch = original;
  }
});

test("task reference links navigate to the exact source message", () => {
  let destination: unknown;
  render(
    <References
      references={[
        {
          kind: "task",
          thread_id: "source-1",
          item_id: "message-2",
          title: "来源任务",
        },
      ]}
      onNavigate={(id, item) => {
        destination = { id, item };
      }}
    />,
  );
  fireEvent.click(screen.getByRole("link", { name: /来源任务/ }));
  assert.deepEqual(destination, { id: "source-1", item: "message-2" });
});

test("a real Windows Markdown image path renders only its bridged authenticated asset", () => {
  let opened: string | undefined;
  render(
    <Markdown
      text={"![工具图片](F:/work/generated-picture.png)"}
      assets={[
        {
          id: "asset-1",
          name: "generated-picture.png",
          mime: "image/png",
          size: 10,
          url: "/api/assets/asset-1",
          source_path: "F:\\work\\generated-picture.png",
        },
      ]}
      onNavigate={() => {}}
      onImage={(source) => {
        opened = source;
      }}
    />,
  );
  const image = screen.getByRole("img", { name: "工具图片" });
  assert.equal(image.getAttribute("src"), "/api/assets/asset-1");
  fireEvent.click(screen.getByRole("button", { name: "工具图片" }));
  assert.equal(opened, "/api/assets/asset-1");
});

test("composer task and web pickers produce model-visible reference requests", async () => {
  const original = globalThis.fetch;
  let payload: MessagePayload | undefined;
  globalThis.fetch = async (input) => {
    assert.match(String(input), /^\/api\/threads\?q=/);
    return new Response(
      JSON.stringify({
        threads: [{ ...baseThread, id: "source-2", title: "引用来源任务" }],
      }),
      { status: 200 },
    );
  };
  try {
    render(
      <Composer
        thread={baseThread}
        hostOnline
        onSend={async (value) => {
          payload = value;
        }}
        onInterrupt={async () => {}}
        onNavigate={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "引用任务" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /引用来源任务/ }),
    );
    await waitFor(() =>
      assert.equal(screen.queryByRole("dialog") === null, true),
    );
    fireEvent.click(screen.getByRole("button", { name: "添加网页引用" }));
    fireEvent.change(screen.getByLabelText(/网页地址/), {
      target: { value: "https://example.com/source" },
    });
    fireEvent.change(screen.getByLabelText(/标题/), {
      target: { value: "网页来源" },
    });
    fireEvent.change(screen.getByLabelText(/引用摘录/), {
      target: { value: "用户提供的来源摘录" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加引用" }));
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => assert.ok(payload));
    assert.deepEqual(payload!.references, [
      { kind: "task", thread_id: "source-2" },
      {
        kind: "web",
        url: "https://example.com/source",
        title: "网页来源",
        excerpt: "用户提供的来源摘录",
      },
    ]);
  } finally {
    globalThis.fetch = original;
  }
});

const generatedAsset: Asset = {
  id: "asset-generated",
  name: "result.png",
  mime: "image/png",
  size: 10,
  url: "/api/assets/asset-generated",
  source_path: "F:\\work\\result.png",
};
function pictureTimeline(text: string, assets: Asset[], type = "agentMessage") {
  return render(
    <Timeline
      items={[
        {
          id: "hub-item",
          native_id: "native-item",
          thread_id: "t1",
          type,
          data: { text, _assets: assets },
        },
      ]}
      streams={{}}
      busy={false}
      loading={false}
      onImage={() => {}}
      onNavigate={() => {}}
    />,
  );
}

test("agent Markdown images appear once while unreferenced assets keep their gallery", () => {
  const other = {
    ...generatedAsset,
    id: "other",
    name: "other.png",
    source_path: "F:\\work\\other.png",
    url: "/api/assets/other",
  };
  const view = pictureTimeline("![生成图片](F:/work/result.png)", [
    generatedAsset,
    other,
  ]);
  assert.equal(screen.getAllByRole("img").length, 2);
  const galleryImages = view.container.querySelectorAll(".asset-gallery img");
  assert.equal(galleryImages.length, 1);
  assert.equal(galleryImages[0].getAttribute("src"), other.url);
});

test("fenced and inline image examples never hide the gallery asset", () => {
  const view = pictureTimeline(
    "```markdown\n![示例](F:/work/result.png)\n```\n\n`![行内示例](F:/work/result.png)`",
    [generatedAsset],
  );
  assert.equal(screen.getAllByRole("img").length, 1);
  assert.equal(view.container.querySelectorAll(".asset-gallery img").length, 1);
});

test("ambiguous duplicate filenames preserve both gallery assets", () => {
  const other = {
    ...generatedAsset,
    id: "other",
    source_path: "F:\\other\\result.png",
    url: "/api/assets/other",
  };
  const view = pictureTimeline("![无法确定来源](result.png)", [
    generatedAsset,
    other,
  ]);
  assert.equal(screen.getAllByRole("img").length, 2);
  assert.equal(view.container.querySelectorAll(".asset-gallery img").length, 2);
});

test("reference-style Markdown images are deduplicated using their definitions", () => {
  const view = pictureTimeline(
    "![生成图片][output]\n\n[output]: F:/work/result.png",
    [generatedAsset],
  );
  assert.equal(screen.getAllByRole("img").length, 1);
  assert.equal(view.container.querySelectorAll(".asset-gallery img").length, 0);
});

test("tool image galleries are preserved", () => {
  const view = pictureTimeline(
    "![图片](F:/work/result.png)",
    [generatedAsset],
    "imageView",
  );
  assert.equal(screen.getAllByRole("img").length, 1);
  assert.equal(view.container.querySelectorAll(".asset-gallery img").length, 1);
});

test("imported history must finish loading before continuing the native session", async () => {
  const sent: MessagePayload[] = [];
  const props = {
    hostOnline: true,
    onSend: async (payload: MessagePayload) => {
      sent.push(payload);
    },
    onInterrupt: async () => {},
    onNavigate: () => {},
  };
  const view = render(
    <Composer
      thread={{
        ...baseThread,
        imported: true,
        history_loaded: false,
        status: "idle",
      }}
      {...props}
    />,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息给 Codex" }), {
    target: { value: "continue the imported conversation" },
  });
  assert.equal(
    (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
      .disabled,
    true,
  );
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  assert.equal(sent.length, 0);
  view.rerender(
    <Composer
      thread={{ ...baseThread, imported: true, history_loaded: true }}
      {...props}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => assert.equal(sent.length, 1));
});
