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
import Timeline, { type TimelineProps } from "./Timeline";
import { timelineUnits } from "./timeline-work";
import type { Item } from "./types";

afterEach(cleanup);

function item(
  id: string,
  type = "reasoning",
  turn = "turn-1",
  data = {},
): Item {
  return {
    id: `hub-${id}`,
    native_id: id,
    thread_id: "thread-1",
    turn_id: turn,
    type,
    data: { status: "completed", ...data },
  };
}

const base: TimelineProps = {
  items: [],
  streams: {},
  busy: false,
  loading: false,
  onImage: () => {},
  onNavigate: () => {},
};

function groupToggle(container: HTMLElement) {
  return container.querySelector<HTMLElement>(".work-process > summary")!;
}

test("work blocks retain native order and stop at messages, images and turn boundaries", () => {
  const units = timelineUnits(
    [
      item("user", "userMessage", "turn-1", { text: "开始" }),
      item("reason"),
      item("command", "commandExecution"),
      item("comment", "agentMessage", "turn-1", {
        phase: "commentary",
        text: "发现了问题",
      }),
      item("search", "webSearch"),
      item("other-turn", "reasoning", "turn-2"),
      item("image", "imageGeneration", "turn-2"),
      item("plan", "plan", "turn-2"),
      item("compact", "contextCompaction", "turn-2"),
      item("future", "futureNativeItem", "turn-2"),
      item("final", "agentMessage", "turn-2", { phase: "final", text: "完成" }),
    ],
    {},
  );
  assert.deepEqual(
    units.map((unit) =>
      unit.kind === "work"
        ? unit.entries.map((entry) => entry.item.native_id)
        : unit.entry.item.native_id,
    ),
    [
      "user",
      ["reason", "command"],
      "comment",
      ["search"],
      ["other-turn"],
      "image",
      ["plan", "compact", "future"],
      "final",
    ],
  );
});

test("collapsed work summaries expose failures and counts without mounting raw commands", () => {
  const view = render(
    <Timeline
      {...base}
      items={[
        item("reason"),
        item("cmd", "commandExecution", "turn-1", {
          command: "secretly-long-command --details",
          exitCode: 1,
        }),
        item("tool", "mcpToolCall", "turn-1", {
          error: { message: "tool failed" },
        }),
        item("unknown", "futureNativeItem", "turn-1", {
          nativePayload: "preserved",
        }),
      ]}
    />,
  );
  const group =
    view.container.querySelector<HTMLDetailsElement>(".work-process")!;
  assert.equal(group.open, false);
  assert.ok(screen.getByText("1 次思考 · 2 次工具调用 · 1 项记录"));
  assert.ok(screen.getByText("2 项失败"));
  assert.equal(view.container.querySelectorAll(".tool-card").length, 0);
  assert.equal(screen.queryByText("secretly-long-command --details"), null);
  fireEvent.click(groupToggle(view.container));
  assert.equal(group.open, true);
  assert.equal(view.container.querySelectorAll(".tool-card").length, 4);
  assert.ok(screen.getByText(/"nativePayload": "preserved"/));
});

test("pending reasoning and commands join the tail block and deduplicate native and hub IDs", () => {
  const native = item("native-command", "commandExecution");
  const units = timelineUnits([native], {
    "hub-native-command": {
      id: "native-command",
      kind: "command",
      text: "first output",
    },
    "native-command": {
      id: "native-command",
      kind: "command",
      text: "latest output",
    },
    "pending-reason": {
      id: "pending-reason",
      kind: "reasoning",
      text: "分析中",
    },
    "pending-command": {
      id: "pending-command",
      kind: "command",
      text: "更多输出",
    },
    "pending-message": {
      id: "pending-message",
      kind: "message",
      text: "正在回复",
    },
  });
  assert.equal(units.length, 2);
  assert.equal(units[0].kind, "work");
  if (units[0].kind !== "work") throw new Error("expected a work block");
  assert.equal(units[0].entries.length, 3);
  assert.equal(units[0].entries[0].stream?.text, "latest output");
  assert.equal(units[1].kind, "item");
});

test("open state survives stream updates, persistence and prepended older items", () => {
  const view = render(
    <Timeline
      {...base}
      items={[item("reason")]}
      busy
      streams={{ live: { id: "live", kind: "command", text: "line one" } }}
    />,
  );
  fireEvent.click(groupToggle(view.container));
  const group =
    view.container.querySelector<HTMLDetailsElement>(".work-process")!;
  const command =
    view.container.querySelectorAll<HTMLDetailsElement>(".tool-card")[1];
  fireEvent.click(command.querySelector("summary")!);
  assert.equal(command.open, true);
  view.rerender(
    <Timeline
      {...base}
      items={[item("reason")]}
      busy
      streams={{
        live: { id: "live", kind: "command", text: "line one\nline two" },
      }}
    />,
  );
  assert.equal(view.container.querySelector(".work-process"), group);
  assert.equal(group.open, true);
  assert.equal(command.open, true);
  assert.match(command.textContent || "", /line two/);
  view.rerender(
    <Timeline
      {...base}
      items={[
        item("earlier"),
        item("reason"),
        item("live", "commandExecution", "turn-1", {
          command: "echo done",
          aggregatedOutput: "finished",
        }),
      ]}
    />,
  );
  assert.equal(view.container.querySelector(".work-process"), group);
  assert.equal(group.open, true);
  assert.equal(view.container.querySelectorAll(".tool-card")[2], command);
  assert.equal(command.open, true);
  assert.match(groupToggle(view.container).textContent || "", /工作过程/);
  assert.match(
    groupToggle(view.container).textContent || "",
    /2 次思考 · 1 次工具调用/,
  );
  fireEvent.click(groupToggle(view.container));
  assert.equal(group.open, false);
  fireEvent.click(groupToggle(view.container));
  assert.equal(command.open, true);
});

