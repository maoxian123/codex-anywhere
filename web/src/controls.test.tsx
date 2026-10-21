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
import { Markdown, References } from "./components";
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

test("idle sends start a turn, active sends steer, and interrupt uses its own action", async () => {
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
  fireEvent.change(screen.getByRole("textbox", { name: "补充任务要求" }), {
    target: { value: "add a test" },
  });
  fireEvent.click(screen.getByRole("button", { name: "补充要求" }));
  await waitFor(() => assert.equal(calls.length, 2));
  assert.equal(calls[1].steer, true);
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
    name: "补充任务要求",
  }) as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: "draft while offline" } });
  assert.equal(input.value, "draft while offline");
  assert.equal(
    (screen.getByRole("button", { name: "补充要求" }) as HTMLButtonElement)
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