test("message streams remain visible beside folded work and image outputs remain visible", () => {
  const view = render(
    <Timeline
      {...base}
      items={[
        item("reason"),
        item("image-tool", "mcpToolCall", "turn-1", {
          _assets: [
            {
              id: "picture",
              mime: "image/png",
              name: "result.png",
              size: 1,
              url: "/api/assets/picture",
            },
          ],
        }),
      ]}
      streams={{
        message: {
          id: "live-message",
          kind: "message",
          text: "这是正在流式输出的正文。",
        },
      }}
    />,
  );
  assert.equal(view.container.querySelectorAll(".work-process").length, 1);
  assert.ok(screen.getByText("这是正在流式输出的正文。"));
  assert.ok(screen.getByRole("img", { name: "result.png" }));
  assert.ok(screen.getByText("正在生成"));
});

test("a target reference opens its work block and inner detail before scrolling", async () => {
  const native = item("target-command", "commandExecution", "turn-1", {
    command: "echo target",
    aggregatedOutput: "target output",
  });
  const view = render(<Timeline {...base} items={[native]} />);
  assert.equal(view.container.querySelectorAll(".tool-card").length, 0);
  view.rerender(
    <Timeline {...base} items={[native]} targetItem="hub-target-command" />,
  );
  await waitFor(() => {
    assert.equal(
      view.container.querySelector<HTMLDetailsElement>(".work-process")?.open,
      true,
    );
    assert.equal(
      view.container.querySelector<HTMLDetailsElement>(".tool-card")?.open,
      true,
    );
    assert.ok(
      view.container.querySelector("#item-target-command.referenced-item"),
    );
  });
  fireEvent.click(groupToggle(view.container));
  assert.equal(
    view.container.querySelector<HTMLDetailsElement>(".work-process")?.open,
    false,
  );
  view.rerender(<Timeline {...base} items={[native]} />);
  view.rerender(
    <Timeline {...base} items={[native]} targetItem="hub-target-command" />,
  );
  assert.equal(
    view.container.querySelector<HTMLDetailsElement>(".work-process")?.open,
    true,
  );
});

test("pagination can reveal an older referenced item inside the same stable work block", async () => {
  let loads = 0;
  const current = item("current", "commandExecution");
  const props = {
    ...base,
    targetItem: "older",
    hasMore: true,
    onLoadOlder: () => {
      loads += 1;
    },
  };
  const view = render(<Timeline {...props} items={[current]} />);
  const group =
    view.container.querySelector<HTMLDetailsElement>(".work-process")!;
  assert.ok(screen.getByText("引用位于更早的历史中，可继续加载消息。"));
  fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
  assert.equal(loads, 1);
  view.rerender(<Timeline {...props} items={[current]} loadingOlder />);
  assert.ok(screen.getByText("正在定位引用消息…"));
  view.rerender(
    <Timeline {...props} hasMore={false} items={[item("older"), current]} />,
  );
  await waitFor(() => {
    assert.equal(view.container.querySelector(".work-process"), group);
    assert.equal(group.open, true);
    assert.ok(view.container.querySelector("#item-older.referenced-item"));
    assert.equal(
      view.container.querySelector<HTMLDetailsElement>("#item-older .tool-card")
        ?.open,
      true,
    );
  });
  assert.equal(
    screen.queryByText("引用位于更早的历史中，可继续加载消息。"),
    null,
  );
});

test("group expansion preserves explicit full-item loading and cached detail state", async () => {
  const originalFetch = globalThis.fetch;
  let reads = 0;
  globalThis.fetch = async (input) => {
    reads += 1;
    assert.equal(String(input), "/api/threads/thread-1/items/hub-compact");
    return new Response(
      JSON.stringify({
        item: item("compact", "commandExecution", "turn-1", {
          command: "echo complete",
          aggregatedOutput: "complete output",
        }),
      }),
    );
  };
  try {
    const view = render(
      <Timeline
        {...base}
        items={[
          item("compact", "commandExecution", "turn-1", {
            command: "echo preview",
            aggregatedOutput: "preview output",
            _truncated: true,
            _references: [
              {
                kind: "web",
                url: "https://example.com/reference",
                title: "保留的来源",
              },
            ],
          }),
        ]}
      />,
    );
    assert.equal(reads, 0);
    fireEvent.click(groupToggle(view.container));
    assert.equal(reads, 0);
    const inner =
      view.container.querySelector<HTMLDetailsElement>(".tool-card")!;
    assert.equal(inner.open, false);
    fireEvent.click(inner.querySelector("summary")!);
    fireEvent.click(screen.getByRole("button", { name: "查看完整内容" }));
    await screen.findByText(/complete output/);
    assert.equal(reads, 1);
    assert.ok(screen.getByRole("link", { name: /保留的来源/ }));
    fireEvent.click(groupToggle(view.container));
    fireEvent.click(groupToggle(view.container));
    assert.equal(inner.open, true);
    assert.equal(reads, 1);
    fireEvent.click(screen.getByRole("button", { name: "收起完整内容" }));
    assert.ok(screen.getByText(/preview output/));
    fireEvent.click(screen.getByRole("button", { name: "查看完整内容" }));
    await screen.findByText(/complete output/);
    assert.equal(reads, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
